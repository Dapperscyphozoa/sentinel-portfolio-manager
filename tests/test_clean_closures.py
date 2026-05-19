"""Tests for common.closures — clean-closure classification.

Bug context: PF/WR metrics that fed promotion gates were poisoned by
operator force-closes and bug-recovery backfills. This module classifies
which closures reflect strategy edge vs operational noise.
"""
from __future__ import annotations

import pytest

from common.closures import is_clean_closure, clean_metrics, NOISY_PREFIXES, NOISY_EXACT


# ─── is_clean_closure ─────────────────────────────────────────────────────
def test_strategy_driven_exits_are_clean():
    for r in ("tp", "sl", "timeout", "fmom_z_neutral_+0.16",
              "fmom_z_flipped_orig=-2.20_now=+3.58",
              "hlp_neutral_z=0.13", "strategy_exit"):
        assert is_clean_closure(r), f"{r!r} should be clean"


def test_force_close_variants_are_noisy():
    for r in ("force_close:session_audit_red_engines",
              "force_close:operator_kill",
              "force_close",
              "stale_force_close_hl_absent",
              "stale_force_close_hl_ok",
              "stale_force_close_hl_refused",
              "stale_force_close_hl_unreachable",
              "stale_force_close_paper",
              "force_closed_unverified",
              "operator_force_close"):
        assert not is_clean_closure(r), f"{r!r} should be noisy"


def test_backfill_and_reconcile_are_noisy():
    for r in ("backfill", "reconciled_off_book", "manual"):
        assert not is_clean_closure(r), f"{r!r} should be noisy"


def test_empty_or_none_treated_as_clean():
    """Legacy rows without recorded reasons assumed clean — don't punish
    strategies for historical data quality."""
    assert is_clean_closure(None)
    assert is_clean_closure("")
    assert is_clean_closure("   ")


def test_whitespace_stripped():
    assert not is_clean_closure("  backfill  ")
    assert not is_clean_closure("force_close:foo\t")


# ─── clean_metrics ────────────────────────────────────────────────────────
def test_clean_metrics_excludes_noisy_rows():
    closures = [
        {"pnl_usd": 1.0, "fees_usd": 0.0, "close_reason": "tp"},
        {"pnl_usd": -2.0, "fees_usd": 0.0, "close_reason": "force_close:audit"},
        {"pnl_usd": -2.0, "fees_usd": 0.0, "close_reason": "backfill"},
        {"pnl_usd": -0.5, "fees_usd": 0.0, "close_reason": "sl"},
    ]
    m = clean_metrics(closures)
    assert m["raw_n"] == 4
    assert m["clean_n"] == 2  # tp + sl
    assert m["wins"] == 1
    assert m["losses"] == 1
    assert abs(m["pf"] - (1.0 / 0.5)) < 1e-9  # PF = wins/losses = 2.0
    assert abs(m["net_pnl"] - 0.5) < 1e-9


def test_clean_metrics_handles_all_noisy_rows():
    """If every closure is noisy, n=0 and PF is None."""
    closures = [
        {"pnl_usd": -1.0, "fees_usd": 0.0, "close_reason": "force_close:audit"},
        {"pnl_usd": -2.0, "fees_usd": 0.0, "close_reason": "backfill"},
    ]
    m = clean_metrics(closures)
    assert m["raw_n"] == 2
    assert m["clean_n"] == 0
    assert m["pf"] is None
    assert m["net_pnl"] == 0


def test_clean_metrics_pf_infinite_when_no_losses():
    closures = [{"pnl_usd": 1.0, "fees_usd": 0.0, "close_reason": "tp"}]
    m = clean_metrics(closures)
    assert m["pf"] == float("inf")


def test_clean_metrics_pf_none_on_empty():
    m = clean_metrics([])
    assert m["clean_n"] == 0
    assert m["pf"] is None


def test_clean_metrics_subtracts_fees_correctly():
    """Net = gross - fees. WR is on net."""
    closures = [
        {"pnl_usd": 0.10, "fees_usd": 0.15, "close_reason": "tp"},   # net -0.05 → loss
        {"pnl_usd": 0.20, "fees_usd": 0.05, "close_reason": "tp"},   # net +0.15 → win
    ]
    m = clean_metrics(closures)
    assert m["clean_n"] == 2
    assert m["wins"] == 1
    assert m["losses"] == 1
    assert abs(m["gross_pnl"] - 0.30) < 1e-9
    assert abs(m["fees"] - 0.20) < 1e-9
    assert abs(m["net_pnl"] - 0.10) < 1e-9


# ─── ict_confluence_4h reality check ──────────────────────────────────────
def test_ict_confluence_4h_real_case_from_production():
    """The exact scenario from session 2026-05-19: 11 e08_dip3d7_td_4h
    closures, 0 clean. clean_metrics must report clean_n=0."""
    closures = [
        # 3 backfill losses
        {"pnl_usd": -1.29, "fees_usd": 0.01, "close_reason": "backfill"},
        {"pnl_usd": -1.36, "fees_usd": 0.01, "close_reason": "backfill"},
        {"pnl_usd": -0.53, "fees_usd": 0.01, "close_reason": "backfill"},
        # 8 force-close losses from operator audit
        {"pnl_usd": -0.75, "fees_usd": 0.0,
         "close_reason": "force_close:session_audit_red_engines"},
        {"pnl_usd": -0.56, "fees_usd": 0.0,
         "close_reason": "force_close:session_audit_red_engines"},
        {"pnl_usd": -0.47, "fees_usd": 0.0,
         "close_reason": "force_close:session_audit_red_engines"},
        {"pnl_usd": -0.54, "fees_usd": 0.0,
         "close_reason": "force_close:session_audit_red_engines"},
        {"pnl_usd": -1.18, "fees_usd": 0.0,
         "close_reason": "force_close:session_audit_red_engines"},
        {"pnl_usd": -0.81, "fees_usd": 0.0,
         "close_reason": "force_close:session_audit_red_engines"},
        {"pnl_usd": -1.39, "fees_usd": 0.0,
         "close_reason": "force_close:session_audit_red_engines"},
        {"pnl_usd": -1.11, "fees_usd": 0.0,
         "close_reason": "force_close:session_audit_red_engines"},
    ]
    m = clean_metrics(closures)
    assert m["raw_n"] == 11
    assert m["clean_n"] == 0  # exactly the point
    assert m["pf"] is None
    # Previously this engine looked like "0% WR, PF 0, -$9.98, halt immediately"
    # With the filter, it correctly reports "no clean data — judgment deferred"


# ─── exported constants ───────────────────────────────────────────────────
def test_noisy_prefixes_have_expected_values():
    assert "force_close" in NOISY_PREFIXES
    assert "stale_force_close" in NOISY_PREFIXES


def test_noisy_exact_includes_backfill():
    assert "backfill" in NOISY_EXACT
    assert "reconciled_off_book" in NOISY_EXACT
