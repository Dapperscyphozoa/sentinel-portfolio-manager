"""strategy-runner HTTP server + scan/position loops."""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common import config, halt, persistence  # noqa: E402
from common.bus_client import BusClient  # noqa: E402
from common.hl_exchange import HLExchange  # noqa: E402
from common.pm_client import PMClient  # noqa: E402

from strategy_runner import runner  # noqa: E402
from strategy_runner.trader import Trader  # noqa: E402


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("strategy_runner")


CONN = None
BUS = None
PM = None
TRADER = None


def _json(handler: BaseHTTPRequestHandler, status: int, body) -> None:
    payload = json.dumps(body, separators=(",", ":"), default=str).encode()
    handler.send_response(status)
    handler.send_header("content-type", "application/json")
    handler.send_header("content-length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        if path == "/health":
            return _json(self, 200, {"ok": True, "ts": time.time(), "registry": runner.registry_info(),
                                     "halted": list(halt.active_halts())})
        if path == "/state":
            rows = CONN.execute("SELECT cloid,strategy,coin,is_long,open_ts,open_px,size_usd,sl_px,tp_px,status,extras_json FROM trades ORDER BY open_ts DESC LIMIT 200").fetchall()
            return _json(self, 200, [dict(r) for r in rows])
        if path == "/signals":
            n = int(q.get("limit", "100"))
            rows = CONN.execute("SELECT * FROM signals ORDER BY id DESC LIMIT ?", (n,)).fetchall()
            return _json(self, 200, [dict(r) for r in rows])
        if path == "/closures":
            n = int(q.get("limit", "1000"))
            since = float(q.get("since", "0"))
            rows = CONN.execute("SELECT * FROM closures WHERE close_ts>=? ORDER BY id DESC LIMIT ?", (since, n)).fetchall()
            return _json(self, 200, [dict(r) for r in rows])
        return _json(self, 404, {"error": "not_found"})

    def do_POST(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        parts = path.strip("/").split("/")
        body_len = int(self.headers.get("content-length", "0") or "0")
        raw = self.rfile.read(body_len) if body_len else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        token = self.headers.get("X-Halt-Token")

        if len(parts) >= 2 and parts[0] == "halt":
            if not halt.halt_token_ok(token):
                return _json(self, 401, {"error": "bad_token"})
            target = parts[1]
            reason = body.get("reason", "")
            actor = body.get("actor", "api")
            if target == "all":
                halt.halt_all(CONN, reason=reason, actor=actor)
            else:
                halt.set_halt(CONN, target, True, reason=reason, actor=actor)
            return _json(self, 200, {"ok": True, "halted": list(halt.active_halts())})

        if len(parts) >= 2 and parts[0] == "resume":
            if not halt.halt_token_ok(token):
                return _json(self, 401, {"error": "bad_token"})
            target = parts[1]
            halt.set_halt(CONN, target, False, actor=body.get("actor", "api"))
            return _json(self, 200, {"ok": True, "halted": list(halt.active_halts())})

        if path == "/precog/webhook":
            # HMAC verification of X-Precog-Sig (hex sha256 of body with PRECOG_WEBHOOK_SECRET)
            import hmac, hashlib
            secret = os.environ.get("PRECOG_WEBHOOK_SECRET", "")
            sig_hdr = self.headers.get("X-Precog-Sig") or self.headers.get("x-precog-sig", "")
            if not secret:
                return _json(self, 503, {"error": "no_secret_configured"})
            expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(expected, (sig_hdr or "").lower()):
                return _json(self, 401, {"error": "bad_signature"})
            coin = (body.get("coin") or "").upper()
            if not coin:
                return _json(self, 400, {"error": "no_coin"})
            try:
                from strategy_runner.strategies import precog as precog_mod
                precog_mod.enqueue(coin, body)
            except Exception as e:
                return _json(self, 500, {"error": str(e)})
            return _json(self, 200, {"ok": True, "queue": precog_mod.queue_stats()})

        return _json(self, 404, {"error": "not_found"})


def _scan_loop() -> None:
    interval = config.get_int("SCAN_INTERVAL_SEC", 300)
    log.info("scan loop interval=%ds", interval)
    while True:
        t0 = time.time()
        try:
            def on_sig(strat, sig, decision):
                TRADER.open(strat, sig, decision.size_usd)
            n = runner.scan_once(BUS, PM, on_sig)
            if n:
                log.info("scan: %d signals processed", n)
        except Exception:
            log.exception("scan error")
        elapsed = time.time() - t0
        time.sleep(max(0, interval - elapsed))


def _position_loop() -> None:
    from .runner import REGISTRY
    while True:
        try:
            closed = TRADER.position_loop_once(registry=REGISTRY)
            if closed:
                log.info("position loop: closed %d", closed)
        except Exception:
            log.exception("position loop error")
        time.sleep(60)


def main() -> None:
    global CONN, BUS, PM, TRADER
    state = config.state_dir()
    CONN = persistence.init_db(os.path.join(state, "strategy_runner.db"))
    halt.load_active_halts(CONN)

    BUS = BusClient()
    PM = PMClient()
    try:
        hl = HLExchange()
    except Exception:
        hl = None
    TRADER = Trader(CONN, BUS, PM, hl)

    threading.Thread(target=_scan_loop, daemon=True, name="scan").start()
    threading.Thread(target=_position_loop, daemon=True, name="positions").start()

    port = config.get_int("HTTP_PORT", 10000)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("strategy-runner listening on :%d; registry=%s", port, runner.registry_info())
    server.serve_forever()


if __name__ == "__main__":
    main()
