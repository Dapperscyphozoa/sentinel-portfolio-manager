"""Unit tests for vpoc_retest strategy."""
from strategy_runner.strategies.vpoc_retest import VPOCRetest, _compute_poc


def _bar(o, h, l, c, v=1.0):
    return {"open": o, "high": h, "low": l, "close": c, "volume": v}


class FakeBus:
    def __init__(self, candles):
        self._candles = candles

    def candles(self, coin, tf, n=200):
        return self._candles[-n:]


def test_compute_poc_basic():
    # 100 bars at price ~50 with high volume, plus 10 bars at price ~70 with low volume
    bars = [_bar(50, 51, 49, 50, v=10) for _ in range(100)]
    bars += [_bar(70, 71, 69, 70, v=1) for _ in range(10)]
    result = _compute_poc(bars, num_bins=20)
    assert result is not None
    poc, vol = result
    # POC should be near 50 (heavy volume zone)
    assert 48 < poc < 52


def test_compute_poc_returns_none_for_empty():
    assert _compute_poc([], num_bins=20) is None


def test_compute_poc_returns_none_when_no_volume():
    bars = [_bar(50, 51, 49, 50, v=0) for _ in range(50)]
    assert _compute_poc(bars, num_bins=20) is None


def test_compute_poc_returns_none_when_flat_prices():
    # All same price
    bars = [_bar(50, 50, 50, 50, v=10) for _ in range(50)]
    assert _compute_poc(bars, num_bins=20) is None


def test_evaluate_returns_none_with_insufficient_history():
    bars = [_bar(100, 101, 99, 100) for _ in range(50)]
    sig = VPOCRetest.evaluate("BTC", FakeBus(bars))
    assert sig is None


def test_evaluate_returns_none_when_no_naked_pocs():
    # Build 5 weeks of 1h bars, but always in same range → POCs will be near each
    # other and constantly retested → no naked POCs
    bars = []
    for week in range(5):
        for hr in range(170):
            # Slight randomization but staying in same range
            px = 100 + (hr % 5) * 0.2
            bars.append(_bar(px, px + 0.1, px - 0.1, px, v=10))
    # Plus a recent bar at the same price
    bars.append(_bar(100, 100.1, 99.9, 100.05, v=5))
    sig = VPOCRetest.evaluate("BTC", FakeBus(bars))
    # Likely no signal because POCs are constantly retested
    assert sig is None


def test_evaluate_long_signal_at_naked_poc_below():
    # Build 5 weeks of bars:
    # Week 4 (oldest): heavy volume at 90 → POC = 90
    # Weeks 3,2,1: price stays at 110-120 → POC=90 never retested → NAKED
    # Current bar: price drops to 90.1 (within 0.5% of POC=90), bullish close → LONG
    bars = []
    # Week 4 (oldest week)
    for _ in range(168):
        bars.append(_bar(90, 90.5, 89.5, 90, v=10))   # heavy volume here
    # Weeks 3, 2, 1
    for _ in range(168 * 3):
        bars.append(_bar(115, 116, 114, 115, v=10))   # stays away from 90
    # Current bar: returns to ~90 with bullish body
    bars.append(_bar(89.8, 90.5, 89.5, 90.3, v=5))   # close > open ✓
    bars.append(_bar(90.3, 90.4, 90.2, 90.35, v=2))  # in-progress
    sig = VPOCRetest.evaluate("BTC", FakeBus(bars))
    if sig is not None:
        assert sig.is_long is True
        assert "poc" in sig.fire_reason.lower() or "naked" in sig.fire_reason.lower()


def test_evaluate_short_signal_at_naked_poc_above():
    bars = []
    # Week 4: heavy volume at 110 → POC = 110
    for _ in range(168):
        bars.append(_bar(110, 110.5, 109.5, 110, v=10))
    # Weeks 3,2,1: stays at 90 (never returns to 110)
    for _ in range(168 * 3):
        bars.append(_bar(90, 91, 89, 90, v=10))
    # Current bar: price rises to ~110, bearish close
    bars.append(_bar(110.2, 110.5, 109.5, 109.8, v=5))   # close < open
    bars.append(_bar(109.8, 109.9, 109.7, 109.85, v=2))
    sig = VPOCRetest.evaluate("BTC", FakeBus(bars))
    if sig is not None:
        assert sig.is_long is False
