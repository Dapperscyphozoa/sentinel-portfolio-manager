"""Verify 4-loss behavior post-PF-replacement (operator 2026-05-19).

The 4-loss auto-demote ACTION was removed when PF gate became the sole
engine-edge gate. The consec-loss COUNTER is still tracked because
monitor.routines.four_loss_audit consumes it as the audit trigger.

Coin-level 4-loss → 1h coin cooldown is unchanged.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.cooldown import CooldownTracker, CONSEC_LOSS_ENGINE


def test_4_consec_losses_does_not_auto_demote(tmp_path):
    """Engine-level 4-loss must NOT trigger automatic permanent demote.
    PF gate alone decides engine fate."""
    db = str(tmp_path / "cd.sqlite")
    cd = CooldownTracker(db)
    eng = "test_engine"
    assert CONSEC_LOSS_ENGINE == 4

    for i in range(CONSEC_LOSS_ENGINE):
        result = cd.record_close(eng, f"COIN{i}", -1.0, backtest_pf=1.5)

    demoted, _ = cd.is_engine_demoted(eng)
    assert not demoted, "4-loss must not auto-demote (PF gate handles it)"
    assert not any(t.get("type") == "engine_demote"
                   for t in result["triggered_cooldowns"]), \
        "no engine_demote events should fire"


def test_consec_loss_counter_still_tracks(tmp_path):
    """The counter is still updated so monitor.four_loss_audit can read it."""
    db = str(tmp_path / "cd.sqlite")
    cd = CooldownTracker(db)
    eng = "test_engine"
    for i in range(CONSEC_LOSS_ENGINE):
        cd.record_close(eng, f"COIN{i}", -1.0, backtest_pf=1.5)
    c = cd._conn()
    row = c.execute(
        "SELECT count FROM engine_consec_losses WHERE engine=?", (eng,)
    ).fetchone()
    c.close()
    assert row is not None
    assert int(row["count"]) >= CONSEC_LOSS_ENGINE, \
        f"counter should reach {CONSEC_LOSS_ENGINE}, got {row['count']}"


def test_win_resets_consec_loss_counter(tmp_path):
    db = str(tmp_path / "cd.sqlite")
    cd = CooldownTracker(db)
    eng = "test_engine"
    for i in range(3):
        cd.record_close(eng, "BTC", -1.0, backtest_pf=1.5)
    cd.record_close(eng, "BTC", +2.0, backtest_pf=1.5)
    c = cd._conn()
    row = c.execute(
        "SELECT count FROM engine_consec_losses WHERE engine=?", (eng,)
    ).fetchone()
    c.close()
    assert int(row["count"]) == 0, "win should reset engine counter"


def test_coin_4loss_still_1h_cooldown(tmp_path):
    """Coin-level 4-loss → 1h cooldown remains unchanged."""
    db = str(tmp_path / "cd.sqlite")
    cd = CooldownTracker(db)
    for _ in range(4):
        cd.record_close("e1", "BTC", -1.0, backtest_pf=1.5)
    blocked, _ = cd.is_coin_blocked("e1", "BTC")
    assert blocked


def test_pf_gate_fires_at_n10_not_n22(tmp_path):
    """PF gate floor was lowered from 22 to 10 (operator 2026-05-19).
    Tests both: gate quiet at n=9, gate active at n=10 with bad PF.

    engine_pnl PK is (engine, ts) so test passes explicit now_ts to spread
    rows across distinct seconds — same-second writes would collapse.
    """
    from common.cooldown import MIN_TRADES_FOR_PF_CHECK
    assert MIN_TRADES_FOR_PF_CHECK == 10
    db = str(tmp_path / "cd.sqlite")
    cd = CooldownTracker(db)
    eng = "pf_test"
    import time as _t
    base_ts = int(_t.time()) - 100
    # 9 trades with bad PF (alt -1.0 / +0.1 → gross_win 0.4, gross_loss 5 → PF 0.08)
    for i in range(9):
        cd.record_close(eng, f"C{i}",
                        -1.0 if i % 2 == 0 else +0.1,
                        backtest_pf=2.0, now_ts=base_ts + i)
    blocked, _ = cd.is_engine_blocked(eng, now_ts=base_ts + 10)
    assert not blocked, "PF gate must NOT fire below n=10"

    # 10th trade tips count to 10. Live PF ≈ 0.08 << 0.74×2.0=1.48 → gate fires.
    result = cd.record_close(eng, "C9", -1.0, backtest_pf=2.0, now_ts=base_ts + 9)
    blocked, reason = cd.is_engine_blocked(eng, now_ts=base_ts + 10)
    assert blocked, f"PF gate must fire at n=10 with bad PF (reason={reason!r})"
    assert "live_pf" in reason or any("live_pf" in t.get("reason", "")
                                       for t in result["triggered_cooldowns"])


def test_pf_gate_quiet_when_live_pf_healthy(tmp_path):
    """No cooldown when live PF >= 0.74 × bt_pf (healthy engine).

    Uses small per-trade pnl to keep cumulative drawdown well under the
    MAX_DD_PCT=12% threshold (otherwise the DD gate fires unrelated to PF).
    """
    import time as _t
    db = str(tmp_path / "cd.sqlite")
    cd = CooldownTracker(db)
    eng = "pf_healthy"
    base_ts = int(_t.time()) - 100
    # 10 trades: 7 small wins (+0.5), 3 small losses (-0.1).
    # gross_win=3.5, gross_loss=0.3 → PF≈11.7  >>  0.74×2.0=1.48
    # cum trajectory peaks at 3.5, only tiny dips → DD stays ~0
    pattern = [+0.5, +0.5, +0.5, +0.5, +0.5, -0.1, +0.5, -0.1, +0.5, -0.1]
    for i, pnl in enumerate(pattern):
        cd.record_close(eng, f"C{i}", pnl, backtest_pf=2.0, now_ts=base_ts + i)
    blocked, reason = cd.is_engine_blocked(eng, now_ts=base_ts + 11)
    assert not blocked, f"healthy PF must not trigger cooldown (reason={reason!r})"


def test_operator_manual_demote_still_works(tmp_path):
    """Operator can still manually demote an engine via demote_engine();
    auto-recovery is also manual via reinstate_engine()."""
    db = str(tmp_path / "cd.sqlite")
    cd = CooldownTracker(db)
    eng = "test_manual"
    cd.demote_engine(eng, reason="operator_manual")
    demoted, reason = cd.is_engine_demoted(eng)
    assert demoted
    assert "operator_manual" in reason
    assert cd.reinstate_engine(eng)
    demoted, _ = cd.is_engine_demoted(eng)
    assert not demoted
