"""precog unit tests."""
from __future__ import annotations

import time

import pytest

from strategy_runner.strategies import precog as precog_mod
from strategy_runner.strategies.precog import Precog


class MarkBus:
    def __init__(self, mark=100.0):
        self._m = mark

    def markprice(self, coin):
        return {"binance_mid": self._m, "hl_mid": self._m + 0.1}


@pytest.fixture(autouse=True)
def _reset_queue():
    with precog_mod._LOCK:
        precog_mod._QUEUE.clear()
    yield


def test_precog_no_pending_returns_none():
    assert Precog.evaluate("BTC", MarkBus()) is None


def test_precog_consumes_queued_long(monkeypatch):
    monkeypatch.setenv("PRECOG_MIN_CONFIDENCE", "0.5")
    precog_mod.enqueue("BTC", {"side": "LONG", "confidence": 0.8})
    sig = Precog.evaluate("BTC", MarkBus(60000.0))
    assert sig is not None
    assert sig.is_long is True
    assert sig.extras["source"] == "precog_webhook"


def test_precog_consumes_queued_short(monkeypatch):
    monkeypatch.setenv("PRECOG_MIN_CONFIDENCE", "0.5")
    precog_mod.enqueue("ETH", {"side": "SHORT", "confidence": 0.7})
    sig = Precog.evaluate("ETH", MarkBus(3000.0))
    assert sig is not None
    assert sig.is_long is False


def test_precog_rejects_low_confidence(monkeypatch):
    monkeypatch.setenv("PRECOG_MIN_CONFIDENCE", "0.65")
    precog_mod.enqueue("BTC", {"side": "LONG", "confidence": 0.5})
    assert Precog.evaluate("BTC", MarkBus()) is None


def test_precog_uses_payload_sl_tp_when_present(monkeypatch):
    monkeypatch.setenv("PRECOG_MIN_CONFIDENCE", "0.5")
    precog_mod.enqueue("BTC", {
        "side": "LONG", "confidence": 0.9,
        "ref_price": 100.0, "sl_px": 95.0, "tp_px": 110.0,
    })
    sig = Precog.evaluate("BTC", MarkBus(100.0))
    assert sig is not None
    assert sig.sl_px == 95.0
    assert sig.tp_px == 110.0


def test_precog_drops_stale_events(monkeypatch):
    monkeypatch.setenv("PRECOG_MIN_CONFIDENCE", "0.5")
    monkeypatch.setenv("PRECOG_MAX_AGE_SEC", "1")
    precog_mod.enqueue("BTC", {"side": "LONG", "confidence": 0.9})
    # mark as old
    with precog_mod._LOCK:
        precog_mod._QUEUE["BTC"][0]["ts"] = time.time() - 10
    assert Precog.evaluate("BTC", MarkBus()) is None


def test_precog_queue_pops_one_per_call(monkeypatch):
    monkeypatch.setenv("PRECOG_MIN_CONFIDENCE", "0.5")
    precog_mod.enqueue("BTC", {"side": "LONG", "confidence": 0.9})
    precog_mod.enqueue("BTC", {"side": "SHORT", "confidence": 0.9})
    s1 = Precog.evaluate("BTC", MarkBus())
    s2 = Precog.evaluate("BTC", MarkBus())
    s3 = Precog.evaluate("BTC", MarkBus())
    assert s1 is not None and s1.is_long is True
    assert s2 is not None and s2.is_long is False
    assert s3 is None
