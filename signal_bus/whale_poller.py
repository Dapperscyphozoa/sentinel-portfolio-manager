"""whale_poller — track top HL wallets by position size; detect new opens.

Stage 1 #5 infrastructure. Council pick — Qwen3 235B rated +3.2%/mo paper-tested.
World-first because HL public position data is unique vs CEX (CEX positions
are private). Nobody else publishes wallet-tracking on HL.

Strategy:
  1. Maintain a curated list of WHALE_WALLETS (operator-managed via env or
     auto-discovered via 7d top-PnL leaderboard).
  2. Every 60 seconds, poll clearinghouseState for each whale.
  3. Diff against previous snapshot. For each NEW position open (no prior
     position on that coin, or position size flipped sign, or position size
     grew by >30%):
       - Emit a "whale_open" event into cache
       - Mark coin + direction + entry_price + whale_address + magnitude
  4. The hl_whale_frontrun engine consumes these events.

Sources for auto-discovery (env WHALE_AUTO_DISCOVER=1):
  - HL has a leaderboard at api.hyperliquid.xyz/info {type:"leaderboard"} — daily
  - Top 20 wallets by 7d PnL are the most-likely informed-flow candidates

Public discovered wallets (high notional, used by hl_whale_frontrun):
  Default seed list curated from public HL stats pages.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Optional

import httpx

log = logging.getLogger("whale_poller")

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
POLL_INTERVAL_S = int(os.environ.get("WHALE_POLL_INTERVAL_S", "60"))
LEADERBOARD_REFRESH_S = 3600           # refresh top-20 list hourly
WHALE_MIN_NOTIONAL = float(os.environ.get("WHALE_MIN_NOTIONAL", "100000"))
WHALE_OPEN_THRESHOLD = float(os.environ.get("WHALE_OPEN_THRESHOLD", "0.30"))   # new-or-grew >30%
WHALE_EVENT_MAXLEN = 2000
MAX_WHALES_TRACKED = int(os.environ.get("WHALE_MAX_TRACKED", "20"))


# Operator-curated seed wallets (high-notional, publicly-known). Auto-replaced
# by leaderboard fetch if WHALE_AUTO_DISCOVER=1 (default ON).
DEFAULT_WHALE_SEEDS = [
    # Known liquid traders on HL — these are public addresses from leaderboard
    # The poller will dynamically replace this list from the API leaderboard.
]


def _fetch_leaderboard() -> list[str]:
    """Fetch top wallets by 7d PnL from HL stats-data leaderboard.

    The public /info endpoint does NOT expose leaderboard (returns 422).
    HL publishes it via stats-data GET endpoint instead.

    Returns list of addresses ranked by 7d absolute PnL (largest first).
    Empty list on failure — poller continues with existing whale list.
    """
    try:
        r = httpx.get("https://stats-data.hyperliquid.xyz/Mainnet/leaderboard",
                      timeout=20.0)
        if r.status_code != 200:
            log.warning("leaderboard fetch HTTP %d", r.status_code)
            return []
        d = r.json()
        # Schema: {"leaderboardRows": [{"ethAddress": ..., "windowPerformances": [...]}]}
        rows = d.get("leaderboardRows", []) or []
        # Sort by 7d PnL absolute value (windowPerformances format varies; defensive)
        def _7d_pnl(row: dict) -> float:
            perfs = row.get("windowPerformances", []) or []
            for p in perfs:
                if isinstance(p, list) and len(p) >= 2:
                    if p[0] == "week" and isinstance(p[1], dict):
                        try:
                            return abs(float(p[1].get("pnl", 0) or 0))
                        except Exception:
                            return 0.0
            return 0.0

        sorted_rows = sorted(rows, key=_7d_pnl, reverse=True)
        addresses = []
        for row in sorted_rows[:MAX_WHALES_TRACKED]:
            addr = row.get("ethAddress")
            if addr and isinstance(addr, str) and addr.startswith("0x"):
                addresses.append(addr.lower())
        log.info("whale leaderboard: %d top wallets fetched", len(addresses))
        return addresses
    except Exception:
        log.exception("leaderboard fetch failed")
        return []


def _fetch_positions(wallet: str) -> dict:
    """Fetch clearinghouseState for one wallet. Returns {coin: {szi, entry_px, ntl_usd}}."""
    try:
        r = httpx.post(HL_INFO_URL,
                       json={"type": "clearinghouseState", "user": wallet},
                       timeout=10.0)
        if r.status_code != 200:
            return {}
        d = r.json()
        out: dict = {}
        for entry in d.get("assetPositions", []) or []:
            pos = entry.get("position", {}) or {}
            coin = pos.get("coin")
            if not coin:
                continue
            try:
                szi = float(pos.get("szi", 0) or 0)
                entry_px = float(pos.get("entryPx", 0) or 0)
                ntl_pos = float(pos.get("positionValue", 0) or 0)
            except Exception:
                continue
            out[coin] = {"szi": szi, "entry_px": entry_px, "ntl_usd": ntl_pos}
        return out
    except Exception:
        return {}


def _detect_opens(prev: dict, curr: dict, wallet: str) -> list[dict]:
    """Compare prev vs curr snapshots for one wallet, return list of new-position events."""
    events: list[dict] = []
    now_ms = int(time.time() * 1000)
    for coin, c_pos in curr.items():
        c_sz = c_pos["szi"]
        c_ntl = abs(c_pos["ntl_usd"])
        if c_ntl < WHALE_MIN_NOTIONAL:
            continue
        p_pos = prev.get(coin) if prev else None
        p_sz = p_pos["szi"] if p_pos else 0.0
        p_ntl = abs(p_pos["ntl_usd"]) if p_pos else 0.0

        # Three conditions for "new open":
        # 1. No prior position
        # 2. Sign flip
        # 3. Grew by ≥WHALE_OPEN_THRESHOLD (30% default)
        is_new = (p_sz == 0)
        flipped = (p_sz != 0 and ((p_sz > 0) != (c_sz > 0)))
        grew = (p_sz != 0 and not flipped
                and abs(c_sz) > abs(p_sz) * (1 + WHALE_OPEN_THRESHOLD))

        if is_new or flipped or grew:
            events.append({
                "ts": now_ms,
                "wallet": wallet,
                "coin": coin,
                "is_long": c_sz > 0,
                "size": abs(c_sz),
                "entry_px": c_pos["entry_px"],
                "ntl_usd": c_ntl,
                "kind": "new" if is_new else ("flip" if flipped else "grow"),
                "delta_ntl_usd": c_ntl - p_ntl,
            })
    return events


def _poll_loop(cache, whales_list: list[str], lock: threading.Lock) -> None:
    """Main poll loop. Updates cache.whale_positions and cache.whale_events."""
    snapshots: dict[str, dict] = {}     # wallet -> {coin: pos}
    last_leaderboard = 0.0

    while True:
        try:
            now = time.time()

            # Refresh leaderboard hourly
            if os.environ.get("WHALE_AUTO_DISCOVER", "1") == "1":
                if now - last_leaderboard > LEADERBOARD_REFRESH_S:
                    lb = _fetch_leaderboard()
                    if lb:
                        with lock:
                            whales_list.clear()
                            whales_list.extend(lb)
                    last_leaderboard = now

            if not whales_list:
                time.sleep(POLL_INTERVAL_S)
                continue

            new_events: list[dict] = []
            curr_snapshots: dict[str, dict] = {}

            with lock:
                whales = list(whales_list)

            for wallet in whales:
                curr = _fetch_positions(wallet)
                curr_snapshots[wallet] = curr
                prev = snapshots.get(wallet)
                if prev is not None:
                    new_events.extend(_detect_opens(prev, curr, wallet))

            snapshots = curr_snapshots

            # Push events into cache
            for ev in new_events:
                cache.push_whale_event(ev)

            # Aggregate stats
            with cache._lock:
                cache.whale_stats = {
                    "ts": int(now * 1000),
                    "n_whales": len(whales),
                    "new_events": len(new_events),
                    "last_poll_s": now,
                }
                cache.last_update["whale_poll"] = now

            if new_events:
                log.info("whale poll: %d new opens detected", len(new_events))
            else:
                log.debug("whale poll: no new opens (%d whales)", len(whales))
        except Exception:
            log.exception("whale poll loop")
        time.sleep(POLL_INTERVAL_S)


def run_in_thread(cache) -> None:
    """Start the whale poller. Reads optional WHALE_WALLETS env (comma-separated)."""
    whales_list: list[str] = []
    seed = os.environ.get("WHALE_WALLETS", "").strip()
    if seed:
        whales_list.extend(w.strip().lower() for w in seed.split(",") if w.strip())
    lock = threading.Lock()

    t = threading.Thread(target=_poll_loop, args=(cache, whales_list, lock),
                         daemon=True, name="whale_poller")
    t.start()
    log.info("whale_poller thread started (interval=%ds, seed=%d, auto=%s)",
             POLL_INTERVAL_S, len(whales_list),
             os.environ.get("WHALE_AUTO_DISCOVER", "1"))
