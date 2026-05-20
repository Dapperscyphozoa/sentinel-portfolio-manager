"""HLP (Hyperliquidity Provider) positioning poller.

HLP is HL's protocol-owned vault that takes the opposite side of aggressive
retail flow. Its positions are public via /info?type=clearinghouseState on
each child vault (HLP parent vault routes via 7 child sub-vaults).

This poller hits the HL REST API every 5 minutes, aggregates net position
per coin across all child vaults, and stores in cache for the hlp_fade
engine to consume.

Council finding (2026-05-17): hlp_fade is the strongest world-first edge.
5+ council voters independently arrived at HLP-fade as #1 pick.

Cache schema (added to Cache class):
    hlp_positions: dict {coin: {net_size, net_usd, ts}}
    hlp_history:   deque [(coin, ts, net_usd)]  — last 30 days for z-score
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from typing import Optional

import httpx

log = logging.getLogger("hlp_poller")

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
HLP_PARENT = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"
POLL_INTERVAL_S = 300            # every 5 minutes (positions change slowly)
HISTORY_MAX_DAYS = 30            # 30 days of snapshots for z-score baseline


def _get_child_vaults() -> list[str]:
    """Fetch the HLP parent vault's child addresses (refreshed daily).

    Costs 20 weight (vaultDetails is a 'normal' info endpoint).
    """
    try:
        from common.weight_budget import get_budget, WEIGHT_NORMAL
        if not get_budget().spend(WEIGHT_NORMAL):
            log.warning("hlp_poller: weight budget exhausted for vaultDetails")
            return []
    except ImportError:
        pass
    try:
        r = httpx.post(HL_INFO_URL, json={"type": "vaultDetails", "vaultAddress": HLP_PARENT},
                       timeout=15.0)
        if r.status_code == 429:
            try:
                from common.weight_budget import get_budget
                get_budget().note_429()
            except ImportError: pass
            return []
        r.raise_for_status()
        d = r.json()
        children = d.get("relationship", {}).get("data", {}).get("childAddresses", []) or []
        return children
    except Exception as e:
        log.warning("could not fetch HLP children: %s", e)
        return []


def _aggregate_positions(children: list[str]) -> dict:
    """Aggregate HLP positions across child vaults: {coin: {net_size, net_usd, ts}}.

    Costs 2 weight per child (clearinghouseState).
    """
    by_coin: dict[str, dict] = {}
    ts = int(time.time() * 1000)
    try:
        from common.weight_budget import get_budget, WEIGHT_CHEAP
        budget = get_budget()
    except ImportError:
        budget = None
    for child in children:
        if budget is not None and not budget.spend(WEIGHT_CHEAP):
            log.warning("hlp_poller: weight budget exhausted; partial aggregation")
            break
        try:
            r = httpx.post(HL_INFO_URL,
                           json={"type": "clearinghouseState", "user": child},
                           timeout=10.0)
            if r.status_code == 429:
                if budget is not None:
                    budget.note_429()
                continue
            if r.status_code != 200:
                continue
            d = r.json()
            for entry in d.get("assetPositions", []):
                pos = entry.get("position", {})
                coin = pos.get("coin")
                if not coin:
                    continue
                try:
                    szi = float(pos.get("szi", 0) or 0)
                    entry_px = float(pos.get("entryPx", 0) or 0)
                except Exception:
                    continue
                if coin not in by_coin:
                    by_coin[coin] = {"net_size": 0.0, "net_usd": 0.0, "vault_count": 0,
                                     "ts": ts}
                by_coin[coin]["net_size"] += szi
                by_coin[coin]["net_usd"] += szi * entry_px
                by_coin[coin]["vault_count"] += 1
        except Exception:
            log.exception("aggregate failed for vault %s", child[:12])
    return by_coin


def _poll_loop(cache) -> None:
    """Run forever, polling HLP positions every POLL_INTERVAL_S."""
    children: list[str] = []
    last_children_refresh = 0.0
    while True:
        try:
            now = time.time()
            # Refresh child list every 6h (rarely changes)
            if now - last_children_refresh > 21600:
                new = _get_child_vaults()
                if new:
                    children = new
                    last_children_refresh = now
                    log.info("hlp children refreshed: %d vaults", len(children))
            if not children:
                time.sleep(60)
                continue

            positions = _aggregate_positions(children)
            cache.hlp_positions = positions
            cache.last_update["hlp_poller"] = now

            # Append to rolling history per coin (keep ~30d at 5min cadence = 8640 points)
            for coin, data in positions.items():
                cache.hlp_history[coin].append((data["ts"], data["net_usd"]))

            # Persist this snapshot to SQLite — survives redeploys.
            try:
                n_persisted = cache.flush_hlp_history()
                log.info("hlp poll ok: %d coins tracked, %d persisted", len(positions), n_persisted)
            except Exception:
                log.exception("hlp persist failed")
                log.info("hlp poll ok: %d coins tracked (NOT persisted)", len(positions))
        except Exception:
            log.exception("hlp_poller iteration failed")
        time.sleep(POLL_INTERVAL_S)


def run_in_thread(cache) -> threading.Thread:
    # Initialize cache fields if missing
    if not hasattr(cache, "hlp_positions"):
        cache.hlp_positions = {}
    if not hasattr(cache, "hlp_history"):
        cache.hlp_history = defaultdict(lambda: deque(maxlen=8640))
    # Cold-load history from SQLite so the engine doesn't need to wait
    # ~17h after each redeploy to accumulate the 200-sample threshold.
    try:
        n_loaded = cache.cold_load_hlp()
        log.info("hlp cold-loaded %d rows from sqlite", n_loaded)
    except Exception:
        log.exception("hlp cold-load failed")
    t = threading.Thread(target=_poll_loop, args=(cache,), daemon=True, name="hlp_poller")
    t.start()
    return t


def compute_zscore(history: deque, current: float, lookback_n: int = 2016) -> Optional[float]:
    """Z-score of current position vs trailing lookback_n samples (default ~7d at 5min).
    
    Returns None if insufficient history.
    """
    if len(history) < 100:           # need at least ~8h of data
        return None
    # Take last lookback_n (or all if fewer)
    sample = list(history)[-lookback_n:]
    values = [v for (_, v) in sample]
    if len(values) < 50:
        return None
    n = len(values)
    mean = sum(values) / n
    var = sum((x - mean) ** 2 for x in values) / n
    if var <= 0:
        return None
    std = var ** 0.5
    return (current - mean) / std
