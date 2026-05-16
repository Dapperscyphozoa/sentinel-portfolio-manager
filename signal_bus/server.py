"""signal-bus HTTP server.

Endpoints per SPEC §5.3:
  GET /health
  GET /candles/{coin}/{tf}?n=N
  GET /liq?since=<ms>&coin=<optional>
  GET /funding/{coin}?hours=N
  GET /markprice/{coin}
  GET /hl/account
  GET /hl/fills?since=<ms>
  GET /hl/positions

Uses stdlib http.server (SPEC §2.2: stdlib by legacy convention).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# allow running as `python3 server.py` (rootDir on Render)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common import config  # noqa: E402
from signal_bus import binance_ws  # noqa: E402
from signal_bus.cache import Cache  # noqa: E402


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("signal_bus")


CACHE: Cache | None = None


def _json_resp(handler: BaseHTTPRequestHandler, status: int, body) -> None:
    payload = json.dumps(body, separators=(",", ":")).encode()
    handler.send_response(status)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter logs
        pass

    def do_GET(self):
        try:
            self._route()
        except Exception as e:
            log.exception("handler error")
            _json_resp(self, 500, {"error": str(e)})

    def _route(self) -> None:
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        parts = path.strip("/").split("/")

        assert CACHE is not None

        if path == "/health":
            return _json_resp(self, 200, {
                "ok": True,
                "ts": time.time(),
                **CACHE.stats(),
            })

        if path == "/":
            return _json_resp(self, 200, {"service": "signal-bus", "ok": True})

        if len(parts) == 3 and parts[0] == "candles":
            coin = parts[1].upper()
            tf = parts[2]
            n = int(q.get("n", "200"))
            return _json_resp(self, 200, CACHE.get_klines(coin, tf, n))

        if path == "/liq":
            since = int(q.get("since", "0"))
            coin = q.get("coin")
            if coin:
                coin = coin.upper()
            return _json_resp(self, 200, CACHE.get_liqs(since, coin))

        if len(parts) == 2 and parts[0] == "funding":
            coin = parts[1].upper()
            hours = int(q.get("hours", "12"))
            venue = q.get("venue")
            rows = CACHE.get_funding(coin, hours)
            if venue:
                rows = [r for r in rows if r.get("venue") == venue]
            return _json_resp(self, 200, rows)

        if len(parts) == 2 and parts[0] == "funding_multi":
            # /funding_multi/{coin}?hours=12  → per-venue grouped
            coin = parts[1].upper()
            hours = int(q.get("hours", "12"))
            rows = CACHE.get_funding(coin, hours)
            out: dict[str, list] = {}
            for r in rows:
                out.setdefault(r.get("venue", "unknown"), []).append({"ts": r["ts"], "rate": r["rate"]})
            return _json_resp(self, 200, out)

        if len(parts) == 2 and parts[0] == "markprice":
            coin = parts[1].upper()
            m = CACHE.get_mark(coin)
            return _json_resp(self, 200, m)

        if path == "/hl/account":
            return _json_resp(self, 200, CACHE.hl_account)

        if path == "/hl/positions":
            return _json_resp(self, 200, CACHE.hl_positions)

        if path == "/hl/fills":
            since = int(q.get("since", "0"))
            fills = [f for f in CACHE.hl_fills if f.get("ts", 0) >= since]
            return _json_resp(self, 200, fills)

        if len(parts) == 3 and parts[0] == "hl" and parts[1] == "confluence":
            # /hl/confluence/{coin}: returns recent fill activity + position direction
            # for use by strategies that want to align with HL flow
            coin = parts[2].upper()
            since = int(q.get("since", str(int((time.time() - 600) * 1000))))
            fills = [f for f in CACHE.hl_fills if f.get("coin") == coin and f.get("ts", 0) >= since]
            buy_qty = sum(f["qty"] for f in fills if f["side"] == "B")
            sell_qty = sum(f["qty"] for f in fills if f["side"] == "A")
            net = buy_qty - sell_qty
            pos = next((p for p in CACHE.hl_positions if p["coin"] == coin), None)
            return _json_resp(self, 200, {
                "coin": coin,
                "fills_in_window": len(fills),
                "buy_qty": buy_qty,
                "sell_qty": sell_qty,
                "net_qty": net,
                "since": since,
                "position": pos,
            })

        return _json_resp(self, 404, {"error": "not_found", "path": path})


def _flush_loop(cache: Cache) -> None:
    """Periodic SQLite flush. Klines hourly, liqs every 5min."""
    last_kline = 0.0
    last_liq = 0.0
    while True:
        now = time.time()
        try:
            if now - last_liq > 300:
                n = cache.flush_liqs()
                log.info("flushed %d liq events", n)
                last_liq = now
            if now - last_kline > 3600:
                n = cache.flush_klines()
                log.info("flushed %d kline bars", n)
                last_kline = now
        except Exception:
            log.exception("flush error")
        time.sleep(15)


def main() -> None:
    global CACHE
    state = config.state_dir()
    db_path = os.path.join(state, "signal_bus.db")
    CACHE = Cache(db_path)
    CACHE.cold_load()
    log.info("cache cold-loaded; stats=%s", CACHE.stats())

    syms = (config.get("BINANCE_SYMBOLS", "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,BNBUSDT") or "").strip()
    symbols = [s.strip().upper() for s in syms.split(",") if s.strip()]

    # DATA_VENUE selects the primary kline+liq source. Binance Futures geoblocks
    # US/EU datacenters silently (TCP connects but no events). OKX accepts all
    # geographies and provides equivalent USDT-perp coverage. Default 'okx'.
    data_venue = config.get("DATA_VENUE", "okx").lower()
    if data_venue == "binance":
        log.info("starting binance ws for %d symbols", len(symbols))
        threading.Thread(target=binance_ws.run_in_thread, args=(symbols, CACHE),
                         daemon=True, name="binance_ws").start()
    else:
        # OKX subscribers expect bare coin tickers; strip USDT suffix
        coins = []
        for s in symbols:
            for sfx in ("USDT", "USDC", "BUSD"):
                if s.endswith(sfx):
                    coins.append(s[: -len(sfx)])
                    break
            else:
                coins.append(s)
        log.info("starting OKX data ws for %d coins (venue=%s, geo-portable)", len(coins), data_venue)
        from signal_bus import okx_data_ws  # noqa: E402
        threading.Thread(target=okx_data_ws.run_in_thread, args=(coins, CACHE),
                         daemon=True, name="okx_data_ws").start()

        # Warm-start: REST backfill ~200 bars per (coin, tf) so strategies and
        # PM regime detector have history at first tick instead of waiting days.
        if config.get_bool("BACKFILL_ON_BOOT", default=True):
            from signal_bus import okx_rest_backfill  # noqa: E402

            def _do_backfill():
                try:
                    n = okx_rest_backfill.backfill_all(coins, CACHE)
                    log.info("REST backfill complete: %d bars total", n)
                except Exception:
                    log.exception("REST backfill failed")
            threading.Thread(target=_do_backfill, daemon=True, name="rest_backfill").start()
    threading.Thread(target=_flush_loop, args=(CACHE,), daemon=True, name="flush").start()

    # HL WS thread is wired up in Session 3 via hl_ws.run_in_thread
    try:
        from signal_bus import hl_ws  # noqa: F401
        hl_wallet = config.get("HL_AGENT_WALLET", "")
        if hl_wallet:
            threading.Thread(target=hl_ws.run_in_thread, args=(hl_wallet, CACHE), daemon=True, name="hl_ws").start()
            log.info("hl ws started for wallet %s", hl_wallet)
    except Exception:
        log.warning("hl_ws not started", exc_info=True)

    # cross-venue (OKX/Bybit) funding WS — for cex_dex_arb (Session 10)
    if config.get_bool("CROSS_VENUE_WS_ENABLED", default=False):
        try:
            from signal_bus import cross_venue_ws  # noqa: F401
            cv_coins = (config.get("CROSS_VENUE_COINS",
                "BTC,ETH,SOL,XRP,BNB,DOGE,AVAX,LINK,LTC,NEAR,SUI,APT,ARB,OP,INJ,SEI,TIA,WIF,JUP,DOT") or "").strip()
            coins = [c.strip().upper() for c in cv_coins.split(",") if c.strip()]
            threading.Thread(target=cross_venue_ws.run_in_thread, args=(coins, CACHE),
                             daemon=True, name="cross_venue_ws").start()
            log.info("cross-venue ws started for %d coins", len(coins))
        except Exception:
            log.warning("cross_venue_ws not started", exc_info=True)

    port = config.get_int("HTTP_PORT", 10000)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("signal-bus listening on :%d", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
