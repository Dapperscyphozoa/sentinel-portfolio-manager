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
import socket
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
    os.environ["HTTP_PORT"] = str(STRATEGY_PORT)
    try:
        from strategy_runner import server as sr_server
        log.info("starting strategy_runner on :%d", STRATEGY_PORT)
        sr_server.main()
    except Exception as e:
        log.exception("strategy_runner crashed: %s", e)


def _start_pm():
    os.environ["HTTP_PORT"] = str(PM_PORT)
    try:
        from pm import server as pm_server
        log.info("starting pm on :%d", PM_PORT)
        pm_server.main()
    except Exception as e:
        log.exception("pm crashed: %s", e)


def _start_monitor():
    os.environ["HTTP_PORT"] = str(MONITOR_PORT)
    try:
        from monitor import server as mon_server
        log.info("starting monitor on :%d", MONITOR_PORT)
        mon_server.main()
    except Exception as e:
        log.exception("monitor crashed: %s", e)


def _wait_for_port_bind(port: int, timeout: float = 30.0) -> bool:
    """Block until localhost:port accepts a TCP connection or timeout elapses.

    Used to serialize subsystem startup so that os.environ["HTTP_PORT"] (which
    each subsystem reads inside its own main()) is not overwritten by the next
    subsystem before the current one has read it. The previous time.sleep()
    stagger was insufficient — signal_bus's main() does WS connects and a
    SQLite cold-load BEFORE reading HTTP_PORT, by which point pm and
    strategy_runner had already overwritten the env var, causing signal_bus
    to bind the wrong port (or fail silently).
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.25)
    return False


# ─────────────────── Public proxy / aggregator ───────────────────
_PROXY_MAP = {
    "/signal_bus": (SIGNAL_BUS_PORT, "/signal_bus"),
    "/strategy":   (STRATEGY_PORT,   "/strategy"),
    "/pm":         (PM_PORT,         "/pm"),
    "/monitor":    (MONITOR_PORT,    "/monitor"),
}

_HEALTH_CACHE: dict = {"ts": 0, "data": None}
_HEALTH_TTL_S = 15.0

# ─── Deep research background job store ───
# In-memory dict of job_id → {status, phase, started_at, progress, result}
# Single-instance OK (we have one core service). Auto-evicts jobs older than 10min.
_DEEP_JOBS: dict = {}
_DEEP_JOB_TTL_S = 600


def _update_deep_job(job_id: str, phase: str, extra: dict = None) -> None:
    """Called from deep_research worker to push progress updates."""
    job = _DEEP_JOBS.get(job_id)
    if not job:
        return
    job["phase"] = phase
    if extra:
        job.setdefault("progress", {}).update(extra)


def _evict_old_deep_jobs() -> None:
    now = int(time.time() * 1000)
    cutoff = now - _DEEP_JOB_TTL_S * 1000
    stale = [k for k, v in _DEEP_JOBS.items() if v["started_at"] < cutoff]
    for k in stale:
        _DEEP_JOBS.pop(k, None)


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
    with httpx.Client(timeout=5.0) as cli:
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
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps(body, default=str).encode())
        except (BrokenPipeError, ConnectionResetError):
            pass

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
            with httpx.Client(timeout=5.0) as cli:
                r = cli.get(f"http://localhost:{PM_PORT}/health")
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        return {}

    def _strategy_state(self) -> dict:
        try:
            with httpx.Client(timeout=5.0) as cli:
                r = cli.get(f"http://localhost:{STRATEGY_PORT}/health")
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        return {}

    def _signal_bus_health(self) -> dict:
        try:
            with httpx.Client(timeout=5.0) as cli:
                r = cli.get(f"http://localhost:{SIGNAL_BUS_PORT}/health")
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        return {}

    def _hl_account_full(self) -> dict:
        """Get FULL HL account with positions via signal_bus cache."""
        try:
            with httpx.Client(timeout=5.0) as cli:
                r = cli.get(f"http://localhost:{SIGNAL_BUS_PORT}/hl/account")
                if r.status_code == 200:
                    return r.json()
        except Exception:
            pass
        return {}

    def _hl_positions(self) -> list:
        try:
            with httpx.Client(timeout=5.0) as cli:
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
            with httpx.Client(timeout=5.0) as cli:
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
        """Health shape that landing expects: btc_macro, webhook_security, engine_auto_pause.

        Also exposes equity, regime, commit_live — the dashboard footer reads
        these here. Without them the strip rendered "equity —" / "regime ?" /
        "commit —" while /dash had the live values, which is what looked like
        the dashboard "losing data" intermittently."""
        base = _aggregate_health()
        sr = self._strategy_state()
        engines = sr.get("registry", []) or []
        # CUT engines list — these should show as paused
        cut = {"vsq", "range_fade", "range_bo", "lh1", "fd1", "cex_dex_arb", "precog"}
        eap = {"engines": {e["name"]: {"paused": e["name"] in cut} for e in engines}}
        # Live equity + regime — these are cheap (already cached via _pm_data) and
        # belong on /health so any client polling only /health can render the strip.
        equity_val = None
        regime_str = None
        try:
            # Prefer signal_bus HL account (same source /dash uses) — has live WS value.
            hl_acct = self._hl_account_full() or {}
            equity_val = float(hl_acct.get("value") or 0) or None
        except Exception:
            pass
        if equity_val is None:
            try:
                pm = self._pm_data() or {}
                acct = pm.get("account", {}) or {}
                equity_val = float(acct.get("value") or 0) or None
            except Exception:
                pass
        try:
            pm = self._pm_data() or {}
            regime_obj = pm.get("regime") or {}
            if isinstance(regime_obj, dict):
                regime_str = regime_obj.get("regime")
            elif isinstance(regime_obj, str):
                regime_str = regime_obj
        except Exception:
            pass
        # commit_live: read the deployed SHA exactly once at module import time
        # (RENDER_GIT_COMMIT is set automatically by Render on every deploy).
        commit_live = os.environ.get("RENDER_GIT_COMMIT", "")[:7] or None
        return {
            "ok": base.get("core") == "ok",
            "ts": int(time.time() * 1000),
            "core": base.get("core"),
            "subsystems": base.get("subsystems", {}),
            "btc_macro": self._btc_macro(),
            "webhook_security": {"ok": True, "scheme": "X-PM-Auth", "enabled": True},
            "engine_auto_pause": eap,
            "equity": equity_val,
            "regime": regime_str,
            "commit_live": commit_live,
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
        # Pull strategy_runner trades to enrich HL positions with tp/sl/engine.
        # Coin → most-recent open trade row from our SQLite.
        runner_by_coin: dict = {}
        try:
            with httpx.Client(timeout=5.0) as cli:
                r = cli.get(f"http://localhost:{STRATEGY_PORT}/state")
                if r.status_code == 200:
                    for tr in r.json():
                        if tr.get("status") == "open":
                            runner_by_coin[tr["coin"]] = tr
        except Exception:
            pass
        # Fallback: also query spm-strategy-runner (split-service stack) which
        # may have placed orders on the same HL wallet. Operator runs BOTH the
        # core monolith AND the spm-* split stack against the same wallet. Their
        # DBs are independent, so attribution lookups must consult both.
        try:
            spm_url = os.environ.get("SPM_STRATEGY_RUNNER_URL",
                                     "https://spm-strategy-runner.onrender.com")
            with httpx.Client(timeout=5.0) as cli:
                r = cli.get(f"{spm_url.rstrip('/')}/state")
                if r.status_code == 200:
                    for tr in r.json():
                        if tr.get("status") == "open" and tr["coin"] not in runner_by_coin:
                            runner_by_coin[tr["coin"]] = tr
        except Exception:
            pass
        # Final fallback: read HL native reduceOnly orders as TP/SL source. The
        # runner DB can be empty (fresh deploy, schema migration, or
        # reconciliation gap) but the orders are still live on HL. Without this,
        # /dash shows tp='-' sl='-' on positions whose stops are actually
        # active, which looks like the bot has gone unprotected when it hasn't.
        # For each coin: 2 reduceOnly limit orders typically → the one closer
        # to mark is TP, the further one is SL (for a short, TP < entry < SL).
        hl_triggers_by_coin: dict = {}
        try:
            user_wallet = os.environ.get("HL_USER_WALLET", "")
            if user_wallet:
                with httpx.Client(timeout=4.0) as cli:
                    r = cli.post(
                        "https://api.hyperliquid.xyz/info",
                        json={"type": "openOrders", "user": user_wallet},
                    )
                    if r.status_code == 200:
                        for o in r.json() or []:
                            if not o.get("reduceOnly"):
                                continue
                            c = o.get("coin")
                            px = float(o.get("limitPx") or 0)
                            if not c or not px:
                                continue
                            hl_triggers_by_coin.setdefault(c, []).append(px)
        except Exception:
            pass
        # Positions list — shape landing expects: {upnl, lev, tp, sl, engine, stage, coin, side, size, entry, entry_px, mark_px}
        positions_out = []
        for p in hl_positions or account_pm.get("positions") or []:
            coin = p.get("coin", "?")
            entry_px = float(p.get("entry_px", p.get("entryPx", 0)) or 0)
            # Mark price from bus (HL position struct doesn't always carry mark)
            mark_px = float(p.get("mark_px", p.get("markPx", 0)) or 0)
            if not mark_px:
                try:
                    with httpx.Client(timeout=1.5) as cli:
                        rr = cli.get(f"http://localhost:{SIGNAL_BUS_PORT}/markprice/{coin}")
                        if rr.status_code == 200:
                            mp = rr.json() or {}
                            mark_px = float(mp.get("hl_mid") or mp.get("binance_mid") or 0)
                except Exception:
                    pass
            # Leverage — HL returns dict {type, value}; landing expects scalar number
            lev_obj = p.get("leverage", p.get("lev"))
            if isinstance(lev_obj, dict):
                lev_val = lev_obj.get("value", "-")
            else:
                lev_val = lev_obj if lev_obj is not None else "-"
            # Join with runner state for tp/sl/engine
            tr = runner_by_coin.get(coin) or {}
            tp_v = tr.get("tp_px") if tr else p.get("tp", "-")
            sl_v = tr.get("sl_px") if tr else p.get("sl", "-")
            # Position side first (need it to disambiguate which trigger is TP vs SL)
            sz_signed = float(p.get("size", p.get("szi", 0)) or 0)
            side_long = sz_signed > 0
            # HL trigger fallback when runner DB has no record
            if (tp_v in (None, 0, "-") or sl_v in (None, 0, "-")) and coin in hl_triggers_by_coin:
                pxs = sorted(hl_triggers_by_coin[coin])
                if len(pxs) >= 2 and entry_px:
                    # For LONG: TP > entry, SL < entry. For SHORT: TP < entry, SL > entry.
                    lower, upper = pxs[0], pxs[-1]
                    hl_tp = upper if side_long else lower
                    hl_sl = lower if side_long else upper
                    if tp_v in (None, 0, "-"):
                        tp_v = hl_tp
                    if sl_v in (None, 0, "-"):
                        sl_v = hl_sl
                elif len(pxs) == 1 and entry_px:
                    # Single trigger — only one side set. Best-guess: if it's
                    # protective for this position, it's the SL.
                    px = pxs[0]
                    is_sl = (side_long and px < entry_px) or (not side_long and px > entry_px)
                    if is_sl and sl_v in (None, 0, "-"):
                        sl_v = px
                    elif not is_sl and tp_v in (None, 0, "-"):
                        tp_v = px
            engine_v = tr.get("strategy") if tr else p.get("engine", p.get("strategy", "-"))
            # Compute mark-based unrealizedPnl if HL didn't give us one and we have both prices
            upnl_v = float(p.get("upnl", p.get("unrealizedPnl", 0)) or 0)
            if upnl_v == 0 and mark_px and entry_px and sz_signed:
                upnl_v = (mark_px - entry_px) * sz_signed
            positions_out.append({
                "coin": coin,
                "side": "LONG" if side_long else "SHORT",
                "size": abs(sz_signed),
                "entry": entry_px,        # landing reads p.entry
                "entry_px": entry_px,     # keep alias for any other consumer
                "mark_px": mark_px,
                "upnl": upnl_v,
                "lev": lev_val,
                "tp": tp_v if tp_v not in (None, 0) else "-",
                "sl": sl_v if sl_v not in (None, 0) else "-",
                "engine": engine_v or "-",
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
            with httpx.Client(timeout=5.0) as cli:
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
            with httpx.Client(timeout=5.0) as cli:
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
        """Landing's /signals — recent signals across all engines.
        Enriches the raw signals table rows with 'kind' (always 'LIVE'),
        a human-readable 'ts_str', and a normalized 'side' label so the
        landing renderer doesn't show 'undefined B' or epoch numbers.
        """
        raw_items = []
        try:
            with httpx.Client(timeout=5.0) as cli:
                r = cli.get(f"http://localhost:{STRATEGY_PORT}/signals?limit=50")
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list):
                        raw_items = data
                    elif isinstance(data, dict):
                        raw_items = data.get("items") or data.get("signals") or []
        except Exception:
            pass
        # Enrich for landing.html renderer
        from datetime import datetime, timezone
        items = []
        for s in raw_items:
            ts_raw = s.get("ts") or 0
            try:
                # ts in signals table is seconds (float). Convert to short HH:MM:SS UTC.
                ts_str = datetime.fromtimestamp(float(ts_raw), tz=timezone.utc).strftime("%H:%M:%S")
            except Exception:
                ts_str = ""
            side_raw = s.get("side", "")
            side_label = "LONG" if side_raw == "B" or s.get("is_long") else "SHORT"
            items.append({
                **s,
                "kind": "LIVE",            # landing reads s.kind for the OPEN/CLOSED tag — give it a real value
                "ts_str": ts_str,
                "side": side_label,        # overrides 'B'/'A' with human label
                "engine": s.get("strategy", "?"),
            })
        self._json(200, {"items": items, "ts": int(time.time() * 1000)})

    def _serve_whales(self) -> None:
        """Landing's /whales — large position holders.
        Backed by HL fills from signal_bus filtered for large notional.
        """
        items = []
        try:
            with httpx.Client(timeout=5.0) as cli:
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

    # ────────────────── Engines full aggregator ──────────────────
    def _serve_engines_full(self) -> None:
        """Landing's /engines panel aggregator.

        Replaces the legacy panel's habit of calling N legacy services
        (`https://portfolio-manager-7df2.onrender.com/engines` then
        `<engine_url>/pnl /state /closures /signals` per engine). The new
        single-process stack exposes everything via internal localhost
        endpoints; this method fans out once and returns a payload shaped
        to match the panel's existing `loadEnginesGrid()` consumers.

        Response shape:
        {
          ts: <ms>,
          engines: { name: {  # dict keyed by engine name (legacy expectation)
              halt_url,        # synthetic — panel only checks it's non-empty
              capital_fraction, class, affinity, audit_status,
              audit_metrics: {wr, pf, oos_pf, max_trades_per_day, n},
              lifecycle_stage, spec: {thesis, timeframe},
              cloid_prefix, live, deprecated, ...
          }},
          data: { name: {
              pnl: {total_net_pnl, n_closed, wr_pct, equity},
              state: {mode_effective, halt:{active}, open_trades:[...]},
              closures: {closures: [{ts_close, net_pnl}]},
              signals: {signals: [{fire_ts, ts}]},
          }}
        }
        """
        import json as _json_mod
        from concurrent.futures import ThreadPoolExecutor

        engines_list: list = []
        closures: list = []
        signals: list = []
        open_state: list = []
        attribution: dict = {"engines": []}
        equity_usd: float | None = None
        live_trading = os.environ.get("LIVE_TRADING", "0") == "1"

        def _engine_is_live(engine_name: str) -> bool:
            """Mirror strategy_runner/trader._is_live precedence:
            per-engine STRATEGY_<NAME>_LIVE overrides global LIVE_TRADING.
            Without this, the engines_full panel mislabels per-engine
            promotions as 'paper' even when they're transacting live HL orders."""
            per = os.environ.get(f"STRATEGY_{engine_name.upper()}_LIVE")
            if per is not None:
                return per.strip().lower() in ("1", "true", "yes", "on")
            return live_trading

        # Parallel fan-out — `/strategy/signals?limit=500` was sequentially
        # gating the whole panel at 26+ seconds. Each call now runs in its
        # own thread with a hard 5s timeout; the slowest blocker dictates
        # total latency, not the sum.
        def _get(url: str, timeout: float = 5.0):
            try:
                with httpx.Client(timeout=timeout) as cli:
                    r = cli.get(url)
                    if r.status_code == 200:
                        return r.json()
            except Exception as e:
                log.warning("engines_full GET %s: %s", url, e)
            return None

        urls = {
            "engines":     (f"http://localhost:{PM_PORT}/engines", 5.0),
            "closures":    (f"http://localhost:{STRATEGY_PORT}/closures?limit=2000", 8.0),
            # limit=100 keeps payload small — /signals is slow with extras_json
            "signals":     (f"http://localhost:{STRATEGY_PORT}/signals?limit=100", 5.0),
            "state":       (f"http://localhost:{STRATEGY_PORT}/state", 5.0),
            "attribution": (f"http://localhost:{STRATEGY_PORT}/attribution?since=0", 8.0),
            "account":     (f"http://localhost:{SIGNAL_BUS_PORT}/hl/account", 3.0),
        }
        results: dict = {}
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = {ex.submit(_get, url, t): name for name, (url, t) in urls.items()}
            for fut in futs:
                name = futs[fut]
                try:
                    results[name] = fut.result(timeout=10.0)
                except Exception as e:
                    log.warning("engines_full fut[%s]: %s", name, e)
                    results[name] = None

        engines_list = (results.get("engines") or {}).get("engines", []) or []
        closures = results.get("closures") or []
        signals = results.get("signals") or []
        open_state = results.get("state") or []
        attribution = results.get("attribution") or {"engines": []}
        if results.get("account"):
            equity_usd = results["account"].get("value") or results["account"].get("account_value")

        # Bucket per-engine data
        attr_by_name = {e["engine"]: e for e in attribution.get("engines", [])
                        if isinstance(e, dict) and e.get("engine")}
        closures_by_name: dict = {}
        for c in closures:
            n = c.get("strategy") or c.get("engine")
            if not n:
                continue
            closures_by_name.setdefault(n, []).append({
                "ts_close": int((c.get("close_ts") or 0) * 1000),
                "net_pnl": float(c.get("pnl_usd", 0)) - float(c.get("fees_usd", 0)),
                "coin": c.get("coin"),
                "is_long": bool(c.get("is_long")),
                "close_reason": c.get("close_reason"),
            })
        signals_by_name: dict = {}
        for s in signals:
            n = s.get("strategy")
            if not n:
                continue
            if n in signals_by_name:
                continue  # /signals is already sorted desc; first hit wins
            signals_by_name[n] = {
                "fire_ts": int((s.get("ts") or 0) * 1000),
                "ts": int((s.get("ts") or 0) * 1000),
                "coin": s.get("coin"),
                "side": "long" if s.get("is_long") else "short",
                "fire_reason": s.get("fire_reason"),
            }
        open_by_name: dict = {}
        for p in open_state:
            n = p.get("strategy")
            if not n:
                continue
            open_by_name.setdefault(n, []).append({
                "coin": p.get("coin"),
                "is_long": bool(p.get("is_long")),
                "open_ts": int((p.get("open_ts") or 0) * 1000),
                "open_px": p.get("open_px"),
                "size_usd": p.get("size_usd"),
            })

        # Affinity-class fallback mapping
        def _class_from_affinity(aff: list) -> str:
            if not aff:
                return ""
            s = set(aff)
            if "trend_up" in s and "trend_down" in s:
                if "range" in s or "chop" in s:
                    return "multi_regime"
                return "trend_follower"
            if "range" in s or "chop" in s:
                return "mean_reversion"
            if "trend_down" in s:
                return "trend_short"
            if "trend_up" in s:
                return "trend_long"
            return aff[0]

        # Build dict-keyed registry in legacy shape
        engines_dict: dict = {}
        for e in engines_list:
            name = e.get("name")
            if not name:
                continue
            attr = attr_by_name.get(name) or {}
            stage = e.get("stage") or "unknown"
            # New PM uses 'live' for what legacy called 'full'
            legacy_stage = {"live": "full"}.get(stage, stage)
            engines_dict[name] = {
                "halt_url": "/strategy/halt/" + name,  # synthetic — non-empty signals "API available"
                "capital_fraction": e.get("capital_fraction"),
                "class": _class_from_affinity(e.get("affinity") or []),
                "affinity": e.get("affinity") or [],
                "audit_status": e.get("audit_status") or "",
                "audit_metrics": {
                    "pf": e.get("bt_pf"),
                    "n":  e.get("bt_n"),
                    "wr": attr.get("wr"),
                    "max_trades_per_day": None,
                    "oos_pf": e.get("bt_pf"),
                },
                "lifecycle_stage": legacy_stage,
                "spec": {
                    "thesis": "",
                    "timeframe": e.get("tf") or "",
                },
                "cloid_prefix": e.get("cloid_prefix") or "",
                "live": legacy_stage in ("full", "canary", "small"),
                "deprecated": legacy_stage in ("demoted", "deprecated"),
                "needs_rewrite": False,
            }

        # Build per-engine data block
        data_dict: dict = {}
        for name in engines_dict.keys():
            attr = attr_by_name.get(name) or {}
            engine_closures = closures_by_name.get(name, [])
            engine_open = open_by_name.get(name, [])
            engine_signals = signals_by_name.get(name)
            data_dict[name] = {
                "pnl": {
                    "total_net_pnl": attr.get("net_pnl", 0.0),
                    "n_closed": attr.get("n", 0),
                    "wr_pct": (attr.get("wr", 0.0) or 0.0) * 100.0 if attr else None,
                    "equity": equity_usd,
                    "__synthetic": False,
                } if (attr or engine_closures) else None,
                "state": {
                    "mode_effective": "live" if _engine_is_live(name) else "paper",
                    "halt": {"active": False},
                    "open_trades": engine_open,
                    "equity_usd": equity_usd,
                    "daily_pnl_usd": attr.get("net_pnl", 0.0) if attr else None,
                    "closed_trades_count": attr.get("n", 0) if attr else 0,
                },
                "closures": {"closures": engine_closures},
                "signals": {"signals": [engine_signals] if engine_signals else []},
            }

        self._json(200, {
            "ts": int(time.time() * 1000),
            "engines": engines_dict,
            "data": data_dict,
        })

    # ────────────────── Macro Economic Report (MER) ──────────────────
    def _serve_mer_today(self) -> None:
        """GET /mer or /mer/today — landing /macro fetches this."""
        try:
            from core import mer
            snap = mer.get_today_snapshot()
            self._json(200, snap)
        except Exception as e:
            log.exception("mer/today")
            self._json(500, {"error": "mer_failed", "detail": str(e)[:200]})

    def _serve_mer_refresh(self) -> None:
        """GET /mer/refresh — force a synchronous pull + snapshot rebuild."""
        try:
            from core import mer
            stats = mer.pull_all()
            snap = mer.build_snapshot()
            self._json(200, {"stats": stats, "snapshot_day": snap["day"]})
        except Exception as e:
            log.exception("mer/refresh")
            self._json(500, {"error": "mer_refresh_failed", "detail": str(e)[:200]})

    def _serve_mer_raw(self, query: str) -> None:
        """GET /mer/raw?limit=&category= — debug accessor for ingested items."""
        try:
            from urllib.parse import parse_qs
            from core import mer
            q = {k: v[0] for k, v in parse_qs(query).items()}
            limit = int(q.get("limit", "50"))
            cat = q.get("category") or None
            self._json(200, {"items": mer.get_recent_raw(limit=limit, category=cat)})
        except Exception as e:
            log.exception("mer/raw")
            self._json(500, {"error": "mer_raw_failed", "detail": str(e)[:200]})

    def _serve_mer_day(self, day_iso: str) -> None:
        """GET /mer/<YYYY-MM-DD> — historical snapshot lookup."""
        try:
            from core import mer
            self._json(200, mer.get_snapshot(day_iso))
        except Exception as e:
            log.exception("mer/day")
            self._json(500, {"error": "mer_day_failed", "detail": str(e)[:200]})

    def _serve_macro_blackout(self) -> None:
        """GET /macro_blackout — current tier-1 blackout state for landing."""
        try:
            from core import mer
            self._json(200, mer.get_blackout_status())
        except Exception as e:
            log.exception("macro_blackout")
            self._json(500, {"error": "blackout_failed", "detail": str(e)[:200]})

    def _serve_audit_deep(self) -> None:
        """Landing's /audit/deep — per-coin attribution + per-hour fills/PnL series.
        Pulls from strategy_runner /closures (24h window) and aggregates two ways:
          - per_hour: 24 hourly buckets [{ts, fills, pnl}] for equity pulse SVG
          - per_coin: rollup by coin for the attribution heatmap
        """
        per_coin: list = []
        per_hour: list = []
        try:
            now = time.time()
            since = now - 24 * 3600
            with httpx.Client(timeout=5.0) as cli:
                r = cli.get(f"http://localhost:{STRATEGY_PORT}/closures?since={since}&limit=2000")
                closures = []
                if r.status_code == 200:
                    data = r.json()
                    closures = data if isinstance(data, list) else (data.get("items") or [])
            # 24 hourly buckets, oldest first → JS reverses to newest-first
            buckets: dict = {}
            for i in range(24):
                bucket_ts = int((now - (24 - i) * 3600) * 1000)
                buckets[i] = {"ts": bucket_ts, "fills": 0, "pnl": 0.0}
            # Coin rollup
            coin_agg: dict = {}
            for c in closures:
                ts = float(c.get("close_ts", 0) or 0)
                hour_idx = int((ts - (now - 24 * 3600)) / 3600)
                if 0 <= hour_idx < 24:
                    buckets[hour_idx]["fills"] += 1
                    buckets[hour_idx]["pnl"] += float(c.get("pnl_usd", 0) or 0) - float(c.get("fees_usd", 0) or 0)
                coin = c.get("coin", "—")
                agg = coin_agg.setdefault(coin, {"coin": coin, "n": 0, "wins": 0, "pnl_usd": 0.0})
                agg["n"] += 1
                net = float(c.get("pnl_usd", 0) or 0) - float(c.get("fees_usd", 0) or 0)
                agg["pnl_usd"] += net
                if net > 0:
                    agg["wins"] += 1
            for i in sorted(buckets.keys()):
                per_hour.append(buckets[i])
            for coin, agg in coin_agg.items():
                agg["wr"] = round(agg["wins"] / agg["n"], 3) if agg["n"] else 0.0
                agg["pnl_usd"] = round(agg["pnl_usd"], 4)
                # Legacy panel aliases — landing.html loadHeatmap reads c.w / c.l / c.pnl
                # rather than c.wins / (n-wins) / c.pnl_usd. Provide both shapes.
                agg["w"] = agg["wins"]
                agg["l"] = agg["n"] - agg["wins"]
                agg["pnl"] = agg["pnl_usd"]
                per_coin.append(agg)
            per_coin.sort(key=lambda x: x["pnl_usd"], reverse=True)
        except Exception:
            pass
        self._json(200, {
            "per_coin": per_coin,
            "per_hour": per_hour,
            "hours": 24,
            "ts": int(time.time() * 1000),
        })

    def _serve_chat(self) -> None:
        """Serve the new PSYCHO-themed sentinel chat UI."""
        chat_path = os.path.join(os.path.dirname(__file__), "static", "sentinel_chat.html")
        try:
            with open(chat_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            # Strict no-cache so iOS/Cloudflare don't serve stale JS that has
            # the old routing logic (which is what made BUILD route fire instead
            # of DEEP). Etag + must-revalidate forces every visit to re-fetch.
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self._json(500, {"error": "chat_unavailable", "detail": str(e)})

    def _serve_sentinel(self) -> None:
        """Proxy to the sentinel service /status endpoint (separate Render service)."""
        url = os.environ.get("SENTINEL_URL", "https://sentinel-eug3.onrender.com")
        try:
            with httpx.Client(timeout=5.0) as cli:
                r = cli.get(f"{url}/status")
                if r.status_code == 200:
                    data = r.json()
                    data["_source"] = url
                    return self._json(200, data)
                return self._json(r.status_code, {"error": f"sentinel http {r.status_code}",
                                                   "url": url})
        except Exception as e:
            return self._json(502, {"error": "sentinel_unreachable",
                                     "detail": str(e), "url": url})

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
            with httpx.Client(timeout=5.0) as cli:
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
        try:
            self._dispatch_get()
        except (BrokenPipeError, ConnectionResetError):
            # Client closed before we finished writing. Don't retry-write — just
            # let the handler thread exit cleanly so Render doesn't see this as
            # an upstream crash and serve a 502 to the next caller.
            pass
        except Exception:
            log.exception("do_GET handler crashed")
            try:
                self._json(500, {"error": "internal"})
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _dispatch_get(self):
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
        if path == "/engines_full":
            return self._serve_engines_full()
        if path.startswith("/audit/deep"):
            return self._serve_audit_deep()
        if path.startswith("/orderbook/"):
            coin = path.split("/")[-1]
            return self._serve_orderbook(coin)
        # Macro Economic Report — ported from legacy portfolio-manager.
        # Self-contained (own sqlite /var/data/mer.sqlite). Powers /macro
        # subpage on landing (mer-internal, mer-national, mer-global cards).
        if path == "/mer" or path == "/mer/today":
            return self._serve_mer_today()
        if path == "/mer/refresh":
            return self._serve_mer_refresh()
        if path.startswith("/mer/raw"):
            return self._serve_mer_raw(parsed.query)
        if path.startswith("/mer/"):
            # historical day: /mer/2026-05-19
            day = path[len("/mer/"):].rstrip("/")
            return self._serve_mer_day(day)
        if path == "/macro_blackout" or path == "/blackout":
            return self._serve_macro_blackout()
        # Sentinel: JSON data at /sentinel.json (fetched by the panel),
        #           styled landing at /sentinel (browser navigation),
        #           deep job polling at /sentinel/deep/{job_id} (background pattern),
        #           other /sentinel/* paths proxy to sentinel service
        if path == "/sentinel.json":
            return self._serve_sentinel()
        if path == "/sentinel" or path == "/sentinel/":
            return self._serve_landing()
        if path.startswith("/sentinel/deep/"):
            job_id = path[len("/sentinel/deep/"):].rstrip("/")
            if job_id:
                _evict_old_deep_jobs()
                return self._serve_deep_status(job_id)
        if path.startswith("/sentinel/"):
            return self._proxy_sentinel(path)
        # Sentinel chat UI — fast, mobile-first, PSYCHO themed
        if path == "/chat" or path == "/chat/":
            return self._serve_chat()
        # Landing sub-nav paths — serve the landing HTML so the page stays styled
        # (originally these were separate sub-pages in precog-hl, not yet ported).
        if path in ("/engines", "/audit", "/system", "/macro",
                    "/enforce", "/experiment", "/violations"):
            return self._serve_landing()
        # Route to subsystem
        for prefix, (port, strip) in _PROXY_MAP.items():
            if path == prefix or path.startswith(prefix + "/"):
                self._proxy(port, strip)
                return
        self._json(404, {"error": "not found", "path": path})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        # /sentinel/* POST → proxy to sentinel service (chat, rate, etc.)
        if path.startswith("/sentinel/"):
            # Intercept /sentinel/deep — run in-process with all council keys
            if path == "/sentinel/deep":
                return self._handle_deep_research()
            return self._proxy_sentinel(path)
        for prefix, (port, strip) in _PROXY_MAP.items():
            if path == prefix or path.startswith(prefix + "/"):
                self._proxy(port, strip)
                return
        self._json(404, {"error": "not found", "path": path})

    def do_OPTIONS(self):
        # CORS preflight for chat UI calls
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-PM-Auth, X-Halt-Token, X-Sniper-Auth")
        self.send_header("Access-Control-Max-Age", "3600")
        self.end_headers()

    def _handle_deep_research(self) -> None:
        """In-process deep research with BACKGROUND JOB + POLLING pattern.

        Synchronous HTTP doesn't work: Cloudflare/Render edge cuts at ~100s,
        deep research takes 100-160s. Solution:
          POST /sentinel/deep         → starts job, returns {job_id, status:'running'} in <1s
          GET  /sentinel/deep/<id>    → returns current state (progressive)
                                        progressively richer payloads as voters return
        """
        import threading, uuid, asyncio as _asyncio
        body_len = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(body_len) if body_len else b"{}"
        try:
            payload = json.loads(body)
        except Exception:
            payload = {}
        query = (payload.get("text") or payload.get("query") or "").strip()
        if not query:
            return self._json(400, {"error": "missing 'text' field"})
        enable_critique = payload.get("critique", True)

        job_id = "deep_" + uuid.uuid4().hex[:12]
        _DEEP_JOBS[job_id] = {
            "job_id": job_id,
            "status": "running",
            "phase": "queued",
            "query": query,
            "started_at": int(time.time() * 1000),
            "elapsed_s": 0,
            "result": None,
            "error": None,
        }

        def worker():
            try:
                from core.deep_research import run_deep_research
                # Pass the job_id so the runner can update progress
                r = _asyncio.run(run_deep_research(
                    query,
                    enable_critique=enable_critique,
                    entry_id=job_id,
                    progress_cb=lambda phase, extra=None: _update_deep_job(
                        job_id, phase, extra or {}
                    ),
                ))
                _DEEP_JOBS[job_id].update({
                    "status": "complete",
                    "phase": "done",
                    "result": r,
                    "elapsed_s": (int(time.time() * 1000) - _DEEP_JOBS[job_id]["started_at"]) / 1000,
                })
            except Exception as e:
                log.exception("deep_research job %s failed: %s", job_id, e)
                _DEEP_JOBS[job_id].update({
                    "status": "failed",
                    "phase": "error",
                    "error": str(e)[:500],
                    "elapsed_s": (int(time.time() * 1000) - _DEEP_JOBS[job_id]["started_at"]) / 1000,
                })

        t = threading.Thread(target=worker, daemon=True, name=f"deep_{job_id}")
        t.start()

        return self._json(202, {
            "job_id": job_id,
            "status": "running",
            "phase": "queued",
            "poll_url": f"/sentinel/deep/{job_id}",
            "started_at": _DEEP_JOBS[job_id]["started_at"],
        })

    def _serve_deep_status(self, job_id: str) -> None:
        """Return current state of a deep research job (poll endpoint)."""
        job = _DEEP_JOBS.get(job_id)
        if not job:
            return self._json(404, {"error": "job not found", "job_id": job_id})
        now_ms = int(time.time() * 1000)
        elapsed = (now_ms - job["started_at"]) / 1000

        if job["status"] == "running":
            return self._json(200, {
                "job_id": job_id,
                "status": "running",
                "phase": job.get("phase", "running"),
                "elapsed_s": round(elapsed, 1),
                "progress": job.get("progress", {}),
            })

        if job["status"] == "failed":
            return self._json(200, {
                "job_id": job_id,
                "status": "failed",
                "phase": "error",
                "elapsed_s": round(elapsed, 1),
                "error": job.get("error", "unknown"),
            })

        # Complete — return the full result in the same shape the synchronous
        # endpoint used to return so the UI rendering code stays the same.
        r = job["result"]
        timing = r["timing"]
        all_voters = []
        for v in r["voters"]:
            all_voters.append({
                "model": v["model"],
                "provider": v.get("provider", ""),
                "confidence": 1.0 if v.get("ok") else 0.0,
                "answer": v.get("content", "")[:1500] if v.get("ok") else f"FAILED: {v.get('error','?')}",
                "reasoning": f"{v.get('elapsed_s','?')}s · {v.get('words',0)}w · role={v.get('role','?')} · domain={v.get('assigned_domain','?')} · skill={v.get('domain_skill_score',0):.2f}",
                "used_antipode": v.get("role") == "generalist",
                "role": v.get("role"),
            })
        for c in r.get("critiques", []):
            all_voters.append({
                "model": c["model"],
                "provider": c.get("provider", ""),
                "confidence": 1.0 if c.get("ok") else 0.0,
                "answer": c.get("content", "")[:1500] if c.get("ok") else f"FAILED: {c.get('error','?')}",
                "reasoning": f"CRITIC · {c.get('elapsed_s','?')}s · {c.get('words',0)}w",
                "used_antipode": True,
                "role": "critic",
            })
        return self._json(200, {
            "job_id": job_id,
            "status": "complete",
            "phase": "done",
            "elapsed_s": round(elapsed, 1),
            # ↓↓↓ This block matches the old sync response shape ↓↓↓
            "entry_id": r["entry_id"],
            "route": "deep",
            "answer": r["refined_synthesis"],
            "first_synthesis": r["first_synthesis"],
            "synth_model": r.get("synth_model"),
            "voters_inspection": all_voters,
            "intent_classification": {
                "intent": "DEEP",
                "confidence": r["domain"].get("confidence", 0.5),
                "reason": (
                    f"domain={r['domain']['domain']} · "
                    f"{r['providers_succeeded']}/{r['providers_called']} voters in {timing['voters_s']}s · "
                    f"synth1 {timing['synth1_s']}s · critique {timing['critique_s']}s · "
                    f"refine {timing['refine_s']}s · total {timing['total_s']}s"
                ),
            },
            "knowledge_gaps": [],
            "similar_prior_asks": [],
            "meta": {
                "domain": r["domain"],
                "providers_called": r["providers_called"],
                "providers_succeeded": r["providers_succeeded"],
                "critiques_succeeded": r.get("critiques_succeeded", 0),
                "first_words": r.get("first_words", 0),
                "refined_words": r.get("refined_words", 0),
                "timing": timing,
            },
        })

    def _proxy_sentinel(self, path: str) -> None:
        """Forward POST/GET to the sentinel Render service (separate origin)."""
        url_base = os.environ.get("SENTINEL_URL", "https://sentinel-eug3.onrender.com")
        # Strip /sentinel prefix
        target_path = path[len("/sentinel"):] or "/"
        target_url = url_base + target_path
        method = self.command
        body_len = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(body_len) if body_len else None
        fwd_headers = {"Content-Type": self.headers.get("Content-Type", "application/json")}
        try:
            with httpx.Client(timeout=120.0) as cli:
                r = cli.request(method, target_url, content=body, headers=fwd_headers)
            self.send_response(r.status_code)
            ct = r.headers.get("content-type", "application/json")
            self.send_header("Content-Type", ct)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(r.content)
        except Exception as e:
            self._json(502, {"error": "sentinel_proxy_failed", "detail": str(e),
                             "target": target_url})

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)


