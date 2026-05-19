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


# ─── reconciler (2-pass confirmation, sentinel H2 fix) ─────────────────────
class _MockBus:
    def __init__(self, hl_positions=None, markprice=None):
        self._hl = hl_positions or []
        self._mark = markprice or {}

    def hl_positions(self):
        return self._hl

    def markprice(self, coin):
        return self._mark.get(coin.upper(), {"hl_mid": 1.0})


def _make_trader_with_bus(conn, bus, hl=None):
    """Build a minimal Trader without going through real PMClient/HLExchange."""
    from strategy_runner.trader import Trader
    class _PM:
        def register_cloid(self, *a, **kw): pass
    return Trader(conn, bus, _PM(), hl=hl)


def test_reconcile_two_pass_releases_ghost_locks():
    """First pass records pending; second pass after min_confirm_s reconciles."""
    _, conn = _fresh_db()
    old_ts = time.time() - 7200  # 2h old, past the 5min safety window
    _insert_trade(conn, cloid="ghost", coin="APT", status="open", open_ts=old_ts)
    bus = _MockBus(hl_positions=[
        {"coin": "BTC", "size_coin": 0.001},  # APT NOT present
    ])
    trader = _make_trader_with_bus(conn, bus)
    # Pass 1 — should record pending but NOT reconcile
    assert trader.reconcile_with_hl(min_confirm_s=0) == 0
    row = conn.execute("SELECT status FROM trades WHERE cloid='ghost'").fetchone()
    assert row["status"] == "open"
    # Pass 2 — now reconciles
    assert trader.reconcile_with_hl(min_confirm_s=0) == 1
    row = conn.execute("SELECT status FROM trades WHERE cloid='ghost'").fetchone()
    assert row["status"] == "reconciled_off_book"


def test_reconcile_two_pass_respects_min_confirm_s():
    """Second pass within min_confirm_s does NOT reconcile."""
    _, conn = _fresh_db()
    old_ts = time.time() - 7200
    _insert_trade(conn, cloid="ghost", coin="APT", status="open", open_ts=old_ts)
    bus = _MockBus(hl_positions=[])
    trader = _make_trader_with_bus(conn, bus)
    # Pass 1 — record pending with current time
    assert trader.reconcile_with_hl(min_confirm_s=60) == 0
    # Pass 2 immediately — must NOT reconcile (no 60s elapsed)
    assert trader.reconcile_with_hl(min_confirm_s=60) == 0
    row = conn.execute("SELECT status FROM trades WHERE cloid='ghost'").fetchone()
    assert row["status"] == "open"


def test_reconcile_reappear_clears_pending():
    """If coin reappears on HL between passes, pending state is cleared
    and we restart confirmation from scratch. This is the H2 fix."""
    _, conn = _fresh_db()
    old_ts = time.time() - 7200
    _insert_trade(conn, cloid="g", coin="APT", status="open", open_ts=old_ts)
    bus_absent = _MockBus(hl_positions=[])
    bus_present = _MockBus(hl_positions=[{"coin": "APT", "size_coin": -0.5}])
    # Pass 1 absent — pending
    t = _make_trader_with_bus(conn, bus_absent)
    t.reconcile_with_hl(min_confirm_s=0)
    # Pass 2 present — clear pending
    t2 = _make_trader_with_bus(conn, bus_present)
    t2.reconcile_with_hl(min_confirm_s=0)
    pending = conn.execute(
        "SELECT v FROM kv_state WHERE k='recon_pending:APT'"
    ).fetchone()
    assert pending is None, "pending state should be cleared on coin reappearance"
    # Pass 3 absent again — should be back at pass 1, not reconcile
    t3 = _make_trader_with_bus(conn, bus_absent)
    assert t3.reconcile_with_hl(min_confirm_s=0) == 0
    row = conn.execute("SELECT status FROM trades WHERE cloid='g'").fetchone()
    assert row["status"] == "open"


def test_reconcile_skips_recently_opened():
    """Don't reconcile rows < 5min old (HL WS lag tolerance)."""
    _, conn = _fresh_db()
    _insert_trade(conn, cloid="new", coin="APT", status="open",
                  open_ts=time.time() - 60)
    bus = _MockBus(hl_positions=[])
    trader = _make_trader_with_bus(conn, bus)
    assert trader.reconcile_with_hl(min_confirm_s=0) == 0
    assert trader.reconcile_with_hl(min_confirm_s=0) == 0
    row = conn.execute("SELECT status FROM trades WHERE cloid='new'").fetchone()
    assert row["status"] == "open"


