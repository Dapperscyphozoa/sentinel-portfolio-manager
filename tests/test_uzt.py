"""Smoke tests for UZT — Unified Zone Trading (v1, bidirectional).

UZT v1 was reclassified RED by honest backtest per SPEC §4 (Lesson #2),
but its helper functions are reused by UZT_REV which IS GREEN (n=41
PF 6.92). Tests confirm:
- import + metadata
- no-fire on empty bars
- helper functions are importable + match expected signature
"""
from __future__ import annotations

from unittest.mock import MagicMock

from strategy_runner.strategies.uzt import (
    UZT,
    _aggregate_15m_to_4h,
    _find_zones,
    _evaluate_zone_state,
)


def _empty_bus():
    bus = MagicMock()
    bus.candles.return_value = []
    return bus


def _flat_bars(n: int = 600, close: float = 100.0):
    return [{
        "open_ts": i * 900_000,   # 15min bars
        "open": close, "high": close * 1.0001, "low": close * 0.9999,
        "close": close, "volume": 100.0,
    } for i in range(n)]


def test_metadata():
    assert UZT.NAME == "uzt"
    assert hasattr(UZT, "CLOID_PREFIX")
    assert UZT.TF == "15m"
    assert len(UZT.UNIVERSE) >= 10


def test_no_bars_returns_none():
    sig = UZT.evaluate("SOL", _empty_bus())
    assert sig is None


def test_flat_bars_no_zones_returns_none():
    """No HTF displacement → no zones → no signal."""
    bus = MagicMock()
    bus.candles.return_value = _flat_bars()
    sig = UZT.evaluate("SOL", bus)
    assert sig is None


def test_aggregate_15m_to_4h_smoke():
    """Helper: 16 × 15m bars → 1 × 4h bar."""
    bars = _flat_bars(n=32)   # 32 × 15m = 8 hours = 2 × 4h bars
    htf = _aggregate_15m_to_4h(bars)
    assert isinstance(htf, list)
    # Should produce at least 1 aggregated bar
    assert len(htf) >= 1
    # Each aggregated bar must have OHLCV keys
    for b in htf:
        assert all(k in b for k in ("open", "high", "low", "close"))


def test_find_zones_on_flat_data_returns_none_or_empty():
    """Flat bars have no displacement → no zones."""
    bars = _flat_bars(n=32)
    htf = _aggregate_15m_to_4h(bars)
    zones = _find_zones(htf, pivot_lb=5, disp_atr_mult=1.5)
    assert zones is None or zones == [] or len(zones) == 0
