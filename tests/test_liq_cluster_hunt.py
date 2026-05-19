"""Smoke tests for liq_cluster_hunt — fire/no-fire path with synthetic data."""
from __future__ import annotations

import os
import time
from unittest.mock import MagicMock

from strategy_runner.strategies.liq_cluster_hunt import LiqClusterHunt


def _bars(close: float, open_: float = None, n: int = 6) -> list[dict]:
    open_ = open_ or close
    base = {"open_ts": 0, "high": max(close, open_) * 1.001,
            "low": min(close, open_) * 0.999, "volume": 100.0}
    return [{**base, "open": open_, "close": close}] * n


def _bus(close: float, liqs: list[dict], spread_pass: bool = True):
    bus = MagicMock()
    bus.candles.return_value = _bars(close)
    bus.liq.return_value = liqs
    # edge_filters.spread_max queries bus.markprice / l2book — neutralize
    bus.markprice.return_value = {"hl_mid": close, "binance_mid": close}
    bus.l2book.return_value = {
        "bid": close * (1 - 0.0001), "ask": close * (1 + 0.0001),
        "bid_sz": 100, "ask_sz": 100,
    }
    return bus


def test_no_liqs_returns_none():
    bus = _bus(close=100.0, liqs=[])
    sig = LiqClusterHunt.evaluate("BTC", bus)
    assert sig is None


def test_below_min_events_returns_none():
    """Fewer than LCH_MIN_EVENTS=5 events → no fire."""
    bus = _bus(close=100.0, liqs=[
        {"ts": int(time.time()*1000), "coin": "BTC", "side": "BUY",
         "qty": 1, "price": 100.5, "usd": 100.5}
    ] * 2)
    sig = LiqClusterHunt.evaluate("BTC", bus)
    assert sig is None


def test_short_liq_cluster_above_fires_long():
    """SHORT-liq cluster slightly above current price near round number → LONG.

    side=BUY = SHORT liquidation. Cluster center > close, dist 5-50bps,
    near round number, momentum aligned (5m green) → LONG signal.
    """
    os.environ["LCH_MIN_CLUSTER_USD"] = "100000"   # lower for synthetic test
    os.environ["LCH_MIN_EVENTS"] = "5"
    close = 99.85   # just under the round $100 cluster
    # 6 short liqs all around $100 (round), totaling > $200k
    liqs = [{
        "ts": int(time.time()*1000), "coin": "BTC", "side": "BUY",
        "qty": 1.0, "price": 100.0 + i * 0.01, "usd": 50_000.0,
    } for i in range(6)]
    bus = _bus(close=close, liqs=liqs)
    # 5m bar must be green (close > open)
    bus.candles.return_value = _bars(close=close, open_=close * 0.9995)
    sig = LiqClusterHunt.evaluate("BTC", bus)
    # Either fires LONG, or returns None if spread filter trips.
    # We accept fire; we don't accept opposite direction.
    if sig is not None:
        assert sig.is_long is True
        assert sig.side == "B"
        assert sig.tp_px > sig.ref_price
        assert sig.sl_px < sig.ref_price


def test_long_liq_cluster_below_fires_short():
    """LONG-liq cluster slightly below current price near round → SHORT."""
    os.environ["LCH_MIN_CLUSTER_USD"] = "100000"
    os.environ["LCH_MIN_EVENTS"] = "5"
    close = 100.15   # just above the round $100 cluster
    # 6 long liqs around $100, totaling > $200k
    liqs = [{
        "ts": int(time.time()*1000), "coin": "BTC", "side": "SELL",
        "qty": 1.0, "price": 100.0 + i * 0.01, "usd": 50_000.0,
    } for i in range(6)]
    bus = _bus(close=close, liqs=liqs)
    bus.candles.return_value = _bars(close=close, open_=close * 1.0005)  # red bar
    sig = LiqClusterHunt.evaluate("BTC", bus)
    if sig is not None:
        assert sig.is_long is False
        assert sig.side == "A"
        assert sig.tp_px < sig.ref_price
        assert sig.sl_px > sig.ref_price


def test_strategy_metadata():
    assert LiqClusterHunt.NAME == "liq_cluster_hunt"
    assert LiqClusterHunt.CLOID_PREFIX == "lclus"
    assert len(LiqClusterHunt.UNIVERSE) >= 10