def test_reconcile_leaves_alive_positions_alone():
    _, conn = _fresh_db()
    _insert_trade(conn, cloid="live", coin="APT", status="open",
                  open_ts=time.time() - 7200)
    bus = _MockBus(hl_positions=[{"coin": "APT", "size_coin": -0.5}])
    trader = _make_trader_with_bus(conn, bus)
    assert trader.reconcile_with_hl(min_confirm_s=0) == 0
    assert trader.reconcile_with_hl(min_confirm_s=0) == 0
    row = conn.execute("SELECT status FROM trades WHERE cloid='live'").fetchone()
    assert row["status"] == "open"


# ─── force-close stale (HL-aware, sentinel H3 fix) ─────────────────────────
class _MockHL:
    def __init__(self, ok=True):
        self.ok_default = ok
        self.calls = []

    def market_close(self, coin, size_coin, cloid):
        self.calls.append((coin, size_coin, cloid))
        class _Res:
            def __init__(self, ok): self.ok = ok
            error = None
        return _Res(self.ok_default)


def test_force_close_stale_clean_close_when_hl_has_no_position():
    """HL says no position → safe local close."""
    _, conn = _fresh_db()
    ancient = time.time() - 100 * 3600
    _insert_trade(conn, cloid="stuck", coin="APT", status="open",
                  open_ts=ancient, max_hold_bars=8)
    bus = _MockBus(hl_positions=[], markprice={"APT": {"hl_mid": 1.05}})
    trader = _make_trader_with_bus(conn, bus)
    assert trader.force_close_stale() == 1
    row = conn.execute(
        "SELECT status, extras_json FROM trades WHERE cloid='stuck'"
    ).fetchone()
    assert row["status"] == "closed"
    import json
    ex = json.loads(row["extras_json"])
    assert ex["close_reason"] == "stale_force_close_hl_absent"


def test_force_close_stale_attempts_hl_close_when_position_exists():
    """HL has position + close succeeds → marked closed cleanly."""
    _, conn = _fresh_db()
    ancient = time.time() - 100 * 3600
    _insert_trade(conn, cloid="live", coin="APT", status="open",
                  open_ts=ancient, max_hold_bars=8)
    bus = _MockBus(hl_positions=[{"coin": "APT", "size_coin": -0.5}],
                   markprice={"APT": {"hl_mid": 1.05}})
    hl = _MockHL(ok=True)
    trader = _make_trader_with_bus(conn, bus, hl=hl)
    assert trader.force_close_stale() == 1
    assert len(hl.calls) == 1
    row = conn.execute(
        "SELECT status, extras_json FROM trades WHERE cloid='live'"
    ).fetchone()
    assert row["status"] == "closed"
    import json
    assert json.loads(row["extras_json"])["close_reason"] == "stale_force_close_hl_ok"


def test_force_close_stale_halts_strategy_when_hl_refuses():
    """HL position exists, hl.market_close fails → unverified + halt."""
    from common import halt as _halt
    _, conn = _fresh_db()
    # ensure halts table primed (initialized in init_db)
    ancient = time.time() - 100 * 3600
    _insert_trade(conn, cloid="orph", coin="APT", status="open",
                  open_ts=ancient, max_hold_bars=8, strategy="my_strat")
    bus = _MockBus(hl_positions=[{"coin": "APT", "size_coin": -0.5}],
                   markprice={"APT": {"hl_mid": 1.05}})
    hl = _MockHL(ok=False)
    trader = _make_trader_with_bus(conn, bus, hl=hl)
    assert trader.force_close_stale() == 1
    row = conn.execute(
        "SELECT status, extras_json FROM trades WHERE cloid='orph'"
    ).fetchone()
    assert row["status"] == "force_closed_unverified"
    # Strategy should be halted
    assert "my_strat" in _halt._HALTED or _halt.is_halted("my_strat")
    # Cleanup for other tests
    _halt._HALTED.discard("my_strat")


