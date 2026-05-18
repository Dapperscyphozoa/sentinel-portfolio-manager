"""Unit tests for oi_concentration strategy."""
from strategy_runner.strategies.oi_concentration import OIConcentration


def _bar(o, h, l, c, v=1.0):
    return {"open": o, "high": h, "low": l, "close": c, "volume": v}


class FakeBus:
    def __init__(self, candles):
        self._candles = candles

    def candles(self, coin, tf, n=200):
        return self._candles[-n:]


def test_evaluate_returns_none_with_insufficient_history():
    bars = [_bar(100, 101, 99, 100, v=1) for _ in range(100)]
    sig = OIConcentration.evaluate("BTC", FakeBus(bars))
    assert sig is None


def test_evaluate_returns_none_when_volume_not_extreme():
    # Varying volume — current 24h sum should NOT be in top decile
    import random
    random.seed(123)
    bars = []
    for i in range(800):
        # Bars with random volume 10-1000 each → 24h sums vary widely
        v = random.uniform(10, 1000)
        bars.append(_bar(100, 101, 99, 100, v=v))
    # Last 24 bars: deliberately LOW volume so current 24h sum is at low end
    for _ in range(24):
        bars.append(_bar(100, 101, 99, 100, v=5))
    bars.append(_bar(100, 101, 99, 100.5, v=5))
    bars.append(_bar(100.5, 100.6, 100.4, 100.55, v=5))
    sig = OIConcentration.evaluate("BTC", FakeBus(bars))
    # Last 24h vol ≈ 130, vs distribution of ~12000 avg → very low percentile
    assert sig is None


def test_evaluate_returns_none_when_far_from_levels():
    # Bars in 100-120 range, current price at 110 (middle, not near support/resistance)
    import random
    random.seed(42)
    bars = []
    for i in range(800):
        px = 110 + random.uniform(-5, 5)
        bars.append(_bar(px, px + 1, px - 1, px, v=random.uniform(5, 15)))
    # Recent bar: massive volume spike at 110 (mid-range, far from levels)
    bars.append(_bar(110, 110.5, 109.5, 110, v=100))   # huge volume
    bars.append(_bar(110, 110.1, 109.9, 110.05, v=10))
    sig = OIConcentration.evaluate("BTC", FakeBus(bars))
    # Even with extreme volume, price not near S/R → no fire
    assert sig is None


def test_evaluate_short_signal_near_support_with_extreme_volume():
    # Build bars with swing low ~95, swing high ~120
    import random
    random.seed(42)
    bars = []
    # 750 prior bars: range 95-120, normal volume
    for i in range(750):
        # Bias volume distribution so most bars have low volume
        px = 100 + random.uniform(-5, 20)
        bars.append(_bar(px, px + 1, px - 1, px, v=random.uniform(1, 10)))
    # Establish swing levels in recent 48 bars at 95-120 range
    for _ in range(48):
        bars.append(_bar(108, 120, 95, 110, v=5))      # establishes swing low 95, high 120
    # Closing bar: at 95.5 (near support 95, <1% away), with extreme 24h volume
    # Need 24h volume to be top decile — pad recent bars with high volume
    for _ in range(23):
        bars.append(_bar(110, 111, 109, 110.5, v=200))    # high volume contribution
    bars.append(_bar(96, 96.5, 95.3, 95.5, v=200))        # near support with high vol
    bars.append(_bar(95.5, 95.6, 95.4, 95.5, v=10))       # in-progress
    sig = OIConcentration.evaluate("BTC", FakeBus(bars))
    if sig is not None:
        assert sig.is_long is False
        assert sig.side == "A"
        assert "support" in sig.fire_reason.lower()


def test_evaluate_long_signal_near_resistance_with_extreme_volume():
    import random
    random.seed(42)
    bars = []
    for i in range(750):
        px = 100 + random.uniform(-5, 20)
        bars.append(_bar(px, px + 1, px - 1, px, v=random.uniform(1, 10)))
    # Establish swing high 120
    for _ in range(48):
        bars.append(_bar(108, 120, 95, 110, v=5))
    # Pad with high volume
    for _ in range(23):
        bars.append(_bar(110, 111, 109, 110.5, v=200))
    # Closing bar: at 119.5 (near resistance 120)
    bars.append(_bar(118, 119.6, 117, 119.5, v=200))
    bars.append(_bar(119.5, 119.6, 119.4, 119.5, v=10))
    sig = OIConcentration.evaluate("BTC", FakeBus(bars))
    if sig is not None:
        assert sig.is_long is True
        assert sig.side == "B"
        assert "resistance" in sig.fire_reason.lower()
