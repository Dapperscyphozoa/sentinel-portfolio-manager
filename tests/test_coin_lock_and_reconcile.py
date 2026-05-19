"""Tests for coin-lock integrity and trade-row reconciliation bugs.

Covers the failure mode that produced 10 duplicate APT shorts in production
on 2026-05-17.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import pytest

from common import persistence


def _fresh_db():
    p = tempfile.mktemp(suffix=".db")
    return p, persistence.init_db(p)


def _insert_trade(conn, *, cloid, coin, status, open_ts=None, max_hold_bars=8,
                   strategy="test", open_px=100.0, size_coin=0.1):
    conn.execute(
        "INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,open_px,"
        "size_usd,size_coin,sl_px,tp_px,max_hold_bars,status,extras_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cloid, strategy, coin, "A", 0, open_ts or time.time(), open_px,
         10.0, size_coin, 101.0, 99.0, max_hold_bars, status, '{"tf":"4h"}'),
    )


# ─── coin lock at DB level ────────────────────────────────────────────────
def test_partial_unique_index_blocks_duplicate_open():
    _, conn = _fresh_db()
    _insert_trade(conn, cloid="a1", coin="APT", status="open")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_trade(conn, cloid="a2", coin="APT", status="open")


def test_partial_unique_index_blocks_pending_when_open_exists():
    _, conn = _fresh_db()
    _insert_trade(conn, cloid="a1", coin="APT", status="open")
    with pytest.raises(sqlite3.IntegrityError):
        _insert_trade(conn, cloid="a2", coin="APT", status="pending")


def test_partial_unique_index_allows_after_closed():
    _, conn = _fresh_db()
    _insert_trade(conn, cloid="a1", coin="APT", status="open")
    conn.execute("UPDATE trades SET status='closed' WHERE cloid='a1'")
    _insert_trade(conn, cloid="a2", coin="APT", status="open")  # OK


# ─── integrity check ──────────────────────────────────────────────────────
def test_verify_integrity_raises_if_index_missing():
    _, conn = _fresh_db()
    conn.execute("DROP INDEX idx_trades_open_coin_lock")
    with pytest.raises(RuntimeError, match="coin lock is NOT enforced"):
        persistence.verify_integrity(conn)


def test_verify_integrity_raises_if_duplicates_present():
    """Simulate a DB that somehow has dupes AND the index — should halt."""
    _, conn = _fresh_db()
    conn.execute("DROP INDEX idx_trades_open_coin_lock")
    _insert_trade(conn, cloid="a1", coin="APT", status="open")
    _insert_trade(conn, cloid="a2", coin="APT", status="open")  # works w/o index
    # Recreate index loosely (won't actually install with dupes, but mock it)
    conn.execute(
        "CREATE UNIQUE INDEX idx_trades_open_coin_lock_tmp "
        "ON trades(coin) WHERE status='closed'"
    )
    # Rename to expected name by drop+create — but real SQLite blocks dupe on real index;
    # we just verify the dupe-detect branch fires.
    conn.execute("DROP INDEX idx_trades_open_coin_lock_tmp")
    conn.execute(
        "CREATE INDEX idx_trades_open_coin_lock ON trades(coin) WHERE status IN ('open','pending')"
    )
    with pytest.raises(RuntimeError, match="malformed|duplicate"):
        persistence.verify_integrity(conn)


def test_init_db_idempotent_recovery_from_duplicates():
    """If a DB exists with duplicate open rows (legacy DB), init_db demotes
    older ones so the index can install."""
    p = tempfile.mktemp(suffix=".db")
    # Phase 1: open without the index (legacy schema simulation)
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE trades (
        id INTEGER PRIMARY KEY, cloid TEXT UNIQUE, strategy TEXT, coin TEXT,
        side TEXT, is_long INT, open_ts REAL, open_px REAL, size_usd REAL,
        size_coin REAL, sl_px REAL, tp_px REAL, max_hold_bars INT,
        status TEXT, close_retries INT DEFAULT 0, extras_json TEXT)""")
    for i in range(10):
        c.execute(
            "INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,"
            "max_hold_bars,status) VALUES(?,?,?,?,?,?,?,?)",
            (f"a{i}", "ict_confluence_4h", "APT", "A", 0, 1779000000 + i*300,
             8, "open"),
        )
    c.commit(); c.close()
    # Phase 2: init_db should demote 9, keep 1, install index, verify OK
    conn = persistence.init_db(p)
    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM trades WHERE coin='APT' AND status='open'"
    ).fetchone()
    assert remaining["n"] == 1
    demoted = conn.execute(
        "SELECT COUNT(*) AS n FROM trades WHERE coin='APT' "
        "AND status='reconciled_off_book'"
    ).fetchone()
    assert demoted["n"] == 9
    # And the index now enforces
    with pytest.raises(sqlite3.IntegrityError):
        _insert_trade(conn, cloid="new", coin="APT", status="open")
    os.unlink(p)


