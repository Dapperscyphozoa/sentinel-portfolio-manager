"""Tests for pm.promotion_gate."""
from __future__ import annotations

import os
import pytest

from pm.promotion_gate import (
    audit, check_engine, infer_stage, enforce, CANARY_CAP_MAX,
)


def test_stage_inference():
    assert infer_stage({"cap_frac": 0.0}) == "paper"
    assert infer_stage({"cap_frac": 0.01}) == "canary"
    assert infer_stage({"cap_frac": CANARY_CAP_MAX}) == "canary"
    assert infer_stage({"cap_frac": 0.05}) == "live"
    # explicit stage wins
    assert infer_stage({"cap_frac": 0.20, "stage": "paper"}) == "paper"
    # capital_fraction alias
    assert infer_stage({"capital_fraction": 0.10}) == "live"


def test_paper_always_passes():
    ok, stage, fails = check_engine("any", {"cap_frac": 0.0})
    assert ok and stage == "paper" and fails == []


def test_canary_requires_metrics():
    eng = {"cap_frac": 0.02}  # canary, no metrics
    ok, stage, fails = check_engine("x", eng)
    assert not ok and stage == "canary"
    assert any("bt_pf" in f for f in fails)
    assert any("paper_n" in f for f in fails)


def test_canary_passes_with_full_metrics():
    eng = {
        "cap_frac": 0.02,
        "bt_pf": 1.6, "oos_pf": 1.3, "bt_n": 60,
        "paper_n": 25, "paper_pf": 1.3,
    }
    ok, _, fails = check_engine("x", eng)
    assert ok, f"unexpected fails: {fails}"


def test_live_needs_canary_history():
    eng = {
        "cap_frac": 0.10,
        "bt_pf": 2.0, "oos_pf": 1.5, "bt_n": 150,
        "paper_n": 60, "paper_pf": 1.5,
        # missing canary_n / canary_pf
    }
    ok, stage, fails = check_engine("x", eng)
    assert stage == "live" and not ok
    assert any("canary_n" in f for f in fails)


def test_live_passes_with_full_metrics():
    eng = {
        "cap_frac": 0.10,
        "bt_pf": 2.0, "oos_pf": 1.5, "bt_n": 150,
        "paper_n": 60, "paper_pf": 1.5,
        "canary_n": 25, "canary_pf": 1.3,
    }
    ok, _, fails = check_engine("x", eng)
    assert ok, f"unexpected fails: {fails}"


def test_override_bypasses_gate(monkeypatch):
    monkeypatch.setenv("PROMOTION_OVERRIDE_X_ENGINE", "1")
    ok, stage, fails = check_engine("x_engine", {"cap_frac": 0.10})
    assert ok and stage == "live" and fails == []


def test_audit_returns_one_row_per_engine():
    reg = {
        "p": {"cap_frac": 0.0},
        "c": {"cap_frac": 0.02},
        "l": {"cap_frac": 0.10, "bt_pf": 2.0, "oos_pf": 1.5, "bt_n": 150,
              "paper_n": 60, "paper_pf": 1.5, "canary_n": 25, "canary_pf": 1.3},
    }
    rows = audit(reg)
    assert len(rows) == 3
    by_name = {r["name"]: r for r in rows}
    assert by_name["p"]["ok"]
    assert not by_name["c"]["ok"]
    assert by_name["l"]["ok"]


def test_enforce_strict_raises_on_fail():
    reg = {"bad": {"cap_frac": 0.10}}
    with pytest.raises(AssertionError):
        enforce(reg, strict=True)


def test_enforce_warn_returns_rows():
    reg = {"bad": {"cap_frac": 0.10}}
    rows = enforce(reg, strict=False)
    assert len(rows) == 1 and not rows[0]["ok"]
