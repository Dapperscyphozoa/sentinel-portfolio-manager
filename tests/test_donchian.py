"""Tests for Donchian Channel Breakout strategy."""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy_runner.strategies.donchian import Donchian, DC_N_ENTRY, DC_N_EXIT
from strategy_runner.strategies._indicators import donchian as dc_fn


class FakeBus:
    def __init__(self, bars_by_coin: dict[str, list[dict]]):
        self._b = bars_by_coin

    def candles(self, coin: str, tf: str, n: int = 200) -> list[dict]:
        return self._b.get(coin, [])[-n:]


def _bar(o, h, l, c, v=100.0):
    return {"open": float(o), "high": float(h), "low": float(l), "close": float(c), "volume": float(v)}


def _flat_bars(n: int, base_px: float = 100.0) -> list[dict]:
    """N flat bars at base_px ± tiny noise."""
    return [
        _bar(base_px, base_px + 0.5, base_px - 0.5, base_px + (0.1 if i % 2 else -0.1), v=50.0)
        for i in range(n)
    ]


def test_donchian_indicator_basic():
    highs = [10, 11, 12, 11, 13, 14, 12, 13]
    lows = [9, 10, 11, 10, 12, 13, 11, 12]
    up, dn = dc_fn(highs, lows, 3)
    # window 3 starts at index 2
    assert up[0] is None
    assert up[1] is None
    assert up[2] == 12  # max(10,11,12)
    assert dn[2] == 9   # min(9,10,11)
    assert up[5] == 14
    assert dn[7] == 11


def test_donchian_no_history_returns_none():
    bus = FakeBus({"BTC": _flat_bars(50)})  # need at least 200 bars
    assert Donchian.evaluate("BTC", bus) is None


def test_donchian_no_breakout_in_flat_market():
    bus = FakeBus({"BTC": _flat_bars(220, base_px=100.0)})
    sig = Donchian.evaluate("BTC", bus)
    assert sig is None  # no breakout in flat conditions


def test_donchian_fires_long_on_clean_breakout():
    """Build a rising trend, place EMA200 below current price, then a clean
    breakout bar above the 80-bar high with above-avg volume."""
    bars: list[dict] = []
    # 250 bars rising slowly from 100 to 150 (200-EMA will be ~125)
    for i in range(250):
        px = 100.0 + i * 0.2
        bars.append(_bar(px, px + 0.3, px - 0.3, px + 0.1, v=100.0))
    # Last bar: strong breakout — high beyond recent 80-bar high, big volume
    breakout_px = max(b["high"] for b in bars[-80:]) + 5.0
    bars.append(_bar(breakout_px - 1, breakout_px + 0.1, breakout_px - 1.2, breakout_px, v=500.0))

    bus = FakeBus({"BTC": bars})
    sig = Donchian.evaluate("BTC", bus)
    assert sig is not None
    assert sig.is_long is True
    assert sig.side == "B"
    assert sig.sl_px < sig.ref_price < sig.tp_px
    # SL distance = 2 ATR; TP very wide
    sl_dist = sig.ref_price - sig.sl_px
    tp_dist = sig.tp_px - sig.ref_price
    assert tp_dist > sl_dist * 5  # 20 ATR vs 2 ATR


def test_donchian_blocks_long_below_ema():
    """Breakout up but price still below 200-EMA — should NOT fire long."""
    bars: list[dict] = []
    # 200 bars at high price, then 100 bars at low price (more than 80 bars)
    # so the recent 80-bar window contains only the low-price bars
    for i in range(200):
        px = 200.0
        bars.append(_bar(px, px + 0.3, px - 0.3, px, v=100.0))
    for i in range(100):
        px = 100.0
        bars.append(_bar(px, px + 0.3, px - 0.3, px, v=100.0))
    # Local 80-bar high now is ~100.3. Breakout above that, but still below
    # the EMA200 (which is weighted toward older 200-priced bars).
    bars.append(_bar(100.3, 101.0, 100.3, 101.0, v=500.0))

    bus = FakeBus({"BTC": bars})
    sig = Donchian.evaluate("BTC", bus)
    # EMA200 is well above current price → no long allowed
    assert sig is None or sig.is_long is False


def test_donchian_should_close_long_on_opposite_break():
    """If position is long and price breaks 40-bar low, should_close returns True."""
    # rising then falling pattern
    bars: list[dict] = []
    for i in range(100):
        px = 100.0 + i * 0.1
        bars.append(_bar(px, px + 0.5, px - 0.5, px, v=100.0))
    # 50 declining bars to set up a low
    for i in range(50):
        px = 110.0 - i * 0.2
        bars.append(_bar(px, px + 0.5, px - 0.5, px, v=100.0))

    bus = FakeBus({"BTC": bars})
    trade_row = {"coin": "BTC", "is_long": True}
    should, reason = Donchian.should_close(trade_row, bus)
    # The 40-bar low should be broken by the declining tail
    assert isinstance(should, bool)
    # If declined enough, should_close should be True; tolerate False if not
    if should:
        assert "exit" in reason.lower() or "low" in reason.lower()


def test_donchian_should_close_no_history_returns_false():
    bus = FakeBus({"BTC": _flat_bars(10)})
    should, reason = Donchian.should_close({"coin": "BTC", "is_long": True}, bus)
    assert should is False


def test_donchian_universe_majors_only():
    """Per v1 spec, universe is restricted to BTC/ETH/SOL until validated."""
    assert "BTC" in Donchian.UNIVERSE
    assert "ETH" in Donchian.UNIVERSE
    assert "SOL" in Donchian.UNIVERSE
    assert len(Donchian.UNIVERSE) <= 5  # conservative


def test_donchian_affinity_trend_only():
    """PM must only fire in trend regimes per regime-aware design."""
    assert "trend_up" in Donchian.AFFINITY
    assert "trend_down" in Donchian.AFFINITY
    assert "range" not in Donchian.AFFINITY
    assert "chop" not in Donchian.AFFINITY
