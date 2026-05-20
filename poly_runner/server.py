"""poly-runner HTTP server entry point.

Exposes runner control endpoints; runner.main() runs the async loops.

Endpoints:
  GET  /health                runner liveness + strategy enable map
  GET  /state                 open positions + recent signals
  GET  /closures              recent resolutions/fills
  POST /halt/<name>           halt one strategy (header X-Halt-Token)
  POST /halt/all              halt every strategy
  POST /unhalt/<name>         resume strategy
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from common.poly_persistence import connect_poly
from poly_runner import runner
from poly_runner.runner import (
    HALT_TOKEN,
    halt as runner_halt,
    halt_all as runner_halt_all,
    is_enabled,
    unhalt as runner_unhalt,
)
from poly_runner.strategies import REGISTRY


log = logging.getLogger("poly_runner_server")

HTTP_PORT = int(os.environ.get("HTTP_PORT", "10101"))


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

    def _auth(self) -> bool:
        token = self.headers.get("X-Halt-Token") or self.headers.get("x-halt-token")
        return bool(HALT_TOKEN) and token == HALT_TOKEN

    def do_GET(self) -> None:  # noqa
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        if not parts:
            return self._send_json(200, {"service": "poly-runner"})

        if parts[0] == "health":
            return self._send_json(200, {
                "service": "poly-runner",
                "live": runner.LIVE_TRADING,
                "strategies": {n: is_enabled(n) for n in REGISTRY},
            })

        if parts[0] == "state":
            conn = connect_poly()
            try:
                positions = [
                    dict(zip(("market_id", "token", "qty", "avg_cost", "last_update_ts"), r))
                    for r in conn.execute(
                        "SELECT market_id, token, qty, avg_cost, last_update_ts"
                        " FROM poly_positions WHERE qty != 0").fetchall()
                ]
                recent_signals = [
                    dict(zip(("ts", "strategy", "market_id", "asset", "token", "side",
                              "price", "size_usdc", "edge_bps", "fire_reason"), r))
                    for r in conn.execute(
                        "SELECT ts, strategy, market_id, asset, token, side, price,"
                        " size_usdc, edge_bps, fire_reason FROM poly_signals"
                        " ORDER BY ts DESC LIMIT 50").fetchall()
                ]
            finally:
                conn.close()
            return self._send_json(200, {
                "positions": positions, "recent_signals": recent_signals,
            })

        if parts[0] == "closures":
            conn = connect_poly()
            try:
                limit = 200
                rows = conn.execute(
                    "SELECT cloid, strategy, market_id, token, side, price, size_usdc,"
                    " status, fill_amount, fill_price, submit_ts, signing_ms, total_ms"
                    " FROM poly_orders ORDER BY submit_ts DESC LIMIT ?", (limit,)).fetchall()
                cols = ("cloid", "strategy", "market_id", "token", "side", "price",
                        "size_usdc", "status", "fill_amount", "fill_price", "submit_ts",
                        "signing_ms", "total_ms")
                out = [dict(zip(cols, r)) for r in rows]
            finally:
                conn.close()
            return self._send_json(200, out)

        self._send_json(404, {"error": "unknown path"})

    def do_POST(self) -> None:  # noqa
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        if not parts:
            return self._send_json(404, {"error": "unknown path"})

        if parts[0] in ("halt", "unhalt"):
            if not self._auth():
                return self._send_json(401, {"error": "bad halt token"})
            action = parts[0]
            if len(parts) < 2:
                return self._send_json(400, {"error": "missing target"})
            target = parts[1]
            if target == "all":
                runner_halt_all()
                return self._send_json(200, {"halted": list(REGISTRY.keys())})
            if target not in REGISTRY:
                return self._send_json(404, {"error": "unknown strategy"})
            if action == "halt":
                runner_halt(target)
            else:
                runner_unhalt(target)
            return self._send_json(200, {"strategy": target, "halted": not is_enabled(target)})

        self._send_json(404, {"error": "unknown path"})


def _serve_http() -> None:
    addr = ("0.0.0.0", HTTP_PORT)
    httpd = ThreadingHTTPServer(addr, Handler)
    log.info(f"poly-runner http listening on {addr}")
    httpd.serve_forever()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    threading.Thread(target=_serve_http, daemon=True).start()
    asyncio.run(runner.main())


if __name__ == "__main__":
    main()
