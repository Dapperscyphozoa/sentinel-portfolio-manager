"""Sniper service — main HTTP server.

Endpoints:
  GET  /health           — service status
  GET  /listings         — recent listing events
  GET  /trades           — sniper trade history
  GET  /memdump          — tracemalloc top allocations (leak hunting)
  POST /approve          — operator approval for next trade (X-Sniper-Auth)
  POST /kill             — manual kill switch
  POST /reset            — manual kill reset

Background loop:
  - Every SNIPER_POLL_INTERVAL_S (default 10s): poll HL meta for new listings
  - For each new listing: check oracle-lag → fire snipe if conditions met
  - Track open positions, exit at TP/SL/timeout
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import tracemalloc
from http.server import BaseHTTPRequestHandler, HTTPServer

# Ensure the project root is on sys.path so 'sniper.X' imports work when
# Render runs us with rootDir=sniper (cwd would otherwise hide the package)
_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ_ROOT not in sys.path:
    sys.path.insert(0, _PROJ_ROOT)

# Start tracemalloc immediately so it captures all allocations.
# Cheap (~10-20% overhead) and lets us inspect via /memdump later.
tracemalloc.start(20)

from sniper.listing_detector import ListingDetector
from sniper.oracle_lag import evaluate_snipe, fetch_hl_mark
from sniper.executor import SniperExecutor
from sniper.risk import get_risk

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("sniper_server")


# Global state (single-threaded background worker)
_open_positions: dict[str, dict] = {}   # coin -> trade dict
_position_lock = threading.Lock()


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _i(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def get_account_value() -> float:
    """Fetch live HL account value. Falls back to env or default."""
    try:
        import httpx
        wallet = os.environ.get("HL_AGENT_WALLET")
        if not wallet:
            return _f("SNIPER_DEFAULT_ACCOUNT_USD", 491.0)
        with httpx.Client(timeout=5.0) as cli:
            r = cli.post("https://api.hyperliquid.xyz/info",
                         json={"type": "clearinghouseState", "user": wallet})
            if r.status_code == 200:
                data = r.json()
                value = float(data.get("marginSummary", {}).get("accountValue", 0))
                if value > 0:
                    return value
    except Exception as e:
        log.warning("get_account_value failed: %s", e)
    return _f("SNIPER_DEFAULT_ACCOUNT_USD", 491.0)


def handle_listing(event: dict, detector: ListingDetector,
                   executor: SniperExecutor) -> None:
    """Handle one listing event: evaluate snipe + fire if conditions met."""
    coin = event["coin"]
    log.info("Handling listing: %s (ts=%d)", coin, event["ts"])
    div_threshold = _f("SNIPER_DIVERGENCE_THRESHOLD", 0.05)

    # Give HL oracle a moment to settle on new listing
    settle_delay = _f("SNIPER_SETTLE_DELAY_S", 5.0)
    time.sleep(settle_delay)

    decision = evaluate_snipe(coin, divergence_threshold=div_threshold,
                              listing_age_s=settle_delay)
    log.info("Snipe decision for %s: fire=%s div=%+.2%% hl=%.6f cex=%.6f (%s) %s",
             coin, decision.fire, decision.divergence_pct,
             decision.hl_mark, decision.cex_mid, decision.cex_source, decision.reason)
    detector.mark_handled(event["ts"], coin)

    if not decision.fire:
        return

    # Risk gate
    risk = get_risk()
    account_value = get_account_value()
    rr = risk.check(coin, account_value, decision.divergence_pct)
    if not rr.allow:
        log.info("Risk gate blocked %s: %s", coin, rr.reason)
        return

    # Fire
    result = executor.fire(coin, decision.is_long, rr.margin_usd,
                           tp_pct=decision.tp_pct, sl_pct=decision.sl_pct)
    log.info("Execution result: %s", result)
    if result.success:
        risk.record_trade(coin, rr.margin_usd, decision.divergence_pct)
        with _position_lock:
            _open_positions[coin] = {
                "is_long": decision.is_long,
                "entry_px": result.fill_px,
                "size_coin": result.size_coin,
                "margin_usd": rr.margin_usd,
                "entry_ts": int(time.time() * 1000),
                "tp_pct": decision.tp_pct,
                "sl_pct": decision.sl_pct,
                "max_hold_s": decision.max_hold_s,
                "cloid": result.cloid,
                "paper": result.paper,
            }


def check_exits(executor: SniperExecutor) -> None:
    """Check all open positions for TP/SL/timeout exit."""
    risk = get_risk()
    with _position_lock:
        coins = list(_open_positions.keys())
    for coin in coins:
        with _position_lock:
            t = _open_positions.get(coin)
        if t is None:
            continue
        mark = fetch_hl_mark(coin)
        if mark is None:
            continue
        # PnL
        if t["is_long"]:
            ret_pct = (mark - t["entry_px"]) / t["entry_px"]
        else:
            ret_pct = (t["entry_px"] - mark) / t["entry_px"]
        elapsed_s = (time.time() * 1000 - t["entry_ts"]) / 1000
        # Exit conditions
        hit_tp = ret_pct >= t["tp_pct"]
        hit_sl = ret_pct <= -t["sl_pct"]
        hit_timeout = elapsed_s >= t["max_hold_s"]
        if hit_tp or hit_sl or hit_timeout:
            reason = "tp" if hit_tp else ("sl" if hit_sl else "timeout")
            close_result = executor.close(coin, t["is_long"], t["size_coin"])
            log.info("Closing %s: reason=%s ret=%+.2%% close=%s", coin, reason, ret_pct, close_result)
            # Compute pnl
            close_px = close_result.get("fill_px", mark)
            gross = (close_px - t["entry_px"]) * t["size_coin"] * (1 if t["is_long"] else -1)
            # Apply fee estimate (HL: ~0.045% taker on each side)
            fees = 0.00045 * t["size_coin"] * (t["entry_px"] + close_px)
            pnl = gross - fees
            account_value = get_account_value()
            risk.record_close(coin, pnl, account_value)
            with _position_lock:
                _open_positions.pop(coin, None)


def main_loop():
    """Background worker: poll listings + check exits."""
    detector = ListingDetector(
        poll_interval_s=_f("SNIPER_POLL_INTERVAL_S", 10.0)
    )
    executor = SniperExecutor(leverage=_f("SNIPER_LEVERAGE", 5.0))

    # Bootstrap on first run
    detector.bootstrap_known_universe()
    log.info("Sniper background loop started (poll=%.1fs)", detector.poll_interval_s)

    while True:
        try:
            events = detector.check_for_new()
            for ev in events:
                handle_listing(
                    {"coin": ev.coin, "ts": ev.detected_ts, "universe_index": ev.hl_universe_index},
                    detector, executor,
                )
            check_exits(executor)
        except Exception as e:
            log.exception("loop iteration failed: %s", e)
        time.sleep(detector.poll_interval_s)


# ─────────────────────────── HTTP Server ───────────────────────────
class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, body: dict) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body, default=str).encode())

    def _auth_ok(self) -> bool:
        token = os.environ.get("SNIPER_AUTH_TOKEN", "")
        if not token:
            return True
        return self.headers.get("X-Sniper-Auth") == token

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            risk = get_risk()
            killed, kreason = risk.is_killed()
            with _position_lock:
                n_open = len(_open_positions)
            self._json(200, {
                "status": "ok" if not killed else "killed",
                "killed": killed,
                "kill_reason": kreason,
                "open_positions": n_open,
                "trades_today": risk.trades_today(),
                "total_live_trades": risk.total_live_trades(),
                "requires_approval": risk.requires_approval(),
                "ts": int(time.time() * 1000),
            })
        elif path == "/listings":
            detector = ListingDetector()
            since = int(time.time() * 1000) - 7 * 86400_000
            events = detector.recent_listings(since)
            self._json(200, {"events": events, "since": since})
        elif path == "/trades":
            risk = get_risk()
            c = risk._conn()
            rows = c.execute(
                "SELECT * FROM sniper_trades ORDER BY ts DESC LIMIT 100"
            ).fetchall()
            c.close()
            self._json(200, {"trades": [dict(r) for r in rows]})
        elif path == "/positions":
            with _position_lock:
                self._json(200, {"open": list(_open_positions.values())})
        elif path == "/memdump":
            # tracemalloc snapshot for leak diagnosis
            import resource
            rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            snapshot = tracemalloc.take_snapshot()
            top = snapshot.statistics("lineno")[:25]
            out = {
                "rss_mb": round(rss_kb / 1024, 1),
                "tracemalloc_total_mb": round(sum(s.size for s in snapshot.statistics("lineno")) / 1024 / 1024, 2),
                "top_25_by_size": [
                    {
                        "file": s.traceback[0].filename.replace(_PROJ_ROOT + "/", ""),
                        "line": s.traceback[0].lineno,
                        "size_kb": round(s.size / 1024, 1),
                        "count": s.count,
                    }
                    for s in top
                ],
            }
            self._json(200, out)
        elif path == "/state":
            risk = get_risk()
            killed, kreason = risk.is_killed()
            with _position_lock:
                positions = list(_open_positions.values())
            self._json(200, {
                "service": "sniper",
                "killed": killed,
                "kill_reason": kreason,
                "open_positions": positions,
                "trades_today": risk.trades_today(),
                "total_live_trades": risk.total_live_trades(),
                "requires_approval": risk.requires_approval(),
                "config": {
                    "live_trading": os.environ.get("SNIPER_LIVE_TRADING", "0"),
                    "divergence_threshold": os.environ.get("SNIPER_DIVERGENCE_THRESHOLD", "0.05"),
                    "max_per_day": os.environ.get("SNIPER_MAX_PER_DAY", "1"),
                    "size_pct": os.environ.get("SNIPER_SIZE_PCT", "0.50"),
                    "leverage": os.environ.get("SNIPER_LEVERAGE", "5.0"),
                },
            })
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth_ok():
            self._json(401, {"error": "unauthorized"})
            return
        path = self.path.split("?")[0]
        body_len = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(body_len) if body_len else b""
        try:
            payload = json.loads(body) if body else {}
        except Exception:
            payload = {}
        if path == "/approve":
            coin = payload.get("coin", "").upper()
            if not coin:
                self._json(400, {"error": "coin required"})
                return
            risk = get_risk()
            risk.grant_approval(coin, by=payload.get("by", "operator"))
            self._json(200, {"approved": coin, "valid_for_s": 600})
        elif path == "/kill":
            reason = payload.get("reason", "manual_kill")
            get_risk().set_killed(reason)
            self._json(200, {"killed": True, "reason": reason})
        elif path == "/reset":
            get_risk().reset_kill()
            self._json(200, {"reset": True})
        else:
            self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)


def main():
    port = _i("HTTP_PORT", 10000)
    worker = threading.Thread(target=main_loop, daemon=True)
    worker.start()
    log.info("Sniper service starting on port %d", port)
    srv = HTTPServer(("0.0.0.0", port), Handler)
    srv.serve_forever()


if __name__ == "__main__":
    main()
