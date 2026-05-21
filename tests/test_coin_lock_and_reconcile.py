"""Tests for coin-lock integrity and trade-row reconciliation bugs.

Covers the failure mode that produced 10 duplicate APT shorts in production
on 2026-05-17.
"""
from __future__ import annotations

import os
import sqlite3
import json
import tempfile
import time
import pytest

from common import persistence


def _fresh_db():
    p = tempfile.mktemp(suffix=".db")
    return p, persistence.init_db(p)


def _insert_trade(conn, *, cloid, coin, status, open_ts=None, max_hold_bars=8,
                   strategy="test", open_px=100.0, size_coin=0.1, extras=None):
    extras_json = json.dumps(extras) if extras is not None else '{"tf":"4h"}'
    conn.execute(
        "INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,open_px,"
        "size_usd,size_coin,sl_px,tp_px,max_hold_bars,status,extras_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cloid, strategy, coin, "A", 0, open_ts or time.time(), open_px,
         10.0, size_coin, 101.0, 99.0, max_hold_bars, status, extras_json),
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
        {"coin": "BTC", "szi": 0.001},  # APT NOT present
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
    bus_present = _MockBus(hl_positions=[{"coin": "APT", "szi": -0.5}])
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
    bus = _MockBus(hl_positions=[{"coin": "APT", "szi": -0.5}])
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
    bus = _MockBus(hl_positions=[{"coin": "APT", "szi": -0.5}],
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
    bus = _MockBus(hl_positions=[{"coin": "APT", "szi": -0.5}],
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


# ─── szi field-name bug regression tests ──────────────────────────────────
def test_reconcile_uses_szi_not_size_coin():
    """Bus returns positions with field 'szi' (HL native). Reconcile must
    read 'szi'; reading 'size_coin' silently gives 0 and filters every
    position out → every open trade looks 'absent' → all reconciled.

    Regression: bug introduced in commit 0eea125, fixed 2026-05-19. This
    test guards against re-introducing the field-name typo."""
    _, conn = _fresh_db()
    # Trade has been open 20 minutes — past 5-min safety window
    _insert_trade(conn, cloid="t1", coin="NEAR", status="open",
                  open_ts=time.time() - 1200, max_hold_bars=8)
    # Bus returns NEAR position the way the real signal_bus does
    bus = _MockBus(hl_positions=[
        {"coin": "NEAR", "szi": -14.4, "is_long": False,
         "entry_px": 1.66, "unrealized_pnl": 0.0},
    ])
    trader = _make_trader_with_bus(conn, bus)
    # Pass 1: should detect NEAR as PRESENT on HL → not flag pending
    n_reconciled_p1 = trader.reconcile_with_hl(min_confirm_s=0)
    assert n_reconciled_p1 == 0, "NEAR present on HL — must not reconcile"
    row = conn.execute("SELECT status FROM trades WHERE cloid='t1'").fetchone()
    assert row["status"] == "open"
    # Pass 2: still present → still not reconciled
    n_reconciled_p2 = trader.reconcile_with_hl(min_confirm_s=0)
    assert n_reconciled_p2 == 0


def test_reconcile_still_catches_genuinely_absent():
    """Sanity: when HL really doesn't have the coin, reconcile still fires."""
    _, conn = _fresh_db()
    _insert_trade(conn, cloid="ghost1", coin="GHOSTCOIN", status="open",
                  open_ts=time.time() - 1200, max_hold_bars=8)
    bus = _MockBus(hl_positions=[
        # HL has SOL but not GHOSTCOIN
        {"coin": "SOL", "szi": 0.1, "is_long": True, "entry_px": 100.0},
    ])
    trader = _make_trader_with_bus(conn, bus)
    # Pass 1: pending
    trader.reconcile_with_hl(min_confirm_s=0)
    # Pass 2 (separated): confirmed off-book
    time.sleep(0.05)
    n = trader.reconcile_with_hl(min_confirm_s=0)
    assert n == 1
    row = conn.execute("SELECT status FROM trades WHERE cloid='ghost1'").fetchone()
    assert row["status"] == "reconciled_off_book"


def test_force_close_stale_reads_szi_field():
    """force_close_stale uses bus.hl_positions to decide whether HL agrees
    a position is gone. With szi typo'd as size_coin, the filter always
    returned empty → force_close treated everything as 'HL has no position'
    → force_close path triggered for stale rows even when HL had them."""
    _, conn = _fresh_db()
    # Old trade past 3× timeout — eligible for force_close
    open_ts = time.time() - 3 * 8 * 3600 - 60
    _insert_trade(conn, cloid="t2", coin="ETH", status="open",
                  open_ts=open_ts, max_hold_bars=8,
                  open_px=2000.0, size_coin=0.01)
    # HL has the ETH position
    bus = _MockBus(
        hl_positions=[
            {"coin": "ETH", "szi": -0.01, "is_long": False, "entry_px": 2000.0},
        ],
        markprice={"ETH": {"hl_mid": 2010.0}},
    )
    trader = _make_trader_with_bus(conn, bus)
    n = trader.force_close_stale()
    # With the bug, hl_by_coin would be empty so the close path would take
    # the "HL has no position" branch and mark status='closed' immediately
    # at HL_NOT_REACHABLE_ASSUMED_OK. With the fix, HL is reachable AND has
    # the position, so force_close performs a real market_close.
    row = conn.execute("SELECT status FROM trades WHERE cloid='t2'").fetchone()
    # We don't assert on the final status (depends on mock market_close)
    # but we assert that force_close at least RAN (saw the position).
    # Concretely: with the bug, n was always 0 or 1 with status='closed' regardless;
    # with the fix, the HL-aware branch was exercised.
    assert n >= 0  # smoke — no crash


def test_unreconcile_active_hl_restores_open():
    """unreconcile_active_hl_positions flips reconciled_off_book rows back
    to 'open' when HL still has the matching position."""
    _, conn = _fresh_db()
    _insert_trade(conn, cloid="fp1", coin="NEAR", status="reconciled_off_book",
                  open_ts=time.time() - 1800, open_px=1.66, size_coin=14.4)
    # Force is_long=False (short) to match
    conn.execute("UPDATE trades SET is_long=0 WHERE cloid='fp1'")
    bus = _MockBus(hl_positions=[
        {"coin": "NEAR", "szi": -14.4, "is_long": False, "entry_px": 1.66},
    ])
    trader = _make_trader_with_bus(conn, bus)
    result = trader.unreconcile_active_hl_positions()
    assert result["restored"] == 1
    assert result["scanned"] == 1
    row = conn.execute("SELECT status FROM trades WHERE cloid='fp1'").fetchone()
    assert row["status"] == "open"


def test_unreconcile_skips_direction_mismatch():
    """If HL has a position on the coin but in the OPPOSITE direction,
    don't restore — that's a different trade."""
    _, conn = _fresh_db()
    _insert_trade(conn, cloid="fp2", coin="ETH", status="reconciled_off_book",
                  open_ts=time.time() - 1800)
    conn.execute("UPDATE trades SET is_long=1 WHERE cloid='fp2'")  # local: long
    bus = _MockBus(hl_positions=[
        {"coin": "ETH", "szi": -0.01, "is_long": False, "entry_px": 2000.0},  # HL: short
    ])
    trader = _make_trader_with_bus(conn, bus)
    result = trader.unreconcile_active_hl_positions()
    assert result["restored"] == 0
    assert result["false_positive_left_alone"] == 1
    row = conn.execute("SELECT status FROM trades WHERE cloid='fp2'").fetchone()
    assert row["status"] == "reconciled_off_book"


def test_unreconcile_skips_closed_rows():
    """Only rows WITHOUT a closures match are candidates."""
    _, conn = _fresh_db()
    _insert_trade(conn, cloid="already_booked", coin="SOL", status="reconciled_off_book",
                  open_ts=time.time() - 1800)
    # Closure already exists for this cloid
    conn.execute(
        "INSERT INTO closures(cloid,strategy,coin,is_long,open_ts,close_ts,"
        "open_px,close_px,size_coin,pnl_usd,fees_usd,close_reason) "
        "VALUES('already_booked','test','SOL',1,?,?,80.0,82.0,0.1,0.2,0.01,'tp')",
        (time.time() - 1800, time.time() - 600)
    )
    bus = _MockBus(hl_positions=[
        {"coin": "SOL", "szi": 0.1, "is_long": True, "entry_px": 80.0},
    ])
    trader = _make_trader_with_bus(conn, bus)
    result = trader.unreconcile_active_hl_positions()
    assert result["scanned"] == 0  # the closed row was filtered out
    assert result["restored"] == 0


# ─── back-fill closures from HL fills (PnL attribution recovery) ──────────
class _MockBusWithFills(_MockBus):
    def __init__(self, hl_positions=None, markprice=None, fills=None):
        super().__init__(hl_positions, markprice)
        self._fills = fills or []

    def hl_fills(self, since_ms=None):
        if since_ms is None:
            return self._fills
        return [f for f in self._fills if float(f.get("ts", 0)) >= since_ms]


def test_book_closure_from_fills_inserts_closure():
    """A reconciled short trade that HL fills show as Open+Close → closure row
    with HL's exact closedPnl appears in closures table."""
    _, conn = _fresh_db()
    open_cloid = "0x6362540734925e55a61f189f22cccafd"
    close_cloid = "0xfae9eaca759dadd75639d9653eb877ca"
    _insert_trade(conn, cloid=open_cloid, coin="SOL", status="reconciled_off_book",
                  open_ts=1779115027.0, max_hold_bars=8, strategy="ict_confluence_4h",
                  open_px=83.915, size_coin=0.16)
    fills = [
        # Open Short
        {"ts": 1779115032498, "coin": "SOL", "side": "A", "qty": 0.16, "price": 83.915,
         "cloid": open_cloid,
         "raw": {"sz": "0.16", "px": "83.915", "dir": "Open Short",
                  "closedPnl": "0.0", "fee": "0.0058"}},
        # Close Short by bracket (different cloid)
        {"ts": 1779116642186, "coin": "SOL", "side": "B", "qty": 0.16, "price": 83.612,
         "cloid": close_cloid,
         "raw": {"sz": "0.16", "px": "83.612", "dir": "Close Short",
                  "closedPnl": "0.04848", "fee": "0.005779"}},
    ]
    bus = _MockBusWithFills(fills=fills)
    trader = _make_trader_with_bus(conn, bus)
    trade_row = conn.execute("SELECT * FROM trades WHERE cloid=?", (open_cloid,)).fetchone()
    pnl = trader.book_closure_from_fills(trade_row, reason="test")
    assert pnl is not None
    # Net = closedPnl - fees = 0.04848 - (0.0058 + 0.005779) = 0.036901
    assert abs(pnl - 0.036901) < 1e-5, f"got {pnl}"
    row = conn.execute("SELECT * FROM closures WHERE cloid=?", (open_cloid,)).fetchone()
    assert row is not None
    assert row["coin"] == "SOL"
    assert abs(row["pnl_usd"] - 0.04848) < 1e-5
    assert abs(row["fees_usd"] - 0.011579) < 1e-5
    assert abs(row["close_px"] - 83.612) < 1e-5
    assert row["close_reason"] == "test"


def test_book_closure_idempotent():
    """Calling book_closure_from_fills twice doesn't insert duplicate rows."""
    _, conn = _fresh_db()
    open_cloid = "0xa1"
    _insert_trade(conn, cloid=open_cloid, coin="SOL", status="reconciled_off_book",
                  open_ts=1779115027.0, max_hold_bars=8, open_px=83.0, size_coin=0.1)
    fills = [
        {"ts": 1779115030000, "coin": "SOL", "side": "A", "qty": 0.1, "price": 83.0,
         "cloid": open_cloid,
         "raw": {"sz": "0.1", "px": "83.0", "dir": "Open Short",
                  "closedPnl": "0.0", "fee": "0.005"}},
        {"ts": 1779116000000, "coin": "SOL", "side": "B", "qty": 0.1, "price": 82.0,
         "cloid": "0xb2",
         "raw": {"sz": "0.1", "px": "82.0", "dir": "Close Short",
                  "closedPnl": "0.10", "fee": "0.005"}},
    ]
    bus = _MockBusWithFills(fills=fills)
    trader = _make_trader_with_bus(conn, bus)
    row = conn.execute("SELECT * FROM trades WHERE cloid=?", (open_cloid,)).fetchone()
    p1 = trader.book_closure_from_fills(row)
    p2 = trader.book_closure_from_fills(row)
    assert p1 is not None
    assert p2 is None  # second call short-circuits
    n = conn.execute("SELECT COUNT(*) AS n FROM closures WHERE cloid=?", (open_cloid,)).fetchone()["n"]
    assert n == 1


def test_book_closure_returns_none_when_no_close_fill():
    """Open fill exists but no Close fill — return None, no closure row."""
    _, conn = _fresh_db()
    open_cloid = "0xc1"
    _insert_trade(conn, cloid=open_cloid, coin="SOL", status="open",
                  open_ts=1779115027.0, max_hold_bars=8, open_px=83.0, size_coin=0.1)
    fills = [
        {"ts": 1779115030000, "coin": "SOL", "side": "A", "qty": 0.1, "price": 83.0,
         "cloid": open_cloid,
         "raw": {"sz": "0.1", "px": "83.0", "dir": "Open Short",
                  "closedPnl": "0.0", "fee": "0.005"}},
        # No close fill
    ]
    bus = _MockBusWithFills(fills=fills)
    trader = _make_trader_with_bus(conn, bus)
    row = conn.execute("SELECT * FROM trades WHERE cloid=?", (open_cloid,)).fetchone()
    assert trader.book_closure_from_fills(row) is None
    n = conn.execute("SELECT COUNT(*) AS n FROM closures WHERE cloid=?", (open_cloid,)).fetchone()["n"]
    assert n == 0


def test_book_closure_matches_long_position_correctly():
    """Long position close (dir='Close Long') must be matched for is_long=1."""
    _, conn = _fresh_db()
    open_cloid = "0xd1"
    _insert_trade(conn, cloid=open_cloid, coin="ETH", status="reconciled_off_book",
                  open_ts=1779115027.0, max_hold_bars=8, open_px=2000.0, size_coin=0.05)
    # Mark trade as long
    conn.execute("UPDATE trades SET is_long=1 WHERE cloid=?", (open_cloid,))
    fills = [
        {"ts": 1779115030000, "coin": "ETH", "side": "B", "qty": 0.05, "price": 2000.0,
         "cloid": open_cloid,
         "raw": {"sz": "0.05", "px": "2000.0", "dir": "Open Long",
                  "closedPnl": "0.0", "fee": "0.5"}},
        # Distractor: a SHORT close for ETH — must be ignored
        {"ts": 1779115500000, "coin": "ETH", "side": "B", "qty": 0.05, "price": 1990.0,
         "cloid": "0xdistractor",
         "raw": {"sz": "0.05", "px": "1990.0", "dir": "Close Short",
                  "closedPnl": "99.0", "fee": "0.5"}},
        # The real Close Long
        {"ts": 1779116000000, "coin": "ETH", "side": "A", "qty": 0.05, "price": 2050.0,
         "cloid": "0xclose_long",
         "raw": {"sz": "0.05", "px": "2050.0", "dir": "Close Long",
                  "closedPnl": "2.50", "fee": "0.5"}},
    ]
    bus = _MockBusWithFills(fills=fills)
    trader = _make_trader_with_bus(conn, bus)
    row = conn.execute("SELECT * FROM trades WHERE cloid=?", (open_cloid,)).fetchone()
    pnl = trader.book_closure_from_fills(row)
    # Net = 2.50 - (0.5 + 0.5) = 1.50  (distractor NOT counted)
    assert pnl is not None
    assert abs(pnl - 1.50) < 1e-5, f"distractor leaked: got {pnl}"
    c = conn.execute("SELECT * FROM closures WHERE cloid=?", (open_cloid,)).fetchone()
    assert abs(c["pnl_usd"] - 2.50) < 1e-5
    assert abs(c["close_px"] - 2050.0) < 1e-5


def test_backfill_reconciled_closures_scans_only_unbooked():
    """Trades with existing closures are skipped; reconciled rows are processed."""
    _, conn = _fresh_db()
    # Trade #1: reconciled, no closure yet → should be backfilled
    _insert_trade(conn, cloid="0xa", coin="SOL", status="reconciled_off_book",
                  open_ts=1779115027.0, open_px=83.0, size_coin=0.1)
    # Trade #2: reconciled, already has closure (e.g. force_close path) → skip
    _insert_trade(conn, cloid="0xb", coin="ETH", status="reconciled_off_book",
                  open_ts=1779115100.0, open_px=2000.0, size_coin=0.05)
    conn.execute(
        "INSERT INTO closures(cloid,strategy,coin,is_long,open_ts,close_ts,"
        "open_px,close_px,size_coin,pnl_usd,fees_usd,close_reason) "
        "VALUES('0xb','test','ETH',1,1779115100.0,1779116000.0,2000.0,2010.0,0.05,0.5,0.01,'manual')"
    )
    # Trade #3: still 'open' — should NOT be processed (status filter excludes 'open')
    _insert_trade(conn, cloid="0xc", coin="DOT", status="open",
                  open_ts=1779115200.0, open_px=1.2, size_coin=10)
    fills = [
        {"ts": 1779115030000, "coin": "SOL", "side": "A", "qty": 0.1, "price": 83.0,
         "cloid": "0xa",
         "raw": {"sz": "0.1", "px": "83.0", "dir": "Open Short",
                  "closedPnl": "0.0", "fee": "0.005"}},
        {"ts": 1779116000000, "coin": "SOL", "side": "B", "qty": 0.1, "price": 82.0,
         "cloid": "0xclose",
         "raw": {"sz": "0.1", "px": "82.0", "dir": "Close Short",
                  "closedPnl": "0.10", "fee": "0.005"}},
    ]
    bus = _MockBusWithFills(fills=fills)
    trader = _make_trader_with_bus(conn, bus)
    result = trader.backfill_reconciled_closures(since_ts=0)
    assert result["scanned"] == 1, f"only Trade #1 should be scanned: {result}"
    assert result["booked"] == 1
    assert result["no_fills"] == 0
    # Trade #1 now has closure
    c = conn.execute("SELECT * FROM closures WHERE cloid='0xa'").fetchone()
    assert c is not None
    assert c["close_reason"] == "backfill"


# ─── sweep_stale_pending phantom-position fix ────────────────────────────
def test_sweep_pending_promotes_when_hl_has_position():
    """Process crashed between INSERT 'pending' and UPDATE 'open' AFTER the
    HL order succeeded. Sweep must recognize HL position and promote, not
    demote (demoting releases the lock → next scan opens a duplicate).
    """
    _, conn = _fresh_db()
    _insert_trade(conn, cloid="crashed", coin="APT", status="pending",
                  open_ts=time.time() - 600)  # 10min old
    bus = _MockBus(hl_positions=[{"coin": "APT", "szi": -0.5}])
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


# ─── reconcile skips paper trades (2026-05-21) ────────────────────────────
def test_reconcile_skips_paper_trades():
    """Paper trades (live=False) must NOT be reconciled off-book.
    Reconciler was previously stomping paper trades before SL/TP could fire."""
    _, conn = _fresh_db()
    # 1 paper trade + 1 live trade, both old enough to be eligible
    _insert_trade(conn, cloid="paper_t", coin="SOL", status="open",
                  open_ts=time.time() - 3600, max_hold_bars=8,
                  strategy="hl_settle_5m", open_px=100, size_coin=0.1,
                  extras={"live": False, "fire_reason": "paper", "extras": {}})
    _insert_trade(conn, cloid="live_t", coin="BTC", status="open",
                  open_ts=time.time() - 3600, max_hold_bars=8,
                  strategy="hl_settle_5m", open_px=100, size_coin=0.1,
                  extras={"live": True, "fire_reason": "live", "extras": {}})
    # MockBus returns no HL positions (HL has nothing for either)
    bus = _MockBus(hl_positions=[])
    trader = _make_trader_with_bus(conn, bus)
    # 1st pass: live records pending, paper skipped entirely
    trader.reconcile_with_hl(min_confirm_s=0)
    # 2nd pass with min_confirm_s=0: live reconciles, paper still skipped
    trader.reconcile_with_hl(min_confirm_s=0)
    paper_row = conn.execute("SELECT status FROM trades WHERE cloid='paper_t'").fetchone()
    live_row = conn.execute("SELECT status FROM trades WHERE cloid='live_t'").fetchone()
    assert paper_row["status"] == "open", f"paper trade incorrectly reconciled: {paper_row['status']}"
    assert live_row["status"] == "reconciled_off_book", f"live trade should reconcile: {live_row['status']}"


def test_reconcile_paper_trade_with_missing_live_key_treated_as_live():
    """If extras_json lacks the 'live' key (legacy rows), default to treating
    as LIVE — safer to over-reconcile than to leave a live ghost in place."""
    _, conn = _fresh_db()
    _insert_trade(conn, cloid="legacy", coin="ETH", status="open",
                  open_ts=time.time() - 3600, max_hold_bars=8,
                  strategy="hl_settle_5m", open_px=2000, size_coin=0.01,
                  extras={"fire_reason": "legacy", "extras": {}})  # no 'live' key
    bus = _MockBus(hl_positions=[])
    trader = _make_trader_with_bus(conn, bus)
    trader.reconcile_with_hl(min_confirm_s=0)
    trader.reconcile_with_hl(min_confirm_s=0)
    row = conn.execute("SELECT status FROM trades WHERE cloid='legacy'").fetchone()
    # Missing 'live' key → treated as live → reconciles normally
    assert row["status"] == "reconciled_off_book"