# ─── reconciler ───────────────────────────────────────────────────────────
class _MockBus:
    def __init__(self, hl_positions=None, markprice=None):
        self._hl = hl_positions or []
        self._mark = markprice or {}

    def hl_positions(self):
        return self._hl

    def markprice(self, coin):
        return self._mark.get(coin.upper(), {"hl_mid": 1.0})


def _make_trader_with_bus(conn, bus):
    """Build a minimal Trader without going through real PMClient/HLExchange."""
    from strategy_runner.trader import Trader
    class _PM:
        def register_cloid(self, *a, **kw): pass
    return Trader(conn, bus, _PM(), hl=None)


def test_reconcile_releases_ghost_locks():
    """Local 'open' trade for APT but HL says no APT position → reconcile."""
    _, conn = _fresh_db()
    old_ts = time.time() - 7200  # 2h old, past the 5min safety window
    _insert_trade(conn, cloid="ghost", coin="APT", status="open", open_ts=old_ts)
    bus = _MockBus(hl_positions=[
        {"coin": "BTC", "size_coin": 0.001},  # APT NOT present
    ])
    trader = _make_trader_with_bus(conn, bus)
    n = trader.reconcile_with_hl()
    assert n == 1
    row = conn.execute("SELECT status FROM trades WHERE cloid='ghost'").fetchone()
    assert row["status"] == "reconciled_off_book"


def test_reconcile_skips_recently_opened():
    """Don't reconcile rows < 5min old (HL WS lag tolerance)."""
    _, conn = _fresh_db()
    _insert_trade(conn, cloid="new", coin="APT", status="open",
                  open_ts=time.time() - 60)
    bus = _MockBus(hl_positions=[])
    trader = _make_trader_with_bus(conn, bus)
    assert trader.reconcile_with_hl() == 0
    row = conn.execute("SELECT status FROM trades WHERE cloid='new'").fetchone()
    assert row["status"] == "open"


def test_reconcile_leaves_alive_positions_alone():
    _, conn = _fresh_db()
    _insert_trade(conn, cloid="live", coin="APT", status="open",
                  open_ts=time.time() - 7200)
    bus = _MockBus(hl_positions=[{"coin": "APT", "size_coin": -0.5}])
    trader = _make_trader_with_bus(conn, bus)
    assert trader.reconcile_with_hl() == 0
    row = conn.execute("SELECT status FROM trades WHERE cloid='live'").fetchone()
    assert row["status"] == "open"


# ─── force-close stale ────────────────────────────────────────────────────
def test_force_close_stale_closes_old_rows():
    _, conn = _fresh_db()
    # 4h tf × 8 max_hold = 32h. Age multiplier 3 → 96h threshold.
    ancient = time.time() - 100 * 3600  # 100h old
    _insert_trade(conn, cloid="stuck", coin="APT", status="open",
                  open_ts=ancient, max_hold_bars=8)
    bus = _MockBus(markprice={"APT": {"hl_mid": 1.05}})
    trader = _make_trader_with_bus(conn, bus)
    n = trader.force_close_stale()
    assert n == 1
    row = conn.execute(
        "SELECT status, extras_json FROM trades WHERE cloid='stuck'"
    ).fetchone()
    assert row["status"] == "closed"
    import json
    ex = json.loads(row["extras_json"])
    assert ex["close_reason"] == "stale_force_close"
    assert ex["close_px"] == 1.05


def test_force_close_stale_leaves_recent_rows():
    _, conn = _fresh_db()
    _insert_trade(conn, cloid="fresh", coin="APT", status="open",
                  open_ts=time.time() - 3600, max_hold_bars=8)  # 1h on a 32h trade
    bus = _MockBus(markprice={"APT": {"hl_mid": 1.05}})
    trader = _make_trader_with_bus(conn, bus)
    assert trader.force_close_stale() == 0
    row = conn.execute("SELECT status FROM trades WHERE cloid='fresh'").fetchone()
    assert row["status"] == "open"
