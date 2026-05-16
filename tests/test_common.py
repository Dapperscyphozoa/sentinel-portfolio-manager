"""Unit tests for common/. Targets Session 1 acceptance:
    pytest tests/test_common.py -k "schema or cloid"
"""
from __future__ import annotations

import os
import re
import tempfile

import pytest

from common import persistence, halt, hl_exchange, config


# -------------------- schema --------------------

def test_schema_creates_all_tables():
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "test.db")
        conn = persistence.init_db(db_path)
        tables = persistence.table_names(conn)
        for required in ("signals", "trades", "closures", "halts", "spend"):
            assert required in tables, f"missing table: {required}"
        conn.close()


def test_schema_idempotent():
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "test.db")
        conn = persistence.init_db(db_path)
        conn.close()
        conn = persistence.init_db(db_path)  # second init must not error
        tables = persistence.table_names(conn)
        assert "signals" in tables
        conn.close()


def test_schema_insert_signal():
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "test.db")
        conn = persistence.init_db(db_path)
        conn.execute(
            "INSERT INTO signals(ts,strategy,coin,side,is_long,ref_price,sl_px,tp_px,max_hold_bars,fire_reason,extras_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (1.0, "fsp", "BTC", "B", 1, 60000.0, 59400.0, 61800.0, 48, "funding_extreme", "{}"),
        )
        row = conn.execute("SELECT strategy,coin,is_long FROM signals").fetchone()
        assert row["strategy"] == "fsp"
        assert row["coin"] == "BTC"
        assert row["is_long"] == 1
        conn.close()


# -------------------- cloid --------------------

CLOID_RE = re.compile(r"^0x[0-9a-f]{32}$")


def test_cloid_format():
    c = hl_exchange.make_cloid("fspv1_", "BTC")
    assert CLOID_RE.match(c), f"cloid not 0x+32hex: {c}"


def test_cloid_unique_with_nonce():
    a = hl_exchange.make_cloid("fspv1_", "BTC", nonce=1, ts_ms=1)
    b = hl_exchange.make_cloid("fspv1_", "BTC", nonce=2, ts_ms=1)
    assert a != b


def test_cloid_deterministic_same_inputs():
    a = hl_exchange.make_cloid("vsqzr_", "ETH", nonce=42, ts_ms=12345)
    b = hl_exchange.make_cloid("vsqzr_", "ETH", nonce=42, ts_ms=12345)
    assert a == b


def test_cloid_prefix_change_changes_hash():
    a = hl_exchange.make_cloid("fspv1_", "BTC", nonce=1, ts_ms=1)
    b = hl_exchange.make_cloid("vsqzr_", "BTC", nonce=1, ts_ms=1)
    assert a != b


# -------------------- halt --------------------

def test_halt_token_rejects_when_unset(monkeypatch):
    monkeypatch.delenv("HALT_TOKEN", raising=False)
    assert halt.halt_token_ok("anything") is False
    assert halt.halt_token_ok(None) is False


def test_halt_token_constant_time(monkeypatch):
    monkeypatch.setenv("HALT_TOKEN", "secret123")
    assert halt.halt_token_ok("secret123") is True
    assert halt.halt_token_ok("secret124") is False
    assert halt.halt_token_ok("") is False
    assert halt.halt_token_ok(None) is False


def test_halt_set_and_check():
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "test.db")
        conn = persistence.init_db(db_path)
        halt.set_halt(conn, "fsp", True, reason="test", actor="pytest")
        assert halt.is_halted("fsp") is True
        halt.set_halt(conn, "fsp", False, reason="resume", actor="pytest")
        assert halt.is_halted("fsp") is False
        conn.close()


def test_halt_all_blocks_individual():
    with tempfile.TemporaryDirectory() as d:
        db_path = os.path.join(d, "test.db")
        conn = persistence.init_db(db_path)
        halt.set_halt(conn, "fsp", False, actor="pytest")
        halt.halt_all(conn, reason="drawdown", actor="monitor")
        assert halt.is_halted("fsp") is True  # __all__ wildcard
        # cleanup so other tests aren't polluted
        halt.set_halt(conn, "__all__", False, actor="pytest")
        conn.close()


# -------------------- config --------------------

def test_config_required(monkeypatch):
    monkeypatch.delenv("SOME_MISSING_VAR", raising=False)
    with pytest.raises(RuntimeError):
        config.get("SOME_MISSING_VAR", required=True)


def test_config_bool(monkeypatch):
    monkeypatch.setenv("FLAG", "1")
    assert config.get_bool("FLAG") is True
    monkeypatch.setenv("FLAG", "false")
    assert config.get_bool("FLAG") is False
    monkeypatch.delenv("FLAG")
    assert config.get_bool("FLAG", default=True) is True


def test_strategy_enabled(monkeypatch):
    monkeypatch.setenv("STRATEGY_FSP_ENABLED", "1")
    monkeypatch.setenv("STRATEGY_VSQ_ENABLED", "0")
    assert config.strategy_enabled("fsp") is True
    assert config.strategy_enabled("vsq") is False
    assert config.strategy_enabled("nonexistent") is False
