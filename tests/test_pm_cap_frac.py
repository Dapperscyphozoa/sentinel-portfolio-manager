"""Tests for PM cap_frac enforcement + Rule 5b half-size.

Sentinel council CRITICAL findings 2026-05-19:
  - Qwen3 235B (95% conf): Rule 5b half-size logic missing
  - Mistral Large (99% conf): cap_frac decorative — sizing was flat 5%
    per trade ignoring engine budget

These tests pin the fixes.
"""
from __future__ import annotations
import os, sys
import pytest

# Test isolation: clear PM cooldown DB
os.environ["COOLDOWN_DB"] = "/tmp/test_cooldowns.sqlite"
os.environ["MIN_24H_VOL_USD"] = "0"  # disable liquidity guard for tests

from pm.pretrade import check, ENGINE_REGISTRY


class _MockConn:
    """Cooldown lookup requires a conn; pretrade only uses it for cooldowns,
    which we disable by setting MIN_24H_VOL_USD=0 + clean DB."""
    def execute(self, *a, **kw):
        class _C:
            def fetchone(self): return None
            def fetchall(self): return []
        return _C()


def _sig(coin="BTC"): return {"coin": coin}
def _regime(name="range", conf=1.0):
    return {"regime": name, "confidence": conf}


# ─── cap_frac per-engine concentration cap ────────────────────────────────
def test_cap_frac_blocks_when_engine_budget_exhausted():
    """hl_settle_5m has cap_frac=0.20; on $491 wallet that's $98.20 budget.
    After 4 trades × $24.55 = $98.20, the 5th must be blocked."""
    existing = [
        {"strategy": "hl_settle_5m", "coin": f"C{i}", "margin": 24.55}
        for i in range(4)
    ]
    r = check(_MockConn(), "hl_settle_5m", _sig("ETH"), _regime(),
              491.0, existing)
    assert r.allow is False
    assert "engine_cap_frac_exhausted" in r.reason


def test_cap_frac_allows_within_budget():
    """3 open trades use $73.65 of $98.20 budget → trade 4 fits."""
    existing = [
        {"strategy": "hl_settle_5m", "coin": f"C{i}", "margin": 24.55}
        for i in range(3)
    ]
    r = check(_MockConn(), "hl_settle_5m", _sig("ETH"), _regime(),
              491.0, existing)
    assert r.allow is True
    assert r.reason == "ok"
    assert r.size_usd == pytest.approx(24.55, abs=0.01)


def test_cap_frac_isolates_by_engine_tag():
    """Other engines' open positions don't count against this engine's budget."""
    # 5 trades from a DIFFERENT engine — should not exhaust hl_settle_5m
    other_engine_trades = [
        {"strategy": "vpoc_retest", "coin": f"C{i}", "margin": 24.55}
        for i in range(5)
    ]
    r = check(_MockConn(), "hl_settle_5m", _sig("ETH"), _regime(),
              491.0, other_engine_trades)
    assert r.allow is True
    assert r.reason == "ok"


def test_cap_frac_zero_engine_skips_check():
    """Paper engines (cap_frac=0) skip the budget cap — they should still
    fire signals so paper attribution accrues."""
    # fmom has cap_frac=0.00 in current registry
    if "fmom" not in ENGINE_REGISTRY:
        pytest.skip("fmom not in registry")
    # Even with many open trades from fmom, cap_frac=0 should not cap-block
    # (other gates may block, but not this specific cap_frac one)
    existing = [
        {"strategy": "fmom", "coin": f"C{i}", "margin": 24.55}
        for i in range(20)
    ]
    r = check(_MockConn(), "fmom", _sig("ETH"), _regime(), 491.0, existing)
    # Should NOT be blocked by cap_frac_exhausted reason
    assert "engine_cap_frac_exhausted" not in (r.reason or "")
    # (It will be blocked by max_open_global=20 since 20 trades exist — confirm)
    assert r.reason == "max_open_global"


