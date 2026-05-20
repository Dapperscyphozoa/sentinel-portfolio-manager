"""Tests for common.poly_persistence schema + connection."""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import pytest

from common import poly_persistence


@pytest.fixture
def tmp_state():
    with tempfile.TemporaryDirectory() as d:
        os.environ["STATE_DIR"] = d
        yield d
        os.environ.pop("STATE_DIR", None)


def test_init_creates_db(tmp_state):
    path = poly_persistence.init_poly_db(tmp_state)
    assert os.path.exists(path)


def test_schema_has_all_tables(tmp_state):
    poly_persistence.init_poly_db(tmp_state)
    conn = poly_persistence.connect_poly(tmp_state)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        names = {r[0] for r in rows}
    finally:
        conn.close()
    expected = {
        "poly_signals", "poly_orders", "poly_fills",
        "poly_positions", "poly_resolutions", "poly_quotes",
        "poly_cl_validation",
    }
    assert expected.issubset(names), f"missing: {expected - names}"


def test_inserts_and_retrieval(tmp_state):
    poly_persistence.init_poly_db(tmp_state)
    conn = poly_persistence.connect_poly(tmp_state)
    try:
        conn.execute(
            "INSERT INTO poly_signals(ts, strategy, market_id, asset, token,"
            " side, price, size_usdc)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (1234.5, "cl_predictor", "m1", "BTC", "YES", "BUY", 0.55, 10.0))
        rows = conn.execute("SELECT strategy, market_id FROM poly_signals").fetchall()
        assert rows == [("cl_predictor", "m1")]
    finally:
        conn.close()
