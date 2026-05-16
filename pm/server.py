"""pm — pre-trade gate, capital fractions, attribution.

Endpoints (X-PM-Auth header for mutating endpoints):
  GET  /health
  GET  /regime
  POST /check                 — body: {strategy, signal}
  POST /register_cloid        — body: {strategy, cloid, coin, side}
  GET  /attribution?since=ms  — P&L by strategy
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common import config, persistence  # noqa: E402
from common.bus_client import BusClient  # noqa: E402

from pm import attribution, pretrade, regime as regime_mod  # noqa: E402


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("pm")


CONN = None
BUS = None
REGIME_CACHE: dict = {"regime": "unknown", "confidence": 0.0, "ts": 0}
ACCOUNT_CACHE: dict = {"value": 0.0, "ts": 0, "positions": []}


def _json(handler: BaseHTTPRequestHandler, status: int, body) -> None:
    payload = json.dumps(body, separators=(",", ":"), default=str).encode()
    handler.send_response(status)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


def _auth_ok(headers) -> bool:
    expected = os.environ.get("PM_AUTH_TOKEN", "")
    if not expected:
        return False
    presented = headers.get("X-PM-Auth", "") or headers.get("x-pm-auth", "")
    return bool(presented) and hmac.compare_digest(expected, presented)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if path == "/health":
            return _json(self, 200, {"ok": True, "ts": time.time(),
                                     "regime": REGIME_CACHE, "account": ACCOUNT_CACHE})
        if path == "/regime":
            return _json(self, 200, REGIME_CACHE)
        if path == "/engines":
            # Dashboard-compatible engine list, derived from pretrade.ENGINE_REGISTRY
            from pm import pretrade as _pt
            base_url = os.environ.get("CORE_PUBLIC_URL", "https://core-o21t.onrender.com")
            out = []
            cut = _pt.CUT_ENGINES
            for name, cfg in _pt.ENGINE_REGISTRY.items():
                live = os.environ.get(f"STRATEGY_{name.upper()}_ENABLED", "1") == "1"
                stage = "cut" if name in cut else ("paper" if os.environ.get("LIVE_TRADING","0") != "1" else "full")
                out.append({
                    "name": name,
                    "url": f"{base_url}/strategy",
                    "halt_url": f"{base_url}/strategy/halt/{name}",
                    "resume_url": f"{base_url}/strategy/resume/{name}",
                    "cloid_prefix": cfg.get("cloid_prefix", ""),
                    "class": ",".join(cfg.get("affinity", [])),
                    "capital_fraction": cfg.get("cap_frac", 0.0),
                    "bt_pf": cfg.get("bt_pf", 0.0),
                    "stage": stage,
                    "live": live,
                    "has_full_api": True,
                    "warning": None if cfg.get("bt_pf", 0) >= 1.4 else "untested" if cfg.get("bt_pf", 0) == 0 else "weak_bt",
                })
            # Add the sniper (separate service, paper mode)
            out.append({
                "name": "sniper",
                "url": "https://sniper-6w9l.onrender.com",
                "halt_url": "https://sniper-6w9l.onrender.com/kill",
                "resume_url": "https://sniper-6w9l.onrender.com/reset",
                "cloid_prefix": "snipe_",
                "class": "pre_listing_arb",
                "capital_fraction": 0.50,
                "bt_pf": 0.0,
                "stage": "paper",
                "live": os.environ.get("SNIPER_LIVE_TRADING", "0") == "1",
                "has_full_api": True,
                "warning": "council_validated_path_1.5_to_2.1yr",
            })
            return _json(self, 200, {"engines": out, "ts": int(time.time() * 1000)})
        if path == "/attribution":
            if not _auth_ok(self.headers):
                return _json(self, 401, {"error": "bad_token"})
            since = int(q.get("since", "0"))
            return _json(self, 200, attribution.by_strategy(CONN, since))
        return _json(self, 404, {"error": "not_found"})

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        if not _auth_ok(self.headers):
            return _json(self, 401, {"error": "bad_token"})
        body_len = int(self.headers.get("content-length", "0") or "0")
        raw = self.rfile.read(body_len) if body_len else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            return _json(self, 400, {"error": "bad_json"})

        if path == "/check":
            strategy = body.get("strategy", "")
            signal = body.get("signal", {})
            try:
                r = pretrade.check(
                    CONN, strategy, signal, REGIME_CACHE,
                    ACCOUNT_CACHE.get("value", 0.0),
                    ACCOUNT_CACHE.get("positions", []),
                )
            except Exception as e:
                log.exception("pretrade")
                return _json(self, 500, {"allow": False, "size_usd": 0, "reason": f"err:{e}"})
            return _json(self, 200, {"allow": r.allow, "size_usd": r.size_usd, "reason": r.reason})

        if path == "/register_cloid":
            attribution.register(CONN,
                                 body.get("cloid", ""), body.get("strategy", ""),
                                 body.get("coin", ""), body.get("side", ""))
            return _json(self, 200, {"ok": True})

        return _json(self, 404, {"error": "not_found"})


def _refresh_loop() -> None:
    """Pull BTC 1h candles and HL account from signal-bus periodically."""
    interval = config.get_int("PM_REFRESH_SEC", 30)
    while True:
        try:
            bars = BUS.candles("BTC", "1h", 80)
            closes = [float(b["close"]) for b in bars]
            highs = [float(b["high"]) for b in bars]
            lows = [float(b["low"]) for b in bars]
            REGIME_CACHE.update(regime_mod.classify(closes, highs, lows))
        except Exception:
            log.exception("regime refresh")
        try:
            acct = BUS.hl_account()
            ACCOUNT_CACHE.update({
                "value": float(acct.get("value", 0)),
                "ts": time.time(),
                "positions": [
                    {"coin": p.get("coin", ""), "is_long": bool(p.get("is_long")),
                     "notional": abs(float(p.get("szi", 0))) * float(p.get("entry_px", 0) or 0),
                     "strategy": ""}  # strategy attribution requires fill→cloid join (future)
                    for p in (acct.get("positions") or [])
                ],
            })
        except Exception:
            log.exception("account refresh")
        time.sleep(interval)


def main() -> None:
    global CONN, BUS
    state = config.state_dir()
    CONN = persistence.init_db(os.path.join(state, "pm.db"))
    attribution.init(CONN)
    BUS = BusClient()
    threading.Thread(target=_refresh_loop, daemon=True, name="pm_refresh").start()
    port = config.get_int("HTTP_PORT", 10000)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("pm listening on :%d", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
