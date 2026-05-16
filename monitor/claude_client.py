"""Thin Anthropic Messages API wrapper.

- Uses CLAUDE_CODE_API_KEY env var (per render.yaml).
- Always checks budget BEFORE calling.
- Records actual usage AFTER calling.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from typing import Optional

import httpx

from . import spend


log = logging.getLogger("claude_client")


ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"  # cheapest by default; routines override
DEFAULT_ANTHROPIC_VERSION = "2023-06-01"


class BudgetExceeded(Exception):
    pass


def call(conn: sqlite3.Connection, routine: str, prompt: str,
         model: Optional[str] = None, max_tokens: int = 1024,
         daily_budget_usd: float = 5.0,
         system: Optional[str] = None) -> dict:
    api_key = os.environ.get("CLAUDE_CODE_API_KEY", "")
    if not api_key:
        raise RuntimeError("CLAUDE_CODE_API_KEY not set")
    m = model or DEFAULT_MODEL
    # rough pre-flight cost estimate using upper bound on input tokens
    est = spend.estimate_cost_usd(m, input_tokens=len(prompt) // 3, output_tokens=max_tokens)
    if not spend.can_spend(conn, daily_budget_usd, est):
        raise BudgetExceeded(f"would exceed budget; spent_today={spend.spent_today_usd(conn):.4f}, "
                             f"est={est:.4f}, budget={daily_budget_usd}")
    messages = [{"role": "user", "content": prompt}]
    body = {"model": m, "max_tokens": max_tokens, "messages": messages}
    if system:
        body["system"] = system
    headers = {
        "x-api-key": api_key,
        "anthropic-version": DEFAULT_ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    with httpx.Client(timeout=120) as client:
        r = client.post(ANTHROPIC_URL, headers=headers, content=json.dumps(body))
        r.raise_for_status()
        resp = r.json()
    usage = resp.get("usage") or {}
    actual_in = int(usage.get("input_tokens", len(prompt) // 3))
    actual_out = int(usage.get("output_tokens", 0))
    cost = spend.record(conn, routine, m, actual_in, actual_out)
    text = "".join(c.get("text", "") for c in (resp.get("content") or []) if c.get("type") == "text")
    return {"text": text, "input_tokens": actual_in, "output_tokens": actual_out,
            "cost_usd": cost, "model": m, "raw": resp}
