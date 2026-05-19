"""Drawdown check — every 5 min.

Pulls HL account value via signal-bus. Maintains a TRUE 24h rolling peak by
persisting peak + peak_ts to the monitor's SQLite (kv_state table). Survives
process restarts (which the previous module-global _PEAK did NOT — sentinel
finding).

If drawdown from peak exceeds DRAWDOWN_HALT_PCT, POSTs /halt/all to
strategy-runner with HALT_TOKEN. Records action.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time

import httpx

from common import persistence


log = logging.getLogger("routine.drawdown")


_KV_KEY = "drawdown_peak_v1"
_FAIL_KEY = "drawdown_consecutive_failures_v1"
# Drawdown check runs every ~5min; 4 consecutive failures = ~20min of
# unevaluated drawdown protection before we fire a precautionary halt.
_FAIL_BUS_HALT_AFTER = 4


def _load_peak(conn: sqlite3.Connection) -> dict:
    raw = persistence.kv_get(conn, _KV_KEY)
    if not raw:
        return {"value": 0.0, "ts": 0.0}
    try:
        d = json.loads(raw)
        return {"value": float(d.get("value", 0.0)), "ts": float(d.get("ts", 0.0))}
    except Exception:
        return {"value": 0.0, "ts": 0.0}


def _save_peak(conn: sqlite3.Connection, peak: dict) -> None:
    persistence.kv_set(conn, _KV_KEY, json.dumps(peak))


def run(conn: sqlite3.Connection) -> dict:
    bus_url = os.environ.get("SIGNAL_BUS_URL", "").rstrip("/")
    runner_url = os.environ.get("STRATEGY_RUNNER_URL", "").rstrip("/")
    halt_token = os.environ.get("HALT_TOKEN", "")
    dd_thr = float(os.environ.get("DRAWDOWN_HALT_PCT", "0.10"))

    try:
        with httpx.Client(timeout=10) as c:
            r = c.get(f"{bus_url}/hl/account")
            r.raise_for_status()
            acct = r.json()
    except Exception as e:
        # Fail-safe: track consecutive bus failures. If we cannot evaluate
        # drawdown for too long, post a precautionary /halt/all so trading
        # does not run uncapped during a multi-poll outage.
        raw = persistence.kv_get(conn, _FAIL_KEY) or "0"
        try:
            fails = int(raw) + 1
        except ValueError:
            fails = 1
        persistence.kv_set(conn, _FAIL_KEY, str(fails))
        out = {"ok": False, "error": str(e), "consecutive_failures": fails}
        if fails >= _FAIL_BUS_HALT_AFTER and halt_token and runner_url:
            try:
                with httpx.Client(timeout=10) as c:
                    resp = c.post(
                        f"{runner_url}/halt/all",
                        headers={"X-Halt-Token": halt_token, "content-type": "application/json"},
                        content=json.dumps({"reason": f"bus_unreachable_failsafe_{fails}",
                                             "actor": "monitor"}),
                    )
                    out["halt_response"] = {"status_code": resp.status_code,
                                            "body": resp.text[:400]}
            except Exception as he:
                out["halt_error"] = str(he)
        return out
    # Reset failure counter on successful poll.
    persistence.kv_set(conn, _FAIL_KEY, "0")
    value = float(acct.get("value", 0))
    now = time.time()

    # 24h rolling peak persisted via SQLite
    peak = _load_peak(conn)
    if value > peak["value"]:
        peak = {"value": value, "ts": now}
        _save_peak(conn, peak)
    elif now - peak["ts"] > 86400:
        peak = {"value": value, "ts": now}
        _save_peak(conn, peak)
    elif peak["value"] == 0.0:
        peak = {"value": value, "ts": now}
        _save_peak(conn, peak)

    dd = (peak["value"] - value) / peak["value"] if peak["value"] > 0 else 0.0
    out = {"ts": now, "value": value, "peak": peak["value"], "peak_ts": peak["ts"],
           "dd": dd, "threshold": dd_thr}

    if dd >= dd_thr and halt_token and runner_url:
        try:
            with httpx.Client(timeout=10) as c:
                resp = c.post(
                    f"{runner_url}/halt/all",
                    headers={"X-Halt-Token": halt_token, "content-type": "application/json"},
                    content=json.dumps({"reason": f"drawdown_{dd:.3f}", "actor": "monitor"}),
                )
                out["halt_response"] = {"status_code": resp.status_code, "body": resp.text[:400]}
        except Exception as e:
            out["halt_error"] = str(e)
    return out
