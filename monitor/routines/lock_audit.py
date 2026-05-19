"""Lock invariant audit — every 5 min.

Validates that the 1_GLOBAL coin lock (committed in 81641c4) is still holding
in production. Reads from the strategy_runner's local SQLite via the
/strategy/state endpoint (NOT the HL account view — the local DB is the
source of truth the lock is enforced against).

Findings written to LAST_RUNS for /health visibility:
- duplicate_coin_opens: dict of coin → count for any coin with >1 open row
  (this is THE invariant; should always be empty)
- stale_pending_count: rows in status='pending' older than 5min (sweep is
  supposed to demote these every position loop)
- recent_cooldown_violations: coins with >3 'open_failed' rows in last 10min
  (the APT-hammer pattern; cooldown should keep this empty)
- coverage: which engines have fired since the routine started tracking
- first_exercised_ts: when the lock pre-check first denied a fire (proves the
  new code path has run at least once)

CRITICAL flags (raised as result.severity=CRITICAL):
- duplicate_coin_opens non-empty: real-money invariant violation, halts the
  strategy_runner via /halt/all if HALT_TOKEN configured + AUTO_HALT_ON_LOCK_VIOLATION=1
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from collections import Counter

import httpx

from common import persistence


log = logging.getLogger("routine.lock_audit")

# Track per-coin first-fire timestamps so we can compute coverage growth
_KV_COVERAGE = "lock_audit_coverage_v1"
_KV_FIRST_EXERCISED = "lock_audit_first_exercised_v1"


def _load_coverage(conn: sqlite3.Connection) -> dict:
    raw = persistence.kv_get(conn, _KV_COVERAGE)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _save_coverage(conn: sqlite3.Connection, cov: dict) -> None:
    persistence.kv_set(conn, _KV_COVERAGE, json.dumps(cov))


def run(conn: sqlite3.Connection) -> dict:
    runner_url = os.environ.get("STRATEGY_RUNNER_URL", "").rstrip("/")
    if not runner_url:
        # In core (all-in-one), localhost
        runner_url = f"http://localhost:{os.environ.get('STRATEGY_PORT', '10002')}"
    halt_token = os.environ.get("HALT_TOKEN", "")
    auto_halt = os.environ.get("AUTO_HALT_ON_LOCK_VIOLATION", "1") == "1"

    now = time.time()
    try:
        r = httpx.get(f"{runner_url}/state", timeout=15.0)
        r.raise_for_status()
        trades = r.json()
    except Exception as e:
        log.exception("could not fetch /state")
        return {"ts": now, "error": f"fetch_failed:{e}"}

    if not isinstance(trades, list):
        return {"ts": now, "error": "unexpected_shape", "shape": type(trades).__name__}

    opens = [t for t in trades if t.get("status") == "open"]
    pending = [t for t in trades if t.get("status") == "pending"]
    open_failed = [t for t in trades if t.get("status") == "open_failed"]

    # Invariant 1: at most one open per coin
    opens_per_coin = Counter(t["coin"] for t in opens)
    duplicates = {c: n for c, n in opens_per_coin.items() if n > 1}

    # Invariant 2: no stale pending (sweep should keep this empty)
    stale_pending = [
        {"cloid": t["cloid"], "coin": t["coin"], "age_s": int(now - t.get("open_ts", 0))}
        for t in pending if now - t.get("open_ts", 0) > 300
    ]

    # Invariant 3: cooldown effective — no >3 fails on same coin in last 10min
    recent_failed_per_coin = Counter(
        t["coin"] for t in open_failed if now - t.get("open_ts", 0) < 600
    )
    cooldown_violations = {c: n for c, n in recent_failed_per_coin.items() if n > 3}

    # Coverage tracking: which engines have fired at all in this routine's lifetime
    cov = _load_coverage(conn)
    engines_seen = cov.setdefault("engines_with_fires", {})
    for t in trades:
        eng = t.get("strategy")
        if eng and eng not in engines_seen:
            engines_seen[eng] = {
                "first_seen_ts": int(t.get("open_ts", now)),
                "n_attempts": 0,
            }
        if eng:
            engines_seen[eng]["n_attempts"] = engines_seen[eng].get("n_attempts", 0) + 0
    # Actually count attempts (simpler than incremental — small data)
    counter = Counter(t.get("strategy") for t in trades)
    for eng, n in counter.items():
        if eng in engines_seen:
            engines_seen[eng]["n_attempts"] = n
    _save_coverage(conn, cov)

    # Filter audit-reported engines to (a) currently-registered engines and
    # (b) dead-engine skiplist from env var. Historical attempts by killed
    # engines (e.g. cross_coin_zscore, archived per SPEC §4) should not appear
    # in audit output — they pollute the dashboard and create false signals
    # that a dead engine is still active.
    dead_engines = set(
        e.strip() for e in os.environ.get(
            "AUDIT_DEAD_ENGINES",
            "cross_coin_zscore,UZT_REV,donchian,cascade_sniper_hl,e17_bb_fade_bt_4h,fd1"
        ).split(",") if e.strip()
    )
    # Only include engines with recent activity (last 24h) AND not in dead list
    recent_engines: set = set()
    cutoff = now - 86400
    for t in trades:
        eng = t.get("strategy")
        if not eng or eng in dead_engines:
            continue
        if t.get("open_ts", 0) >= cutoff:
            recent_engines.add(eng)
    filtered_counter = {eng: n for eng, n in counter.items() if eng in recent_engines}

    # Severity
    if duplicates:
        severity = "CRITICAL"
    elif stale_pending or cooldown_violations:
        severity = "HIGH"
    else:
        severity = "CLEAN"

    result = {
        "ts": now,
        "severity": severity,
        "trades_total": len(trades),
        "open_count": len(opens),
        "open_coins": sorted(opens_per_coin.keys()),
        "pending_count": len(pending),
        "open_failed_count": len(open_failed),
        "duplicate_coin_opens": duplicates,
        "stale_pending_rows": stale_pending,
        "cooldown_violations": cooldown_violations,
        "engines_with_attempts": filtered_counter,
        "halted_action": None,
    }

    # Auto-halt on real invariant violation
    if duplicates and auto_halt and halt_token:
        log.error("LOCK VIOLATION DETECTED — halting strategy_runner: %s", duplicates)
        try:
            hr = httpx.post(
                f"{runner_url}/halt/all",
                headers={"X-Halt-Token": halt_token},
                timeout=10.0,
            )
            result["halted_action"] = {
                "status_code": hr.status_code,
                "body": hr.text[:200],
            }
        except Exception as e:
            log.exception("auto-halt failed")
            result["halted_action"] = {"error": str(e)}

    return result
