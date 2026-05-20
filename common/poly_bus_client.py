"""HTTP client for poly-signal-bus, consumed by poly-runner strategies.

Mirrors common/bus_client.py for SPM. All calls are synchronous (each
strategy.evaluate() is a single-shot scan call so blocking is fine).
"""
from __future__ import annotations

import os
from typing import Optional

import httpx


BUS_URL = os.environ.get("POLY_BUS_URL", "http://127.0.0.1:10100")
TIMEOUT_S = 3.0


def _get(path: str, params: Optional[dict] = None):
    r = httpx.get(f"{BUS_URL}{path}", params=params, timeout=TIMEOUT_S)
    r.raise_for_status()
    return r.json()


def cex_consensus(asset: str) -> dict:
    return _get(f"/cex_consensus/{asset}")


def cl_predicted(asset: str) -> Optional[dict]:
    try:
        return _get(f"/cl_predicted/{asset}")
    except httpx.HTTPStatusError:
        return None


def cl_actual(asset: str) -> Optional[dict]:
    try:
        return _get(f"/cl_actual/{asset}")
    except httpx.HTTPStatusError:
        return None


def cl_divergence(asset: str) -> Optional[dict]:
    try:
        return _get(f"/cl_divergence/{asset}")
    except httpx.HTTPStatusError:
        return None


def market_list() -> list[dict]:
    return _get("/market_list")


def pm_book(market_id: str) -> Optional[dict]:
    try:
        return _get(f"/pm_book/{market_id}")
    except httpx.HTTPStatusError:
        return None


def implied_prob(market_id: str) -> Optional[dict]:
    try:
        return _get(f"/implied_prob/{market_id}")
    except httpx.HTTPStatusError:
        return None


def realized_vol(asset: str, lookback_s: int = 60) -> float:
    j = _get(f"/realized_vol/{asset}", params={"lookback_s": lookback_s})
    return float(j.get("vol", 0.0))


def reflex_signal(asset: str) -> dict:
    return _get(f"/reflex_signal/{asset}")


def health() -> dict:
    return _get("/health")
