"""Tests for sniper service — listing detector, oracle-lag, risk controller."""
from __future__ import annotations

import os
import tempfile


def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    os.unlink(path)
    return path


# ─────────────── Listing Detector ───────────────
def test_listing_detector_bootstrap_then_no_new():
    """First run bootstraps everything as known; no spurious events."""
    from sniper.listing_detector import ListingDetector
    p = fresh_db()
    try:
        det = ListingDetector(state_path=p)
        # Mock fetch_universe to return fixed list
        det.fetch_universe = lambda: ["BTC", "ETH", "SOL"]
        n = det.bootstrap_known_universe()
        assert n == 3
        events = det.check_for_new()
        assert events == []   # no new events after bootstrap
    finally:
        if os.path.exists(p): os.unlink(p)


def test_listing_detector_detects_new_coin():
    from sniper.listing_detector import ListingDetector
    p = fresh_db()
    try:
        det = ListingDetector(state_path=p)
        det.fetch_universe = lambda: ["BTC", "ETH"]
        det.bootstrap_known_universe()
        # Universe expands
        det.fetch_universe = lambda: ["BTC", "ETH", "NEWCOIN"]
        events = det.check_for_new()
        assert len(events) == 1
        assert events[0].coin == "NEWCOIN"
        assert events[0].hl_universe_index == 2
    finally:
        if os.path.exists(p): os.unlink(p)


def test_listing_detector_survives_restart():
    """Restart with same DB → no spurious events."""
    from sniper.listing_detector import ListingDetector
    p = fresh_db()
    try:
        det1 = ListingDetector(state_path=p)
        det1.fetch_universe = lambda: ["BTC", "ETH"]
        det1.bootstrap_known_universe()
        # New instance, same DB
        det2 = ListingDetector(state_path=p)
        det2.fetch_universe = lambda: ["BTC", "ETH"]
        events = det2.check_for_new()
        assert events == []
        # New coin appears
        det2.fetch_universe = lambda: ["BTC", "ETH", "BRAND_NEW"]
        events = det2.check_for_new()
        assert len(events) == 1
    finally:
        if os.path.exists(p): os.unlink(p)


def test_listing_detector_handled_mark():
    from sniper.listing_detector import ListingDetector
    import time
    p = fresh_db()
    try:
        det = ListingDetector(state_path=p)
        det.fetch_universe = lambda: ["BTC"]
        det.bootstrap_known_universe()
        det.fetch_universe = lambda: ["BTC", "FOO"]
        events = det.check_for_new()
        assert len(events) == 1
        ev = events[0]
        det.mark_handled(ev.detected_ts, "FOO")
        recents = det.recent_listings(0)
        foo = [r for r in recents if r["coin"] == "FOO"][0]
        assert foo["handled"] == 1
    finally:
        if os.path.exists(p): os.unlink(p)


# ─────────────── Oracle Lag ───────────────
def test_evaluate_snipe_no_cex_listing():
    """No CEX equivalent → don't fire."""
    from sniper import oracle_lag
    oracle_lag.fetch_hl_mark = lambda c, **kw: 100.0
    oracle_lag.cex_consensus = lambda c, **kw: (None, "no_cex_listing")
    d = oracle_lag.evaluate_snipe("MYSTERY")
    assert not d.fire
    assert "no_cex" in d.reason


def test_evaluate_snipe_divergence_below_threshold():
    """HL ~ CEX → no fire."""
    from sniper import oracle_lag
    oracle_lag.fetch_hl_mark = lambda c, **kw: 100.0
    oracle_lag.cex_consensus = lambda c, **kw: (101.0, "binance")   # 1% diff
    d = oracle_lag.evaluate_snipe("BTC", divergence_threshold=0.05)
    assert not d.fire
    assert "div_below_threshold" in d.reason


def test_evaluate_snipe_hl_below_cex_fires_long():
    """HL price < CEX → long HL toward CEX."""
    from sniper import oracle_lag
    oracle_lag.fetch_hl_mark = lambda c, **kw: 90.0
    oracle_lag.cex_consensus = lambda c, **kw: (100.0, "binance")   # 10% gap
    d = oracle_lag.evaluate_snipe("BTC", divergence_threshold=0.05)
    assert d.fire
    assert d.is_long
    assert d.divergence_pct > 0


def test_evaluate_snipe_hl_above_cex_fires_short():
    """HL price > CEX → short HL toward CEX."""
    from sniper import oracle_lag
    oracle_lag.fetch_hl_mark = lambda c, **kw: 110.0
    oracle_lag.cex_consensus = lambda c, **kw: (100.0, "binance")
    d = oracle_lag.evaluate_snipe("BTC", divergence_threshold=0.05)
    assert d.fire
    assert not d.is_long
    assert d.divergence_pct < 0