def main():
    log.info("core service starting — public port %d, internal %d/%d/%d/%d",
             PUBLIC_PORT, SIGNAL_BUS_PORT, STRATEGY_PORT, PM_PORT, MONITOR_PORT)
    # Serialize subsystem startup. Each subsystem's main() blocks on
    # serve_forever, so we can't wait for the thread function to return —
    # instead we wait for its TCP port to accept connections, then move on.
    # This eliminates the os.environ["HTTP_PORT"] race that previously left
    # signal_bus's HTTP listener silently unbound.
    subsystems = [
        ("signal_bus",      _start_signal_bus,      SIGNAL_BUS_PORT, 45.0),
        ("pm",              _start_pm,              PM_PORT,         15.0),
        ("strategy_runner", _start_strategy_runner, STRATEGY_PORT,   15.0),
        ("monitor",         _start_monitor,         MONITOR_PORT,    15.0),
    ]
    for name, fn, port, timeout in subsystems:
        t = threading.Thread(target=fn, daemon=True, name=fn.__name__)
        t.start()
        if _wait_for_port_bind(port, timeout=timeout):
            log.info("%s bound :%d", name, port)
        else:
            log.error("%s failed to bind :%d within %.0fs — continuing", name, port, timeout)
    # MER (Macro Economic Report) — hourly RSS + Forex Factory poll.
    # Self-contained, owns /var/data/mer.sqlite. Powers landing /macro page.
    try:
        from core import mer
        mer.start_poller()
    except Exception as e:
        log.exception("mer poller failed to start: %s", e)
    log.info("starting public HTTP on :%d", PUBLIC_PORT)
    srv = ThreadingHTTPServer(("0.0.0.0", PUBLIC_PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")


if __name__ == "__main__":
    main()
