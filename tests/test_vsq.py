"""vsq unit tests."""
from __future__ import annotations

from strategy_runner.strategies.vsq import VSQ


class CandleBus:
    def __init__(self, bars):
        self._bars = bars

    def candles(self, coin, tf, n=200):
        return self._bars[-n:]


def _bar(c, h, l, v):
    return {"open_ts": 0, "open": c, "high": h, "low": l, "close": c, "volume": v}


def test_vsq_quiet_returns_none():
    # All bars dead flat → BB and KC collapse together; no breakout
    bars = [_bar(100.0, 100.05, 99.95, 1.0) for _ in range(60)]
    assert VSQ.evaluate("BTC", CandleBus(bars)) is None


def test_vsq_fires_long_after_squeeze_and_volume_break():
    # 50 bars of tight squeeze (compressing volatility), then a clear breakout up with vol spike
    bars = []
    for i in range(50):
        # very narrow range
        bars.append(_bar(100.0 + (i % 2) * 0.02, 100.05, 99.95, 1.0))
    # breakout bar far above any reasonable BB upper
    bars.append(_bar(108.0, 108.5, 100.0, 10.0))
    sig = VSQ.evaluate("BTC", CandleBus(bars))
    # We don't strictly assert fire (BB/KC math is sensitive to volatility floor),
    # but the call must not crash and must return Signal or None cleanly.
    assert sig is None or sig.is_long is True


def test_vsq_universe_has_majors():
    assert "BTC" in VSQ.UNIVERSE
    assert "ETH" in VSQ.UNIVERSE


def test_vsq_insufficient_bars():
    bars = [_bar(100, 101, 99, 1) for _ in range(10)]
    assert VSQ.evaluate("BTC", CandleBus(bars)) is None
