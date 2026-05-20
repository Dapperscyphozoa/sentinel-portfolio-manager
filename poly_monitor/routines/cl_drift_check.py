"""cl_drift_check: alert if rolling-1h CL prediction median error > 10bps."""
from __future__ import annotations

import logging
import os
import time

from common.poly_persistence import connect_poly


log = logging.getLogger("cl_drift_check")

LOOKBACK_S = 3600
MEDIAN_THRESH_BPS = float(os.environ.get("CL_HEALTH_MEDIAN_BPS_MAX", "10.0"))
P95_THRESH_BPS = float(os.environ.get("CL_HEALTH_P95_BPS_MAX", "25.0"))


def run() -> None:
    conn = connect_poly()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS poly_monitor_alerts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,"
            " routine TEXT, level TEXT, message TEXT)")
        cutoff = time.time() - LOOKBACK_S
        for asset in ("BTC", "ETH"):
            rows = conn.execute(
                "SELECT diff_bps FROM poly_cl_validation"
                " WHERE asset=? AND ts > ?"
                " ORDER BY ts DESC LIMIT 7200", (asset, cutoff)).fetchall()
            if len(rows) < 100:
                continue
            vals = sorted(abs(r[0]) for r in rows)
            n = len(vals)
            median = vals[n // 2]
            p95 = vals[min(n - 1, int(n * 0.95))]
            level = None; msg = None
            if median > MEDIAN_THRESH_BPS:
                level = "CRITICAL"
                msg = f"{asset} cl median={median:.2f}bps > {MEDIAN_THRESH_BPS}bps; "\
                      f"recommend halt cl_predictor + endgame"
            elif p95 > P95_THRESH_BPS:
                level = "WARN"
                msg = f"{asset} cl p95={p95:.2f}bps > {P95_THRESH_BPS}bps"
            if level:
                conn.execute(
                    "INSERT INTO poly_monitor_alerts(ts, routine, level, message)"
                    " VALUES(?,?,?,?)", (time.time(), "cl_drift_check", level, msg))
                log.warning(msg)
    finally:
        conn.close()
