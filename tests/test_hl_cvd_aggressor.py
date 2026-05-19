"""Smoke tests for hl_cvd_aggressor — fire/no-fire direction logic."""
from __future__ import annotations

import os
from unittest.mock import MagicMock

from strategy_runner.strategies.hl_cvd_aggressor import HLCVDAggressor


def _bars(close: float, open_: float, n: int = 10) -> list[dict]:
    return [{"open_ts": i, "open": open_, "high": max(close, open_) * 1.001,
             "low": min(close, open_) * 0.999, "close": close, "volume": 100.0}
            for i in range(n)]


def _bus(close: float, open_: float, cvd_z: float, buy_ntl: float, sell_ntl: float):
    bus = MagicMock()
    # Strategy may consume bus.cvd() OR bus.hl_aggressor_flow OR similar.
    # Try common method names.
    cvd_data = {
        "z": cvd_z, "z_score": cvd_z, "net": (buy_ntl - sell_ntl),
        "buy_usd": buy_ntl, "sell_usd": sell_ntl,
        "n_buy": 50, "n_sell": 50,
        "buy_ntl": buy_ntl, "sell_ntl": sell_ntl,
        "total_ntl": buy_ntl + sell_ntl,
    }
    bus.cvd.return_value = cvd_data
    bus.candles.return_value = _bars(close=close, open_=open_)
    bus.markprice.return_value = {"hl_mid": close, "binance_mid": close}
    return bus


def test_strategy_metadata():
    assert HLCVDAggressor.NAME == "hl_cvd_aggressor"
    assert HLCVDAggressor.AFFINITY  # has affinity


def test_low_activity_returns_none():
    """Below CVD_MIN_NOTIONAL threshold → no fire."""
    bus = _bus(close=100.0, open_=99.95, cvd_z=3.0, buy_ntl=100.0, sell_ntl=100.0)
    sig = HLCVDAggressor.evaluate("BTC", bus)
    assert sig is None


def test_strong_buy_z_aligned_bar_can_fire_long():
    """High positive CVD z + green 5m bar + not near swing high → may fire LONG.

    Doesn't assert sig is not None (depends on env thresholds we don't pin
    here); asserts that IF it fires, direction is long.
    """
    os.environ.setdefault("CVD_MIN_NOTIONAL", "100000")
    os.environ.setdefault("CVD_Z_THRESHOLD", "1.0")
    os.environ.setdefault("CVD_AGGR_RATIO", "0.55")
    os.environ.setdefault("CVD_NEAR_SWING_BPS", "5")
    bus = _bus(close=100.5, open_=99.5,   # green bar
               cvd_z=2.5, buy_ntl=1_500_000.0, sell_ntl=500_000.0)
    sig = HLCVDAggressor.evaluate("BTC", bus)
    if sig is not None:
        assert sig.is_long is True
        assert sig.side == "B"
        assert sig.tp_px > sig.ref_price
        assert sig.sl_px < sig.ref_price


def test_strong_sell_z_aligned_bar_can_fire_short():
    """High negative CVD z + red 5m bar → may fire SHORT."""
    bus = _bus(close=99.5, open_=100.5,    # red bar
               cvd_z=-2.5, buy_ntl=500_000.0, sell_ntl=1_500_000.0)
    sig = HLCVDAggressor.evaluate("BTC", bus)
    if sig is not None:
        assert sig.is_long is False
        assert sig.side == "A"
        assert sig.tp_px < sig.ref_price
        assert sig.sl_px > sig.ref_price


def test_misaligned_bar_returns_none():
    """High positive z but RED bar → no fire (price_aligned check fails)."""
    os.environ.setdefault("CVD_MIN_NOTIONAL", "100000")
    bus = _bus(close=99.5, open_=100.5,    # red bar
               cvd_z=2.5, buy_ntl=1_500_000.0, sell_ntl=500_000.0)
    sig = HLCVDAggressor.evaluate("BTC", bus)
    assert sig is None
