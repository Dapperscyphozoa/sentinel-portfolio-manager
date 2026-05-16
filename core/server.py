"""core service — collapsed TA stack.

Runs in ONE Render process:
  - signal_bus  (Binance + HL WS feeds, HTTP cache)  on localhost:10001
  - strategy_runner (engine scan loop, order placement) on localhost:10002
  - pm (pretrade gate, attribution)                  on localhost:10003
  - monitor (Claude routines + drawdown watch)       on localhost:10004

Exposes ONE public port (HTTP_PORT, default 10000):
  GET  /health                  aggregated subsystem health
  GET  /state                   aggregated state snapshot
  *    /signal_bus/<path>       proxies to localhost:10001/<path>
  *    /strategy/<path>         proxies to localhost:10002/<path>
  *    /pm/<path>               proxies to localhost:10003/<path>
  *    /monitor/<path>          proxies to localhost:10004/<path>

Inter-service calls (BusClient, PMClient) auto-route to localhost ports via
the SIGNAL_BUS_URL and PM_URL env vars we set below.

This eliminates 4-way HTTP latency between services + 3/4 of Render cost.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import httpx

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("core")

# Make project root importable
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ─────────────────── Internal subsystem ports ───────────────────
SIGNAL_BUS_PORT = 10001
STRATEGY_PORT   = 10002
PM_PORT         = 10003
MONITOR_PORT    = 10004
PUBLIC_PORT     = int(os.environ.get("HTTP_PORT", "10000"))

# Wire inter-service URLs to localhost BEFORE importing subsystems
os.environ["SIGNAL_BUS_URL"] = f"http://localhost:{SIGNAL_BUS_PORT}"
os.environ["PM_URL"]         = f"http://localhost:{PM_PORT}"


def _start_signal_bus():
    os.environ["HTTP_PORT"] = str(SIGNAL_BUS_PORT)
    try:
        from signal_bus import server as sb_server
        log.info("starting signal_bus on :%d", SIGNAL_BUS_PORT)
        sb_server.main()
    except Exception as e:
        log.exception("signal_bus crashed: %s", e)


def _start_strategy_runner():
    # Wait briefly for signal_bus to bind so first scan loop finds it
    time.sleep(3.0)
    os.environ["HTTP_PORT"] = str(STRATEGY_PORT)
    try:
        from strategy_runner import server as sr_server
        log.info("starting strategy_runner on :%d", STRATEGY_PORT)
        sr_server.main()
    except Exception as e:
        log.exception("strategy_runner crashed: %s", e)


def _start_pm():
    time.sleep(1.5)
    os.environ["HTTP_PORT"] = str(PM_PORT)
    try:
        from pm import server as pm_server
        log.info("starting pm on :%d", PM_PORT)
        pm_server.main()
    except Exception as e:
        log.exception("pm crashed: %s", e)


def _start_monitor():
    time.sleep(4.5)
    os.environ["HTTP_PORT"] = str(MONITOR_PORT)
    try:
        from monitor import server as mon_server
        log.info("starting monitor on :%d", MONITOR_PORT)
        mon_server.main()
    except Exception as e:
        log.exception("monitor crashed: %s", e)


# ─────────────────── Public proxy / aggregator ───────────────────
_PROXY_MAP = {
    "/signal_bus": (SIGNAL_BUS_PORT, "/signal_bus"),
    "/strategy":   (STRATEGY_PORT,   "/strategy"),
    "/pm":         (PM_PORT,         "/pm"),
    "/monitor":    (MONITOR_PORT,    "/monitor"),
}

_HEALTH_CACHE: dict = {"ts": 0, "data": None}
_HEALTH_TTL_S = 3.0


def _aggregate_health() -> dict:
    """Poll all 4 subsystems and aggregate their /health."""
    now = time.time()
    if _HEALTH_CACHE["data"] and now - _HEALTH_CACHE["ts"] < _HEALTH_TTL_S:
        return _HEALTH_CACHE["data"]
    out: dict = {"core": "ok", "ts": int(now * 1000), "subsystems": {}}
    targets = [
        ("signal_bus", SIGNAL_BUS_PORT),
        ("strategy_runner", STRATEGY_PORT),
        ("pm", PM_PORT),
        ("monitor", MONITOR_PORT),
    ]
    with httpx.Client(timeout=2.0) as cli:
        for name, port in targets:
            try:
                r = cli.get(f"http://localhost:{port}/health")
                if r.status_code == 200:
                    out["subsystems"][name] = {"status": "ok",
                                                "data": (r.json() if r.headers.get("content-type","").startswith("application/json") else r.text[:200])}
                else:
                    out["subsystems"][name] = {"status": f"http_{r.status_code}"}
            except Exception as e:
                out["subsystems"][name] = {"status": "down", "error": str(e)[:120]}
    # Core is "ok" only if at least signal_bus + strategy + pm are up
    core_critical = ["signal_bus", "strategy_runner", "pm"]
    if any(out["subsystems"].get(n, {}).get("status") != "ok" for n in core_critical):
        out["core"] = "degraded"
    _HEALTH_CACHE["ts"] = now
    _HEALTH_CACHE["data"] = out
    return out


class Handler(BaseHTTPRequestHandler):
    def _json(self, code: int, body: dict) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body, default=str).encode())

    def _proxy(self, target_port: int, strip_prefix: str) -> None:
        """Forward request to localhost:target_port."""
        path = self.path
        if path.startswith(strip_prefix):
            path = path[len(strip_prefix):] or "/"
        method = self.command
        body_len = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(body_len) if body_len else None
        # Forward selected headers (auth tokens)
        fwd_headers = {}
        for h in ("X-PM-Auth", "X-Halt-Token", "X-Sniper-Auth", "Content-Type"):
            if h in self.headers:
                fwd_headers[h] = self.headers[h]
        url = f"http://localhost:{target_port}{path}"
        try:
            with httpx.Client(timeout=30.0) as cli:
                r = cli.request(method, url, content=body, headers=fwd_headers)
            self.send_response(r.status_code)
            ct = r.headers.get("content-type", "application/json")
            self.send_header("Content-Type", ct)
            self.end_headers()
            self.wfile.write(r.content)
        except Exception as e:
            self._json(502, {"error": "proxy_failed", "detail": str(e)})

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/health":
            self._json(200, _aggregate_health())
            return
        if path == "/":
            self._json(200, {"service": "core", "version": "1.0",
                              "endpoints": ["/health",
                                            "/signal_bus/*", "/strategy/*",
                                            "/pm/*", "/monitor/*"]})
            return
        # Route to subsystem
        for prefix, (port, strip) in _PROXY_MAP.items():
            if path == prefix or path.startswith(prefix + "/"):
                self._proxy(port, strip)
                return
        self._json(404, {"error": "not found", "path": path})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        for prefix, (port, strip) in _PROXY_MAP.items():
            if path == prefix or path.startswith(prefix + "/"):
                self._proxy(port, strip)
                return
        self._json(404, {"error": "not found", "path": path})

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)


def main():
    log.info("core service starting — public port %d, internal %d/%d/%d/%d",
             PUBLIC_PORT, SIGNAL_BUS_PORT, STRATEGY_PORT, PM_PORT, MONITOR_PORT)
    threads = []
    for fn in (_start_signal_bus, _start_strategy_runner, _start_pm, _start_monitor):
        t = threading.Thread(target=fn, daemon=True, name=fn.__name__)
        t.start()
        threads.append(t)
    # Give subsystems time to bind
    log.info("subsystems launching — waiting 8s for binds...")
    time.sleep(8)
    log.info("starting public HTTP on :%d", PUBLIC_PORT)
    srv = ThreadingHTTPServer(("0.0.0.0", PUBLIC_PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")


if __name__ == "__main__":
    main()
