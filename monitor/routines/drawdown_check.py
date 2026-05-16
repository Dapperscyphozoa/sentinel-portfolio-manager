"""Drawdown check — every 5 min.

Pulls HL account value via signal-bus. If drawdown from rolling 24h peak exceeds
threshold, posts /halt/all to strategy-runner with HALT_TOKEN. Records action.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time

import httpx


log = logging.getLogger("routine.drawdown")


_PEAK: dict = {"value": 0.0, "ts": 0.0}


def run(conn: sqlite3.Connection) -> dict:
    bus_url = os.environ.get("SIGNAL_BUS_URL", "").rstrip("/")
    runner_url = os.environ.get("STRATEGY_RUNNER_URL", "").rstrip("/")
    halt_token = os.environ.get("HALT_TOKEN", "")
    dd_thr = float(os.environ.get("DRAWDOWN_HALT_PCT", "0.10"))  # 10% intraday

    try:
        with httpx.Client(timeout=10) as c:
            r = c.get(f"{bus_url}/hl/account")
            r.raise_for_status()
            acct = r.json()
    except Exception as e:
        return {"ok": False, "error": str(e)}
    value = float(acct.get("value", 0))
    now = time.time()
    if value > _PEAK["value"] or now - _PEAK["ts"] > 86400:
        _PEAK.update({"value": value, "ts": now})
    peak = _PEAK["value"] or value
    dd = (peak - value) / peak if peak > 0 else 0.0
    out = {"ts": now, "value": value, "peak": peak, "dd": dd, "threshold": dd_thr}
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
