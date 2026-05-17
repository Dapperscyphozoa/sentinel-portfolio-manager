"""Tests for monitor/routines/lock_audit.py."""
from __future__ import annotations

import os
import tempfile
import time
from unittest.mock import patch, MagicMock

from common import persistence
from monitor.routines import lock_audit


def _trades(*items):
    """Build a fake /strategy/state response from (cloid, coin, status, age_s) tuples."""
    now = time.time()
    out = []
    for cloid, coin, status, age_s in items:
        out.append({
            "cloid": cloid, "strategy": "test_eng",
            "coin": coin, "status": status,
            "open_ts": now - age_s,
        })
    return out


def _mock_state(trades_list):
    """Build a httpx response mock returning trades_list as JSON."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = trades_list
    resp.raise_for_status.return_value = None
    return resp


def test_clean_state_returns_clean():
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "m.db"))
        trades = _trades(
            ("a", "BTC", "open", 100),
            ("b", "ETH", "open", 200),
            ("c", "SOL", "closed", 5000),
        )
        with patch("monitor.routines.lock_audit.httpx") as hx:
            hx.get.return_value = _mock_state(trades)
            os.environ["STRATEGY_RUNNER_URL"] = "http://test"
            r = lock_audit.run(conn)
        assert r["severity"] == "CLEAN"
        assert r["duplicate_coin_opens"] == {}
        assert r["stale_pending_rows"] == []
        assert r["cooldown_violations"] == {}
        assert r["open_count"] == 2


def test_duplicate_coin_open_raises_critical():
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "m.db"))
        trades = _trades(
            ("a", "BTC", "open", 100),
            ("b", "BTC", "open", 50),  # DUPLICATE — lock violation
        )
        with patch("monitor.routines.lock_audit.httpx") as hx:
            hx.get.return_value = _mock_state(trades)
            os.environ["STRATEGY_RUNNER_URL"] = "http://test"
            os.environ["AUTO_HALT_ON_LOCK_VIOLATION"] = "0"  # no halt during test
            r = lock_audit.run(conn)
        assert r["severity"] == "CRITICAL"
        assert r["duplicate_coin_opens"] == {"BTC": 2}


def test_stale_pending_raises_high():
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "m.db"))
        trades = _trades(
            ("a", "ETH", "open", 100),
            ("stale", "SOL", "pending", 600),  # 10min old — sweep should have run
        )
        with patch("monitor.routines.lock_audit.httpx") as hx:
            hx.get.return_value = _mock_state(trades)
            os.environ["STRATEGY_RUNNER_URL"] = "http://test"
            r = lock_audit.run(conn)
        assert r["severity"] == "HIGH"
        assert len(r["stale_pending_rows"]) == 1
        assert r["stale_pending_rows"][0]["coin"] == "SOL"


def test_cooldown_violation_raises_high():
    """4+ open_failed on same coin in last 10min = cooldown not working."""
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "m.db"))
        trades = _trades(
            ("f1", "APT", "open_failed", 60),
            ("f2", "APT", "open_failed", 120),
            ("f3", "APT", "open_failed", 180),
            ("f4", "APT", "open_failed", 240),
            ("f5", "APT", "open_failed", 300),
        )
        with patch("monitor.routines.lock_audit.httpx") as hx:
            hx.get.return_value = _mock_state(trades)
            os.environ["STRATEGY_RUNNER_URL"] = "http://test"
            r = lock_audit.run(conn)
        assert r["severity"] == "HIGH"
        assert r["cooldown_violations"] == {"APT": 5}


def test_auto_halt_fires_on_violation():
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "m.db"))
        trades = _trades(
            ("a", "BTC", "open", 100),
            ("b", "BTC", "open", 50),  # violation
        )
        # Set up env BEFORE calling
        os.environ["AUTO_HALT_ON_LOCK_VIOLATION"] = "1"
        os.environ["HALT_TOKEN"] = "test_token"
        os.environ["STRATEGY_RUNNER_URL"] = "http://test"
        halt_resp = MagicMock()
        halt_resp.status_code = 200
        halt_resp.text = '{"ok":true,"halted":["all"]}'
        with patch("monitor.routines.lock_audit.httpx") as hx:
            hx.get.return_value = _mock_state(trades)
            hx.post.return_value = halt_resp
            r = lock_audit.run(conn)
        assert r["halted_action"] is not None
        assert r["halted_action"]["status_code"] == 200
        # Verify the POST was to /halt/all with token
        call_args = hx.post.call_args
        assert "halt/all" in call_args[0][0]
        assert call_args[1]["headers"]["X-Halt-Token"] == "test_token"


def test_fetch_error_returned_cleanly():
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "m.db"))
        with patch("monitor.routines.lock_audit.httpx") as hx:
            hx.get.side_effect = Exception("connection refused")
            os.environ["STRATEGY_RUNNER_URL"] = "http://test"
            r = lock_audit.run(conn)
        assert "error" in r
        assert "fetch_failed" in r["error"]
