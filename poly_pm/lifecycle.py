"""Lifecycle promotion / demotion engine.

Run by the poly-monitor scheduler every N minutes. Evaluates each
strategy's recent performance against the gate criteria in registry.REGISTRY
and recommends stage transitions.

Does NOT auto-promote — operator confirmation required (per
WORKFLOW.md discipline). Logs recommendations to poly_pm_recommendations.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

from common.poly_persistence import connect_poly
from poly_pm.registry import REGISTRY, set_stage


log = logging.getLogger("poly_lifecycle")


@dataclass
class Recommendation:
    strategy: str
    current_stage: str
    proposed_stage: str
    reason: str
    n: int
    pf: float
    auto_apply: bool = False


def evaluate_all() -> list[Recommendation]:
    out = []
    for name in REGISTRY:
        rec = evaluate_one(name)
        if rec is not None:
            out.append(rec)
    return out


def evaluate_one(name: str) -> Optional[Recommendation]:
    info = REGISTRY[name]
    cur_stage = info.get("stage", "paper")

    n, pf = _compute_pf(name)
    if n == 0:
        return None

    # Halt condition: rolling-20 PF < 1.5 OR drawdown > 30%
    n20, pf20 = _compute_pf(name, limit=20)
    dd = _max_drawdown(name)
    if n20 >= 20 and pf20 < 1.5:
        if cur_stage != "halted":
            return Recommendation(name, cur_stage, "halted",
                                   f"rolling-20 PF={pf20:.2f} < 1.5", n20, pf20,
                                   auto_apply=True)
    if dd is not None and dd < -0.30:
        if cur_stage != "halted":
            return Recommendation(name, cur_stage, "halted",
                                   f"drawdown={dd:.2%} < -30%", n, pf,
                                   auto_apply=True)

    # Promotion logic
    if cur_stage == "paper" and n >= 20 and pf >= 2.0:
        return Recommendation(name, cur_stage, "canary",
                              f"n={n} pf={pf:.2f} meets paper→canary gate",
                              n, pf, auto_apply=False)
    if cur_stage == "canary" and n >= 50 and pf >= 3.0:
        return Recommendation(name, cur_stage, "full",
                              f"n={n} pf={pf:.2f} meets canary→full gate",
                              n, pf, auto_apply=False)

    # Demotion if canary/full but underperforming
    if cur_stage == "canary" and n >= 30 and pf < 1.5:
        return Recommendation(name, cur_stage, "paper",
                              f"n={n} pf={pf:.2f} < 1.5 demote canary→paper",
                              n, pf, auto_apply=True)
    if cur_stage == "full" and n >= 50 and pf < 2.0:
        return Recommendation(name, cur_stage, "canary",
                              f"n={n} pf={pf:.2f} < 2.0 demote full→canary",
                              n, pf, auto_apply=True)

    return None


def _compute_pf(name: str, limit: int = 1000) -> tuple[int, float]:
    conn = connect_poly()
    try:
        rows = conn.execute(
            "SELECT price, fill_price, fill_amount, side FROM poly_orders"
            " WHERE strategy=? AND fill_amount > 0"
            " ORDER BY submit_ts DESC LIMIT ?",
            (name, limit)).fetchall()
        wins = 0.0; losses = 0.0; n = 0
        for p, fp, fa, side in rows:
            if fp is None or fa is None:
                continue
            sign = 1 if side == "BUY" else -1
            pnl = sign * (fp - p) * fa
            if pnl > 0:
                wins += pnl
            else:
                losses += abs(pnl)
            n += 1
        if losses <= 0:
            return n, float("inf") if wins > 0 else 0.0
        return n, wins / losses
    finally:
        conn.close()


def _max_drawdown(name: str) -> Optional[float]:
    conn = connect_poly()
    try:
        rows = conn.execute(
            "SELECT price, fill_price, fill_amount, side, submit_ts FROM poly_orders"
            " WHERE strategy=? AND fill_amount > 0"
            " ORDER BY submit_ts ASC", (name,)).fetchall()
        if not rows:
            return None
        equity = 0.0; peak = 0.0; mdd = 0.0
        for p, fp, fa, side, _ts in rows:
            if fp is None or fa is None:
                continue
            sign = 1 if side == "BUY" else -1
            equity += sign * (fp - p) * fa
            if equity > peak:
                peak = equity
            if peak > 0:
                dd = (equity - peak) / peak
                if dd < mdd:
                    mdd = dd
        return mdd
    finally:
        conn.close()


def apply_recommendation(rec: Recommendation) -> bool:
    """Apply a stage transition. Returns True if applied."""
    if not rec.auto_apply:
        return False
    return set_stage(rec.strategy, rec.proposed_stage)
