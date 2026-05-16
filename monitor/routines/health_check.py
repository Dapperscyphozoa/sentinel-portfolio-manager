"""15-min health-check routine.

Pulls /health from signal-bus + /regime from pm + /health from strategy-runner,
then asks Claude to summarise + flag anomalies.
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


log = logging.getLogger("routine.health")


def run(conn: sqlite3.Connection) -> dict:
    bus_url = os.environ.get("SIGNAL_BUS_URL", "").rstrip("/")
    pm_url = os.environ.get("PM_URL", "").rstrip("/")
    runner_url = os.environ.get("STRATEGY_RUNNER_URL", "").rstrip("/")
    statuses: dict = {}
    with httpx.Client(timeout=10) as c:
        for name, url in (("signal_bus", f"{bus_url}/health"),
                          ("pm", f"{pm_url}/health"),
                          ("strategy_runner", f"{runner_url}/health")):
            if not url:
                continue
            try:
                r = c.get(url)
                statuses[name] = {"status_code": r.status_code, "body": r.json()}
            except Exception as e:
                statuses[name] = {"error": str(e)}

    # local check first — cheap
    issues = _detect_issues(statuses)
    out: dict = {"ts": time.time(), "statuses": statuses, "issues": issues}

    # only invoke Claude if there are issues OR every 4th run
    if issues or int(time.time() // 900) % 4 == 0:
        try:
            summary = _summarise_with_claude(conn, statuses, issues)
            out["summary"] = summary
        except claude_client.BudgetExceeded as e:
            out["summary"] = f"budget exceeded; skipped claude call: {e}"
        except Exception as e:
            out["summary"] = f"claude call failed: {e}"
    return out


def _detect_issues(statuses: dict) -> list[str]:
    issues: list[str] = []
    for name, s in statuses.items():
        if "error" in s:
            issues.append(f"{name}_unreachable:{s['error'][:80]}")
            continue
        if s.get("status_code", 0) >= 400:
            issues.append(f"{name}_http_{s['status_code']}")
            continue
        body = s.get("body") or {}
        if name == "signal_bus":
            ws_alive = body.get("ws_alive", {})
            for venue, alive in ws_alive.items():
                if alive is False:
                    issues.append(f"signal_bus_ws_down:{venue}")
            last = body.get("last_update", {})
            now = time.time()
            for k, t in last.items():
                if t and (now - t) > 600:
                    issues.append(f"signal_bus_stale:{k}={int(now - t)}s")
    return issues


def _summarise_with_claude(conn: sqlite3.Connection, statuses: dict, issues: list[str]) -> str:
    prompt = (
        "You are monitoring an automated crypto trading stack. Below is the latest health "
        "snapshot from three services. Summarise in ≤3 sentences. If issues are listed, "
        "name the most urgent and a concrete next step. Do NOT speculate beyond the data.\n\n"
        f"ISSUES: {json.dumps(issues)}\n\nSTATUSES: {json.dumps(statuses, default=str)[:6000]}"
    )
    r = claude_client.call(conn, routine="health_check", prompt=prompt,
                           model="claude-haiku-4-5-20251001", max_tokens=300,
                           daily_budget_usd=float(os.environ.get("DAILY_API_BUDGET_USD", "5")))
    return r["text"]
