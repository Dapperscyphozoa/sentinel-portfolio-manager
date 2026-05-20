"""poly-signal-bus HTTP server.

Runs the CEX WS subscribers, Chainlink Data Stream subscriber, and
Polymarket CLOB WS subscriber as background tasks. Exposes HTTP endpoints
for the rest of the poly stack.

Endpoints:
  GET /health                              status of all subs
  GET /cex_consensus/{asset}               {venue: mid, ...}
  GET /cl_actual/{asset}                   {ts, price}
  GET /cl_predicted/{asset}                {ts, price, diag}
  GET /cl_divergence/{asset}               {predicted, actual, diff_bps}
  GET /market_list                         [{market_id, asset, ...}]
  GET /pm_book/{market_id}                 full book snapshot
  GET /implied_prob/{market_id}            {yes_mid, no_mid, yes_implied, no_implied}
  GET /reflex_signal/{asset}               {state, since_ts, pm_prob, time_remaining}
  GET /realized_vol/{asset}?lookback_s=60  {vol}
  GET /candles/{venue}/{asset}/1s?n=N      historical replay (from cache + DB)
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from poly_signal_bus import cex_ws, chainlink_stream, polymarket_clob
from poly_signal_bus.cache import get_cache
from poly_signal_bus.cl_aggregator import (
    DEFAULT_VENUES,
    aggregate_with_diagnostics,
    diff_bps,
)


logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("poly_signal_bus")

ASSETS = [a.strip() for a in os.environ.get("POLY_ASSETS", "BTC,ETH").split(",") if a.strip()]
HTTP_PORT = int(os.environ.get("HTTP_PORT", "10100"))
CL_PREDICT_INTERVAL_MS = int(os.environ.get("CL_PREDICT_INTERVAL_MS", "500"))


# ────────────────────────── Tick handlers ──────────────────────────
async def on_cex_tick(venue: str, asset: str, ts_ms: int,
                      mid: float, bid: float, ask: float) -> None:
    cache = get_cache()
    cache.add_cex(venue, asset, ts_ms, mid, bid, ask)


async def on_cl_report(asset: str, ts_ms: int, price: float) -> None:
    cache = get_cache()
    cache.add_cl_actual(asset, ts_ms, price)


async def on_pm_book(market_id: str, book: dict) -> None:
    cache = get_cache()
    token_id = book.get("token_id")
    if token_id is None:
        return
    cache.update_book(market_id, str(token_id), book)


# ────────────────────────── CL prediction loop ──────────────────────────
async def cl_prediction_loop() -> None:
    """Every CL_PREDICT_INTERVAL_MS, compute our local CL prediction from
    the latest CEX consensus and stash it in the cache."""
    cache = get_cache()
    while True:
        try:
            await asyncio.sleep(CL_PREDICT_INTERVAL_MS / 1000.0)
            for asset in ASSETS:
                venues = cache.latest_cex_mids(asset)
                diag = aggregate_with_diagnostics(venues)
                if diag["predicted"] is not None:
                    cache.set_cl_predicted(asset, int(time.time() * 1000),
                                           diag["predicted"], diag)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"cl_prediction_loop: {e}")


# ────────────────────────── Market discovery loop ──────────────────────────
async def market_discovery_loop() -> None:
    cache = get_cache()
    interval = int(os.environ.get("PM_DISCOVERY_INTERVAL_S", "30"))
    while True:
        try:
            markets = await polymarket_clob.discover_active_markets(ASSETS)
            for m in markets:
                cache.upsert_market(m)
            cache.expire_stale_markets()
            log.debug(f"market discovery: {len(markets)} active")
        except Exception as e:
            log.warning(f"market_discovery: {e}")
        await asyncio.sleep(interval)


# ────────────────────────── PM CLOB subscription manager ──────────────────────────
async def clob_subscription_manager() -> None:
    """Rotates the CLOB WS subscription as markets come and go.

    Simple v1: every 60s, restart the subscriber with the current set of
    token IDs across all active markets. Production-grade would use the
    CLOB WS's incremental sub/unsub events.
    """
    cache = get_cache()
    current_task: asyncio.Task | None = None
    current_tokens: set[str] = set()
    while True:
        try:
            await asyncio.sleep(60)
            tokens: set[str] = set()
            for m in cache.active_markets():
                if m.get("token_id_yes"):
                    tokens.add(str(m["token_id_yes"]))
                if m.get("token_id_no"):
                    tokens.add(str(m["token_id_no"]))
            if tokens != current_tokens and tokens:
                log.info(f"reconnecting CLOB WS for {len(tokens)} tokens")
                if current_task and not current_task.done():
                    current_task.cancel()
                current_task = asyncio.create_task(
                    polymarket_clob.run_clob_ws(list(tokens), on_pm_book),
                    name="pm-clob-ws")
                current_tokens = tokens
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning(f"clob_subscription_manager: {e}")


# ────────────────────────── Realized vol helper ──────────────────────────
def realized_vol(asset: str, lookback_s: int = 60) -> float:
    """Per-second log returns from Binance ticks (or any single venue with
    high tick density), stdev * sqrt(60) for 1-minute vol."""
    cache = get_cache()
    hist = cache.cex_history("binance", asset, n=lookback_s + 1)
    if len(hist) < 5:
        return 0.0
    prices = [h[1] for h in hist]
    rets = []
    for i in range(1, len(prices)):
        if prices[i-1] > 0:
            rets.append(math.log(prices[i] / prices[i-1]))
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)


def implied_prob(market: dict) -> dict:
    yb = market.get("yes_bid")
    ya = market.get("yes_ask")
    nb = market.get("no_bid")
    na = market.get("no_ask")
    yes_mid = ((yb + ya) / 2) if (yb and ya) else None
    no_mid = ((nb + na) / 2) if (nb and na) else None
    return {
        "yes_bid": yb, "yes_ask": ya, "yes_mid": yes_mid,
        "no_bid": nb,  "no_ask": na,  "no_mid": no_mid,
        "yes_implied": yes_mid,
        "no_implied": no_mid,
        "sum_implied": (yes_mid + no_mid) if (yes_mid and no_mid) else None,
    }


def reflex_signal(asset: str, threshold_hi: float = 0.85,
                   threshold_lo: float = 0.15,
                   min_time_remaining: int = 90,
                   sustained_s: int = 5) -> dict:
    """Find PM extremes for the asset for reflexivity emitter."""
    cache = get_cache()
    state = "neutral"; since_ts = None; pm_prob = None; time_rem = None
    now = time.time()
    for m in cache.active_markets():
        if m.get("asset") != asset:
            continue
        if not m.get("end_ts"):
            continue
        tr = m["end_ts"] - now
        if tr < min_time_remaining:
            continue
        ip = implied_prob(m)
        ym = ip.get("yes_mid")
        if ym is None:
            continue
        if ym >= threshold_hi:
            state = "extreme_up"; pm_prob = ym; time_rem = tr
            since_ts = m.get("last_update_ts"); break
        elif ym <= threshold_lo:
            state = "extreme_down"; pm_prob = ym; time_rem = tr
            since_ts = m.get("last_update_ts"); break
    return {
        "state": state, "since_ts": since_ts,
        "pm_prob": pm_prob, "time_remaining": time_rem,
    }


# ────────────────────────── HTTP handler ──────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:  # noqa
        if log.isEnabledFor(logging.DEBUG):
            log.debug(format % args)

    def _send_json(self, code: int, payload) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa
        try:
            u = urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]
            q = parse_qs(u.query)
            cache = get_cache()

            if not parts:
                return self._send_json(200, {"service": "poly-signal-bus"})

            if parts[0] == "health":
                now = int(time.time() * 1000)
                return self._send_json(200, {
                    "service": "poly-signal-bus",
                    "ws_alive": {v: (now - cache.venue_last_seen.get(v, 0)) < 30000
                                 for v in DEFAULT_VENUES},
                    "venue_last_seen_ms": cache.venue_last_seen,
                    "cl_actual_count": {a: len(d) for a, d in cache.cl_actual.items()},
                    "markets_active": len(cache.active_markets()),
                    "now_ms": now,
                })

            if parts[0] == "cex_consensus" and len(parts) >= 2:
                asset = parts[1].upper()
                return self._send_json(200, cache.latest_cex_mids(asset))

            if parts[0] == "cl_actual" and len(parts) >= 2:
                asset = parts[1].upper()
                v = cache.latest_cl_actual(asset)
                if v is None:
                    return self._send_json(404, {"error": "no cl tick yet"})
                ts, price = v
                return self._send_json(200, {"ts_ms": ts, "price": price})

            if parts[0] == "cl_predicted" and len(parts) >= 2:
                asset = parts[1].upper()
                v = cache.latest_cl_predicted(asset)
                if v is None:
                    return self._send_json(404, {"error": "no prediction yet"})
                ts, predicted, diag = v
                return self._send_json(200, {"ts_ms": ts, "predicted": predicted, "diag": diag})

            if parts[0] == "cl_divergence" and len(parts) >= 2:
                asset = parts[1].upper()
                p = cache.latest_cl_predicted(asset)
                a = cache.latest_cl_actual(asset)
                if p is None or a is None:
                    return self._send_json(404, {"error": "missing data"})
                _, pred, _ = p
                ts_a, actual = a
                return self._send_json(200, {
                    "predicted": pred, "actual": actual,
                    "diff_bps": diff_bps(pred, actual),
                    "actual_ts_ms": ts_a,
                })

            if parts[0] == "market_list":
                return self._send_json(200, cache.active_markets())

            if parts[0] == "pm_book" and len(parts) >= 2:
                m = cache.markets.get(parts[1])
                if not m:
                    return self._send_json(404, {"error": "unknown market"})
                return self._send_json(200, m)

            if parts[0] == "implied_prob" and len(parts) >= 2:
                m = cache.markets.get(parts[1])
                if not m:
                    return self._send_json(404, {"error": "unknown market"})
                return self._send_json(200, implied_prob(m))

            if parts[0] == "reflex_signal" and len(parts) >= 2:
                asset = parts[1].upper()
                return self._send_json(200, reflex_signal(asset))

            if parts[0] == "realized_vol" and len(parts) >= 2:
                asset = parts[1].upper()
                lb = int(q.get("lookback_s", ["60"])[0])
                return self._send_json(200, {"vol": realized_vol(asset, lb)})

            if parts[0] == "candles" and len(parts) >= 4:
                venue, asset, tf = parts[1], parts[2].upper(), parts[3]
                n = int(q.get("n", ["60"])[0])
                hist = cache.cex_history(venue, asset, n=n)
                return self._send_json(200, [
                    {"ts": ts, "mid": mid, "bid": bid, "ask": ask}
                    for (ts, mid, bid, ask) in hist
                ])

            self._send_json(404, {"error": "unknown path"})
        except Exception as e:
            log.exception("handler error")
            self._send_json(500, {"error": str(e)})


def _serve_http() -> None:
    addr = ("0.0.0.0", HTTP_PORT)
    httpd = ThreadingHTTPServer(addr, Handler)
    log.info(f"http listening on {addr}")
    httpd.serve_forever()


# ────────────────────────── main ──────────────────────────
async def main() -> None:
    cache = get_cache()

    # HTTP server in a thread
    threading.Thread(target=_serve_http, daemon=True).start()

    # All WS subscribers + helpers
    tasks = [
        asyncio.create_task(cex_ws.run_all(ASSETS, on_cex_tick), name="cex"),
        asyncio.create_task(chainlink_stream.run(ASSETS, on_cl_report), name="cl"),
        asyncio.create_task(cl_prediction_loop(), name="cl-predict"),
        asyncio.create_task(market_discovery_loop(), name="pm-discover"),
        asyncio.create_task(clob_subscription_manager(), name="pm-clob-mgr"),
        asyncio.create_task(cache.persist_loop(), name="persist"),
    ]
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
