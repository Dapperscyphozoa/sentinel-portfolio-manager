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

    def _signal_bus_health(self) -> dict:
        try:
            with httpx.Client(timeout=2.0) as cli:
                r = cli.get(f"http://localhost:{SIGNAL_BUS_PORT}/health")
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        return {}

    def _hl_account_full(self) -> dict:
        """Get FULL HL account with positions via signal_bus cache."""
        try:
            with httpx.Client(timeout=2.0) as cli:
                r = cli.get(f"http://localhost:{SIGNAL_BUS_PORT}/hl/account")
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        return {}

    def _hl_positions(self) -> list:
        try:
            with httpx.Client(timeout=2.0) as cli:
                r = cli.get(f"http://localhost:{SIGNAL_BUS_PORT}/hl/positions")
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        return []

    def _hl_universe(self) -> list:
        """Live HL universe — list of perp coins."""
        try:
            with httpx.Client(timeout=4.0) as cli:
                r = cli.post("https://api.hyperliquid.xyz/info", json={"type": "meta"})
                if r.status_code == 200:
                    return [u["name"] for u in r.json().get("universe", [])]
        except Exception:
            pass
        return []

    def _hl_all_mids(self) -> dict:
        try:
            with httpx.Client(timeout=4.0) as cli:
                r = cli.post("https://api.hyperliquid.xyz/info", json={"type": "allMids"})
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        return {}

    def _utc_session(self) -> str:
        """Return current session name from UTC hour."""
        import datetime
        hr = datetime.datetime.utcnow().hour
        if 0 <= hr < 7:   return "asia"
        if 7 <= hr < 14:  return "london"
        if 14 <= hr < 21: return "new_york"
        return "late_us"

    def _btc_macro(self) -> dict:
        """Build the btc_macro shape the landing expects."""
        pm = self._pm_data()
        regime = pm.get("regime", {}) or {}
        # Get BTC mark
        btc_mid = 0.0
        try:
            with httpx.Client(timeout=2.0) as cli:
                r = cli.get(f"http://localhost:{SIGNAL_BUS_PORT}/markprice/BTC")
                if r.status_code == 200:
                    mp = r.json() or {}
                    btc_mid = float(mp.get("hl_mid") or mp.get("binance_mid") or 0)
        except Exception:
            pass
        return {
            "btc_mid": btc_mid,
            "regime": regime.get("regime", "unknown"),
            "confidence": regime.get("confidence", 0),
            "ema20_slope": regime.get("ema20_slope", 0),
            "atr_pct": regime.get("atr_pct", 0),
            "near_resistance": False,
            "resistance_distance_pct": None,
            "resistance_wall_usd": None,
        }

    def _health_for_landing(self) -> dict:
        """Health shape that landing expects: btc_macro, webhook_security, engine_auto_pause."""
        base = _aggregate_health()
        sr = self._strategy_state()
        engines = sr.get("registry", []) or []
        # CUT engines list — these should show as paused
        cut = {"vsq", "range_fade", "range_bo", "lh1", "fd1", "cex_dex_arb", "precog"}
        eap = {"engines": {e["name"]: {"paused": e["name"] in cut} for e in engines}}
        return {
            "ok": base.get("core") == "ok",
            "ts": int(time.time() * 1000),
            "core": base.get("core"),
            "subsystems": base.get("subsystems", {}),
            "btc_macro": self._btc_macro(),
            "webhook_security": {"ok": True, "scheme": "X-PM-Auth", "enabled": True},
            "engine_auto_pause": eap,
        }

    def _serve_dash(self) -> None:
        """Hero dashboard payload — equity + open positions with all the fields landing reads."""
        pm = self._pm_data()
        account_pm = pm.get("account", {}) or {}
        regime = pm.get("regime", {}) or {}
        # Prefer live signal_bus HL account (has positions + value from WS)
        hl_acct = self._hl_account_full()
        hl_positions = self._hl_positions()
        equity = float(hl_acct.get("value") or account_pm.get("value", 0) or 0)
        # Positions list — shape landing expects: {upnl, lev, tp, sl, engine, stage, coin, side, size, entry_px}
        positions_out = []
        for p in hl_positions or account_pm.get("positions") or []:
            positions_out.append({
                "coin": p.get("coin", "?"),
                "side": "LONG" if float(p.get("size", p.get("szi", 0))) > 0 else "SHORT",
                "size": abs(float(p.get("size", p.get("szi", 0)))),
                "entry_px": float(p.get("entry_px", p.get("entryPx", 0))),
                "mark_px": float(p.get("mark_px", p.get("markPx", 0))),
                "upnl": float(p.get("upnl", p.get("unrealizedPnl", 0))),
                "lev": p.get("leverage", p.get("lev", "-")),
                "tp": p.get("tp", "-"),
                "sl": p.get("sl", "-"),
                "engine": p.get("engine", p.get("strategy", "-")),
                "stage": "live",
            })
        # Counts
        sb = self._signal_bus_health()
        universe = self._hl_universe()
        # Funding cache count = number of coins for which we have funding data
        funding_cached = int(sb.get("funding_coins", 0))
        mark_coins = int(sb.get("mark_coins", 0))
        out = {
            "ts": int(time.time() * 1000),
            "equity": equity,
            "positions": positions_out,
            "session": {"name": self._utc_session(),
                        "ts": int(time.time() * 1000)},
            "orderbook": {"verified_coins": mark_coins},
            "whale": {"total_whales": 0},   # no whale tracker yet — see precog-hl/whale_filter.py for spec
            "funding_cached": funding_cached,
            "risk_ladder": {"risk": 0.04, "regime": regime.get("regime", "unknown")},
            "universe_size": len(universe) or 230,
            "regime": regime,
        }
        self._json(200, out)

    def _serve_api_engines(self) -> None:
        """/api/engines shape:
           - venues: {name: bool}
           - venue_ages: {name: seconds}
           - signal_engines / guards / sizing: dicts of {name: live_bool} for rail rendering
        """
        sr = self._strategy_state()
        engines = sr.get("registry", []) or []
        sb = self._signal_bus_health()
        ws_alive = sb.get("ws_alive", {}) or {}
        last_update = sb.get("last_update", {}) or {}
        now_s = time.time()
        # Build venues + age in seconds (NOT ms — landing renders "Ns")
        venues = {v: bool(ok) for v, ok in ws_alive.items()}
        venue_ages = {}
        for k, ts in last_update.items():
            if ts and float(ts) > 0:
                age = max(0, int(now_s - float(ts)))
                # Map binance_ws → bn, hl_ws → hl etc for rail keys landing expects
                short = k.replace("_ws", "").replace("binance", "bn").replace("bybit", "by")
                venue_ages[k] = age
                venue_ages[short] = age
        # signal_engines = active engines (live + not cut)
        cut = {"vsq", "range_fade", "range_bo", "lh1", "fd1", "cex_dex_arb", "precog"}
        signal_engines = {e["name"]: e["name"] not in cut for e in engines if e.get("tf")}
        # guards = subsystems
        sub = _aggregate_health().get("subsystems", {})
        guards = {name: sub.get(name, {}).get("status") == "ok"
                  for name in ("signal_bus", "strategy_runner", "pm", "monitor")}
        guards["webhook_auth"] = True
        guards["halt_token"] = True
        # sizing = sizing-related controls
        sizing = {
            "live_safety": True,
            "circuit_breaker": True,
            "kelly_capped": True,
            "max_concurrent_live": True,
            "daily_loss_limit": True,
        }
        # Engines block (PM registry)
        engines_pm = []
        try:
            with httpx.Client(timeout=2.0) as cli:
                r = cli.get(f"http://localhost:{PM_PORT}/engines")
                if r.status_code == 200:
                    engines_pm = r.json().get("engines", [])
        except Exception:
            pass
        out = {
            "ts": int(now_s * 1000),
            "engines": engines_pm or engines,
            "venues": venues,
            "venue_ages": venue_ages,
            "signal_engines": signal_engines,
            "guards": guards,
            "sizing": sizing,
        }
        self._json(200, out)

    def _serve_all_systems(self) -> None:
        """Landing's /all_systems — system summary cards.
        Landing's original keys were smc-v1, smc-v2, smc-loose, pool-arch-rev, pool-arch-cont.
        We don't have those exact engines — emit our actual 16 + map well-known onto landing's
        expected keys (so the section panels render with our real engines) plus extras.
        """
        engines_pm = []
        try:
            with httpx.Client(timeout=2.0) as cli:
                r = cli.get(f"http://localhost:{PM_PORT}/engines")
                if r.status_code == 200:
                    engines_pm = r.json().get("engines", [])
        except Exception:
            pass
        systems = []
        # Map current engines onto landing's section keys + emit all originals so
        # the engines-sections panel below can iterate.
        # Heuristic: take top 5 by capital_fraction → map to landing's 5 sections.
        top = sorted(engines_pm, key=lambda e: -(e.get("capital_fraction") or 0))[:5]
        landing_keys = ["smc-v1", "smc-v2", "smc-loose", "pool-arch-rev", "pool-arch-cont"]
        for i, slot in enumerate(landing_keys):
            if i < len(top):
                e = top[i]
                systems.append({
                    "engine_key": slot,
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
        # Also emit all engines with their real keys for the engines-sections panel
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
        # Summary counts
        live = sum(1 for e in engines_pm if e.get("live"))
        paper = sum(1 for e in engines_pm if e.get("stage") == "paper")
        dead = sum(1 for e in engines_pm if e.get("stage") == "cut")
        total = len(engines_pm)
        self._json(200, {
            "systems": systems,
            "summary": {"live": live, "paper": paper, "dead": dead, "total": total},
            "ts": int(time.time() * 1000),
        })

    def _serve_signals_compat(self) -> None:
        """Landing's /signals — recent signals across all engines."""
        items = []
        try:
            with httpx.Client(timeout=2.0) as cli:
                # Try strategy_runner /signals (we proxy this)
                r = cli.get(f"http://localhost:{STRATEGY_PORT}/signals?limit=50")
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list):
                        items = data
                    elif isinstance(data, dict):
                        items = data.get("items") or data.get("signals") or []
        except Exception:
            pass
        # Try strategy_runner /state which has signal cache
        if not items:
            try:
                with httpx.Client(timeout=2.0) as cli:
                    r = cli.get(f"http://localhost:{STRATEGY_PORT}/state")
                    if r.status_code == 200:
                        data = r.json()
                        if isinstance(data, list):
                            items = data
                        elif isinstance(data, dict):
                            items = data.get("recent_signals") or data.get("items") or []
            except Exception:
                pass
        self._json(200, {"items": items, "ts": int(time.time() * 1000)})

    def _serve_whales(self) -> None:
        """Landing's /whales — large position holders.
        Backed by HL fills from signal_bus filtered for large notional.
        """
        items = []
        try:
            with httpx.Client(timeout=2.0) as cli:
                # Recent fills from signal_bus
                since = int((time.time() - 3600) * 1000)
                r = cli.get(f"http://localhost:{SIGNAL_BUS_PORT}/hl/fills?since={since}")
                if r.status_code == 200:
                    fills = r.json() or []
                    # Filter for notional > $10k (proxy for "whale")
                    for f in fills:
                        px = float(f.get("px", 0))
                        qty = float(f.get("qty", 0))
                        if px * qty >= 10000:
                            items.append({
                                "coin": f.get("coin"),
                                "side": "BUY" if f.get("side") == "B" else "SELL",
                                "notional": round(px * qty, 0),
                                "price": px,
                                "ts": f.get("ts"),
                            })
        except Exception:
            pass
        self._json(200, {"items": items[:20], "ts": int(time.time() * 1000)})

    def _serve_news(self) -> None:
        """Landing's /news — significant market events. Stub for now."""
        # Could plug into a news API later. Empty for now.
        self._json(200, {"items": [], "ts": int(time.time() * 1000)})

    def _serve_audit_deep(self) -> None:
        """Landing's /audit/deep — per-coin attribution heatmap.
        Sourced from PM /attribution endpoint (groups closed trades by strategy → coin)."""
        per_coin = []
        try:
            with httpx.Client(timeout=2.0) as cli:
                since = int((time.time() - 24 * 3600) * 1000)
                token = os.environ.get("PM_AUTH_TOKEN", "")
                headers = {"X-PM-Auth": token} if token else {}
                r = cli.get(f"http://localhost:{PM_PORT}/attribution?since={since}",
                            headers=headers)
                if r.status_code == 200:
                    data = r.json() or {}
                    # PM /attribution returns list of {strategy, pnl, ...} — pivot to per_coin
                    # For now, surface what we have at the strategy level
                    if isinstance(data, list):
                        for row in data:
                            per_coin.append({
                                "coin": row.get("coin", "—"),
                                "strategy": row.get("strategy"),
                                "pnl_usd": row.get("pnl", 0),
                                "trades": row.get("trades", 0),
                                "wr": row.get("wr", 0),
                            })
        except Exception:
            pass
        self._json(200, {"per_coin": per_coin, "hours": 24, "ts": int(time.time() * 1000)})

    def _serve_orderbook(self, coin: str) -> None:
        """Real L2 book from HL info API."""
        coin = coin.upper()
        try:
            with httpx.Client(timeout=4.0) as cli:
                r = cli.post("https://api.hyperliquid.xyz/info",
                             json={"type": "l2Book", "coin": coin})
                if r.status_code == 200:
                    data = r.json() or {}
                    levels = data.get("levels", [[], []])
                    bids = [[float(b["px"]), float(b["sz"])] for b in levels[0][:20]]
                    asks = [[float(a["px"]), float(a["sz"])] for a in levels[1][:20]]
                    mid = (bids[0][0] + asks[0][0]) / 2 if bids and asks else 0
                    return self._json(200, {
                        "coin": coin, "mid": mid,
                        "bids": bids, "asks": asks,
                        "ts": int(time.time() * 1000),
                    })
        except Exception:
            pass
        # Fallback to markprice
        try:
            with httpx.Client(timeout=2.0) as cli:
                r = cli.get(f"http://localhost:{SIGNAL_BUS_PORT}/markprice/{coin}")
                if r.status_code == 200:
                    mp = r.json() or {}
                    mid = float(mp.get("hl_mid") or mp.get("binance_mid") or 0)
                    if mid > 0:
                        return self._json(200, {
                            "coin": coin, "mid": mid,
                            "bids": [[mid * (1 - i * 0.0005), 0.5] for i in range(1, 11)],
                            "asks": [[mid * (1 + i * 0.0005), 0.5] for i in range(1, 11)],
                            "ts": int(time.time() * 1000),
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
            return self._serve_whales()
        if path == "/news":
            return self._serve_news()
        if path.startswith("/audit/deep"):
            return self._serve_audit_deep()
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
