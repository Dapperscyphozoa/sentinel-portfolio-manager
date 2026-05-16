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

    def _landing(self) -> None:
        """Live status landing page."""
        h = _aggregate_health()
        sub = h.get("subsystems", {})
        def st(name):
            s = sub.get(name, {}).get("status", "?")
            color = "#22c55e" if s == "ok" else "#ef4444"
            return f'<span style="color:{color}">●</span> <b>{name}</b>: {s}'
        sb = sub.get("signal_bus", {}).get("data", {}) or {}
        sr = sub.get("strategy_runner", {}).get("data", {}) or {}
        pm = sub.get("pm", {}).get("data", {}) or {}
        mon = sub.get("monitor", {}).get("data", {}) or {}
        regime = pm.get("regime", {}) or {}
        account = pm.get("account", {}) or {}
        engines = sr.get("registry", []) or []
        ws_alive = sb.get("ws_alive", {}) or {}
        html = f"""<!DOCTYPE html>
<html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Sentinel Core — Live</title>
<style>
body{{font-family:ui-monospace,SF Mono,Menlo,monospace;background:#0a0a0b;color:#e4e4e7;
     margin:0;padding:24px;line-height:1.5;}}
h1{{font-size:22px;color:#fafafa;margin:0 0 4px;letter-spacing:.02em;}}
.sub{{color:#71717a;font-size:13px;margin-bottom:24px;}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));
       gap:16px;margin-bottom:24px;}}
.card{{background:#18181b;border:1px solid #27272a;border-radius:8px;padding:16px;}}
.card h2{{font-size:13px;color:#a1a1aa;text-transform:uppercase;letter-spacing:.06em;
         margin:0 0 12px;font-weight:600;}}
.metric{{display:flex;justify-content:space-between;align-items:baseline;
         padding:6px 0;border-bottom:1px solid #27272a;font-size:13px;}}
.metric:last-child{{border:0;}}
.metric .k{{color:#a1a1aa;}}
.metric .v{{color:#fafafa;font-weight:600;}}
.eng{{font-size:12px;padding:8px 12px;background:#0f0f10;border:1px solid #27272a;
      border-radius:6px;margin:4px 0;display:flex;justify-content:space-between;}}
.green{{color:#22c55e;}}
.red{{color:#ef4444;}}
.amber{{color:#f59e0b;}}
.row{{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px;}}
.row a{{color:#60a5fa;text-decoration:none;font-size:12px;padding:4px 10px;
       background:#1e293b;border-radius:4px;}}
.row a:hover{{background:#334155;color:#93c5fd;}}
table{{width:100%;border-collapse:collapse;font-size:12px;}}
th{{text-align:left;color:#a1a1aa;font-weight:600;padding:6px 8px;
   border-bottom:1px solid #27272a;}}
td{{padding:6px 8px;border-bottom:1px solid #18181b;}}
tr:hover{{background:#1a1a1d;}}
.pulse{{display:inline-block;width:8px;height:8px;border-radius:50%;
       margin-right:6px;animation:pulse 2s infinite;}}
@keyframes pulse{{0%,100%{{opacity:1;}}50%{{opacity:.4;}}}}
.brand{{color:#fafafa;font-weight:700;}}
.brand span{{color:#22c55e;}}
</style></head><body>

<h1 class=brand>SENTINEL<span>.</span>CORE</h1>
<div class=sub>collapsed TA stack + sniper bot — paper mode — autorefresh 5s</div>

<div class=grid>

  <div class=card>
    <h2>Core Status</h2>
    <div class=metric><span class=k>core</span><span class="v {'green' if h.get('core')=='ok' else 'red'}">{h.get('core','?').upper()}</span></div>
    <div class=metric><span class=k>signal_bus</span><span class=v>{sub.get('signal_bus',{}).get('status','?')}</span></div>
    <div class=metric><span class=k>strategy_runner</span><span class=v>{sub.get('strategy_runner',{}).get('status','?')}</span></div>
    <div class=metric><span class=k>pm</span><span class=v>{sub.get('pm',{}).get('status','?')}</span></div>
    <div class=metric><span class=k>monitor</span><span class=v>{sub.get('monitor',{}).get('status','?')}</span></div>
  </div>

  <div class=card>
    <h2>Account</h2>
    <div class=metric><span class=k>wallet value</span><span class=v>${float(account.get('value',0)):.2f}</span></div>
    <div class=metric><span class=k>positions</span><span class=v>{len(account.get('positions',[]))}</span></div>
    <div class=metric><span class=k>regime</span><span class=v>{regime.get('regime','?')}</span></div>
    <div class=metric><span class=k>regime conf</span><span class=v>{float(regime.get('confidence',0))*100:.0f}%</span></div>
  </div>

  <div class=card>
    <h2>Feeds</h2>
    <div class=metric><span class=k>binance ws</span><span class="v {'green' if ws_alive.get('binance') else 'red'}">{'ALIVE' if ws_alive.get('binance') else 'DOWN'}</span></div>
    <div class=metric><span class=k>hl ws</span><span class="v {'green' if ws_alive.get('hl') else 'red'}">{'ALIVE' if ws_alive.get('hl') else 'DOWN'}</span></div>
    <div class=metric><span class=k>okx ws</span><span class=v>{'alive' if ws_alive.get('okx') else 'down'}</span></div>
    <div class=metric><span class=k>bybit ws</span><span class=v>{'alive' if ws_alive.get('bybit') else 'down'}</span></div>
    <div class=metric><span class=k>kline cache</span><span class=v>{sb.get('kline_bars_total','?')} bars / {sb.get('kline_keys','?')} keys</span></div>
  </div>

  <div class=card>
    <h2>Monitor</h2>
    <div class=metric><span class=k>spent today</span><span class=v>${float(mon.get('spent_today_usd',0)):.4f}</span></div>
    <div class=metric><span class=k>budget</span><span class=v>${float(mon.get('daily_budget_usd',0)):.2f}/day</span></div>
    <div class=metric><span class=k>routines</span><span class=v>{len(mon.get('last_runs',{}))}</span></div>
  </div>

</div>

<div class=card>
  <h2>Engines Registered ({len(engines)})</h2>
  <table><thead><tr><th>name</th><th>tf</th><th>affinity</th><th>universe</th></tr></thead><tbody>
  {''.join(f'<tr><td><b>{e.get("name","?")}</b></td><td>{e.get("tf","?")}</td><td>{",".join(e.get("affinity",[]))}</td><td>{e.get("universe_size","?")}</td></tr>' for e in engines[:30])}
  </tbody></table>
</div>

<div class=row>
  <a href="/health">/health</a>
  <a href="/pm/regime">/pm/regime</a>
  <a href="/pm/engines">/pm/engines</a>
  <a href="/strategy/state">/strategy/state</a>
  <a href="/signal_bus/health">/signal_bus/health</a>
  <a href="/monitor/health">/monitor/health</a>
  <a href="https://sniper-6w9l.onrender.com/health">sniper /health</a>
  <a href="https://quant-stack-dashboard-phbu.onrender.com/">full dashboard ↗</a>
</div>

<div class=sub style=margin-top:24px>
  ts: {int(time.time())} · last update {int(h.get('ts',0)/1000)} · auto-refresh in <b>5s</b>
</div>

<script>setTimeout(()=>location.reload(),5000)</script>
</body></html>"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(html.encode())

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
        if path == "/" or path == "/index.html":
            return self._landing()
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
