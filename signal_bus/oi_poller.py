"""HL Open Interest poller.

Polls the HL REST API every 60s for openInterest across all coins via the
metaAndAssetCtxs endpoint (single call returns OI for all coins, so this
is cheap and avoids per-coin rate limits).

Cache schema (added to Cache class):
    oi_latest:  dict {coin: {ts, oi, oi_usd}}     # latest snapshot
    oi_history: dict {coin: deque[(ts, oi, oi_usd)]}  # rolling 24h @ 60s = ~1440 points

Council finding (2026-05-18): oi_concentration v1 uses volume-as-OI-proxy
which was unanimously flagged by 5/5 voters as a structural defect. This
poller provides real OI so the strategy can be reparameterized off true
data, then activated.

Endpoint exposed by signal-bus:
    GET /oi/{coin}?n=N  → [{ts, oi, oi_usd}, ...]
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Optional

import httpx

log = logging.getLogger("oi_poller")

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
POLL_INTERVAL_S = 300       # 5 minutes (matches hlp_poller cadence; OI is slow-moving)
HISTORY_POINTS = 8640       # 30 days × 288 (5min bars) — matches hlp_history sizing
REQUEST_TIMEOUT_S = 15.0
MAX_BACKOFF_S = 300


def _fetch_oi_snapshot() -> Optional[dict]:
    """Pull metaAndAssetCtxs, return {coin: {ts, oi, oi_usd, mark_px}}."""
    try:
        r = httpx.post(HL_INFO_URL, json={"type": "metaAndAssetCtxs"},
                       timeout=REQUEST_TIMEOUT_S)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        log.warning("metaAndAssetCtxs fetch failed: %s", e)
        return None

    if not isinstance(d, list) or len(d) < 2:
        log.warning("metaAndAssetCtxs unexpected shape")
        return None

    meta, ctxs = d[0], d[1]
    universe = meta.get("universe", [])
    if len(universe) != len(ctxs):
        log.warning("universe/ctxs length mismatch: %d vs %d", len(universe), len(ctxs))
        return None

    ts_ms = int(time.time() * 1000)
    snap: dict[str, dict] = {}
    for u, ctx in zip(universe, ctxs):
        coin = u.get("name")
        if not coin:
            continue
        try:
            oi = float(ctx.get("openInterest", 0) or 0)
            mark = float(ctx.get("markPx", 0) or 0)
        except (TypeError, ValueError):
            continue
        if oi <= 0:
            continue
        snap[coin] = {
            "ts": ts_ms,
            "oi": oi,
            "oi_usd": oi * mark,
            "mark_px": mark,
        }
    return snap


def _run_loop(cache, stop_event: threading.Event) -> None:
    backoff = 1.0
    while not stop_event.is_set():
        try:
            snap = _fetch_oi_snapshot()
            if snap is not None:
                cache.update_oi(snap)
                cache.last_update["oi_poll"] = time.time()
                backoff = 1.0   # reset
            else:
                # transient — back off but don't crash the thread
                backoff = min(MAX_BACKOFF_S, backoff * 2)
        except Exception:
            log.exception("oi_poller loop failure")
            backoff = min(MAX_BACKOFF_S, backoff * 2)

        # Wait POLL_INTERVAL_S, but exit fast on stop_event
        slept = 0.0
        while slept < POLL_INTERVAL_S * backoff and not stop_event.is_set():
            time.sleep(0.5)
            slept += 0.5


def start(cache) -> tuple[threading.Thread, threading.Event]:
    """Start the OI poller. Returns (thread, stop_event)."""
    stop_event = threading.Event()
    t = threading.Thread(target=_run_loop, args=(cache, stop_event),
                         name="oi_poller", daemon=True)
    t.start()
    log.info("oi_poller started (interval %ds)", POLL_INTERVAL_S)
    return t, stop_event


# ── Convenience wrapper to match hlp_poller.run_in_thread() convention ───
def run_in_thread(cache) -> None:
    """Start the poller in a daemon thread. Compatible with server.py boot pattern."""
    start(cache)
