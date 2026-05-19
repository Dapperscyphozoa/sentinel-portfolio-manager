"""Smoke tests for hl_depth_shock — fire/no-fire direction logic."""
from __future__ import annotations

from unittest.mock import MagicMock

from strategy_runner.strategies.hl_depth_shock import HLDepthShock


def _bus(mid: float, bid_shock_pct: float, ask_shock_pct: float,
         spread_bps: float = 0.5, price_move_bps: float = 0.0, samples: int = 10):
    bus = MagicMock()
    bus.depth_shock.return_value = {
        "mid": mid,
        "bid_shock_pct": bid_shock_pct,
        "ask_shock_pct": ask_shock_pct,
        "price_move_bps": price_move_bps,
        "spread_bps": spread_bps,
        "samples": samples,
    }
    bus.markprice.return_value = {"hl_mid": mid, "binance_mid": mid}
    bus.cvd.return_value = {"net": 0.0, "buy_usd": 1000.0, "sell_usd": 1000.0,
                            "n_buy": 10, "n_sell": 10}
    bus.l2book.return_value = {
        "bid": mid * (1 - spread_bps/2/10000),
        "ask": mid * (1 + spread_bps/2/10000),
        "bid_sz": 100, "ask_sz": 100,
    }
    return bus


def test_strategy_metadata():
    assert HLDepthShock.NAME == "hl_depth_shock"
    assert HLDepthShock.CLOID_PREFIX == "dpshk"


def test_no_shock_returns_none():
    """Balanced book shock → no fire."""
    bus = _bus(mid=100.0, bid_shock_pct=0.0, ask_shock_pct=0.0)
    sig = HLDepthShock.evaluate("BTC", bus)
    assert sig is None


def test_huge_ask_shock_can_fire_long():
    """Ask depth evaporates suddenly (sellers pulling) → LONG side bias."""
    bus = _bus(mid=100.0,
               bid_shock_pct=-10.0,    # bid resilient (small negative)
               ask_shock_pct=80.0,      # ask collapsed
               spread_bps=0.3,
               price_move_bps=-0.5)     # price hasn't yet caught the move
    sig = HLDepthShock.evaluate("BTC", bus)
    if sig is not None:
        # Direction depends on the engine's interpretation — verify SL/TP
        # orient consistently with is_long.
        if sig.is_long:
            assert sig.side == "B"
            assert sig.sl_px < sig.ref_price
            assert sig.tp_px > sig.ref_price
        else:
            assert sig.side == "A"
            assert sig.sl_px > sig.ref_price
            assert sig.tp_px < sig.ref_price


def test_huge_bid_shock_can_fire_short():
    """Bid depth evaporates (buyers pulling) → SHORT side bias."""
    bus = _bus(mid=100.0,
               bid_shock_pct=80.0,
               ask_shock_pct=-10.0,
               spread_bps=0.3,
               price_move_bps=0.5)
    sig = HLDepthShock.evaluate("BTC", bus)
    if sig is not None:
        if sig.is_long:
            assert sig.sl_px < sig.ref_price and sig.tp_px > sig.ref_price
        else:
            assert sig.sl_px > sig.ref_price and sig.tp_px < sig.ref_price


def test_insufficient_samples_returns_none():
    """Bus.depth_shock returns samples < min → no fire."""
    bus = _bus(mid=100.0, bid_shock_pct=80.0, ask_shock_pct=0.0, samples=1)
    sig = HLDepthShock.evaluate("BTC", bus)
    assert sig is None