def test_force_close_stale_halts_when_hl_unreachable():
    """bus.hl_positions raises → unverified + halt (last resort)."""
    from common import halt as _halt
    _, conn = _fresh_db()
    ancient = time.time() - 100 * 3600
    _insert_trade(conn, cloid="unr", coin="APT", status="open",
                  open_ts=ancient, max_hold_bars=8, strategy="unr_strat")
    class _DeadBus(_MockBus):
        def hl_positions(self): raise RuntimeError("bus dead")
    bus = _DeadBus(markprice={"APT": {"hl_mid": 1.05}})
    trader = _make_trader_with_bus(conn, bus)
    assert trader.force_close_stale() == 1
    row = conn.execute(
        "SELECT status, extras_json FROM trades WHERE cloid='unr'"
    ).fetchone()
    assert row["status"] == "force_closed_unverified"
    import json
    assert json.loads(row["extras_json"])["close_reason"] == "stale_force_close_hl_unreachable"
    assert _halt.is_halted("unr_strat")
    _halt._HALTED.discard("unr_strat")


def test_force_close_stale_leaves_recent_rows():
    _, conn = _fresh_db()
    _insert_trade(conn, cloid="fresh", coin="APT", status="open",
                  open_ts=time.time() - 3600, max_hold_bars=8)
    bus = _MockBus(markprice={"APT": {"hl_mid": 1.05}})
    trader = _make_trader_with_bus(conn, bus)
    assert trader.force_close_stale() == 0
    row = conn.execute("SELECT status FROM trades WHERE cloid='fresh'").fetchone()
    assert row["status"] == "open"


# ─── sweep_stale_pending phantom-position fix ────────────────────────────
def test_sweep_pending_promotes_when_hl_has_position():
    """Process crashed between INSERT 'pending' and UPDATE 'open' AFTER the
    HL order succeeded. Sweep must recognize HL position and promote, not
    demote (demoting releases the lock → next scan opens a duplicate).
    """
    _, conn = _fresh_db()
    _insert_trade(conn, cloid="crashed", coin="APT", status="pending",
                  open_ts=time.time() - 600)  # 10min old
    bus = _MockBus(hl_positions=[{"coin": "APT", "size_coin": -0.5}])
    trader = _make_trader_with_bus(conn, bus)
    n = trader.sweep_stale_pending()
    assert n == 1
    row = conn.execute(
        "SELECT status, extras_json FROM trades WHERE cloid='crashed'"
    ).fetchone()
    assert row["status"] == "open"
    import json
    ex = json.loads(row["extras_json"])
    assert ex["recovered"] == "sweep_promoted_from_pending"
    assert ex["hl_size_at_recover"] == -0.5


def test_sweep_pending_demotes_when_hl_has_no_position():
    """Order never reached HL (or was rejected) — pending row should be
    demoted to open_failed so the lock releases."""
    _, conn = _fresh_db()
    _insert_trade(conn, cloid="failed", coin="APT", status="pending",
                  open_ts=time.time() - 600)
    bus = _MockBus(hl_positions=[])
    trader = _make_trader_with_bus(conn, bus)
    n = trader.sweep_stale_pending()
    assert n == 1
    row = conn.execute(
        "SELECT status FROM trades WHERE cloid='failed'"
    ).fetchone()
    assert row["status"] == "open_failed"


def test_sweep_pending_keeps_pending_when_bus_unavailable():
    """Cannot query HL → safest default is leave 'pending' for next pass.
    Demoting blindly risks the phantom-position duplicate-open bug."""
    _, conn = _fresh_db()
    _insert_trade(conn, cloid="unknown", coin="APT", status="pending",
                  open_ts=time.time() - 600)
    class _BrokenBus:
        def hl_positions(self): raise RuntimeError("bus down")
        def markprice(self, coin): raise RuntimeError("bus down")
    trader = _make_trader_with_bus(conn, _BrokenBus())
    n = trader.sweep_stale_pending()
    assert n == 0
    row = conn.execute(
        "SELECT status FROM trades WHERE cloid='unknown'"
    ).fetchone()
    assert row["status"] == "pending"


def test_sweep_pending_leaves_fresh_rows():
    """Pending rows younger than max_age_s are still in-flight; don't touch."""
    _, conn = _fresh_db()
    _insert_trade(conn, cloid="fresh", coin="APT", status="pending",
                  open_ts=time.time() - 30)
    bus = _MockBus(hl_positions=[])
    trader = _make_trader_with_bus(conn, bus)
    n = trader.sweep_stale_pending()
    assert n == 0
    row = conn.execute(
        "SELECT status FROM trades WHERE cloid='fresh'"
    ).fetchone()
    assert row["status"] == "pending"
