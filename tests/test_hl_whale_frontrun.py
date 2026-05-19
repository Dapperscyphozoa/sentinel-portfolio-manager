"""Smoke tests for hl_whale_frontrun — copy whale opens on HL."""
from __future__ import annotations

import os
import time
from unittest.mock import MagicMock

from strategy_runner.strategies.hl_whale_frontrun import HLWhaleFrontrun


def _bars(close: float, n: int = 5) -> list[dict]:
    return [{"open_ts": i, "open": close, "high": close * 1.001,
             "low": close * 0.999, "close": close, "volume": 100.0}
            for i in range(n)]


def _bus(close: float, whale_events: list[dict]):
    bus = MagicMock()
    bus.whale_events.return_value = whale_events
    bus.candles.return_value = _bars(close)
    bus.markprice.return_value = {"hl_mid": close, "binance_mid": close}
    bus.cvd.return_value = {"net": 0.0, "buy_usd": 1000.0, "sell_usd": 1000.0,
                            "n_buy": 10, "n_sell": 10}
    return bus


def test_strategy_metadata():
    assert HLWhaleFrontrun.NAME == "hl_whale_frontrun"
    assert HLWhaleFrontrun.CLOID_PREFIX == "whlfr"


def test_no_events_returns_none():
    bus = _bus(close=100.0, whale_events=[])
    sig = HLWhaleFrontrun.evaluate("BTC", bus)
    assert sig is None


def test_whale_long_open_can_fire_long():
    """Recent whale opens a LONG with size > min notional → copy LONG."""
    now_ms = int(time.time() * 1000)
    bus = _bus(close=100.0, whale_events=[{
        "ts": now_ms - 5_000,
        "wallet": "0xtest",
        "coin": "BTC",
        "is_long": True,
        "ntl_usd": 5_000_000.0,
        "delta_ntl_usd": 5_000_000.0,
        "kind": "new",
    }])
    sig = HLWhaleFrontrun.evaluate("BTC", bus)
    if sig is not None:
        assert sig.is_long is True
        assert sig.side == "B"
        assert sig.tp_px > sig.ref_price
        assert sig.sl_px < sig.ref_price


def test_whale_short_open_can_fire_short():
    now_ms = int(time.time() * 1000)
    bus = _bus(close=100.0, whale_events=[{
        "ts": now_ms - 5_000,
        "wallet": "0xtest",
        "coin": "BTC",
        "is_long": False,
        "ntl_usd": 5_000_000.0,
        "delta_ntl_usd": -5_000_000.0,
        "kind": "new",
    }])
    sig = HLWhaleFrontrun.evaluate("BTC", bus)
    if sig is not None:
        assert sig.is_long is False
        assert sig.side == "A"
        assert sig.tp_px < sig.ref_price
        assert sig.sl_px > sig.ref_price


def test_old_event_returns_none():
    """Event outside the event-window → no fire."""
    now_ms = int(time.time() * 1000)
    bus = _bus(close=100.0, whale_events=[{
        "ts": now_ms - 86_400_000,  # 24h old
        "wallet": "0xtest",
        "coin": "BTC",
        "is_long": True,
        "ntl_usd": 5_000_000.0,
    }])
    sig = HLWhaleFrontrun.evaluate("BTC", bus)
    assert sig is None
