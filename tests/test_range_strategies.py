"""range_fade + range_breakout unit tests."""
from __future__ import annotations

import time

from strategy_runner.strategies.range_fade import RangeFade
from strategy_runner.strategies.range_breakout import RangeBreakout


class CandleBus:
    def __init__(self, bars: list[dict]):
        self._bars = bars

    def candles(self, coin, tf, n=200):
        return self._bars[-n:]

    def markprice(self, coin):
        return {"binance_mid": self._bars[-1]["close"], "hl_mid": None}


def _bars(closes, vols=None, highs=None, lows=None):
    out = []
    n = len(closes)
    if vols is None:
        vols = [1.0] * n
    for i, c in enumerate(closes):
        h = highs[i] if highs else c * 1.001
        l = lows[i] if lows else c * 0.999
        out.append({"open_ts": i * 60000, "open": c, "high": h, "low": l, "close": c, "volume": vols[i]})
    return out


# -------- range_fade --------

def test_range_fade_fires_long_on_oversold_at_bb_lower():
    # Construct closes: 20 bars at ~100 with mild noise, then a sharp dump
    closes = [100 + (i % 3 - 1) * 0.5 for i in range(25)] + [97.0, 95.5, 94.0, 92.0]
    bus = CandleBus(_bars(closes))
    sig = RangeFade.evaluate("SOL", bus)
    assert sig is not None
    assert sig.is_long is True


def test_range_fade_fires_short_on_overbought_at_bb_upper():
    closes = [100 + (i % 3 - 1) * 0.5 for i in range(25)] + [103.0, 105.5, 108.0, 110.0]
    bus = CandleBus(_bars(closes))
    sig = RangeFade.evaluate("SOL", bus)
    assert sig is not None
    assert sig.is_long is False


def test_range_fade_quiet_market_no_fire():
    closes = [100.0 + (i % 2) * 0.1 for i in range(40)]
    bus = CandleBus(_bars(closes))
    assert RangeFade.evaluate("SOL", bus) is None


# -------- range_breakout --------

def test_range_breakout_fires_long_on_break_up_with_volume():
    # 48-bar compressed range ~100 ± 1.5% then break up with vol spike
    N = 75
    closes = [100.0 + (i % 4 - 1.5) * 0.3 for i in range(N)]
    closes[-1] = 102.0  # breakout above range_high
    highs = [c * 1.002 for c in closes]
    lows = [c * 0.998 for c in closes]
    vols = [1.0] * N
    vols[-1] = 5.0  # vol spike
    bus = CandleBus(_bars(closes, vols=vols, highs=highs, lows=lows))
    sig = RangeBreakout.evaluate("BTC", bus)
    assert sig is not None
    assert sig.is_long is True


def test_range_breakout_blocked_when_volume_normal():
    N = 75
    closes = [100.0 + (i % 4 - 1.5) * 0.3 for i in range(N)]
    closes[-1] = 102.0
    highs = [c * 1.002 for c in closes]
    lows = [c * 0.998 for c in closes]
    vols = [1.0] * N
    vols[-1] = 1.1  # no spike
    bus = CandleBus(_bars(closes, vols=vols, highs=highs, lows=lows))
    assert RangeBreakout.evaluate("BTC", bus) is None


def test_range_breakout_blocked_when_range_too_wide():
    # range > 4% — strategy refuses
    closes = list(range(95, 115)) * 4 + [120.0]  # 81 bars, range > 4%
    vols = [1.0] * len(closes)
    vols[-1] = 10.0
    bus = CandleBus(_bars([float(x) for x in closes], vols=vols))
    assert RangeBreakout.evaluate("BTC", bus) is None


def test_range_breakout_universe_is_majors_only():
    assert RangeBreakout.UNIVERSE == ["BTC", "ETH", "SOL", "XRP", "BNB"]
