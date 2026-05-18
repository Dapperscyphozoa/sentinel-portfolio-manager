"""monitor — runs scheduled routines + exposes /health and run history."""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common import config, persistence  # noqa: E402
from monitor import spend  # noqa: E402
from monitor.routines import health_check, daily_report, drawdown_check, lock_audit, auto_demote  # noqa: E402


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("monitor")


CONN = None
LAST_RUNS: dict = {}


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
        if path == "/health":
            return _json(self, 200, {
                "ok": True, "ts": time.time(),
                "spent_today_usd": spend.spent_today_usd(CONN),
                "daily_budget_usd": float(os.environ.get("DAILY_API_BUDGET_USD", "5")),
                "last_runs": LAST_RUNS,
            })
        if path == "/last":
            return _json(self, 200, LAST_RUNS)
        return _json(self, 404, {"error": "not_found"})


def _runner(name: str, fn, interval_s: int) -> None:
    log.info("routine %s every %ds", name, interval_s)
    while True:
        t0 = time.time()
        try:
            res = fn(CONN)
            LAST_RUNS[name] = {"ts": time.time(), "ok": True, "result": res}
            log.info("routine %s ok in %.1fs", name, time.time() - t0)
        except Exception as e:
            log.exception("routine %s failed", name)
            LAST_RUNS[name] = {"ts": time.time(), "ok": False, "error": str(e)}
        elapsed = time.time() - t0
        time.sleep(max(5, interval_s - elapsed))


def main() -> None:
    global CONN
    state = config.state_dir()
    CONN = persistence.init_db(os.path.join(state, "monitor.db"))

    threading.Thread(target=_runner, args=("drawdown_check", drawdown_check.run, 300),
                     daemon=True, name="drawdown_loop").start()
    threading.Thread(target=_runner, args=("health_check", health_check.run, 900),
                     daemon=True, name="health_loop").start()
    threading.Thread(target=_runner, args=("daily_report", daily_report.run, 86400),
                     daemon=True, name="daily_loop").start()
    threading.Thread(target=_runner, args=("lock_audit", lock_audit.run, 300),
                     daemon=True, name="lock_audit_loop").start()
    threading.Thread(target=_runner, args=("auto_demote", auto_demote.run, 3600),
                     daemon=True, name="auto_demote_loop").start()

    port = config.get_int("HTTP_PORT", 10000)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("monitor listening on :%d", port)
    server.serve_forever()


if __name__ == "__main__":
    main()
