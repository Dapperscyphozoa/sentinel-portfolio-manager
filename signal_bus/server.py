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
  GET /oi/{coin}?n=N
  GET /cvd/{coin}?window_ms=30000  (HL CVD aggregator)
  GET /whale_events?since=<ms>&coin=<optional>  (whale opens)
  GET /whale_stats
  GET /l2book/{coin}
  GET /depth_shock/{coin}?window_s=5
  GET /hl/trades/{coin}?n=50

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
from signal_bus import oi_poller  # noqa: E402
from signal_bus import whale_poller  # noqa: E402
from signal_bus.cache import Cache  # noqa: E402
from signal_bus.ohlcv_store import OhlcvStore, fetch_bars  # noqa: E402
from signal_bus import bench_config  # noqa: E402


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("signal_bus")


CACHE: Cache | None = None
OHLCV_STORE: OhlcvStore | None = None


def _json_resp(handler: BaseHTTPRequestHandler, status: int, body) -> None:
    payload = json.dumps(body, separators=(",", ":")).encode()
    try:
        handler.send_response(status)
        handler.send_header("content-type", "application/json")
        handler.send_header("content-length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
    except (BrokenPipeError, ConnectionResetError):
        pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter logs
        pass

    def do_GET(self):
        try:
            self._route()
        except (BrokenPipeError, ConnectionResetError):
            # Client closed before we finished. Do NOT try to write a 500 —
            # that'll just raise BrokenPipe again and kill the handler thread,
            # which Render misreads as upstream failure and serves a 502.
            pass
        except Exception as e:
            log.exception("handler error")
            try:
                _json_resp(self, 500, {"error": str(e)})
            except (BrokenPipeError, ConnectionResetError):
                pass

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

        # HLP (Hyperliquidity Provider vault) positioning — for hlp_fade engine
        if path == "/hlp_positions":
            return _json_resp(self, 200, getattr(CACHE, "hlp_positions", {}))

        if len(parts) == 2 and parts[0] == "hlp_position":
            # Coin names may be mixed-case in HLP (e.g. kLUNC, kPEPE, kSHIB).
            # Look up case-insensitively to avoid 404s on those tokens.
            req = parts[1]
            positions = getattr(CACHE, "hlp_positions", {})
            pos = positions.get(req)
            if pos is None:
                # Case-insensitive fallback
                lower = req.lower()
                for k, v in positions.items():
                    if k.lower() == lower:
                        pos = v
                        req = k  # use canonical form for history lookup
                        break
            if pos is None:
                return _json_resp(self, 404, {"error": "no_hlp_position", "coin": req})
            # Include rolling z-score if history is available
            from signal_bus.hlp_poller import compute_zscore
            history = getattr(CACHE, "hlp_history", {}).get(req)
            z = None
            if history is not None:
                z = compute_zscore(history, pos["net_usd"])
            return _json_resp(self, 200, {**pos, "zscore_7d": z, "history_n": len(history) if history else 0})

        if len(parts) == 2 and parts[0] == "oi":
            coin = parts[1].upper()
            n = int(q.get("n", "60"))
            return _json_resp(self, 200, CACHE.get_oi(coin, n))

        # CVD aggregator (council priority — world-first HL CVD edge).
        # /cvd/{coin}?window_ms=30000  default 30s
        if len(parts) == 2 and parts[0] == "cvd":
            coin = parts[1].upper()
            window_ms = int(q.get("window_ms", "30000"))
            return _json_resp(self, 200, CACHE.get_cvd(coin, window_ms))

        # Whale events (Stage 1 #5 — world-first edge)
        # GET /whale_events?since=<ms>&coin=<optional>
        if path == "/whale_events":
            since = int(q.get("since", "0"))
            coin = q.get("coin")
            if coin: coin = coin.upper()
            return _json_resp(self, 200, CACHE.get_whale_events(since, coin))

        # Whale poller stats / health
        if path == "/whale_stats":
            return _json_resp(self, 200, CACHE.whale_stats)

        # L2 book snapshot (Stage 1 #6)
        if len(parts) == 2 and parts[0] == "l2book":
            coin = parts[1].upper()
            return _json_resp(self, 200, CACHE.get_l2book(coin))

        # Depth shock detector (Stage 1 #6)
        # /depth_shock/{coin}?window_s=5
        if len(parts) == 2 and parts[0] == "depth_shock":
            coin = parts[1].upper()
            window_s = int(q.get("window_s", "5"))
            return _json_resp(self, 200, CACHE.get_depth_shock(coin, window_s))

        # Raw HL trades inspect — diagnostics. /hl/trades/{coin}?n=N
        if len(parts) == 3 and parts[0] == "hl" and parts[1] == "trades":
            coin = parts[2].upper()
            n = int(q.get("n", "50"))
            with CACHE._lock:
                dq = CACHE.hl_trades.get(coin)
                rows = list(dq)[-n:] if dq else []
            return _json_resp(self, 200, rows)

        # ─── sentinel-pm bench endpoints (bearer-auth gated) ────────────
        if path in ("/ohlcv", "/universe", "/costs"):
            expected = os.environ.get("SENTINEL_PM_TOKEN", "").strip()
            if not expected:
                return _json_resp(self, 503, {"error": "auth_not_configured"})
            auth = self.headers.get("Authorization", "")
            presented = auth[7:].strip() if auth.startswith("Bearer ") else ""
            if presented != expected:
                return _json_resp(self, 401, {"error": "unauthorized"})

            if path == "/universe":
                as_of = q.get("as_of")
                syms = bench_config.universe_as_of(as_of)
                return _json_resp(self, 200, {
                    "as_of": as_of or time.strftime("%Y-%m-%d", time.gmtime()),
                    "symbols": syms,
                })

            if path == "/costs":
                return _json_resp(self, 200, bench_config.costs_table())

            # /ohlcv
            assert OHLCV_STORE is not None
            sym = (q.get("symbol") or "").upper().strip()
            interval = (q.get("interval") or "4h").strip()
            start = q.get("start") or ""
            end = q.get("end") or ""
            cursor = q.get("cursor")
            if not sym or not start or not end:
                return _json_resp(self, 400, {
                    "error": "missing_params",
                    "required": ["symbol", "interval", "start", "end"],
                })
            try:
                # ISO 8601 → ms
                from datetime import datetime, timezone
                def _iso_ms(s: str) -> int:
                    s = s.replace("Z", "+00:00")
                    return int(datetime.fromisoformat(s).astimezone(timezone.utc).timestamp() * 1000)
                start_ms = _iso_ms(start)
                end_ms = _iso_ms(end)
            except Exception as e:
                return _json_resp(self, 400, {"error": "bad_iso_timestamp", "detail": str(e)})

            if cursor:
                try:
                    start_ms = max(start_ms, int(cursor))
                except ValueError:
                    return _json_resp(self, 400, {"error": "bad_cursor"})

            bars, next_cursor = fetch_bars(OHLCV_STORE, sym, interval, start_ms, end_ms,
                                            page_limit=10_000)
            # Reshape bars: brief expects ISO timestamps
            from datetime import datetime, timezone
            out_bars = [{
                "timestamp": datetime.fromtimestamp(b["open_ts"] / 1000, tz=timezone.utc)
                              .strftime("%Y-%m-%dT%H:%M:%SZ"),
                "open": b["open"], "high": b["high"], "low": b["low"],
                "close": b["close"], "volume": b["volume"],
            } for b in bars]
            return _json_resp(self, 200, {
                "symbol": sym,
                "interval": interval,
                "bars": out_bars,
                "next_cursor": next_cursor,
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
    global CACHE, OHLCV_STORE
    state = config.state_dir()
    db_path = os.path.join(state, "signal_bus.db")
    CACHE = Cache(db_path)
    CACHE.cold_load()
    log.info("cache cold-loaded; stats=%s", CACHE.stats())

    # Historical OHLCV store (sentinel-pm bench validation). Separate SQLite
    # file to keep the realtime cache lean.
    OHLCV_STORE = OhlcvStore(os.path.join(state, "ohlcv.db"))
    log.info("ohlcv_store ready at %s", OHLCV_STORE.db_path)

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

        # Warm-start: REST backfill N bars per (coin, tf) so strategies and PM
        # regime detector have history at first tick instead of waiting days.
        # Default 1000 = the KLINE_CAP in cache.py, which saturates the ring
        # buffer and unblocks uzt_rev's 500-bar gate on 15m immediately.
        # Tunable via OKX_REST_BACKFILL_BARS.
        if config.get_bool("BACKFILL_ON_BOOT", default=True):
            from signal_bus import okx_rest_backfill  # noqa: E402
            backfill_bars = int(config.get("OKX_REST_BACKFILL_BARS", "1000") or "1000")

            def _do_backfill():
                try:
                    n = okx_rest_backfill.backfill_all(coins, CACHE, bars=backfill_bars)
                    log.info("REST backfill complete: %d bars total (target=%d/coin/tf)",
                             n, backfill_bars)
                except Exception:
                    log.exception("REST backfill failed")
            threading.Thread(target=_do_backfill, daemon=True, name="rest_backfill").start()
    threading.Thread(target=_flush_loop, args=(CACHE,), daemon=True, name="flush").start()

    # HL WS thread is wired up via hl_ws.run_in_thread
    try:
        from signal_bus import hl_ws  # noqa: F401
        # HL agent wallets sign-only; reads must target the MAIN wallet.
        hl_wallet = config.get("HL_USER_WALLET", "") or config.get("HL_AGENT_WALLET", "")
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

    # HLP (Hyperliquidity Provider vault) poller — for hlp_fade engine
    if config.get_bool("HLP_POLLER_ENABLED", default=True):
        try:
            from signal_bus import hlp_poller
            hlp_poller.run_in_thread(CACHE)
            log.info("hlp poller started")
        except Exception:
            log.warning("hlp_poller not started", exc_info=True)

    # HL Open Interest poller — for oi_concentration engine (council Q5 fix)
    if config.get_bool("OI_POLLER_ENABLED", default=True):
        try:
            oi_poller.run_in_thread(CACHE)
            log.info("oi poller started")
        except Exception:
            log.warning("oi_poller not started", exc_info=True)

    # Whale wallet tracker (Stage 1 #5 — world-first edge)
    if config.get_bool("WHALE_POLLER_ENABLED", default=True):
        try:
            whale_poller.run_in_thread(CACHE)
            log.info("whale poller started")
        except Exception:
            log.warning("whale_poller not started", exc_info=True)

    # OKX liquidations REST poller — WS channel rejected by OKX (error 60018);
    # REST is the only reliable path (2026-05-19 decision). Pushes into the
    # same cache.push_liq() sink as the (dead) WS path so /liq is unaffected.
    if config.get_bool("OKX_LIQ_POLLER_ENABLED", default=True):
        try:
            from signal_bus import okx_liq_poller  # noqa: E402
            okx_liq_poller.run_in_thread(CACHE)
            log.info("okx_liq poller started")
        except Exception:
            log.warning("okx_liq_poller not started", exc_info=True)

    port = config.get_int("HTTP_PORT", 10000)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("signal-bus listening on :%d", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
