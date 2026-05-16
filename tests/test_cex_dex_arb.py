"""cex_dex_arb unit tests."""
from __future__ import annotations

import time

from strategy_runner.strategies.cex_dex_arb import CexDexArb


class MultiBus:
    def __init__(self, grouped: dict, mark: float = 100.0):
        self._g = grouped
        self._m = mark

    def funding_multi(self, coin, hours=12):
        return dict(self._g)

    def markprice(self, coin):
        return {"binance_mid": self._m, "hl_mid": self._m + 0.05}


def _rows(rate: float, n: int = 3) -> list[dict]:
    now = int(time.time() * 1000)
    return [{"ts": now - i * 3600_000, "rate": rate} for i in range(n)]


def test_short_hl_when_hl_premium(monkeypatch):
    monkeypatch.setenv("CDA_FUNDING_SPREAD_THR", "0.0001")
    grouped = {"hyperliquid": _rows(0.0005), "binance": _rows(0.0001),
               "okx": _rows(0.0001), "bybit": _rows(0.0001)}
    sig = CexDexArb.evaluate("BTC", MultiBus(grouped))
    assert sig is not None
    assert sig.is_long is False
    assert sig.extras["hedge_recommend"]["side"] == "long"


def test_long_hl_when_hl_discount(monkeypatch):
    monkeypatch.setenv("CDA_FUNDING_SPREAD_THR", "0.0001")
    grouped = {"hyperliquid": _rows(-0.0004), "binance": _rows(0.0001)}
    sig = CexDexArb.evaluate("BTC", MultiBus(grouped))
    assert sig is not None
    assert sig.is_long is True


def test_no_fire_below_threshold(monkeypatch):
    monkeypatch.setenv("CDA_FUNDING_SPREAD_THR", "0.0010")
    grouped = {"hyperliquid": _rows(0.0002), "binance": _rows(0.0001)}
    assert CexDexArb.evaluate("BTC", MultiBus(grouped)) is None


def test_no_hl_data_returns_none(monkeypatch):
    grouped = {"binance": _rows(0.001), "okx": _rows(0.002)}
    assert CexDexArb.evaluate("BTC", MultiBus(grouped)) is None


def test_empty_grouped_returns_none():
    assert CexDexArb.evaluate("BTC", MultiBus({})) is None


def test_picks_max_magnitude_cex(monkeypatch):
    """When multiple cex venues, choose the one with max |rate|."""
    monkeypatch.setenv("CDA_FUNDING_SPREAD_THR", "0.0001")
    grouped = {
        "hyperliquid": _rows(0.0001),
        "binance":     _rows(-0.0008),  # max-magnitude cex
        "okx":         _rows(0.0001),
    }
    sig = CexDexArb.evaluate("BTC", MultiBus(grouped))
    # spread = 0.0001 - (-0.0008) = 0.0009 > thr → SHORT HL
    assert sig is not None
    assert sig.is_long is False
