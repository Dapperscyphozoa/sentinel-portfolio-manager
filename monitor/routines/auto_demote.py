"""auto_demote — rolling-window engine PF monitor.

Runs every 1h. For each LIVE engine with n>=30 closed live trades:
  rolling_PF = sum(wins last 30) / sum(losses last 30)
  threshold  = 0.7 × engine.bt_pf
  if rolling_PF < threshold: demote (LIVE=0 via Render env, halt in-memory)

Council finding (2026-05-17): without this, a decaying engine trades forever.
Operator has no time to babysit; auto-demote enforces the invariant the
honest-backtest audit established.

Side effects:
- Sets STRATEGY_<NAME>_LIVE=0 env var on the core Render service (persists
  across redeploys; needs RENDER_API_TOKEN + CORE_SERVICE_ID env vars)
- POSTs /strategy/halt/<name> to in-memory halt (so the next scan respects
  it without waiting for redeploy)
- Writes a demotion event to extras_json of last closure for audit trail

Severity gating: CLEAN if no demotions; HIGH if 1+ demotions in this cycle.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from collections import defaultdict

import httpx


log = logging.getLogger("routine.auto_demote")

ROLLING_N = 30
PF_THRESHOLD_FACTOR = 0.7  # demote if rolling_PF < 0.7 × bt_pf
MIN_BT_PF = 1.0  # only check engines that claimed positive edge


def _compute_rolling_pf(conn: sqlite3.Connection, strategy: str, n: int) -> tuple[float, int]:
    """Return (rolling_pf, n_trades) for the last n CLEAN closures of this
    strategy. Excludes operator-driven and bug-recovery closures
    (force_close*, backfill, reconciled_off_book, force_closed_unverified)
    — these don't reflect strategy edge.

    Counts only LIVE-closed trades (extras_json says live=true)."""
    from common.closures import is_clean_closure
    rows = conn.execute(
        "SELECT pnl_usd, extras_json, close_reason FROM closures WHERE strategy=? "
        "ORDER BY close_ts DESC LIMIT ?",
        (strategy, n * 3),  # over-fetch — most recent N CLEAN may be deeper than N raw
    ).fetchall()
    live_rows = []
    for r in rows:
        if not is_clean_closure(r["close_reason"]):
            continue
        try:
            e = json.loads(r["extras_json"] or "{}")
            if isinstance(e, dict) and e.get("live") is True:
                live_rows.append(r)
        except Exception:
            pass
        if len(live_rows) >= n:
            break
    if len(live_rows) < n:
        return (0.0, len(live_rows))
    wins = sum(r["pnl_usd"] for r in live_rows if r["pnl_usd"] > 0)
    losses = sum(-r["pnl_usd"] for r in live_rows if r["pnl_usd"] < 0)
    pf = wins / losses if losses > 0 else float("inf")
    return (pf, len(live_rows))


def _render_set_env(key: str, value: str) -> bool:
    token = os.environ.get("RENDER_API_TOKEN", "")
    svc = os.environ.get("CORE_SERVICE_ID", "")
    if not token or not svc:
        log.warning("RENDER_API_TOKEN or CORE_SERVICE_ID missing — cannot persist demote")
        return False
    try:
        r = httpx.put(
            f"https://api.render.com/v1/services/{svc}/env-vars/{key}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"value": value}, timeout=20,
        )
        return r.status_code == 200
    except Exception:
        log.exception("render set env failed")
        return False


def _runtime_halt(strategy: str) -> bool:
    runner_url = os.environ.get("STRATEGY_RUNNER_URL") or \
                 f"http://localhost:{os.environ.get('STRATEGY_PORT','10002')}"
    halt_token = os.environ.get("HALT_TOKEN", "")
    if not halt_token:
        return False
    try:
        r = httpx.post(
            f"{runner_url}/halt/{strategy}",
            headers={"X-Halt-Token": halt_token},
            json={"reason": "auto_demote_rolling_pf", "actor": "auto_demote"},
            timeout=15,
        )
        return r.status_code == 200
    except Exception:
        log.exception("runtime halt failed")
        return False


def run(conn: sqlite3.Connection) -> dict:
    """Inspect all engines with bt_pf ≥ MIN_BT_PF and rolling-30 PF.
    Demote any whose rolling PF < 0.7 × bt_pf."""
    from pm.pretrade import ENGINE_REGISTRY

    now = time.time()
    checked = {}
    demoted = []
    insufficient_n = []

    for name, cfg in ENGINE_REGISTRY.items():
        bt_pf = cfg.get("bt_pf", 0.0)
        if bt_pf < MIN_BT_PF:
            continue  # engines with no claimed edge already halted
        pf, n = _compute_rolling_pf(conn, name, ROLLING_N)
        threshold = PF_THRESHOLD_FACTOR * bt_pf
        if n < ROLLING_N:
            insufficient_n.append({"engine": name, "n": n, "bt_pf": bt_pf})
            checked[name] = {"n": n, "rolling_pf": None, "bt_pf": bt_pf,
                             "threshold": threshold, "verdict": "insufficient_n"}
            continue
        verdict = "demote" if pf < threshold else "keep"
        checked[name] = {"n": n, "rolling_pf": round(pf, 3),
                         "bt_pf": bt_pf, "threshold": round(threshold, 3),
                         "verdict": verdict}
        if verdict == "demote":
            env_key = f"STRATEGY_{name.upper()}_LIVE"
            env_ok = _render_set_env(env_key, "0")
            runtime_ok = _runtime_halt(name)
            log.error("AUTO-DEMOTE %s: rolling_PF=%.2f < threshold=%.2f "
                      "(bt_pf=%.2f n=%d) render_env=%s runtime_halt=%s",
                      name, pf, threshold, bt_pf, n, env_ok, runtime_ok)
            demoted.append({
                "engine": name, "rolling_pf": round(pf, 3),
                "threshold": round(threshold, 3), "bt_pf": bt_pf,
                "n": n, "env_persisted": env_ok, "runtime_halted": runtime_ok,
            })

    severity = ("HIGH" if demoted
                else "CLEAN")
    return {
        "ts": now,
        "severity": severity,
        "rolling_n": ROLLING_N,
        "pf_threshold_factor": PF_THRESHOLD_FACTOR,
        "checked": checked,
        "demoted": demoted,
        "insufficient_n_count": len(insufficient_n),
        "insufficient_n": insufficient_n[:5],
    }
