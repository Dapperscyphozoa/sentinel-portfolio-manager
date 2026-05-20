"""poly-monitor: scheduled drift/PnL/inventory checks for the poly stack.

Routines (each cadence in seconds):
  cl_drift_check        300   alert if rolling-1h CL prediction median > 10bps
  fee_drag              900   compute net-of-fee P&L per strategy
  inventory_skew        180   alert if maker_quote inventory > 80% of cap
  promotion_candidates  600   surface lifecycle.evaluate_all()
  drawdown_watch        120   trigger halt if any strategy DD > 25%

Output: writes to poly_monitor_alerts table; exposes /alerts endpoint.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from common.poly_persistence import connect_poly, init_poly_db

from . import scheduler   # populated below
from .routines import (
    cl_drift_check,
    drawdown_watch,
    fee_drag,
    inventory_skew,
    promotion_candidates,
)

log = logging.getLogger("poly_monitor")


def main_loop() -> None:
    sched = scheduler.Scheduler()
    sched.every(300, "cl_drift_check", cl_drift_check.run)
    sched.every(900, "fee_drag", fee_drag.run)
    sched.every(180, "inventory_skew", inventory_skew.run)
    sched.every(600, "promotion_candidates", promotion_candidates.run)
    sched.every(120, "drawdown_watch", drawdown_watch.run)
    sched.run_forever()


def serve_http() -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse
    import json

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa
            pass

        def do_GET(self) -> None:  # noqa
            u = urlparse(self.path)
            parts = [p for p in u.path.split("/") if p]
            if not parts or parts[0] == "health":
                body = json.dumps({"service": "poly-monitor"}).encode()
                self.send_response(200); self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers(); self.wfile.write(body); return
            if parts[0] == "alerts":
                conn = connect_poly()
                try:
                    rows = conn.execute(
                        "CREATE TABLE IF NOT EXISTS poly_monitor_alerts ("
                        "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,"
                        " routine TEXT, level TEXT, message TEXT)")
                    rows = conn.execute(
                        "SELECT ts, routine, level, message FROM poly_monitor_alerts"
                        " ORDER BY ts DESC LIMIT 200").fetchall()
                finally:
                    conn.close()
                body = json.dumps([
                    {"ts": r[0], "routine": r[1], "level": r[2], "message": r[3]}
                    for r in rows]).encode()
                self.send_response(200); self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers(); self.wfile.write(body); return
            self.send_response(404); self.end_headers()

    port = int(os.environ.get("HTTP_PORT", "10103"))
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info(f"poly-monitor http listening on :{port}")
    httpd.serve_forever()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    init_poly_db()
    threading.Thread(target=serve_http, daemon=True).start()
    main_loop()


if __name__ == "__main__":
    main()
