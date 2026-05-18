"""Tests for hl_settle_5m — verify settlement boundary logic + maker-only gate."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from strategy_runner.strategies.hl_settle_5m import HLSettle5m, _minutes_to_settle


class FakeBus:
    """Minimal bus impl for unit tests."""

    def __init__(self, funding_rate: float = 1e-4, hl_mid: float = 100.0,
                 candle_range: float = 0.001, funding_ts: int | None = None):
        self.funding_rate = funding_rate
        self.hl_mid = hl_mid
        self.candle_range = candle_range  # high-low/mid for spread check
        self.funding_ts = funding_ts

    def funding(self, coin: str, hours: int) -> list[dict]:
        ts = self.funding_ts if self.funding_ts is not None else int(time.time() * 1000)
        return [{"ts": ts, "rate": self.funding_rate}]

    def markprice(self, coin: str) -> dict:
        return {"hl_mid": self.hl_mid, "binance_mid": self.hl_mid}

    def candles(self, coin: str, tf: str, n: int = 5) -> list[dict]:
        # tight bar with known high-low for spread calc
        mid = self.hl_mid
        return [{
            "open_ts": 0, "open": mid, "high": mid * (1 + self.candle_range / 2),
            "low": mid * (1 - self.candle_range / 2), "close": mid, "volume": 100.0
        }] * n


def _ts_at_minute_offset(min_offset: int) -> int:
    """Build a UTC timestamp at exactly :MM:00 of the current hour."""
    now = datetime.now(timezone.utc).replace(microsecond=0)
    # Set to exactly minute=min_offset of the current hour
    target = now.replace(minute=min_offset % 60, second=0)
    return int(target.timestamp() * 1000)


def test_minutes_to_settle_at_55min_returns_to_next_5():
    # 5 minutes before next hour
    ts = _ts_at_minute_offset(55)
    to_next, since_last = _minutes_to_settle(ts)
    assert to_next == 5
    assert since_last == 55


def test_minutes_to_settle_at_10min_returns_post_10():
    ts = _ts_at_minute_offset(10)
    to_next, since_last = _minutes_to_settle(ts)
    assert to_next == 50
    assert since_last == 10


def test_evaluate_returns_none_when_funding_too_small():
    """Rate below HL_SETTLE_FUNDING_MIN_ABS (1e-5) → no trade."""
    bus = FakeBus(funding_rate=5e-7,  # too tiny
                  funding_ts=_ts_at_minute_offset(56))  # in pre window
    sig = HLSettle5m.evaluate("BTC", bus)
    assert sig is None


def test_evaluate_returns_none_outside_settle_windows():
    """Not in pre-settle (T-5min) or post-settle (T+5 to T+30) → no trade."""
    # T+40min: outside both windows
    bus = FakeBus(funding_rate=1e-4, funding_ts=_ts_at_minute_offset(40))
    sig = HLSettle5m.evaluate("BTC", bus)
    assert sig is None


def test_evaluate_returns_none_when_spread_too_wide_maker_only():
    """Maker-only mode: skip if 1m candle range > HL_SETTLE_SPREAD_BPS_MAX (5bps default)."""
    bus = FakeBus(funding_rate=1e-4,
                  funding_ts=_ts_at_minute_offset(56),  # in pre window
                  candle_range=0.005)  # 50bps range >> 5bps gate
    sig = HLSettle5m.evaluate("BTC", bus)
    assert sig is None


def test_evaluate_fires_pre_settle_with_positive_funding_goes_short():
    """funding > 0 (longs pay) in PRE-settle → mechanical longs close → SHORT
    
    Per engine logic: in PRE window, trade WITH mechanical direction.
    funding < 0 → shorts pay → buying pressure → LONG.
    funding > 0 → longs pay → selling pressure → SHORT.
    """
    bus = FakeBus(funding_rate=1e-4,  # longs pay
                  funding_ts=_ts_at_minute_offset(56),  # T-4min
                  candle_range=0.0001)  # 1bp range, well below 5bps gate
    sig = HLSettle5m.evaluate("BTC", bus)
    assert sig is not None
    assert sig.is_long is False  # SHORT
    assert sig.side == "A"
    assert "pre_T" in sig.fire_reason
    assert sig.extras["settle_phase"] == "pre"


def test_evaluate_fires_post_settle_with_positive_funding_goes_long():
    """funding > 0 in POST-settle → mechanical sold has overshot → BUY the dip = LONG.
    
    Per engine logic: in POST window, trade AGAINST mechanical direction.
    """
    bus = FakeBus(funding_rate=1e-4,  # longs pay
                  funding_ts=_ts_at_minute_offset(10),  # T+10min
                  candle_range=0.0001)
    sig = HLSettle5m.evaluate("BTC", bus)
    assert sig is not None
    assert sig.is_long is True  # LONG (post-settle reverse)
    assert sig.side == "B"
    assert "post_T+" in sig.fire_reason
    assert sig.extras["settle_phase"] == "post"


def test_evaluate_sl_tp_correctly_oriented_for_short():
    bus = FakeBus(funding_rate=1e-4, funding_ts=_ts_at_minute_offset(56),
                  candle_range=0.0001)
    sig = HLSettle5m.evaluate("BTC", bus)
    assert sig is not None
    assert sig.is_long is False
    # SHORT: SL above entry, TP below
    assert sig.sl_px > sig.ref_price
    assert sig.tp_px < sig.ref_price


def test_evaluate_sl_tp_correctly_oriented_for_long():
    bus = FakeBus(funding_rate=-1e-4, funding_ts=_ts_at_minute_offset(56),
                  candle_range=0.0001)
    sig = HLSettle5m.evaluate("BTC", bus)
    assert sig is not None
    assert sig.is_long is True
    # LONG: SL below entry, TP above
    assert sig.sl_px < sig.ref_price
    assert sig.tp_px > sig.ref_price
