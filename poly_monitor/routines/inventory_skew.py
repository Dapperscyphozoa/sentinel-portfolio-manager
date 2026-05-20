"""inventory_skew: alert if maker_quote inventory > 80% of MM_MAX_INVENTORY_USD."""
from __future__ import annotations

import logging
import os
import time

from common.poly_persistence import connect_poly


log = logging.getLogger("inventory_skew")

MAX_INV = float(os.environ.get("MM_MAX_INVENTORY_USD", "100"))
THRESHOLD = 0.80


def run() -> None:
    conn = connect_poly()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS poly_monitor_alerts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,"
            " routine TEXT, level TEXT, message TEXT)")
        rows = conn.execute(
            "SELECT market_id, token, qty, avg_cost FROM poly_positions"
            " WHERE qty != 0").fetchall()
        for mid, token, qty, avg_cost in rows:
            notional = abs(qty or 0) * (avg_cost or 0.5)
            if notional > MAX_INV * THRESHOLD:
                msg = f"inventory skew {mid}/{token}: ${notional:.2f} (>{THRESHOLD:.0%} cap)"
                conn.execute(
                    "INSERT INTO poly_monitor_alerts(ts, routine, level, message)"
                    " VALUES(?,?,?,?)", (time.time(), "inventory_skew", "WARN", msg))
                log.warning(msg)
    finally:
        conn.close()