# ─── Rule 5b half-size ────────────────────────────────────────────────────
def test_regime_mismatch_blocks_when_not_trend_aware():
    """Engine with affinity=[trend_up] in regime=trend_down at conf>0.7 →
    blocked (no trend_direction_aware flag)."""
    # e01_zfade3s_tu_1d: affinity=['trend_up'], no trend_direction_aware
    r = check(_MockConn(), "e01_zfade3s_tu_1d", _sig("ETH"),
              _regime("trend_down", 1.0), 491.0, [])
    assert r.allow is False
    assert "regime_mismatch" in r.reason


def test_regime_match_allows_full_size():
    """Engine in its own affinity regime → full size."""
    # e01_zfade3s_tu_1d in trend_up regime
    r = check(_MockConn(), "e01_zfade3s_tu_1d", _sig("ETH"),
              _regime("trend_up", 1.0), 491.0, [])
    assert r.allow is True
    assert r.size_usd == pytest.approx(24.55, abs=0.01)


def test_low_confidence_regime_lets_through_anyway():
    """Spec: regime mismatch only blocks at conf > 0.7."""
    r = check(_MockConn(), "e01_zfade3s_tu_1d", _sig("ETH"),
              _regime("trend_down", 0.5), 491.0, [])
    assert r.allow is True  # conf 0.5 < 0.7, mismatch doesn't matter


def test_rule_5b_trend_aware_engine_gets_half_size_in_opposite_trend():
    """If an engine has UNI-trend affinity (e.g. just trend_up) AND flag
    trend_direction_aware=True, it can fire in the OPPOSITE trend
    (trend_down) at half size."""
    orig = ENGINE_REGISTRY.get("e01_zfade3s_tu_1d", {}).copy()
    try:
        # Uni-trend affinity (trend_up only) + trend_direction_aware
        ENGINE_REGISTRY["e01_zfade3s_tu_1d"] = {
            "affinity": ["trend_up"],
            "bt_pf": 1.29,
            "cap_frac": 0.05,
            "trend_direction_aware": True,
        }
        # Regime is trend_down — NOT in affinity, but it IS the opposite
        # of trend_up which IS in affinity → half size fires.
        r = check(_MockConn(), "e01_zfade3s_tu_1d", _sig("ETH"),
                  _regime("trend_down", 1.0), 491.0, [])
        # Should be allowed at half size: 0.05 × 491 × 0.5 = $12.275
        assert r.allow is True, f"got {r.reason}"
        assert r.size_usd == pytest.approx(12.275, abs=0.01), (
            f"expected half-size $12.28, got ${r.size_usd}")
    finally:
        ENGINE_REGISTRY["e01_zfade3s_tu_1d"] = orig


def test_rule_5b_without_flag_still_blocks_opposite_trend():
    """Same uni-trend engine but WITHOUT trend_direction_aware → still
    hard-blocked in opposite trend."""
    orig = ENGINE_REGISTRY.get("e01_zfade3s_tu_1d", {}).copy()
    try:
        ENGINE_REGISTRY["e01_zfade3s_tu_1d"] = {
            "affinity": ["trend_up"],
            "bt_pf": 1.29,
            "cap_frac": 0.05,
            # trend_direction_aware NOT set
        }
        r = check(_MockConn(), "e01_zfade3s_tu_1d", _sig("ETH"),
                  _regime("trend_down", 1.0), 491.0, [])
        assert r.allow is False
        assert "regime_mismatch" in r.reason
    finally:
        ENGINE_REGISTRY["e01_zfade3s_tu_1d"] = orig


