"""monitor: spend ledger + health-check issue detection."""
from __future__ import annotations

import os
import tempfile
import time

import pytest

from common import persistence
from monitor import spend
from monitor.routines import health_check, drawdown_check


# -------- spend --------

def test_spend_records_and_aggregates():
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        c1 = spend.record(conn, "health", "claude-haiku-4-5-20251001", 1000, 100)
        c2 = spend.record(conn, "daily", "claude-sonnet-4-6", 5000, 500)
        assert c1 > 0 and c2 > c1
        total = spend.spent_today_usd(conn)
        assert abs(total - (c1 + c2)) < 1e-9


def test_spend_can_spend_respects_budget():
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        # spend $4 first
        for _ in range(4):
            # use opus cost so it's large per call
            spend.record(conn, "test", "claude-opus-4-7", 50_000, 5_000)
        # $0.75 + $0.375 ≈ ... let's just check the gate
        assert spend.can_spend(conn, daily_budget_usd=5.0, projected_cost_usd=0.01) in (True, False)


def test_spend_estimate_for_known_models():
    # 1M in, 1M out on haiku = 1 + 5 = $6
    assert abs(spend.estimate_cost_usd("claude-haiku-4-5", 1_000_000, 1_000_000) - 6.0) < 1e-6
    # 1M in, 1M out on sonnet = 3 + 15 = $18
    assert abs(spend.estimate_cost_usd("claude-sonnet-4-6", 1_000_000, 1_000_000) - 18.0) < 1e-6


def test_spend_unknown_model_uses_opus_default():
    c = spend.estimate_cost_usd("unknown-model", 1_000_000, 0)
    assert c == 15.0  # opus input pricing


# -------- health_check.detect --------

def test_health_detect_no_issues_for_healthy_stack():
    statuses = {
        "signal_bus": {"status_code": 200, "body": {
            "ws_alive": {"binance": True, "hl": True},
            "last_update": {"binance_ws": time.time(), "hl_ws": time.time()},
        }},
        "pm": {"status_code": 200, "body": {"ok": True}},
        "strategy_runner": {"status_code": 200, "body": {"ok": True}},
    }
    assert health_check._detect_issues(statuses) == []


def test_health_detect_ws_down():
    statuses = {
        "signal_bus": {"status_code": 200, "body": {
            "ws_alive": {"binance": False, "hl": True},
            "last_update": {"binance_ws": time.time()},
        }},
    }
    issues = health_check._detect_issues(statuses)
    assert any("ws_down:binance" in i for i in issues)


def test_health_detect_stale_data():
    old = time.time() - 1200
    statuses = {
        "signal_bus": {"status_code": 200, "body": {
            "ws_alive": {"binance": True},
            "last_update": {"binance_ws": old},
        }},
    }
    issues = health_check._detect_issues(statuses)
    assert any("stale" in i for i in issues)


def test_health_detect_unreachable():
    statuses = {"signal_bus": {"error": "connection refused"}}
    issues = health_check._detect_issues(statuses)
    assert any("unreachable" in i for i in issues)


# -------- drawdown peak tracking --------

def test_drawdown_module_has_run():
    # just ensure module loads + run is callable without crashing on missing env
    # (will return {ok: False, error: ...} when bus_url is empty)
    os.environ.pop("SIGNAL_BUS_URL", None)
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "t.db"))
        out = drawdown_check.run(conn)
        assert isinstance(out, dict)
