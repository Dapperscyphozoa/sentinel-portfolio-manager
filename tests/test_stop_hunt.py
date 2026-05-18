"""Unit tests for stop_hunt strategy."""
from strategy_runner.strategies.stop_hunt import StopHunt


def _bar(o, h, l, c, v=1.0):
    return {"open": o, "high": h, "low": l, "close": c, "volume": v}


class FakeBus:
    def __init__(self, candles):
        self._candles = candles

    def candles(self, coin, tf, n=200):
        return self._candles[-n:]


def test_evaluate_none_with_insufficient_history():
    sig = StopHunt.evaluate("BTC", FakeBus([_bar(100, 101, 99, 100) for _ in range(10)]))
    assert sig is None


def test_evaluate_long_signal_on_clean_sweep_low():
    # 50 prior bars in 100-102 range; swing low = 100
    prior = [_bar(101, 102, 100, 101) for _ in range(50)]
    # Sweep bar: opens 100.5, dips to 99.5 (sweep!), closes 101 (back inside)
    # body = 0.5, wick_below = 100-99.5 = 0.5, bar_range = 102-99.5 = 2.5
    # wick_pct = 0.5/2.5 = 0.2 — TOO LOW. Adjust.
    sweep_bar = _bar(100.5, 101.0, 98.5, 100.8)
    # bar_range = 101-98.5 = 2.5, wick_below = 100-98.5 = 1.5, wick_pct = 0.6 ✓
    # body = 100.8-100.5 = 0.3, |close-open| > 0 ✓, close > open ✓
    in_progress = _bar(100.8, 100.9, 100.7, 100.85)
    sig = StopHunt.evaluate("BTC", FakeBus(prior + [sweep_bar, in_progress]))
    assert sig is not None
    assert sig.is_long is True
    assert sig.side == "B"
    assert sig.sl_px < 98.5  # SL below sweep low
    assert sig.tp_px > sig.ref_price
    assert "sweep_low" in sig.fire_reason


def test_evaluate_short_signal_on_clean_sweep_high():
    # Prior bars in 100-102 range; swing high = 102
    prior = [_bar(101, 102, 100, 101) for _ in range(50)]
    # Sweep bar: wicks to 103.5 (above swing high), closes back at 101.2
    # bar_range = 103.5-101 = 2.5, wick_above = 103.5-102 = 1.5, wick_pct = 0.6 ✓
    # body = 101.5-101.2 = 0.3, close < open ✓
    sweep_bar = _bar(101.5, 103.5, 101.0, 101.2)
    in_progress = _bar(101.2, 101.3, 101.1, 101.15)
    sig = StopHunt.evaluate("BTC", FakeBus(prior + [sweep_bar, in_progress]))
    assert sig is not None
    assert sig.is_long is False
    assert sig.side == "A"
    assert sig.sl_px > 103.5
    assert sig.tp_px < sig.ref_price
    assert "sweep_high" in sig.fire_reason


def test_evaluate_none_when_no_sweep():
    # Bars stay within range; no sweep beyond swing levels
    prior = [_bar(101, 102, 100, 101) for _ in range(50)]
    normal_bar = _bar(101, 101.5, 100.5, 101.2)
    in_progress = _bar(101.2, 101.3, 101.1, 101.25)
    sig = StopHunt.evaluate("BTC", FakeBus(prior + [normal_bar, in_progress]))
    assert sig is None


def test_evaluate_none_when_wick_too_small():
    # Sweep exists but wick is small fraction of bar
    prior = [_bar(101, 102, 100, 101) for _ in range(50)]
    # Big body, small wick below swing low
    sweep_bar = _bar(101, 101.1, 99.8, 100.0)  # wick = 100-99.8 = 0.2, range = 1.3
    # wick_pct = 0.2/1.3 = 0.15 < 0.5 → no signal
    in_progress = _bar(100.0, 100.1, 99.9, 100.05)
    sig = StopHunt.evaluate("BTC", FakeBus(prior + [sweep_bar, in_progress]))
    assert sig is None


def test_evaluate_none_when_bar_closes_outside_swing():
    # Sweep but bar closes BELOW swing low (full breakdown, not stop hunt)
    prior = [_bar(101, 102, 100, 101) for _ in range(50)]
    breakdown_bar = _bar(101, 101.2, 99, 99.2)  # closes below 100
    in_progress = _bar(99.2, 99.3, 99.1, 99.15)
    sig = StopHunt.evaluate("BTC", FakeBus(prior + [breakdown_bar, in_progress]))
    assert sig is None


def test_evaluate_none_when_doji_body():
    # Tiny body (essentially doji) — disqualify
    prior = [_bar(101, 102, 100, 101) for _ in range(50)]
    # close ≈ open, body < 10bps
    doji_bar = _bar(101.0, 101.5, 98.5, 101.005)
    in_progress = _bar(101.005, 101.01, 100.99, 101.0)
    sig = StopHunt.evaluate("BTC", FakeBus(prior + [doji_bar, in_progress]))
    assert sig is None
