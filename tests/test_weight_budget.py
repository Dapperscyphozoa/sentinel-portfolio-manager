"""Tests for common.weight_budget — HL rate-limit safety net."""
from __future__ import annotations

import threading
import time

import pytest

from common.weight_budget import WeightBudget, get_budget, WEIGHT_CHEAP, WEIGHT_NORMAL


def test_can_spend_within_budget():
    b = WeightBudget(max_weight_per_min=100)
    assert b.can_spend(50) is True
    assert b.can_spend(100) is True
    assert b.can_spend(101) is False


def test_spend_records_and_blocks():
    b = WeightBudget(max_weight_per_min=100)
    assert b.spend(40) is True
    assert b.spend(40) is True
    assert b.spend(40) is False  # 80+40 > 100
    assert b.current_weight() == 80


def test_rolling_window_evicts_old_entries():
    b = WeightBudget(max_weight_per_min=100, window_s=0.3)
    assert b.spend(80) is True
    assert b.spend(80) is False  # over budget
    time.sleep(0.4)  # window expired
    assert b.spend(80) is True   # old entries dropped, fresh budget
    assert b.current_weight() == 80


def test_stats_reflects_state():
    b = WeightBudget(max_weight_per_min=100)
    b.spend(40)
    b.spend(20)
    s = b.stats()
    assert s["current_weight_per_min"] == 60
    assert s["max_weight_per_min"] == 100
    assert s["utilization_pct"] == 60.0
    assert s["total_spent"] == 60
    assert s["total_rejected"] == 0


def test_total_rejected_increments_on_overflow():
    b = WeightBudget(max_weight_per_min=100)
    b.spend(100)
    b.spend(1)  # rejected
    b.spend(1)  # rejected
    assert b.stats()["total_rejected"] == 2


def test_thread_safe_concurrent_spends():
    """Many threads racing to spend should never exceed the cap."""
    b = WeightBudget(max_weight_per_min=1000)
    successes = []
    lock = threading.Lock()

    def worker():
        for _ in range(100):
            if b.spend(10):
                with lock:
                    successes.append(1)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads: t.start()
    for t in threads: t.join()

    total_spent = b.current_weight()
    # Total successful spends × 10 weight each = current_weight
    assert sum(successes) * 10 == total_spent
    # And total must NEVER exceed the cap
    assert total_spent <= 1000


def test_acquire_blocks_then_succeeds_when_budget_clears():
    """acquire() returns True if budget frees up within timeout."""
    b = WeightBudget(max_weight_per_min=100, window_s=0.2)
    b.spend(100)
    # Budget full; acquire(50) with timeout 0.5 should succeed after window
    started = time.time()
    ok = b.acquire(50, timeout_s=0.5)
    elapsed = time.time() - started
    assert ok is True, "acquire should succeed after window clears"
    assert elapsed >= 0.15, f"acquire should have waited; took {elapsed:.3f}s"


def test_acquire_times_out_when_budget_stays_full():
    """If budget never clears (window > timeout), acquire returns False."""
    b = WeightBudget(max_weight_per_min=100, window_s=60.0)
    b.spend(100)
    ok = b.acquire(50, timeout_s=0.2)
    assert ok is False


def test_get_budget_returns_singleton():
    b1 = get_budget()
    b2 = get_budget()
    assert b1 is b2


def test_note_429_increments_counter():
    b = WeightBudget()
    b.note_429()
    b.note_429()
    assert b.stats()["observed_429s"] == 2


def test_weight_constants_match_hl_docs():
    """Pin the magic numbers so refactors don't silently drift."""
    assert WEIGHT_CHEAP == 2     # clearinghouseState, l2Book, allMids, orderStatus
    assert WEIGHT_NORMAL == 20   # most info endpoints


def test_realistic_budget_scenario():
    """Realistic mix of REST calls under the default 1080/min cap should
    fit comfortably; over-spending fails."""
    b = WeightBudget(max_weight_per_min=1080)
    # Whale outer-tier poller: 14 wallets × 2 weight × 12 cycles/min = 336/min
    for _ in range(12):
        for _ in range(14):
            assert b.spend(2) is True
    # liq_cluster_hunt OI poll: 240 weight/min
    for _ in range(12):
        assert b.spend(20) is True
    # candles + account housekeeping: ~150 weight/min
    for _ in range(8):
        assert b.spend(20) is True
    # Should still have plenty of headroom (~340/min unused)
    assert b.current_weight() <= 1080
    # Headroom check: should be able to spend an additional 300 weight
    assert b.spend(300) is True


def test_note_429_charges_penalty_weight():
    """note_429() must add penalty weight to the rolling window so callers
    back off harder than just incrementing a counter (sentinel H6 fix)."""
    b = WeightBudget(max_weight_per_min=1000)
    b.spend(800)
    assert b.current_weight() == 800
    b.note_429()
    assert b.current_weight() == 1000   # 200 penalty added
    # And spend should fail until window clears
    assert b.spend(50) is False
