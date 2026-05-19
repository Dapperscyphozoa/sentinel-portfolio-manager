"""Promotion gate — prevents capital drift from paper → canary → live.

Stage is INFERRED from cap_frac so the existing registry shape is preserved:
    cap_frac == 0        → paper
    0 < cap_frac ≤ 0.025 → canary
    cap_frac >  0.025    → live

Required metrics per stage (all minimums):
    paper:  nothing.
    canary: bt_pf ≥ 1.5,  oos_pf ≥ 1.2,  bt_n ≥ 50,
            paper_n ≥ 20, paper_pf ≥ 1.2.
    live:   canary criteria + bt_pf ≥ 1.8, oos_pf ≥ 1.4, bt_n ≥ 100,
            paper_n ≥ 50, paper_pf ≥ 1.4,
            canary_n ≥ 20, canary_pf ≥ 1.2.

Missing field = 0 = fail. The gate cannot approve what it cannot measure.

Override: set env PROMOTION_OVERRIDE_<NAME_UPPER>=1 to bypass a single engine
(emergency only — recorded in logs).
"""
from __future__ import annotations

import os
import logging
from typing import Iterable

log = logging.getLogger(__name__)

CANARY_CAP_MAX = 0.025

# Minimum metrics per stage. None = no minimum at this stage.
GATE: dict[str, dict] = {
    "paper": {},
    "canary": {
        "bt_pf":    1.5,
        "oos_pf":   1.2,
        "bt_n":     50,
        "paper_n":  20,
        "paper_pf": 1.2,
    },
    "live": {
        "bt_pf":     1.8,
        "oos_pf":    1.4,
        "bt_n":      100,
        "paper_n":   50,
        "paper_pf":  1.4,
        "canary_n":  20,
        "canary_pf": 1.2,
    },
}


def _cap_of(eng: dict) -> float:
    return float(eng.get("cap_frac", eng.get("capital_fraction", 0.0)))


def infer_stage(eng: dict) -> str:
    """Stage from cap_frac. Explicit eng['stage'] wins if set."""
    if eng.get("stage") in GATE:
        return eng["stage"]
    cf = _cap_of(eng)
    if cf <= 0.0:
        return "paper"
    if cf <= CANARY_CAP_MAX:
        return "canary"
    return "live"


def _override_active(name: str) -> bool:
    key = f"PROMOTION_OVERRIDE_{name.upper()}"
    return os.environ.get(key, "").strip() in ("1", "true", "yes")


def check_engine(name: str, eng: dict) -> tuple[bool, str, list[str]]:
    """Return (ok, stage, failed_fields).

    ok=True if (a) stage=paper OR (b) all required minimums met OR (c) override.
    """
    stage = infer_stage(eng)
    if stage == "paper":
        return True, stage, []
    if _override_active(name):
        log.warning("promotion_gate OVERRIDE active for %s (stage=%s)", name, stage)
        return True, stage, []
    req = GATE.get(stage, {})
    fails = []
    for field, minimum in req.items():
        val = eng.get(field)
        if val is None or float(val) < float(minimum):
            fails.append(f"{field}={val!r}<{minimum}")
    return (len(fails) == 0), stage, fails


def audit(registry: dict) -> list[dict]:
    """Run gate over the whole registry. Returns one row per engine."""
    rows = []
    for name, eng in registry.items():
        ok, stage, fails = check_engine(name, eng)
        rows.append({
            "name": name,
            "stage": stage,
            "cap_frac": _cap_of(eng),
            "bt_pf":    eng.get("bt_pf"),
            "oos_pf":   eng.get("oos_pf"),
            "bt_n":     eng.get("bt_n"),
            "paper_n":  eng.get("paper_n"),
            "paper_pf": eng.get("paper_pf"),
            "ok":       ok,
            "fails":    fails,
            "override": _override_active(name),
        })
    return rows


def enforce(registry: dict, *, strict: bool = True) -> list[dict]:
    """Run audit. In strict mode, raise on any fail (refuses service boot).
    In non-strict mode, log the failures and return the audit rows.
    """
    rows = audit(registry)
    failed = [r for r in rows if not r["ok"]]
    if failed:
        for r in failed:
            log.error("promotion_gate FAIL %s stage=%s cap_frac=%.4f fails=%s",
                      r["name"], r["stage"], r["cap_frac"], r["fails"])
        if strict:
            names = ", ".join(r["name"] for r in failed)
            raise AssertionError(
                f"promotion_gate refuses boot: {len(failed)} engines fail gate: {names}. "
                "Either demote (cap_frac=0), populate required metrics, "
                "or set PROMOTION_OVERRIDE_<NAME>=1 per engine."
            )
    return rows


def format_table(rows: Iterable[dict]) -> str:
    """Plain-text audit table."""
    lines = []
    head = f"{'engine':<28} {'stage':<7} {'cap':>6} {'bt_pf':>6} {'oos':>5} {'bt_n':>5} {'pn':>4} {'p_pf':>5} {'ok':>3}  fails"
    lines.append(head)
    lines.append("-" * len(head))
    for r in sorted(rows, key=lambda x: (not x["ok"], x["stage"], x["name"])):
        def _f(v, w, fmt="{:>{w}}"):
            if v is None: return f"{'-':>{w}}"
            try:
                if isinstance(v, float): return f"{v:>{w}.2f}"
                return f"{v:>{w}}"
            except Exception: return f"{str(v):>{w}}"
        lines.append(
            f"{r['name']:<28} {r['stage']:<7} "
            f"{r['cap_frac']:>6.3f} "
            f"{_f(r['bt_pf'], 6)} {_f(r['oos_pf'], 5)} "
            f"{_f(r['bt_n'], 5)} {_f(r['paper_n'], 4)} {_f(r['paper_pf'], 5)} "
            f"{'YES' if r['ok'] else 'NO':>3}  "
            f"{','.join(r['fails']) if r['fails'] else ''}"
            + ("  [OVERRIDE]" if r["override"] else "")
        )
    return "\n".join(lines)
