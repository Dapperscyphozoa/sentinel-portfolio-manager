"""Tests for monitor/routines/auto_demote.py."""
from __future__ import annotations

import json
import os
import tempfile
import time
from unittest.mock import patch

from common import persistence
from monitor.routines import auto_demote


def _seed_closures(conn, strategy, pnls, base_ts=None):
    base_ts = base_ts or time.time()
    for i, pnl in enumerate(pnls):
        cloid = f"0x{strategy[:6]}_{i:04x}"
        ts = base_ts - (len(pnls) - i) * 3600
        conn.execute(
            "INSERT INTO closures(cloid, strategy, coin, is_long, open_ts, close_ts, "
            "open_px, close_px, size_coin, pnl_usd, fees_usd, close_reason, extras_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cloid, strategy, "BTC", 1, ts - 3600, ts,
             100.0, 100.0 + pnl, 1.0, pnl, 0.0, "test",
             json.dumps({"live": True}))
        )


def test_clean_state_no_demotion():
    """Engines without enough live closures should not trigger anything."""
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "m.db"))
        # No closures at all
        r = auto_demote.run(conn)
        assert r["severity"] == "CLEAN"
        assert r["demoted"] == []
        # Most engines should be insufficient_n
        assert r["insufficient_n_count"] > 0


def test_demote_when_rolling_pf_below_threshold():
    """Engine with bt_pf 2.0 and rolling_PF 0.5 should demote (threshold 1.4)."""
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "m.db"))
        # ict_confluence_1d has bt_pf 3.35; threshold = 0.7 × 3.35 = 2.345
        # Seed 30 trades: 5 wins of +1, 25 losses of -1 → PF = 5/25 = 0.2 (well below 2.345)
        pnls = [1.0]*5 + [-1.0]*25
        _seed_closures(conn, "ict_confluence_1d", pnls)
        # Block actual Render API calls
        with patch.dict(os.environ, {"RENDER_API_TOKEN": "", "HALT_TOKEN": ""}, clear=False):
            r = auto_demote.run(conn)
        assert r["severity"] == "HIGH"
        assert any(d["engine"] == "ict_confluence_1d" for d in r["demoted"])


def test_no_demote_when_pf_above_threshold():
    """Engine with bt_pf 2.0 and rolling_PF 1.6 should NOT demote (threshold 1.4)."""
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "m.db"))
        # e09_pump3d10_td_1d has bt_pf 2.20; threshold = 1.54
        # Seed 30: 20 wins +2, 10 losses -2 → PF = 40/20 = 2.0 (above threshold)
        pnls = [2.0]*20 + [-2.0]*10
        _seed_closures(conn, "e09_pump3d10_td_1d", pnls)
        with patch.dict(os.environ, {"RENDER_API_TOKEN": "", "HALT_TOKEN": ""}, clear=False):
            r = auto_demote.run(conn)
        assert r["severity"] == "CLEAN"
        assert r["demoted"] == []
        checked = r["checked"].get("e09_pump3d10_td_1d", {})
        assert checked.get("verdict") == "keep"


def test_skips_engines_below_min_bt_pf():
    """Engines with bt_pf < 1.0 (already known bad) should not be checked."""
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "m.db"))
        # e08_dip3d10_td_1d has bt_pf 0.50 (below MIN_BT_PF) — should not appear
        pnls = [1.0]*30
        _seed_closures(conn, "e08_dip3d10_td_1d", pnls)
        with patch.dict(os.environ, {"RENDER_API_TOKEN": "", "HALT_TOKEN": ""}, clear=False):
            r = auto_demote.run(conn)
        assert "e08_dip3d10_td_1d" not in r["checked"]


def test_paper_trades_excluded():
    """Paper trades (live=False in extras) must not count toward rolling_PF."""
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "m.db"))
        # Insert 30 closures all with live=False — should be insufficient_n
        for i in range(30):
            conn.execute(
                "INSERT INTO closures(cloid, strategy, coin, is_long, open_ts, close_ts, "
                "open_px, close_px, size_coin, pnl_usd, fees_usd, close_reason, extras_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"0x_{i}", "ict_confluence_1d", "BTC", 1, time.time(), time.time(),
                 100.0, 100.0, 1.0, -1.0, 0.0, "test",
                 json.dumps({"live": False}))
            )
        with patch.dict(os.environ, {"RENDER_API_TOKEN": "", "HALT_TOKEN": ""}, clear=False):
            r = auto_demote.run(conn)
        # Should not demote — paper trades shouldn't trigger
        assert "ict_confluence_1d" not in [d["engine"] for d in r["demoted"]]


def test_demote_triggers_render_env_call():
    """When demoting, _render_set_env should be invoked with LIVE=0."""
    with tempfile.TemporaryDirectory() as d:
        conn = persistence.init_db(os.path.join(d, "m.db"))
        pnls = [1.0]*5 + [-1.0]*25  # PF 0.2
        _seed_closures(conn, "ict_confluence_1d", pnls)
        with patch("monitor.routines.auto_demote._render_set_env") as render_mock, \
             patch("monitor.routines.auto_demote._runtime_halt") as halt_mock:
            render_mock.return_value = True
            halt_mock.return_value = True
            r = auto_demote.run(conn)
        # Should have tried to set STRATEGY_ICT_CONFLUENCE_1D_LIVE=0
        render_mock.assert_any_call("STRATEGY_ICT_CONFLUENCE_1D_LIVE", "0")
        halt_mock.assert_any_call("ict_confluence_1d")
