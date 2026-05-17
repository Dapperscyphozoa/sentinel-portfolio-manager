"""Tests for the 1_GLOBAL coin-lock fix (sentinel audit 2026-05-17).

Invariants under test:
1. Partial unique index prevents two open/pending trades on same coin.
2. trader.is_coin_locked() returns True when an open trade exists on a coin.
3. trader.is_coin_locked() returns True for `open_failed` within cooldown.
4. trader.is_coin_locked() returns False after cooldown expires.
5. trader.open() returns OpenResult(ok=False, error='coin_locked:...') when
   a pending/open row already exists for the coin.
6. Closed trades release the lock (next open on same coin allowed).
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from unittest.mock import MagicMock

from common import persistence
from strategy_runner.strategies._base import Signal
from strategy_runner.trader import Trader


def _make_trader(conn, live=False):
    bus = MagicMock()
    pm = MagicMock()
    pm.register_cloid.return_value = None
    hl = None  # paper mode — no HL calls
    trader = Trader(conn, bus, pm, hl)
    trader.live_default = live
    return trader


def _make_sig(coin, ref_price=100.0):
    return Signal(
        coin=coin, side="B", is_long=True, ref_price=ref_price,
        sl_px=ref_price * 0.98, tp_px=ref_price * 1.03, max_hold_bars=24,
        fire_ts=time.time() * 1000, fire_reason="test", extras={},
    )


def _make_strat(name="test_strat"):
    s = MagicMock()
    s.NAME = name
    s.CLOID_PREFIX = "tst_"
    return s


def test_unique_index_blocks_duplicate_open():
    """Schema-level: two raw INSERTs of status='open' on same coin must fail."""
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        conn.execute(
            "INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,status) "
            "VALUES('a','s1','BTC','B',1,?,'open')", (time.time(),))
        try:
            conn.execute(
                "INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,status) "
                "VALUES('b','s2','BTC','B',1,?,'open')", (time.time(),))
            assert False, "Second INSERT should have raised IntegrityError"
        except sqlite3.IntegrityError:
            pass  # Expected


def test_unique_index_blocks_open_vs_pending():
    """One open + one pending on same coin must also fail (both are locks)."""
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        conn.execute(
            "INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,status) "
            "VALUES('a','s1','ETH','B',1,?,'pending')", (time.time(),))
        try:
            conn.execute(
                "INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,status) "
                "VALUES('b','s2','ETH','B',1,?,'open')", (time.time(),))
            assert False, "Open after pending on same coin should fail"
        except sqlite3.IntegrityError:
            pass


def test_unique_index_allows_after_close():
    """closed → next open on same coin allowed."""
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        conn.execute(
            "INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,status) "
            "VALUES('a','s1','SOL','B',1,?,'closed')", (time.time(),))
        conn.execute(
            "INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,status) "
            "VALUES('b','s2','SOL','B',1,?,'open')", (time.time(),))
        rows = conn.execute("SELECT cloid,status FROM trades WHERE coin='SOL'").fetchall()
        assert len(rows) == 2


def test_unique_index_allows_after_open_failed():
    """open_failed releases the lock (status not in partial index)."""
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        conn.execute(
            "INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,status) "
            "VALUES('a','s1','AVAX','B',1,?,'open_failed')", (time.time(),))
        # New open allowed at DB level (cooldown lives in is_coin_locked, not index)
        conn.execute(
            "INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,status) "
            "VALUES('b','s2','AVAX','B',1,?,'open')", (time.time(),))


def test_is_coin_locked_true_when_open_exists():
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        trader = _make_trader(conn)
        conn.execute(
            "INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,status) "
            "VALUES('a','s1','APT','B',1,?,'open')", (time.time(),))
        locked, reason = trader.is_coin_locked("APT")
        assert locked is True
        assert "coin_locked" in reason


def test_is_coin_locked_true_when_pending_exists():
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        trader = _make_trader(conn)
        conn.execute(
            "INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,status) "
            "VALUES('a','s1','WIF','B',1,?,'pending')", (time.time(),))
        locked, _ = trader.is_coin_locked("WIF")
        assert locked is True


def test_is_coin_locked_respects_failed_cooldown():
    """open_failed within cooldown → locked; outside cooldown → unlocked."""
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        trader = _make_trader(conn)
        # Just failed
        conn.execute(
            "INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,status) "
            "VALUES('a','s1','JUP','B',1,?,'open_failed')", (time.time(),))
        locked, reason = trader.is_coin_locked("JUP", failed_cooldown_s=60)
        assert locked is True
        assert reason == "coin_recently_failed"
        # Old failure (2 minutes ago, cooldown 60s)
        conn.execute("DELETE FROM trades")
        conn.execute(
            "INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,status) "
            "VALUES('b','s1','JUP','B',1,?,'open_failed')", (time.time() - 120,))
        locked, _ = trader.is_coin_locked("JUP", failed_cooldown_s=60)
        assert locked is False


def test_is_coin_locked_false_when_only_closed_exists():
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        trader = _make_trader(conn)
        conn.execute(
            "INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,status) "
            "VALUES('a','s1','TIA','B',1,?,'closed')", (time.time(),))
        locked, _ = trader.is_coin_locked("TIA")
        assert locked is False


def test_trader_open_blocks_duplicate_coin():
    """End-to-end: trader.open() twice on same coin — second returns coin_locked."""
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        trader = _make_trader(conn, live=False)  # paper mode
        strat = _make_strat()
        # First open succeeds
        r1 = trader.open(strat, _make_sig("OP"), size_usd=20.0)
        assert r1.ok is True
        # Second open on same coin must fail with coin_locked
        r2 = trader.open(strat, _make_sig("OP"), size_usd=20.0)
        assert r2.ok is False
        assert "coin_locked" in (r2.error or "")
        # Only one trades row should exist (no duplicate insert leaked through)
        rows = conn.execute(
            "SELECT cloid, status FROM trades WHERE coin='OP'").fetchall()
        assert len(rows) == 1, f"Expected 1 row, got {len(rows)}: {[dict(r) for r in rows]}"


def test_trader_open_blocks_across_engines():
    """Different engines, same coin — second engine must be blocked."""
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        trader = _make_trader(conn, live=False)
        e1 = _make_strat("e08_dip3d7_td_4h")
        e1.CLOID_PREFIX = "e08_"
        e2 = _make_strat("ict_confluence_4h")
        e2.CLOID_PREFIX = "ict_"
        r1 = trader.open(e1, _make_sig("SUI"), size_usd=20.0)
        assert r1.ok is True
        r2 = trader.open(e2, _make_sig("SUI"), size_usd=20.0)
        assert r2.ok is False
        assert "coin_locked" in (r2.error or "")


def test_migration_demotes_duplicates_on_init():
    """Old DB with duplicate open rows: migration must demote excess so the
    unique index can install. Simulates an existing core deployment with
    pre-fix duplicates."""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "t.db")
        # Build a DB WITHOUT the unique index (raw schema, no constraint)
        raw = sqlite3.connect(path)
        raw.execute("""
            CREATE TABLE trades(
                id INTEGER PRIMARY KEY AUTOINCREMENT, cloid TEXT UNIQUE NOT NULL,
                strategy TEXT NOT NULL, coin TEXT NOT NULL, side TEXT NOT NULL,
                is_long INTEGER NOT NULL, open_ts REAL NOT NULL, open_px REAL,
                size_usd REAL, size_coin REAL, sl_px REAL, tp_px REAL,
                max_hold_bars INTEGER, status TEXT NOT NULL,
                close_retries INTEGER NOT NULL DEFAULT 0, extras_json TEXT)
        """)
        raw.execute("INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,status) "
                    "VALUES('a','s1','BTC','B',1,?,'open')", (time.time() - 100,))
        raw.execute("INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,status) "
                    "VALUES('b','s2','BTC','B',1,?,'open')", (time.time() - 50,))  # newer
        raw.execute("INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,status) "
                    "VALUES('c','s3','BTC','B',1,?,'pending')", (time.time(),))     # newest
        raw.commit()
        raw.close()
        # Now init via the migration path
        conn = persistence.init_db(path)
        # The migration keeps the newest 'open'/'pending' row, demotes others
        statuses = sorted(r["status"] for r in conn.execute(
            "SELECT status FROM trades WHERE coin='BTC'").fetchall())
        # Exactly one open/pending kept; rest reconciled_off_book
        opens = [s for s in statuses if s in ("open", "pending")]
        assert len(opens) == 1, f"Expected 1 open/pending after migration, got {statuses}"
        assert statuses.count("reconciled_off_book") == 2


def test_concurrent_inserts_race_safe():
    """Even bypassing trader.is_coin_locked pre-check, the DB index must reject
    the second insert. Simulates the race between two scan threads firing on
    the same coin simultaneously."""
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        # Thread A inserts pending first — succeeds
        conn.execute(
            "INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,status) "
            "VALUES('a','s1','LINK','B',1,?,'pending')", (time.time(),))
        # Thread B (microseconds later) tries to insert — DB rejects atomically
        try:
            conn.execute(
                "INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,status) "
                "VALUES('b','s2','LINK','B',1,?,'pending')", (time.time(),))
            assert False, "Race should have been blocked by partial unique index"
        except sqlite3.IntegrityError:
            pass
        # Verify only one row
        n = conn.execute("SELECT COUNT(*) AS n FROM trades WHERE coin='LINK'").fetchone()["n"]
        assert n == 1


def test_sweep_stale_pending_demotes_old_rows():
    """sweep_stale_pending() demotes 'pending' rows older than max_age_s."""
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        trader = _make_trader(conn)
        # Stale pending — 10 minutes old
        conn.execute(
            "INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,status) "
            "VALUES('old','s1','DOGE','B',1,?,'pending')", (time.time() - 600,))
        # Fresh pending — 10 seconds old (must NOT be swept)
        conn.execute(
            "INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,status) "
            "VALUES('new','s2','PEPE','B',1,?,'pending')", (time.time() - 10,))
        n = trader.sweep_stale_pending(max_age_s=300)
        assert n == 1
        # Confirm states
        old = conn.execute("SELECT status FROM trades WHERE cloid='old'").fetchone()
        new = conn.execute("SELECT status FROM trades WHERE cloid='new'").fetchone()
        assert old["status"] == "open_failed"
        assert new["status"] == "pending"


def test_open_releases_lock_on_hl_exception():
    """If hl.market_open raises an unexpected exception, the pending row must
    still transition to open_failed (try/finally guarantee). Without this,
    the coin would stay locked until the next sweep."""
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        bus = MagicMock()
        pm = MagicMock()
        pm.register_cloid.return_value = None
        # HL that raises on market_open
        hl = MagicMock()
        hl.market_open.side_effect = RuntimeError("simulated network blowup")
        trader = Trader(conn, bus, pm, hl)
        trader.live_default = True  # force live path to hit hl.market_open
        strat = _make_strat()
        result = trader.open(strat, _make_sig("ARB"), size_usd=20.0)
        assert result.ok is False
        assert "hl_raised" in (result.error or "")
        # Critical: row must NOT be stranded at 'pending'
        row = conn.execute("SELECT status FROM trades WHERE coin='ARB'").fetchone()
        assert row["status"] == "open_failed", \
            f"Expected open_failed after HL exception, got {row['status']}"
        # And the lock is releasable (no open/pending row blocking)
        locked, _ = trader.is_coin_locked("ARB", failed_cooldown_s=0)
        assert locked is False


def test_scan_pre_check_skips_locked_coin(monkeypatch):
    """runner.scan_once with trader= should skip a coin already in trades
    table without calling pm.check (HTTP round-trip avoided)."""
    from strategy_runner import runner
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        trader = _make_trader(conn)
        # Pre-lock SOL via an open row
        conn.execute(
            "INSERT INTO trades(cloid,strategy,coin,side,is_long,open_ts,status) "
            "VALUES('x','e1','SOL','B',1,?,'open')", (time.time(),))
        # Build a fake strategy that always fires for SOL
        class FakeStrat:
            NAME = "fake"
            CLOID_PREFIX = "fk_"
            UNIVERSE = ["SOL"]
            @staticmethod
            def evaluate(coin, bus):
                return _make_sig(coin)
        # Inject into REGISTRY
        monkeypatch.setattr(runner, "REGISTRY", [FakeStrat])
        # PM should NEVER be consulted because pre-check skips
        pm = MagicMock()
        pm.check.side_effect = AssertionError("pm.check called for locked coin")
        bus = MagicMock()
        signals_processed = []
        def on_sig(strat, sig, decision):
            signals_processed.append((strat.NAME, sig.coin))
        # Mock halt + config.strategy_enabled so the loop runs
        monkeypatch.setattr("common.config.strategy_enabled", lambda n: True)
        monkeypatch.setattr("common.halt.is_halted", lambda n: False)
        n = runner.scan_once(bus, pm, on_sig, trader=trader)
        assert n == 0
        assert pm.check.call_count == 0  # not called at all
        assert signals_processed == []