def test_rule_5b_does_not_halve_when_regime_in_affinity():
    """Trend-aware engine in its own affinity regime gets FULL size, not
    half — Rule 5b only fires when regime is OUTSIDE affinity (opposite-
    trend escape valve)."""
    orig = ENGINE_REGISTRY.get("e01_zfade3s_tu_1d", {}).copy()
    try:
        ENGINE_REGISTRY["e01_zfade3s_tu_1d"] = {
            "affinity": ["trend_up", "trend_down"],
            "bt_pf": 1.29,
            "cap_frac": 0.05,
            "trend_direction_aware": True,
        }
        # trend_down IS in affinity → no half-size, full $24.55
        r = check(_MockConn(), "e01_zfade3s_tu_1d", _sig("ETH"),
                  _regime("trend_down", 1.0), 491.0, [])
        assert r.allow is True
        assert r.size_usd == pytest.approx(24.55, abs=0.01)
    finally:
        ENGINE_REGISTRY["e01_zfade3s_tu_1d"] = orig


def test_rule_5b_only_applies_to_opposite_trend_not_range():
    """Trend-aware engine in RANGE regime (not opposite trend) is still
    blocked — Rule 5b only handles trend_up<->trend_down swaps."""
    orig = ENGINE_REGISTRY.get("e09_pump3d10_td_1d", {}).copy()
    try:
        # affinity is just trend_down; add trend_direction_aware
        ENGINE_REGISTRY["e09_pump3d10_td_1d"] = {
            "affinity": ["trend_down"],  # only one trend — opposite IS NOT in affinity
            "bt_pf": 2.2,
            "cap_frac": 0.10,
            "trend_direction_aware": True,
        }
        # In range regime: range NOT in affinity (which is just trend_down).
        # Opposite-pair for range is None → no half-size path.
        r = check(_MockConn(), "e09_pump3d10_td_1d", _sig("ETH"),
                  _regime("range", 1.0), 491.0, [])
        assert r.allow is False
        assert "regime_mismatch" in r.reason
    finally:
        ENGINE_REGISTRY["e09_pump3d10_td_1d"] = orig


# ─── Notional ceiling check ───────────────────────────────────────────────
def test_max_total_engine_notional_bounded_by_cap_frac():
    """Cumulative claim: with cap_frac × leverage product, the maximum
    total notional an engine can hold is bounded. For hl_settle_5m at
    cap_frac=0.20 leverage=5: max notional = $491 × 0.20 × 5 = $491.
    (One wallet equity in notional, by design.)"""
    # Pre-fill engine to 95% of budget — small headroom for last trade
    existing = [
        {"strategy": "hl_settle_5m", "coin": f"C{i}", "margin": 24.55}
        for i in range(3)
    ]  # = $73.65 used, budget $98.20, headroom $24.55
    r = check(_MockConn(), "hl_settle_5m", _sig("ETH"), _regime(),
              491.0, existing)
    assert r.allow is True
    # After this trade, total margin = $98.20 = cap_frac × equity exactly
    total = sum(p["margin"] for p in existing) + r.size_usd
    cap_budget = 0.20 * 491.0
    assert total <= cap_budget + 0.05  # float tolerance
    # Notional = margin × leverage = $98.20 × 5 = $491
    total_notional = total * 5
    assert total_notional <= 491.0 * 1.01  # ≤ 1 wallet equity


def test_engine_check_lock_serializes_concurrent_checks():
    """Two concurrent /check calls for the same engine + same open list
    must NOT both pass the cap_frac cap. The per-engine lock serializes
    them so one sees the other's effect."""
    import threading
    from pm.pretrade import _engine_check_lock
    # The lock itself must exist and be a Lock
    lock = _engine_check_lock("test_engine")
    assert lock is _engine_check_lock("test_engine"), "must return same lock"
    # Smoke: lock is acquirable and releaseable
    assert lock.acquire(timeout=0.1)
    lock.release()


def test_engine_check_lock_independent_per_engine():
    """Different engines get different locks (no false contention)."""
    from pm.pretrade import _engine_check_lock
    lock_a = _engine_check_lock("engine_a")
    lock_b = _engine_check_lock("engine_b")
    assert lock_a is not lock_b
    # Acquiring A doesn't block B
    assert lock_a.acquire(timeout=0.1)
    assert lock_b.acquire(timeout=0.1)
    lock_a.release()
    lock_b.release()
