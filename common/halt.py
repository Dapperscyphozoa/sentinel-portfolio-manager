"""Halt state + token validation. In-memory + SQLite-backed.

Token check is constant-time. Halt token defaults to None — if unset, POST /halt rejects.
"""
from __future__ import annotations

import hmac
import logging
import os
import threading
import time
from typing import Optional

from . import persistence


_LOCK = threading.RLock()
_HALTED: set[str] = set()  # in-memory mirror; SQLite is source of truth

_log = logging.getLogger(__name__)


def require_halt_token_or_abort() -> None:
    """Refuse to start a process whose safety relies on HALT_TOKEN being set.

    Without the token, drawdown_check.run() and operator /halt routes silently
    no-op, so trading runs uncapped. Boot loud rather than fail silent.
    """
    if not os.environ.get("HALT_TOKEN"):
        raise RuntimeError(
            "HALT_TOKEN env var is not set. Drawdown halt and operator /halt "
            "endpoints will refuse to fire without it. Refusing to boot."
        )


def halt_token_ok(presented: Optional[str]) -> bool:
    expected = os.environ.get("HALT_TOKEN")
    if not expected:
        return False
    if not presented:
        return False
    return hmac.compare_digest(expected, presented)


def is_halted(strategy: str) -> bool:
    with _LOCK:
        return strategy in _HALTED or "__all__" in _HALTED


def set_halt(
    conn,
    strategy: str,
    halted: bool,
    reason: Optional[str] = None,
    actor: Optional[str] = None,
) -> None:
    with _LOCK:
        if halted:
            _HALTED.add(strategy)
        else:
            _HALTED.discard(strategy)
        conn.execute(
            "INSERT INTO halts(ts,strategy,halted,reason,actor) VALUES(?,?,?,?,?)",
            (time.time(), strategy, 1 if halted else 0, reason, actor),
        )


def halt_all(conn, reason: str, actor: str) -> None:
    set_halt(conn, "__all__", True, reason=reason, actor=actor)


def load_active_halts(conn) -> set[str]:
    """Rehydrate _HALTED from latest halt row per strategy."""
    rows = conn.execute(
        """
        SELECT strategy, halted FROM halts h1
        WHERE ts = (SELECT MAX(ts) FROM halts h2 WHERE h2.strategy = h1.strategy)
        """
    ).fetchall()
    with _LOCK:
        _HALTED.clear()
        for r in rows:
            if r["halted"]:
                _HALTED.add(r["strategy"])
    return set(_HALTED)


def active_halts() -> set[str]:
    with _LOCK:
        return set(_HALTED)
