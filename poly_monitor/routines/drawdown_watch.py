"""drawdown_watch: trigger halt if any strategy drawdown > 25%."""
from __future__ import annotations

import logging
import os
import time

import httpx

from common.poly_persistence import connect_poly
from poly_pm.lifecycle import _max_drawdown
from poly_pm.registry import REGISTRY


log = logging.getLogger("drawdown_watch")

DD_THRESH = float(os.environ.get("DRAWDOWN_HALT_PCT", "0.25"))
RUNNER_URL = os.environ.get("POLY_RUNNER_URL", "http://127.0.0.1:10101")
HALT_TOKEN = os.environ.get("HALT_TOKEN", "")


def run() -> None:
    conn = connect_poly()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS poly_monitor_alerts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,"
            " routine TEXT, level TEXT, message TEXT)")
        for name in REGISTRY:
            dd = _max_drawdown(name)
            if dd is None:
                continue
            if dd < -DD_THRESH:
                msg = f"{name} DD={dd:.2%} < -{DD_THRESH:.0%}; triggering halt"
                conn.execute(
                    "INSERT INTO poly_monitor_alerts(ts, routine, level, message)"
                    " VALUES(?,?,?,?)", (time.time(), "drawdown_watch", "CRITICAL", msg))
                log.error(msg)
                if HALT_TOKEN:
                    try:
                        httpx.post(f"{RUNNER_URL}/halt/{name}",
                                  headers={"X-Halt-Token": HALT_TOKEN}, timeout=3)
                    except Exception as e:
                        log.warning(f"halt POST failed: {e}")
    finally:
        conn.close()
