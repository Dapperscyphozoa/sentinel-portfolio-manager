"""Daily API spend ledger (SPEC §8.1 — autonomous routines budget).

Tracks per-call cost; refuses calls that would exceed DAILY_API_BUDGET_USD.
Pricing is hardcoded for Claude models (Anthropic public pricing as of
build time; verify against docs.claude.com if drift suspected).
"""
from __future__ import annotations

import sqlite3
import time
from typing import Optional


# USD per 1M tokens (input, output)
PRICING = {
    "claude-opus-4-7":            (15.0, 75.0),
    "claude-opus-4-6":            (15.0, 75.0),
    "claude-sonnet-4-6":          (3.0, 15.0),
    "claude-haiku-4-5":           (1.0, 5.0),
    "claude-haiku-4-5-20251001":  (1.0, 5.0),
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    in_per_m, out_per_m = PRICING.get(model, (15.0, 75.0))  # default opus
    return (input_tokens / 1_000_000) * in_per_m + (output_tokens / 1_000_000) * out_per_m


def record(conn: sqlite3.Connection, routine: str, model: str,
           input_tokens: int, output_tokens: int) -> float:
    cost = estimate_cost_usd(model, input_tokens, output_tokens)
    conn.execute(
        "INSERT INTO spend(ts, routine, model, input_tokens, output_tokens, cost_usd) "
        "VALUES(?,?,?,?,?,?)",
        (time.time(), routine, model, input_tokens, output_tokens, cost),
    )
    return cost


def spent_today_usd(conn: sqlite3.Connection) -> float:
    midnight = _today_midnight_unix()
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM spend WHERE ts >= ?", (midnight,)
    ).fetchone()
    return float(row["s"] or 0.0)


def can_spend(conn: sqlite3.Connection, daily_budget_usd: float, projected_cost_usd: float) -> bool:
    return (spent_today_usd(conn) + projected_cost_usd) <= daily_budget_usd


def _today_midnight_unix() -> float:
    """UTC midnight as unix seconds."""
    now = time.time()
    return now - (now % 86400)
