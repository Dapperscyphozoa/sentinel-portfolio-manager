"""fee_drag: net-of-fee P&L per strategy over last 24h."""
from __future__ import annotations

import logging
import os
import time

from common.poly_persistence import connect_poly
from poly_runner.strategies._base import dynamic_fee


log = logging.getLogger("fee_drag")


def run() -> None:
    conn = connect_poly()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS poly_monitor_alerts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL,"
            " routine TEXT, level TEXT, message TEXT)")
        cutoff = time.time() - 86400
        # Compute gross + fee per strategy
        rows = conn.execute(
            "SELECT strategy, price, fill_price, fill_amount, side"
            " FROM poly_orders WHERE submit_ts > ? AND fill_amount > 0",
            (cutoff,)).fetchall()
        by_strat: dict[str, dict[str, float]] = {}
        for strat, p, fp, fa, side in rows:
            if fp is None or fa is None:
                continue
            sign = 1 if side == "BUY" else -1
            gross = sign * (fp - p) * fa
            fee = dynamic_fee(p) * fa * p
            s = by_strat.setdefault(strat, {"gross": 0, "fee": 0, "n": 0})
            s["gross"] += gross; s["fee"] += fee; s["n"] += 1
        for strat, s in by_strat.items():
            net = s["gross"] - s["fee"]
            if s["gross"] > 0 and s["fee"] > 0.5 * s["gross"]:
                msg = f"{strat} fee_drag high: gross=${s['gross']:.2f} "\
                      f"fee=${s['fee']:.2f} net=${net:.2f} n={s['n']}"
                conn.execute(
                    "INSERT INTO poly_monitor_alerts(ts, routine, level, message)"
                    " VALUES(?,?,?,?)", (time.time(), "fee_drag", "WARN", msg))
                log.warning(msg)
    finally:
        conn.close()
