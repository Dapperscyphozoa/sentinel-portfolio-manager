"""promotion_candidates: surface lifecycle recommendations as alerts."""
from __future__ import annotations

import logging
import time

from common.poly_persistence import connect_poly
from poly_pm.lifecycle import evaluate_all


log = logging.getLogger("promotion_candidates")


def run() -> None:
    conn = connect_poly()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS poly_monitor_alerts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,"
            " routine TEXT, level TEXT, message TEXT)")
        for r in evaluate_all():
            level = "INFO" if not r.auto_apply else "ACTION"
            msg = (f"{r.strategy}: {r.current_stage} → {r.proposed_stage} "
                   f"(n={r.n} pf={r.pf:.2f}) {r.reason}")
            conn.execute(
                "INSERT INTO poly_monitor_alerts(ts, routine, level, message)"
                " VALUES(?,?,?,?)", (time.time(), "promotion_candidates", level, msg))
            log.info(msg)
    finally:
        conn.close()
