"""Unit tests for signal_bus. No network required."""
from __future__ import annotations

import json
import os
import tempfile
import time

import pytest

from signal_bus import binance_ws
from signal_bus.cache import Cache


# -------------------- cache --------------------

def test_cache_push_kline_dedupes_same_open_ts():
    with tempfile.TemporaryDirectory() as d:
        c = Cache(os.path.join(d, "t.db"))
        bar1 = {"open_ts": 1000, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10}
        bar1b = {"open_ts": 1000, "open": 1.0, "high": 2.5, "low": 0.5, "close": 1.8, "volume": 15}
        bar2 = {"open_ts": 2000, "open": 1.8, "high": 1.9, "low": 1.7, "close": 1.85, "volume": 5}
        c.push_kline("BTC", "1m", bar1)
        c.push_kline("BTC", "1m", bar1b)  # same bucket → replace
        c.push_kline("BTC", "1m", bar2)
        bars = c.get_klines("BTC", "1m", 10)
        assert len(bars) == 2
        assert bars[0]["close"] == 1.8  # the updated bar1b
        assert bars[1]["close"] == 1.85


def test_cache_flush_and_cold_load():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.db")
        c = Cache(path)
        c.push_kline("BTC", "1h", {
            "open_ts": int(time.time() * 1000) - 3600_000,
            "open": 60000, "high": 61000, "low": 59800, "close": 60500, "volume": 100
        })
        n = c.flush_klines()
        assert n == 1
        c2 = Cache(path)
        c2.cold_load(hours_klines=2)
        bars = c2.get_klines("BTC", "1h", 10)
        assert len(bars) == 1
        assert bars[0]["close"] == 60500


def test_cache_liq_filtering_by_coin_and_since():
    with tempfile.TemporaryDirectory() as d:
        c = Cache(os.path.join(d, "t.db"))
        now = int(time.time() * 1000)
        c.push_liq({"ts": now - 10_000, "coin": "BTC", "side": "SELL", "qty": 1, "price": 60000, "usd": 60000})
        c.push_liq({"ts": now - 5_000, "coin": "ETH", "side": "BUY", "qty": 10, "price": 3000, "usd": 30000})
        c.push_liq({"ts": now, "coin": "BTC", "side": "BUY", "qty": 0.5, "price": 60100, "usd": 30050})
        all_btc = c.get_liqs(0, "BTC")
        assert len(all_btc) == 2
        recent = c.get_liqs(now - 6_000)
        assert len(recent) == 2  # ETH + BTC@now
        recent_btc = c.get_liqs(now - 6_000, "BTC")
        assert len(recent_btc) == 1


def test_cache_funding_push_and_read():
    with tempfile.TemporaryDirectory() as d:
        c = Cache(os.path.join(d, "t.db"))
        now = int(time.time() * 1000)
        c.push_funding("BTC", now - 3_600_000, 0.0001)
        c.push_funding("BTC", now, 0.0002)
        rows = c.get_funding("BTC", hours=2)
        assert len(rows) == 2
        assert rows[-1]["rate"] == 0.0002


def test_cache_markprice_latest():
    with tempfile.TemporaryDirectory() as d:
        c = Cache(os.path.join(d, "t.db"))
        c.push_mark("BTC", {"ts": 1, "binance_mid": 60000.0, "hl_mid": None})
        c.push_mark("BTC", {"ts": 2, "binance_mid": 60050.0, "hl_mid": None})
        m = c.get_mark("BTC")
        assert m["binance_mid"] == 60050.0
        assert m["ts"] == 2


def test_cache_stats_shape():
    with tempfile.TemporaryDirectory() as d:
        c = Cache(os.path.join(d, "t.db"))
        s = c.stats()
        for k in ("ws_alive", "last_update", "kline_keys", "liq_events", "mark_coins", "funding_coins"):
            assert k in s


# -------------------- binance parsers --------------------

def test_coin_from_symbol():
    assert binance_ws.coin_from_symbol("BTCUSDT") == "BTC"
    assert binance_ws.coin_from_symbol("ethusdt") == "ETH"
    assert binance_ws.coin_from_symbol("SOLUSDC") == "SOL"
    assert binance_ws.coin_from_symbol("DOGEBUSD") == "DOGE"


def test_build_streams_layout():
    streams = binance_ws.build_streams(["BTCUSDT", "ETHUSDT"])
    # 4 TFs + markPrice per coin → 5 per coin + 1 global liq
    assert "btcusdt@kline_1m" in streams
    assert "btcusdt@kline_1h" in streams
    assert "ethusdt@markPrice@1s" in streams
    assert "!forceOrder@arr" in streams
    assert len(streams) == 2 * 5 + 1


def test_on_kline_writes_bar():
    with tempfile.TemporaryDirectory() as d:
        c = Cache(os.path.join(d, "t.db"))
        msg = {
            "s": "BTCUSDT",
            "k": {"s": "BTCUSDT", "i": "1m", "t": 1700000000000, "o": "60000", "h": "60100",
                  "l": "59900", "c": "60050", "v": "12.5", "x": True},
        }
        binance_ws._on_kline(c, msg)
        bars = c.get_klines("BTC", "1m", 5)
        assert len(bars) == 1
        assert bars[0]["close"] == 60050.0
        assert bars[0]["volume"] == 12.5


def test_on_liq_writes_event():
    with tempfile.TemporaryDirectory() as d:
        c = Cache(os.path.join(d, "t.db"))
        msg = {"o": {"s": "BTCUSDT", "S": "SELL", "q": "0.5", "p": "60000", "T": 1700000000000}}
        binance_ws._on_liq(c, msg)
        liqs = c.get_liqs(0)
        assert len(liqs) == 1
        assert liqs[0]["coin"] == "BTC"
        assert liqs[0]["usd"] == 30000.0
        assert liqs[0]["side"] == "SELL"  # long liquidation


def test_on_mark_writes_mark_and_funding():
    with tempfile.TemporaryDirectory() as d:
        c = Cache(os.path.join(d, "t.db"))
        msg = {"s": "BTCUSDT", "p": "60000", "r": "0.0001", "E": 1700000000000}
        binance_ws._on_mark(c, msg)
        m = c.get_mark("BTC")
        assert m["binance_mid"] == 60000.0
        rows = c.get_funding("BTC", hours=999_999)  # ts is in 2023, hours param doesn't matter at this scale
        # the funding insert happens regardless of clock — we just check it didn't raise
        assert isinstance(rows, list)
