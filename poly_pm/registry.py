"""Poly strategy registry — per-strategy capital fractions + lifecycle stage.

Lifecycle stages (mirroring SPM's gating discipline):
  paper    — eval only, no orders submitted (POLY_LIVE=0 globally OR strategy locally disabled)
  canary   — live with reduced capital_fraction (default 0.20x of full allocation)
  full     — live with audited capital_fraction
  halted   — emergency halt; runner skips evaluate()

Promotion gates (mirror VALIDATION_*.md discipline):
  paper→canary: n≥20 paper signals with simulated PF ≥ 2.0
  canary→full:  n≥50 live signals with PF ≥ 3.0
  any→halted:   rolling-20 PF < 1.5 OR drawdown > 30%
"""
from __future__ import annotations

import os


# Stage definitions per strategy. Operator edits this to promote / demote.
# `capital_fraction` is the *full* allocation; `stage` controls the discount.
STAGE_DISCOUNT = {"paper": 0.0, "canary": 0.20, "full": 1.0, "halted": 0.0}


REGISTRY = {
    "cl_predictor": {
        "capital_fraction": float(os.environ.get("CL_PRED_CAPITAL_FRACTION", "0.30")),
        "stage": os.environ.get("CL_PRED_STAGE", "paper"),
        "audit_status": "pending_validation_gate",
        "gate_required": "scripts/cl_aggregator_validate.py median<5bps p95<15bps n>=100k",
        "kill_condition": "cl_aggregator median > 10bps OR p95 > 25bps over 100 hours",
    },
    "endgame": {
        "capital_fraction": float(os.environ.get("EG_CAPITAL_FRACTION", "0.20")),
        "stage": os.environ.get("EG_STAGE", "paper"),
        "audit_status": "pending_validation_gate",
        "gate_required": "same as cl_predictor (depends on aggregator)",
        "kill_condition": "shares cl_predictor kill condition",
    },
    "maker_quote": {
        "capital_fraction": float(os.environ.get("MM_CAPITAL_FRACTION", "0.30")),
        "stage": os.environ.get("MM_STAGE", "paper"),
        "audit_status": "ready",
        "gate_required": "n>=200 quotes with inventory-aware fill ratio > 0.4",
        "kill_condition": "rolling-100 net P&L < -2% of allocation",
    },
    "cross_asset": {
        "capital_fraction": float(os.environ.get("XA_CAPITAL_FRACTION", "0.10")),
        "stage": os.environ.get("XA_STAGE", "paper"),
        "audit_status": "ready",
        "gate_required": "n>=50 with PF >= 1.8 AND correlation persistence test",
        "kill_condition": "rolling-20 correlation < 0.4 (regime break)",
    },
    "reflexivity_emitter": {
        "capital_fraction": 0.0,            # never trades PM
        "stage": os.environ.get("RE_STAGE", "canary"),
        "audit_status": "feed_only",
        "gate_required": "n>=30 reflex events with measurable SPM downstream PF impact",
        "kill_condition": "n/a — feed only",
    },
}


def effective_capital_fraction(name: str) -> float:
    info = REGISTRY.get(name)
    if not info:
        return 0.0
    return info["capital_fraction"] * STAGE_DISCOUNT.get(info.get("stage", "paper"), 0.0)


def is_live(name: str) -> bool:
    info = REGISTRY.get(name)
    if not info:
        return False
    return info.get("stage") in ("canary", "full")


def set_stage(name: str, stage: str) -> bool:
    if stage not in STAGE_DISCOUNT:
        return False
    if name not in REGISTRY:
        return False
    REGISTRY[name]["stage"] = stage
    return True