# ─────────────── Risk Controller ───────────────
def test_sniper_risk_kill_blocks():
    from sniper.risk import SniperRiskController
    p = fresh_db()
    try:
        risk = SniperRiskController(db_path=p)
        risk.set_killed("test")
        r = risk.check("BTC", 491.0, 0.10)
        assert not r.allow
        assert "killed" in r.reason
    finally:
        if os.path.exists(p): os.unlink(p)


def test_sniper_risk_daily_cap():
    from sniper.risk import SniperRiskController
    p = fresh_db()
    try:
        # Grant approval to bypass operator gate for this test
        os.environ["SNIPER_REQUIRE_APPROVAL"] = "0"
        risk = SniperRiskController(db_path=p)
        # First trade allowed
        r = risk.check("BTC", 491.0, 0.10)
        assert r.allow
        risk.record_trade("BTC", r.margin_usd, 0.10)
        # Second blocked
        r = risk.check("ETH", 491.0, 0.10)
        assert not r.allow
        assert "daily_cap" in r.reason
        del os.environ["SNIPER_REQUIRE_APPROVAL"]
    finally:
        if os.path.exists(p): os.unlink(p)


def test_sniper_risk_3_consec_losses_kills():
    from sniper.risk import SniperRiskController
    p = fresh_db()
    try:
        os.environ["SNIPER_REQUIRE_APPROVAL"] = "0"
        risk = SniperRiskController(db_path=p)
        # 3 trades with losses
        import time as _t
        for i in range(3):
            risk.record_trade(f"C{i}", 50.0, 0.10)
            _t.sleep(0.01)
            risk.record_close(f"C{i}", -10.0, 491.0 - (i+1)*10)
            _t.sleep(0.01)
        killed, reason = risk.is_killed()
        assert killed
        assert "consec" in reason
        del os.environ["SNIPER_REQUIRE_APPROVAL"]
    finally:
        if os.path.exists(p): os.unlink(p)


def test_sniper_risk_approval_gate_first_10_trades():
    from sniper.risk import SniperRiskController
    p = fresh_db()
    try:
        os.environ["SNIPER_REQUIRE_APPROVAL"] = "1"
        risk = SniperRiskController(db_path=p)
        # No approval → blocked
        r = risk.check("BTC", 491.0, 0.10)
        assert not r.allow
        assert "approval" in r.reason
        # With approval → allowed
        risk.grant_approval("BTC", "operator")
        r = risk.check("BTC", 491.0, 0.10)
        assert r.allow
        del os.environ["SNIPER_REQUIRE_APPROVAL"]
    finally:
        if os.path.exists(p): os.unlink(p)


def test_sniper_risk_size_50pct_after_5_trades():
    """First 5 trades: 25% size. After: 50%."""
    from sniper.risk import SniperRiskController
    p = fresh_db()
    try:
        os.environ["SNIPER_REQUIRE_APPROVAL"] = "0"
        os.environ["SNIPER_MAX_PER_DAY"] = "100"  # bypass daily cap for sizing test
        risk = SniperRiskController(db_path=p)
        # First trade: size capped at 25% (council: half-size for first 5)
        r = risk.check("BTC", 491.0, 0.10)
        assert r.allow
        # margin = 0.25 * 491 = 122.75
        assert 120 < r.margin_usd < 125
        # Simulate 5 prior trades by recording them
        import time as _t
        for i in range(5):
            risk.record_trade(f"X{i}", 50.0, 0.10)
            risk.record_close(f"X{i}", 5.0, 491.0)   # winners (no consec-loss kill)
            _t.sleep(0.01)
        # Now full 50% size
        r = risk.check("ETH", 491.0, 0.10)
        assert r.allow
        # margin = 0.50 * 491 = 245.5
        assert 240 < r.margin_usd < 250
        del os.environ["SNIPER_REQUIRE_APPROVAL"]
        del os.environ["SNIPER_MAX_PER_DAY"]
    finally:
        if os.path.exists(p): os.unlink(p)


def test_sniper_risk_force_kill_env():
    from sniper.risk import SniperRiskController
    p = fresh_db()
    try:
        os.environ["SNIPER_FORCE_KILL"] = "1"
        risk = SniperRiskController(db_path=p)
        r = risk.check("BTC", 491.0, 0.10)
        assert not r.allow
        assert "force_kill" in r.reason
        del os.environ["SNIPER_FORCE_KILL"]
    finally:
        if os.path.exists(p): os.unlink(p)


# ─────────────── Executor (paper mode) ───────────────
def test_executor_paper_mode_returns_simulated_fill():
    os.environ["SNIPER_LIVE_TRADING"] = "0"
    from sniper.executor import SniperExecutor
    from sniper import oracle_lag
    oracle_lag.fetch_hl_mark = lambda c: 100.0
    ex = SniperExecutor(leverage=5.0)
    r = ex.fire("BTC", is_long=True, margin_usd=50.0)
    assert r.success
    assert r.paper
    assert r.fill_px > 100.0   # slipped up on long entry
    # notional = 50 × 5 = 250
    assert 240 < r.notional_usd < 260
