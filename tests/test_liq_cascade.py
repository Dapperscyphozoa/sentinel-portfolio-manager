"""liq_cascade unit tests."""
from __future__ import annotations

import time

from strategy_runner.strategies.liq_cascade import LiqCascade


class LiqBus:
    def __init__(self, liqs: list[dict], mark: float = 60000.0):
        self._liqs = liqs
        self._mark = mark

    def liq(self, since_ms=None, coin=None):
        out = self._liqs
        if since_ms is not None:
            out = [e for e in out if e["ts"] >= since_ms]
        if coin is not None:
            out = [e for e in out if e["coin"] == coin]
        return out

    def markprice(self, coin):
        return {"binance_mid": self._mark, "hl_mid": None}


def _ev(ts_ago_s: float, side: str, usd: float, coin: str = "BTC") -> dict:
    return {
        "ts": int((time.time() - ts_ago_s) * 1000),
        "coin": coin, "side": side,
        "qty": usd / 60000.0, "price": 60000.0, "usd": usd,
    }


def test_fires_long_on_long_liq_cascade(monkeypatch):
    monkeypatch.setenv("LC_WINDOW_SEC", "60")
    monkeypatch.setenv("LC_MIN_USD", "100000")
    monkeypatch.setenv("LC_MIN_EVENTS", "3")
    # cluster of large SELL (long-liq) events in current window
    liqs = [_ev(10, "SELL", 50_000), _ev(20, "SELL", 60_000), _ev(30, "SELL", 50_000)]
    sig = LiqCascade.evaluate("BTC", LiqBus(liqs))
    assert sig is not None
    assert sig.is_long is True
    assert sig.fire_reason == "long_liq_cascade_fade"


def test_fires_short_on_short_liq_cascade(monkeypatch):
    monkeypatch.setenv("LC_WINDOW_SEC", "60")
    monkeypatch.setenv("LC_MIN_USD", "100000")
    monkeypatch.setenv("LC_MIN_EVENTS", "3")
    liqs = [_ev(5, "BUY", 80_000), _ev(15, "BUY", 60_000), _ev(25, "BUY", 50_000)]
    sig = LiqCascade.evaluate("BTC", LiqBus(liqs))
    assert sig is not None
    assert sig.is_long is False


def test_does_not_fire_below_min_usd(monkeypatch):
    monkeypatch.setenv("LC_WINDOW_SEC", "60")
    monkeypatch.setenv("LC_MIN_USD", "1000000")
    monkeypatch.setenv("LC_MIN_EVENTS", "3")
    liqs = [_ev(10, "SELL", 50_000), _ev(20, "SELL", 60_000), _ev(30, "SELL", 50_000)]
    assert LiqCascade.evaluate("BTC", LiqBus(liqs)) is None


def test_does_not_fire_below_min_events(monkeypatch):
    monkeypatch.setenv("LC_WINDOW_SEC", "60")
    monkeypatch.setenv("LC_MIN_USD", "100000")
    monkeypatch.setenv("LC_MIN_EVENTS", "5")
    liqs = [_ev(10, "SELL", 200_000), _ev(20, "SELL", 200_000)]
    assert LiqCascade.evaluate("BTC", LiqBus(liqs)) is None


def test_freshness_blocks_already_cascading(monkeypatch):
    monkeypatch.setenv("LC_WINDOW_SEC", "60")
    monkeypatch.setenv("LC_MIN_USD", "100000")
    monkeypatch.setenv("LC_MIN_EVENTS", "3")
    # both prior and current have cascade → not fresh
    liqs = [
        _ev(70, "SELL", 60_000), _ev(80, "SELL", 60_000), _ev(90, "SELL", 60_000),
        _ev(10, "SELL", 60_000), _ev(20, "SELL", 60_000), _ev(30, "SELL", 60_000),
    ]
    assert LiqCascade.evaluate("BTC", LiqBus(liqs)) is None


def test_dominant_side_wins(monkeypatch):
    monkeypatch.setenv("LC_WINDOW_SEC", "60")
    monkeypatch.setenv("LC_MIN_USD", "100000")
    monkeypatch.setenv("LC_MIN_EVENTS", "3")
    # mixed; SELL dominant → long signal
    liqs = [
        _ev(10, "SELL", 80_000), _ev(20, "SELL", 80_000), _ev(30, "SELL", 80_000),
        _ev(40, "BUY", 30_000),
    ]
    sig = LiqCascade.evaluate("BTC", LiqBus(liqs))
    assert sig is not None and sig.is_long is True
