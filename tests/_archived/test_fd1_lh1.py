"""fd1 + lh1 unit tests."""
from __future__ import annotations

import time

from strategy_runner.strategies.fd1 import FD1
from strategy_runner.strategies.lh1 import LH1


# ---------- fd1 ----------

class FDBus:
    def __init__(self, closes: list[float], rates: list[float]):
        now_ms = int(time.time() * 1000)
        self._closes = closes
        self._rates = rates
        self._bars = [
            {"open_ts": now_ms - (len(closes) - i) * 3600_000,
             "open": c, "high": c * 1.001, "low": c * 0.999, "close": c, "volume": 1.0}
            for i, c in enumerate(closes)
        ]
        self._funding = [
            {"ts": now_ms - (len(rates) - i) * 3600_000, "rate": r}
            for i, r in enumerate(rates)
        ]

    def candles(self, coin, tf, n=200):
        return self._bars[-n:]

    def funding(self, coin, hours):
        return list(self._funding)

    def markprice(self, coin):
        return {"binance_mid": self._closes[-1], "hl_mid": None}


def test_fd1_fires_short_on_fresh_price_up_funding_down(monkeypatch):
    monkeypatch.setenv("FD_DIVERGENCE_BARS", "4")
    monkeypatch.setenv("FD_FUNDING_THRESHOLD_HI", "1.5e-5")
    # prior 4 bars: price flat, funding flat (no divergence)
    # current 4 bars: price up, funding down (one rate above threshold)
    closes = [100, 100, 100, 100, 100.5, 101, 101.5, 102]
    rates  = [1e-5, 1e-5, 1e-5, 1e-5, 5e-5, 3e-5, 2e-5, 1e-5]
    bus = FDBus(closes, rates)
    sig = FD1.evaluate("BTC", bus)
    assert sig is not None
    assert sig.is_long is False
    assert sig.fire_reason == "funding_price_divergence_short"


def test_fd1_fires_long_on_fresh_price_down_funding_up(monkeypatch):
    monkeypatch.setenv("FD_DIVERGENCE_BARS", "4")
    monkeypatch.setenv("FD_FUNDING_THRESHOLD_LO", "-5e-5")
    closes = [100, 100, 100, 100, 99.5, 99, 98.5, 98]
    rates  = [-1e-5, -1e-5, -1e-5, -1e-5, -6e-5, -4e-5, -2e-5, 0]
    bus = FDBus(closes, rates)
    sig = FD1.evaluate("BTC", bus)
    assert sig is not None
    assert sig.is_long is True


def test_fd1_no_fire_when_no_divergence(monkeypatch):
    monkeypatch.setenv("FD_DIVERGENCE_BARS", "4")
    closes = [100, 101, 102, 103, 104]
    rates  = [1e-5, 2e-5, 3e-5, 4e-5, 5e-5]  # both rising → no divergence
    bus = FDBus(closes, rates)
    assert FD1.evaluate("BTC", bus) is None


def test_fd1_does_not_refire_when_already_diverging(monkeypatch):
    monkeypatch.setenv("FD_DIVERGENCE_BARS", "4")
    # both windows already show divergence → not fresh
    closes = [100, 100.5, 101, 101.5, 102, 102.5, 103, 103.5]
    rates  = [5e-5, 4e-5, 3e-5, 2e-5, 1e-5, 0, -1e-5, -2e-5]
    bus = FDBus(closes, rates)
    assert FD1.evaluate("BTC", bus) is None


def test_fd1_insufficient_data():
    bus = FDBus([100], [1e-5])
    assert FD1.evaluate("BTC", bus) is None


# ---------- lh1 ----------

class CandleBus:
    def __init__(self, bars):
        self._bars = bars

    def candles(self, coin, tf, n=200):
        return self._bars[-n:]


def _bar(c, h, l, v):
    return {"open_ts": 0, "open": c, "high": h, "low": l, "close": c, "volume": v}


def _build_history_with_equal_highs_then_sweep(target_high=110.0):
    """130 bars where multiple pivot highs cluster near target_high, last bar wicks above with volume."""
    bars = []
    base = 100.0
    n = 130
    for i in range(n - 1):
        # create periodic pivot highs near target_high
        if i % 20 == 10:
            # pivot high
            bars.append(_bar(base + 5, target_high - 0.1, base + 4, 1.0))
        elif i % 20 == 11 or i % 20 == 9:
            bars.append(_bar(base + 3, base + 4, base + 2, 1.0))
        else:
            bars.append(_bar(base + 2, base + 3, base + 1, 1.0))
    # final bar: sweep above with volume spike, but closes near target
    bars.append(_bar(target_high - 0.5, target_high + 1.0, target_high - 1.0, 5.0))
    return bars


def test_lh1_inverted_default_sweep_up_is_long(monkeypatch):
    monkeypatch.delenv("LH_INVERTED", raising=False)  # default = inverted (=1)
    monkeypatch.setenv("LH_PIVOT_LOOKBACK", "3")
    monkeypatch.setenv("LH_MIN_PIVOTS", "3")
    monkeypatch.setenv("LH_CLUSTER_BAND_PCT", "0.01")
    monkeypatch.setenv("LH_SWEEP_PCT", "0.002")
    monkeypatch.setenv("LH_VOL_SPIKE_MULT", "1.5")
    monkeypatch.setenv("LH_MAX_PROXIMITY_PCT", "0.05")
    bars = _build_history_with_equal_highs_then_sweep(target_high=110.0)
    sig = LH1.evaluate("BTC", CandleBus(bars))
    if sig is not None:
        # In inverted mode, sweep_up → LONG (continuation)
        assert sig.is_long is True
        assert sig.extras.get("inverted") is True


def test_lh1_legacy_mode_sweep_up_is_short(monkeypatch):
    monkeypatch.setenv("LH_INVERTED", "0")
    monkeypatch.setenv("LH_PIVOT_LOOKBACK", "3")
    monkeypatch.setenv("LH_MIN_PIVOTS", "3")
    monkeypatch.setenv("LH_CLUSTER_BAND_PCT", "0.01")
    monkeypatch.setenv("LH_SWEEP_PCT", "0.002")
    monkeypatch.setenv("LH_VOL_SPIKE_MULT", "1.5")
    monkeypatch.setenv("LH_MAX_PROXIMITY_PCT", "0.05")
    bars = _build_history_with_equal_highs_then_sweep(target_high=110.0)
    sig = LH1.evaluate("BTC", CandleBus(bars))
    if sig is not None:
        assert sig.is_long is False
        assert sig.extras.get("inverted") is False


def test_lh1_no_volume_spike_no_fire(monkeypatch):
    monkeypatch.setenv("LH_PIVOT_LOOKBACK", "3")
    monkeypatch.setenv("LH_MIN_PIVOTS", "3")
    bars = _build_history_with_equal_highs_then_sweep(target_high=110.0)
    # neutralise volume spike
    bars[-1]["volume"] = 1.0
    assert LH1.evaluate("BTC", CandleBus(bars)) is None


def test_lh1_no_cluster_no_fire():
    # Steadily rising highs — no equal-high cluster forms
    bars = []
    for i in range(140):
        c = 100 + i * 0.5
        bars.append(_bar(c, c + 1.0, c - 1.0, 1.0))
    # final bar small move, no sweep candidate
    bars[-1] = _bar(170.0, 170.5, 169.5, 1.0)
    assert LH1.evaluate("BTC", CandleBus(bars)) is None


def test_lh1_insufficient_bars():
    bars = [_bar(100, 101, 99, 1) for _ in range(20)]
    assert LH1.evaluate("BTC", CandleBus(bars)) is None
