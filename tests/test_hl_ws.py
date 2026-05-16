"""HL WS parser tests. No network."""
from __future__ import annotations

import os
import tempfile

from signal_bus import hl_ws
from signal_bus.cache import Cache


def test_on_user_fills_persists_and_buffers():
    with tempfile.TemporaryDirectory() as d:
        c = Cache(os.path.join(d, "t.db"))
        data = {
            "isSnapshot": False,
            "fills": [
                {"tid": "t1", "time": 1700000000000, "coin": "BTC", "side": "B",
                 "sz": "0.05", "px": "60000", "cloid": "0x" + "ab" * 16},
                {"tid": "t2", "time": 1700000001000, "coin": "ETH", "side": "A",
                 "sz": "1.0", "px": "3000", "cloid": None},
            ],
        }
        hl_ws._on_user_fills(c, data)
        assert len(c.hl_fills) == 2
        rows = c.db.execute("SELECT fill_id, coin, qty, price FROM hl_fills ORDER BY fill_id").fetchall()
        assert len(rows) == 2
        assert rows[0]["coin"] == "BTC"
        assert rows[0]["qty"] == 0.05


def test_on_user_fills_dedupes_on_fill_id():
    with tempfile.TemporaryDirectory() as d:
        c = Cache(os.path.join(d, "t.db"))
        data = {"fills": [{"tid": "x", "time": 1, "coin": "BTC", "side": "B", "sz": "1", "px": "100"}]}
        hl_ws._on_user_fills(c, data)
        hl_ws._on_user_fills(c, data)
        rows = c.db.execute("SELECT COUNT(*) AS n FROM hl_fills").fetchone()
        assert rows["n"] == 1


def test_on_webdata2_extracts_account_and_positions():
    with tempfile.TemporaryDirectory() as d:
        c = Cache(os.path.join(d, "t.db"))
        data = {
            "clearinghouseState": {
                "marginSummary": {"accountValue": "491.35", "totalMarginUsed": "100.0", "totalNtlPos": "500.0"},
                "withdrawable": "391.35",
                "assetPositions": [
                    {"position": {"coin": "BTC", "szi": "0.01", "entryPx": "60000",
                                  "unrealizedPnl": "5.0", "leverage": {"type": "cross", "value": 5}}},
                    {"position": {"coin": "ETH", "szi": "-0.5", "entryPx": "3000",
                                  "unrealizedPnl": "-2.0"}},
                ],
            }
        }
        hl_ws._on_webdata2(c, data)
        assert c.hl_account["value"] == 491.35
        assert c.hl_account["margin_used"] == 100.0
        assert len(c.hl_positions) == 2
        btc = next(p for p in c.hl_positions if p["coin"] == "BTC")
        assert btc["is_long"] is True
        eth = next(p for p in c.hl_positions if p["coin"] == "ETH")
        assert eth["is_long"] is False
        assert eth["szi"] == -0.5


def test_on_active_asset_ctx_sets_hl_mid_preserving_binance():
    with tempfile.TemporaryDirectory() as d:
        c = Cache(os.path.join(d, "t.db"))
        # seed binance mark
        c.push_mark("BTC", {"ts": 1, "binance_mid": 60000.0, "hl_mid": None})
        hl_ws._on_active_asset_ctx(c, {"coin": "BTC", "ctx": {"markPx": "60010"}})
        m = c.get_mark("BTC")
        assert m["binance_mid"] == 60000.0
        assert m["hl_mid"] == 60010.0
