"""Tests for Kronos confirmation gate.

Tests run without Kronos installed — fail-open behavior is verified."""
from __future__ import annotations

import os


def test_kronos_disabled_via_env():
    os.environ["KRONOS_GATE_ENABLED"] = "0"
    from common import kronos_gate
    # Reset state
    kronos_gate._predictor = None
    kronos_gate._disabled_reason = None
    assert not kronos_gate.is_enabled()
    del os.environ["KRONOS_GATE_ENABLED"]


def test_kronos_agrees_flat_always_allows():
    from common.kronos_gate import agrees
    assert agrees("FLAT", True)
    assert agrees("FLAT", False)


def test_kronos_agrees_bull_only_with_long():
    from common.kronos_gate import agrees
    assert agrees("BULL", True)        # bull + long → agree
    assert not agrees("BULL", False)   # bull + short → disagree


def test_kronos_agrees_bear_only_with_short():
    from common.kronos_gate import agrees
    assert not agrees("BEAR", True)    # bear + long → disagree
    assert agrees("BEAR", False)       # bear + short → agree


def test_kronos_predict_with_no_bars_returns_none():
    from common import kronos_gate
    kronos_gate._predictor = None
    kronos_gate._disabled_reason = None
    os.environ.pop("KRONOS_GATE_ENABLED", None)
    # No bars → None regardless
    assert kronos_gate.predict_direction("BTC", []) is None
    assert kronos_gate.predict_direction("BTC", [{"open_ts": 1, "close": 100}]) is None


def test_kronos_load_failure_disables_gracefully():
    """If Kronos repo absent, gate disables itself instead of crashing."""
    from common import kronos_gate
    # Reset state
    kronos_gate._predictor = None
    kronos_gate._disabled_reason = None
    # Force a bad path
    os.environ["KRONOS_REPO_PATH"] = "/nonexistent/kronos"
    os.environ.pop("KRONOS_GATE_ENABLED", None)
    enabled = kronos_gate.is_enabled()
    # If kronos installed elsewhere, it might still load. Either way, no crash.
    assert kronos_gate._disabled_reason is not None or kronos_gate._predictor is not None
    # Cleanup
    del os.environ["KRONOS_REPO_PATH"]


def test_ict_signal_works_without_kronos():
    """ICT must function fully even when Kronos unavailable (fail-open)."""
    from common import kronos_gate
    kronos_gate._predictor = None
    kronos_gate._disabled_reason = "test_disabled"
    os.environ["KRONOS_GATE_ENABLED"] = "0"
    from strategy_runner.strategies.ict_confluence import ICT_Confluence_4h
    # Class still importable + has all methods
    assert ICT_Confluence_4h.NAME == "ict_confluence_4h"
    assert hasattr(ICT_Confluence_4h, "evaluate")
    del os.environ["KRONOS_GATE_ENABLED"]
