"""fsp unit tests with a mock bus. Exercises SPEC §3.1 entry/exit conditions."""
from __future__ import annotations

import os
import time

import pytest

from strategy_runner.strategies.fsp import FSP


class MockBus:
    def __init__(self, funding_rates: list[float], mark: float = 100.0):
        # rates oldest→newest
        now = int(time.time() * 1000)
        self._funding = [{"ts": now - (len(funding_rates) - i) * 3600_000, "rate": r}
                         for i, r in enumerate(funding_rates)]
        self._mark = mark

    def funding(self, coin, hours):
        return list(self._funding)

    def markprice(self, coin):
        return {"binance_mid": self._mark, "hl_mid": self._mark}


def test_fsp_fires_long_on_fresh_sustained_negative(monkeypatch):
    monkeypatch.setenv("FSP_F_NEG", "0.0003")
    monkeypatch.setenv("FSP_F_POS", "0.0003")
    monkeypatch.setenv("FSP_CONSEC", "3")
    # latest 3 (window) all ≤ -0.0003; prior 3 NOT all ≤ -0.0003
    # rates len=6, window=rates[-3:]=[-.0004,-.0005,-.0006], prior=rates[-4:-1]=[0,0,-.0004] → not all neg
    bus = MockBus([0.0, 0.0, 0.0, -0.0004, -0.0005, -0.0006])
    sig = FSP.evaluate("BTC", bus)
    assert sig is not None
    assert sig.is_long is True
    assert sig.side == "B"


def test_fsp_fires_short_on_fresh_sustained_positive(monkeypatch):
    monkeypatch.setenv("FSP_F_NEG", "0.0003")
    monkeypatch.setenv("FSP_F_POS", "0.0003")
    monkeypatch.setenv("FSP_CONSEC", "3")
    bus = MockBus([0.0, 0.0, 0.0, 0.0004, 0.0005, 0.0006])
    sig = FSP.evaluate("BTC", bus)
    assert sig is not None
    assert sig.is_long is False
    assert sig.side == "A"


def test_fsp_does_not_refire_when_already_sustained(monkeypatch):
    monkeypatch.setenv("FSP_F_NEG", "0.0003")
    monkeypatch.setenv("FSP_F_POS", "0.0003")
    monkeypatch.setenv("FSP_CONSEC", "3")
    # All 4 readings ≤ -0.0003 → prior window also already triggers → no fresh entry
    bus = MockBus([-0.0005, -0.0005, -0.0005, -0.0005])
    sig = FSP.evaluate("BTC", bus)
    assert sig is None


def test_fsp_does_not_fire_below_threshold(monkeypatch):
    monkeypatch.setenv("FSP_F_NEG", "0.0003")
    monkeypatch.setenv("FSP_F_POS", "0.0003")
    monkeypatch.setenv("FSP_CONSEC", "3")
    bus = MockBus([0.0, 0.0, -0.0001, -0.0001, -0.0001])
    assert FSP.evaluate("BTC", bus) is None


def test_fsp_returns_none_with_insufficient_data():
    bus = MockBus([-0.001])
    assert FSP.evaluate("BTC", bus) is None


def test_fsp_sl_tp_levels(monkeypatch):
    monkeypatch.setenv("FSP_TP_PCT", "0.030")
    monkeypatch.setenv("FSP_SL_PCT", "0.010")
    monkeypatch.setenv("FSP_F_NEG", "0.0003")
    monkeypatch.setenv("FSP_CONSEC", "3")
    bus = MockBus([0.0, 0.0, 0.0, -0.0004, -0.0005, -0.0006], mark=100.0)
    sig = FSP.evaluate("BTC", bus)
    assert sig is not None
    # long: tp above, sl below
    assert abs(sig.tp_px - 103.0) < 1e-6
    assert abs(sig.sl_px - 99.0) < 1e-6


def test_fsp_registry_membership():
    from strategy_runner import runner
    runner._load_registered()
    names = [s.NAME for s in runner.REGISTRY]
    assert "fsp" in names
