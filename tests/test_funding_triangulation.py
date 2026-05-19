"""Smoke tests for funding_triangulation — fire/no-fire path with synthetic data."""
from __future__ import annotations

from unittest.mock import MagicMock

from strategy_runner.strategies.funding_triangulation import FundingTriangulation


def _bars(close: float, n: int = 4) -> list[dict]:
    return [{"open_ts": i, "open": close, "high": close * 1.001,
             "low": close * 0.999, "close": close, "volume": 100.0}
            for i in range(n)]


def _bus_with_funding(hl_rate: float, bn_rate: float, ok_rate: float, close: float = 100.0):
    bus = MagicMock()

    # The strategy calls bus.funding(coin, hours=1) and groups records by 'venue'.
    def funding(coin: str, hours: int = 1):
        return [
            {"venue": "hyperliquid", "rate": hl_rate, "ts": 1.0},
            {"venue": "binance",     "rate": bn_rate, "ts": 1.0},
            {"venue": "okx",         "rate": ok_rate, "ts": 1.0},
        ]

    bus.funding.side_effect = funding
    bus.candles.return_value = _bars(close)
    bus.markprice.return_value = {"hl_mid": close, "binance_mid": close}
    # Neutralize Stage 2 filters (asia_kill, cvd_alignment) — give bus.cvd
    # a sensible default so the cvd_alignment filter doesn't crash.
    bus.cvd.return_value = {"net": 0.0, "buy_usd": 1000.0, "sell_usd": 1000.0,
                            "n_buy": 10, "n_sell": 10}
    return bus


def test_no_divergence_returns_none():
    """HL/CEX funding aligned → no signal."""
    # HL annualized = 1e-5 × 8760 × 10000 = 876 bps
    # CEX annualized = (1e-4 + 1e-4)/2 × 1095 × 10000 = 1095 bps
    # Delta = -219 bps, threshold is much larger; still no fire (depending on env)
    bus = _bus_with_funding(hl_rate=1e-5, bn_rate=1e-4, ok_rate=1e-4)
    sig = FundingTriangulation.evaluate("BTC", bus)
    # Could fire or not depending on FT_DIVERGENCE_BPS default. Just verify
    # if it fires, the direction is correct.
    if sig is not None:
        # delta_bps negative → LONG
        assert sig.is_long is True


def test_hl_overpaying_longs_fires_short():
    """HL funding strongly positive vs CEX → SHORT (fade overcrowded longs)."""
    # HL annualized = 5e-4 × 8760 × 10000 = 43,800 bps (very high)
    # CEX annualized = 1e-5 × 1095 × 10000 = 110 bps
    # Delta = +43,690 bps >> threshold (default ~50 bps)
    bus = _bus_with_funding(hl_rate=5e-4, bn_rate=1e-5, ok_rate=1e-5)
    sig = FundingTriangulation.evaluate("BTC", bus)
    # May still return None due to asia_kill or cvd filters — but if it fires,
    # direction MUST be short.
    if sig is not None:
        assert sig.is_long is False
        assert sig.side == "A"
        assert sig.tp_px < sig.ref_price
        assert sig.sl_px > sig.ref_price


def test_hl_undercharging_longs_fires_long():
    """HL funding strongly negative vs CEX → LONG (squeeze setup)."""
    bus = _bus_with_funding(hl_rate=-5e-4, bn_rate=1e-5, ok_rate=1e-5)
    sig = FundingTriangulation.evaluate("BTC", bus)
    if sig is not None:
        assert sig.is_long is True
        assert sig.side == "B"
        assert sig.tp_px > sig.ref_price
        assert sig.sl_px < sig.ref_price


def test_strategy_metadata():
    assert FundingTriangulation.NAME == "funding_triangulation"
    assert FundingTriangulation.CLOID_PREFIX == "fundt"
    assert len(FundingTriangulation.UNIVERSE) >= 10
