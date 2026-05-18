"""Verify 4-loss permanent demote behavior. Operator 2026-05-18."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common.cooldown import CooldownTracker, CONSEC_LOSS_ENGINE


def test_4_consec_losses_triggers_permanent_demote(tmp_path):
    db = str(tmp_path / "cd.sqlite")
    cd = CooldownTracker(db)
    eng = "test_engine"
    assert CONSEC_LOSS_ENGINE == 4

    for i in range(3):
        cd.record_close(eng, f"COIN{i}", -1.0, backtest_pf=1.5)
    demoted, _ = cd.is_engine_demoted(eng)
    assert not demoted, "should not demote on 3 losses"

    result = cd.record_close(eng, "COIN3", -1.0, backtest_pf=1.5)
    demoted, reason = cd.is_engine_demoted(eng)
    assert demoted, f"should demote on 4 losses, got reason={reason}"
    assert "paper_demoted" in reason
    assert any(t.get("type") == "engine_demote" for t in result["triggered_cooldowns"])

    # Wins do NOT auto-reinstate (operator-only)
    cd.record_close(eng, "COIN4", +5.0, backtest_pf=1.5)
    demoted, _ = cd.is_engine_demoted(eng)
    assert demoted, "wins must NOT auto-reinstate"

    assert cd.reinstate_engine(eng)
    demoted, _ = cd.is_engine_demoted(eng)
    assert not demoted


def test_coin_4loss_still_1h_cooldown(tmp_path):
    db = str(tmp_path / "cd.sqlite")
    cd = CooldownTracker(db)
    for _ in range(4):
        cd.record_close("e1", "BTC", -1.0, backtest_pf=1.5)
    blocked, _ = cd.is_coin_blocked("e1", "BTC")
    assert blocked


def test_alternating_wins_reset_engine_counter(tmp_path):
    db = str(tmp_path / "cd.sqlite")
    cd = CooldownTracker(db)
    eng = "test_alt"
    # L L W L L L L — 4th consecutive is hit, demote
    cd.record_close(eng, "BTC", -1.0, 1.5)
    cd.record_close(eng, "BTC", -1.0, 1.5)
    cd.record_close(eng, "BTC", +2.0, 1.5)  # win resets engine counter
    demoted, _ = cd.is_engine_demoted(eng)
    assert not demoted
    cd.record_close(eng, "BTC", -1.0, 1.5)
    cd.record_close(eng, "BTC", -1.0, 1.5)
    cd.record_close(eng, "BTC", -1.0, 1.5)
    demoted, _ = cd.is_engine_demoted(eng)
    assert not demoted, "3 consec after win shouldn't demote"
    cd.record_close(eng, "BTC", -1.0, 1.5)
    demoted, _ = cd.is_engine_demoted(eng)
    assert demoted, "4 consec after win should demote"
