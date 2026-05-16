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
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(body, default=str).encode())

    # ─── Landing page support ───
    def _serve_landing(self) -> None:
        """Serve the PSYCHO PM TERMINAL landing (operator's designed page)."""
        landing_path = os.path.join(os.path.dirname(__file__), "static", "landing.html")
        try:
            with open(landing_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self._json(500, {"error": "landing_unavailable", "detail": str(e)})

    def _pm_data(self) -> dict:
        """Fetch PM regime + account for shared use across endpoints."""
        try:
            with httpx.Client(timeout=2.0) as cli:
                r = cli.get(f"http://localhost:{PM_PORT}/health")
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        return {}

    def _strategy_state(self) -> dict:
        try:
            with httpx.Client(timeout=2.0) as cli:
                r = cli.get(f"http://localhost:{STRATEGY_PORT}/health")
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        return {}

    def _health_for_landing(self) -> dict:
        """Health shape that landing expects: btc_macro, webhook_security, engine_auto_pause."""
        base = _aggregate_health()
        pm = self._pm_data()
        sr = self._strategy_state()
        # Landing uses btc_macro for top hero, engine_auto_pause for paused engines list
        regime = pm.get("regime", {}) or {}
        account = pm.get("account", {}) or {}
        engines = sr.get("registry", []) or []
        # Build engine_auto_pause map
        eap = {"engines": {e["name"]: {"paused": False} for e in engines}}
        return {
            "ok": base.get("core") == "ok",
            "ts": int(time.time() * 1000),
            "core": base.get("core"),
            "subsystems": base.get("subsystems", {}),
            "btc_macro": {
                "regime": regime.get("regime", "unknown"),
                "confidence": regime.get("confidence", 0),
                "ema20_slope": regime.get("ema20_slope", 0),
                "atr_pct": regime.get("atr_pct", 0),
            },
            "webhook_security": {"ok": True, "scheme": "X-PM-Auth"},
            "engine_auto_pause": eap,
            "account_value": account.get("value", 0),
        }

    def _serve_dash(self) -> None:
        """Hero dashboard payload — equity + open positions."""
        pm = self._pm_data()
        sr = self._strategy_state()
        account = pm.get("account", {}) or {}
        regime = pm.get("regime", {}) or {}
        engines = sr.get("registry", []) or []
        out = {
            "ts": int(time.time() * 1000),
            "equity": float(account.get("value", 0)),
            "open_positions": account.get("positions", []),
            "open_count": len(account.get("positions", []) or []),
            "upnl": 0.0,
            "rpnl_24h": 0.0,
            "regime": regime.get("regime", "unknown"),
            "regime_confidence": regime.get("confidence", 0),
            "engines_total": len(engines),
            "engines_live": len([e for e in engines if e.get("tf")]),
        }
        self._json(200, out)

    def _serve_api_engines(self) -> None:
        """Landing's /api/engines — slightly different shape than PM /engines.
        Adds venues + venue_ages for top-of-page counters."""
        sr = self._strategy_state()
        engines = sr.get("registry", []) or []
        # Get PM /engines for the rich registry
        engines_pm = []
        try:
            with httpx.Client(timeout=2.0) as cli:
                r = cli.get(f"http://localhost:{PM_PORT}/engines")
                if r.status_code == 200:
                    engines_pm = r.json().get("engines", [])
        except Exception:
            pass
        sb_data = {}
        try:
            with httpx.Client(timeout=2.0) as cli:
                r = cli.get(f"http://localhost:{SIGNAL_BUS_PORT}/health")
                if r.status_code == 200:
                    sb_data = r.json()
        except Exception:
            pass
        ws_alive = sb_data.get("ws_alive", {}) or {}
        venues = {v: bool(ok) for v, ok in ws_alive.items()}
        last_update = sb_data.get("last_update", {}) or {}
        now_ms = int(time.time() * 1000)
        venue_ages = {v: max(0, int(now_ms - float(t) * 1000))
                      for v, t in last_update.items() if t}
        out = {
            "ts": now_ms,
            "engines": engines_pm or engines,
            "venues": venues,
            "venue_ages": venue_ages,
        }
        self._json(200, out)

    def _serve_all_systems(self) -> None:
        """Landing's /all_systems endpoint — engine summary cards."""
        engines_pm = []
        try:
            with httpx.Client(timeout=2.0) as cli:
                r = cli.get(f"http://localhost:{PM_PORT}/engines")
                if r.status_code == 200:
                    engines_pm = r.json().get("engines", [])
        except Exception:
            pass
        systems = []
        for e in engines_pm:
            systems.append({
                "engine_key": e.get("name"),
                "name": e.get("name"),
                "stage": e.get("stage", "paper"),
                "class": e.get("class", ""),
                "capital_fraction": e.get("capital_fraction", 0),
                "bt_pf": e.get("bt_pf", 0),
                "live": e.get("live", False),
                "warning": e.get("warning"),
                "url": e.get("url", ""),
                "halt_url": e.get("halt_url", ""),
            })
        self._json(200, {"systems": systems, "ts": int(time.time() * 1000)})

    def _serve_signals_compat(self) -> None:
        """Landing's /signals — recent signals across all engines."""
        try:
            with httpx.Client(timeout=2.0) as cli:
                r = cli.get(f"http://localhost:{STRATEGY_PORT}/signals?limit=50")
                if r.status_code == 200:
                    data = r.json()
                    # Landing expects {items: [...]}
                    if isinstance(data, list):
                        return self._json(200, {"items": data})
                    if "items" not in data:
                        data = {"items": data.get("signals", []) or []}
                    return self._json(200, data)
        except Exception:
            pass
        self._json(200, {"items": []})

    def _serve_orderbook(self, coin: str) -> None:
        """Landing's /orderbook/<coin> — pull from HL via signal_bus markprice."""
        try:
            with httpx.Client(timeout=3.0) as cli:
                r = cli.get(f"http://localhost:{SIGNAL_BUS_PORT}/markprice/{coin}")
                if r.status_code == 200:
                    mp = r.json()
                    mid = mp.get("hl_mid") or mp.get("binance_mid") or 0
                    # Synthetic minimal book — landing draws bars not real depth
                    return self._json(200, {
                        "coin": coin, "mid": mid,
                        "bids": [[mid * 0.999, 1.0]],
                        "asks": [[mid * 1.001, 1.0]],
                    })
        except Exception:
            pass
        self._json(200, {"coin": coin, "mid": 0, "bids": [], "asks": []})

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
            self._json(200, self._health_for_landing())
            return
        if path == "/" or path == "/index.html":
            return self._serve_landing()
        # Compatibility endpoints for the precog landing.html
        if path == "/dash":
            return self._serve_dash()
        if path == "/api/engines":
            return self._serve_api_engines()
        if path == "/all_systems":
            return self._serve_all_systems()
        if path == "/signals":
            return self._serve_signals_compat()
        if path == "/whales":
            return self._json(200, {"items": []})
        if path == "/news":
            return self._json(200, {"items": []})
        if path.startswith("/audit/deep"):
            return self._json(200, {"per_coin": [], "hours": 24})
        if path.startswith("/orderbook/"):
            coin = path.split("/")[-1]
            return self._serve_orderbook(coin)
        # Other landing sub-pages — redirect to existing equivalents
        if path in ("/engines", "/audit", "/system", "/macro",
                    "/enforce", "/experiment", "/violations"):
            target = os.environ.get(
                "DASHBOARD_URL",
                "https://quant-stack-dashboard-phbu.onrender.com/",
            ) + path.lstrip("/")
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
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
