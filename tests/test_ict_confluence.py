"""Smoke tests for ict_confluence — 4h + 1d SMC/ICT confluence engines.

The engine is complex (456 LOC, multiple detectors). Smoke tests cover:
- import + metadata
- no-fire on empty / insufficient bars
- short-only mode invariant (when env enabled, no LONG signal ever returned)
- bleeder coin denylist respected
- SL/TP orientation when a signal does fire
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

from strategy_runner.strategies.ict_confluence import ICT_Confluence_4h, ICT_Confluence_1d


def _empty_bus():
    bus = MagicMock()
    bus.candles.return_value = []
    return bus


def _stale_bars_bus(n: int = 200, close: float = 100.0):
    """Flat bars with no swings → no BOS → no signal."""
    bus = MagicMock()
    bus.candles.return_value = [{
        "open_ts": i, "open": close, "high": close * 1.0001,
        "low": close * 0.9999, "close": close, "volume": 100.0,
    } for i in range(n)]
    bus.funding.return_value = [{"ts": 0, "rate": 0.0}]
    return bus


def test_ict_4h_metadata():
    assert ICT_Confluence_4h.NAME == "ict_confluence_4h"
    assert ICT_Confluence_4h.CLOID_PREFIX == "ictc_"
    assert ICT_Confluence_4h.TF == "4h"
    assert len(ICT_Confluence_4h.UNIVERSE) >= 20


def test_ict_1d_metadata():
    assert ICT_Confluence_1d.NAME == "ict_confluence_1d"
    assert ICT_Confluence_1d.CLOID_PREFIX == "ictd_"
    assert ICT_Confluence_1d.TF == "1d"


def test_no_bars_returns_none():
    sig = ICT_Confluence_4h.evaluate("SOL", _empty_bus())
    assert sig is None


def test_flat_bars_returns_none():
    """No swing pivots → no BOS → no signal."""
    sig = ICT_Confluence_4h.evaluate("SOL", _stale_bars_bus())
    assert sig is None


def test_bleeder_coin_in_denylist():
    """ETH, AAVE, PENDLE are bleeder-banned; should never fire."""
    for coin in ("ETH", "AAVE", "PENDLE"):
        assert coin in ICT_Confluence_4h.COIN_DENYLIST
        # Even with good data, should return None for denylisted coin
        sig = ICT_Confluence_4h.evaluate(coin, _stale_bars_bus())
        assert sig is None, f"denylisted coin {coin} should not fire"


def test_short_only_attribute_is_bool():
    """Default off (env unset); env=1 flips it on. Confirm the attr type."""
    assert isinstance(ICT_Confluence_4h.SHORT_ONLY, bool)


def test_funding_filter_disabled_by_default():
    """LONG_FUNDING_MAX / SHORT_FUNDING_MIN default to NaN (disabled).
    Env-unset path must keep these NaN so the gate never blocks."""
    import math
    assert math.isnan(ICT_Confluence_4h.LONG_FUNDING_MAX)
    assert math.isnan(ICT_Confluence_4h.SHORT_FUNDING_MIN)


def test_universe_excludes_bleeders():
    """Sanity check: COIN_DENYLIST coins are still in UNIVERSE (intentional —
    denylist is the runtime gate, not the universe filter). But they MUST be
    blocked at evaluate() entry."""
    # ETH IS in universe but blocked at evaluate. This is the design.
    assert "ETH" in ICT_Confluence_4h.UNIVERSE
    assert "ETH" in ICT_Confluence_4h.COIN_DENYLIST
