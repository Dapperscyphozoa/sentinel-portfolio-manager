"""HL REST weight budget — central 429 safety net.

Hyperliquid imposes a hard 1,200 weight/min/IP REST budget. Endpoints have
different weights:
  - clearinghouseState / l2Book / allMids / orderStatus: 2
  - userFills / fundingHistory / recentTrades: 20 + 1 per 20 items returned
  - most other info endpoints: 20
  - userRole: 60
  - exchange order/cancel: 1 + floor(batch_length/40)

If we cross 1,200 weight in a rolling minute window, HL returns 429. When
that happens at scale across our engines, signal degrades catastrophically
right when conditions are most actionable.

This module provides a thread-safe rolling-window token bucket with a
configurable safety margin (default 90% of HL's cap = 1,080 weight/min).
Engines should call:

    if not budget.can_spend(weight):
        return  # back off this cycle
    budget.spend(weight)
    # ... make the REST call

Or use the .acquire(weight) helper which blocks briefly when over budget.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Optional


log = logging.getLogger("weight_budget")


# HL's hard cap is 1,200/min. Default to 90% to leave safety margin for
# burst events. Override via env HL_WEIGHT_BUDGET_PER_MIN.
DEFAULT_MAX_WEIGHT_PER_MIN = int(os.environ.get("HL_WEIGHT_BUDGET_PER_MIN", "1080"))

# Endpoint weight constants — keep next to the code that consumes them.
# Reference: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits
WEIGHT_CHEAP = 2          # clearinghouseState, l2Book, allMids, orderStatus, spotClearinghouseState, exchangeStatus
WEIGHT_PAGED = 20         # userFills, recentTrades, fundingHistory (+1 per 20 items)
WEIGHT_NORMAL = 20        # most other info endpoints
WEIGHT_HEAVY = 60         # userRole
WEIGHT_ORDER = 1          # exchange API: 1 + floor(batch/40)


class WeightBudget:
    """Rolling-window weight tracker.

    Thread-safe via internal lock. Tracks (timestamp, weight) pairs in a
    deque, evicting entries older than the window (60s default).
    """

    def __init__(self,
                 max_weight_per_min: int = DEFAULT_MAX_WEIGHT_PER_MIN,
                 window_s: float = 60.0):
        self.max = int(max_weight_per_min)
        self.window_s = float(window_s)
        self._events: deque = deque()    # (ts, weight) FIFO
        self._lock = threading.Lock()
        self._total_spent = 0            # lifetime, for telemetry
        self._total_rejected = 0
        # Telemetry: 429s observed (incremented externally)
        self._observed_429 = 0

    def _prune(self, now: float) -> None:
        """Drop events outside the rolling window. Caller must hold lock."""
        cutoff = now - self.window_s
        ev = self._events
        while ev and ev[0][0] < cutoff:
            ev.popleft()

    def current_weight(self) -> int:
        """Weight spent in the last `window_s` seconds."""
        with self._lock:
            self._prune(time.time())
            return sum(w for _, w in self._events)

    def can_spend(self, weight: int) -> bool:
        """Return True if `weight` would fit in remaining budget."""
        with self._lock:
            self._prune(time.time())
            cur = sum(w for _, w in self._events)
            return cur + weight <= self.max

    def spend(self, weight: int) -> bool:
        """Record `weight` if it fits; return success/failure.

        Returns True if the spend was recorded (caller proceeds with REST
        call), False if budget exhausted (caller should back off).
        """
        with self._lock:
            now = time.time()
            self._prune(now)
            cur = sum(w for _, w in self._events)
            if cur + weight > self.max:
                self._total_rejected += 1
                return False
            self._events.append((now, weight))
            self._total_spent += weight
            return True

    def acquire(self, weight: int, timeout_s: float = 5.0) -> bool:
        """Block (with sleep) until `weight` budget is available, up to
        `timeout_s` seconds. Returns True if acquired, False if timed out.

        Useful for non-urgent polls that can wait briefly; urgent callers
        should use spend() directly to avoid latency.
        """
        deadline = time.time() + timeout_s
        while True:
            if self.spend(weight):
                return True
            if time.time() >= deadline:
                self._total_rejected += 1
                return False
            # Sleep proportional to deficit, capped
            time.sleep(min(0.5, max(0.05, timeout_s / 20)))

    def note_429(self) -> None:
        """Caller observed a 429 from HL.

        Records telemetry AND charges a synthetic penalty (200 weight) to
        the budget to force backoff. Without the penalty, a thread that
        slipped through the can_spend check before another thread spent
        could continue triggering 429s in rapid succession (sentinel H6,
        Codestral 80% conf).

        The 200 weight ≈ 10 cheap calls — enough to give a real cooldown
        but not so harsh we starve essential traffic for minutes.
        """
        now = time.time()
        with self._lock:
            self._observed_429 += 1
            self._events.append((now, 200))
            self._total_spent += 200
        log.warning("weight_budget: 429 observed (+200 penalty, total=%d)",
                    self._observed_429)

    def stats(self) -> dict:
        """Snapshot for /health / /diagnostics."""
        with self._lock:
            self._prune(time.time())
            cur = sum(w for _, w in self._events)
            return {
                "current_weight_per_min": cur,
                "max_weight_per_min": self.max,
                "utilization_pct": round(100 * cur / max(self.max, 1), 1),
                "events_in_window": len(self._events),
                "total_spent": self._total_spent,
                "total_rejected": self._total_rejected,
                "observed_429s": self._observed_429,
            }


# Module-level singleton, lazily initialized.
_singleton: Optional[WeightBudget] = None
_singleton_lock = threading.Lock()


def get_budget() -> WeightBudget:
    """Return the process-wide WeightBudget singleton."""
    global _singleton
    if _singleton is not None:
        return _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = WeightBudget()
    return _singleton
