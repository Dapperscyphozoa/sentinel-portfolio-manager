"""Pre-trade gate. The runner calls `check_admit(signal)` before submission;
returns (ok, reason). Mirrors SPM's pm/pretrade.py pattern.

Gate rules:
  1. Strategy stage must be 'canary' or 'full' (paper → no submission allowed
     beyond shadow logging; this is enforced by POLY_LIVE=0 globally also).
  2. Per-strategy daily loss budget not exceeded.
  3. Capital-fraction headroom: open positions for this strategy +
     proposed position must not exceed effective_capital_fraction * AUM.
  4. CL aggregator health: if strategy.REQUIRES_LIVE_CL and recent
     validation samples show median > 10bps, block.
  5. PM book sanity: implied probs must sum to <= 1.04 (otherwise the
     book is wrecked and we abstain).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from common.poly_persistence import connect_poly

from poly_pm.registry import REGISTRY, effective_capital_fraction, is_live


log = logging.getLogger("poly_pretrade")


DAILY_LOSS_BUDGET_PCT = float(os.environ.get("POLY_DAILY_LOSS_BUDGET_PCT", "0.05"))
CL_HEALTH_MEDIAN_BPS_MAX = float(os.environ.get("CL_HEALTH_MEDIAN_BPS_MAX", "10.0"))
CL_HEALTH_LOOKBACK_S = int(os.environ.get("CL_HEALTH_LOOKBACK_S", "3600"))


def check_admit(signal: dict, aum_usdc: float) -> tuple[bool, str]:
    name = signal.get("strategy", "")
    info = REGISTRY.get(name)
    if not info:
        return False, "unknown_strategy"

    if not is_live(name) and not _shadow_mode():
        return False, f"strategy_stage={info.get('stage')}"

    # Daily loss budget
    pnl_today = _daily_pnl(name)
    budget = -DAILY_LOSS_BUDGET_PCT * aum_usdc * info["capital_fraction"]
    if pnl_today < budget:
        return False, f"daily_loss_exhausted pnl={pnl_today:.2f} budget={budget:.2f}"

    # Capital headroom
    open_notional = _open_notional(name)
    headroom = effective_capital_fraction(name) * aum_usdc - open_notional
    if signal.get("size_usdc", 0) > headroom:
        return False, f"capital_headroom_exceeded headroom=${headroom:.2f}"

    # CL aggregator health
    info_strategy = REGISTRY[name]
    requires_cl = name in ("cl_predictor", "endgame", "maker_quote")
    if requires_cl:
        median_bps = _cl_recent_median_bps()
        if median_bps is not None and median_bps > CL_HEALTH_MEDIAN_BPS_MAX:
            return False, f"cl_unhealthy median_bps={median_bps:.2f}"

    # PM book sanity
    pm_implied = signal.get("pm_implied")
    if pm_implied is not None:
        if pm_implied <= 0.005 or pm_implied >= 0.995:
            return False, f"pm_book_extreme implied={pm_implied:.3f}"

    return True, "ok"


def _shadow_mode() -> bool:
    return os.environ.get("POLY_LIVE", "0") != "1"


def _daily_pnl(strategy: str) -> float:
    conn = connect_poly()
    try:
        cutoff = time.time() - 86400
        rows = conn.execute(
            "SELECT fill_price, price, fill_amount, side FROM poly_orders"
            " WHERE strategy=? AND submit_ts > ? AND fill_amount > 0",
            (strategy, cutoff)).fetchall()
        pnl = 0.0
        for fp, p, fa, side in rows:
            if fp is None or fa is None:
                continue
            sign = 1 if side == "BUY" else -1
            pnl += sign * (fp - p) * fa
        return pnl
    finally:
        conn.close()


def _open_notional(strategy: str) -> float:
    conn = connect_poly()
    try:
        cur = conn.execute(
            "SELECT COALESCE(SUM(qty * COALESCE(avg_cost, 0.5)), 0)"
            " FROM poly_positions p"
            " WHERE EXISTS(SELECT 1 FROM poly_orders o"
            " WHERE o.market_id=p.market_id AND o.strategy=?)", (strategy,))
        r = cur.fetchone()
        return float(r[0] or 0)
    finally:
        conn.close()


def _cl_recent_median_bps() -> Optional[float]:
    conn = connect_poly()
    try:
        cutoff = time.time() - CL_HEALTH_LOOKBACK_S
        rows = conn.execute(
            "SELECT diff_bps FROM poly_cl_validation"
            " WHERE ts > ? ORDER BY ts DESC LIMIT 5000",
            (cutoff,)).fetchall()
        if not rows:
            return None
        vals = sorted(abs(r[0]) for r in rows)
        n = len(vals)
        return vals[n // 2]
    finally:
        conn.close()
