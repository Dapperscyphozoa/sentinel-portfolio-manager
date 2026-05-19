"""Daily report routine.

Pulls 24h closures from strategy-runner (via /closures), per-strategy P&L from PM
(/attribution), and asks Claude for a one-pager: best/worst strategy, regime
spent in, suggested next-day knobs.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Optional

import httpx

from .. import claude_client


log = logging.getLogger("routine.daily")


def _dead_engines() -> set:
    return set(
        e.strip() for e in os.environ.get(
            "AUDIT_DEAD_ENGINES",
            "cross_coin_zscore,UZT_REV,donchian,cascade_sniper_hl,e17_bb_fade_bt_4h,fd1"
        ).split(",") if e.strip()
    )


def _filter_dead(rows, dead: set):
    """Drop rows whose strategy/engine is in the dead set."""
    if isinstance(rows, list):
        return [r for r in rows if not (isinstance(r, dict) and
                (r.get("strategy") or r.get("engine") or "") in dead)]
    if isinstance(rows, dict):
        for k in ("open", "closed", "positions"):
            if k in rows and isinstance(rows[k], list):
                rows[k] = [r for r in rows[k] if not (isinstance(r, dict) and
                    (r.get("strategy") or r.get("engine") or "") in dead)]
    return rows


def run(conn: sqlite3.Connection) -> dict:
    pm_url = os.environ.get("PM_URL", "").rstrip("/")
    runner_url = os.environ.get("STRATEGY_RUNNER_URL", "").rstrip("/")
    pm_token = os.environ.get("PM_AUTH_TOKEN", "")
    since_ms = int((time.time() - 86400) * 1000)
    dead = _dead_engines()
    out: dict = {"ts": time.time()}
    with httpx.Client(timeout=20) as c:
        try:
            r = c.get(f"{pm_url}/attribution?since={since_ms}&clean_only=1", headers={"X-PM-Auth": pm_token})
            r.raise_for_status()
            out["attribution"] = _filter_dead(r.json(), dead)
        except Exception as e:
            out["attribution_error"] = str(e)
        try:
            r = c.get(f"{runner_url}/closures?since={since_ms / 1000.0}&limit=500")
            r.raise_for_status()
            out["closures"] = _filter_dead(r.json(), dead)
        except Exception as e:
            out["closures_error"] = str(e)

    try:
        out["summary"] = _summarise(conn, out)
    except claude_client.BudgetExceeded as e:
        out["summary"] = f"budget exceeded; report skipped claude: {e}"
    except Exception as e:
        out["summary"] = f"claude call failed: {e}"
    return out


def _summarise(conn: sqlite3.Connection, data: dict) -> str:
    closures = data.get("closures") or []
    attribution = data.get("attribution") or []
    # Filter out dead engines from audit input — they pollute reports with
    # stale attribution and create false signals about active trading.
    # See SPEC §4 for the dead engine list.
    dead_engines = set(
        e.strip() for e in os.environ.get(
            "AUDIT_DEAD_ENGINES",
            "cross_coin_zscore,UZT_REV,donchian,cascade_sniper_hl,e17_bb_fade_bt_4h,fd1"
        ).split(",") if e.strip()
    )
    def _alive(row):
        if not isinstance(row, dict): return True
        s = row.get("strategy") or row.get("engine") or ""
        return s not in dead_engines
    closures = [c for c in closures if _alive(c)]
    if isinstance(attribution, list):
        attribution = [a for a in attribution if _alive(a)]
    elif isinstance(attribution, dict):
        # filter known shapes: {open: [...], closed: [...]}
        for k in ("open", "closed", "positions"):
            if k in attribution and isinstance(attribution[k], list):
                attribution[k] = [a for a in attribution[k] if _alive(a)]
    payload = {
        "n_closures_24h": len(closures),
        "attribution": attribution,
        "sample_closures": closures[:10],
    }
    prompt = (
        "You are reviewing the last 24 hours of an automated crypto trading stack. "
        "Produce a one-paragraph report (≤5 sentences). Identify: 1) best and worst "
        "strategy by P&L, 2) any strategy with >5 closures and PF < 1.0 (flag it), "
        "3) one concrete suggestion for tomorrow (env knob to change, halt, or no-op). "
        "Be terse. Do not invent numbers.\n\n"
        f"DATA:\n{json.dumps(payload, default=str)[:7000]}"
    )
    r = claude_client.call(conn, routine="daily_report", prompt=prompt,
                           model="claude-sonnet-4-6", max_tokens=500,
                           daily_budget_usd=float(os.environ.get("DAILY_API_BUDGET_USD", "5")))
    return r["text"]
