"""OKX liquidations REST poller.

OKX's WS `liquidation-orders` channel returns error 60018 ("channel doesn't
exist") on both /v5/public AND /v5/business endpoints as of 2026-05-19 — see
commit f4f54c6 and 368f0df for the failed migration attempts.

The REST endpoint /api/v5/public/liquidation-orders is documented stable and
returns recent liquidations. **Required params** (empirically verified
2026-05-19): instType + instFamily + state. Bulk-pulling "all SWAP" is not
supported — must poll per instFamily. With 30s interval × ~30 coins this is
well within OKX's 40 req/2s public rate limit (~1.7 req/s avg).

Council decision 2026-05-19: chosen over WS-channel research (option B) and
Binance fallback (option D, Frankfurt geoblock risk) for reliability and
zero-downstream-code-change profile.

Pushes events into the same `cache.push_liq()` sink used by the (dead) WS
path. Dedup by (instFamily, ts, side, sz) so re-polls don't double-count.
"""
from __future__ import annotations

import collections
import logging
import os
import threading
import time
from typing import Iterable, Optional

import httpx

log = logging.getLogger("okx_liq_poller")

OKX_LIQ_URL = "https://www.okx.com/api/v5/public/liquidation-orders"
POLL_INTERVAL_S = 30        # 30s — same density target as the dead WS path
REQUEST_TIMEOUT_S = 10.0
MAX_BACKOFF_S = 300
DEDUP_CAP = 10000
INTER_COIN_SLEEP_S = 0.05   # 50ms between coins → ≤20/sec, well under OKX's 40/2s

# Default universe = top-30 coins by HL volume + UZT_REV's 16. Override via
# OKX_LIQ_COINS env if you want a different scope.
DEFAULT_COINS = [
    "BTC","ETH","SOL","XRP","BNB","DOGE","AVAX","LINK","LTC","NEAR",
    "SUI","APT","ARB","OP","INJ","FET","UNI","ATOM","FIL","WIF",
    "DOT","APE","TIA","SEI","STG","JUP","BLUR","POLYX","COMP","YGG",
]


def _coin_universe() -> list[str]:
    raw = os.environ.get("OKX_LIQ_COINS", "").strip()
    if raw:
        return [c.strip().upper() for c in raw.split(",") if c.strip()]
    return DEFAULT_COINS


def _fetch_liqs_one(coin: str) -> Optional[list[dict]]:
    """Pull recent filled liquidations for one coin's USDT-perp family."""
    params = {
        "instType":   "SWAP",
        "instFamily": f"{coin}-USDT",
        "state":      "filled",
        "limit":      "100",
    }
    try:
        r = httpx.get(OKX_LIQ_URL, params=params, timeout=REQUEST_TIMEOUT_S)
        r.raise_for_status()
        d = r.json()
    except Exception as e:
        log.warning("okx liq REST %s fetch failed: %s", coin, e)
        return None
    if d.get("code") != "0":
        log.warning("okx liq REST %s code=%s msg=%s", coin, d.get("code"), (d.get("msg") or "")[:120])
        return None
    return d.get("data") or []


def _run_loop(cache, stop_event: threading.Event) -> None:
    seen = collections.deque(maxlen=DEDUP_CAP)
    seen_set: set[str] = set()
    coins = _coin_universe()
    log.info("okx_liq_poller universe: %d coins (%s...)", len(coins), ",".join(coins[:5]))

    backoff = 1.0
    while not stop_event.is_set():
        cycle_start = time.time()
        pushed_total = 0
        fetched_ok = 0
        try:
            for coin in coins:
                if stop_event.is_set():
                    break
                data = _fetch_liqs_one(coin)
                if data is None:
                    continue
                fetched_ok += 1
                for entry in data:
                    inst_family = entry.get("instFamily") or ""
                    inst = entry.get("instId") or ""
                    coin_key = (inst_family.split("-", 1)[0] or coin).upper()
                    for det in entry.get("details") or []:
                        try:
                            side_raw = (det.get("side") or "").lower()
                            sz = float(det.get("sz") or 0)
                            px = float(det.get("bkPx") or det.get("fillPx") or 0)
                            ts = int(det.get("ts") or 0)
                        except (ValueError, TypeError):
                            continue
                        if px <= 0 or sz <= 0 or ts <= 0:
                            continue
                        fp = f"{inst}|{ts}|{side_raw}|{sz}"
                        if fp in seen_set:
                            continue
                        seen.append(fp)
                        seen_set.add(fp)
                        # Periodically rebuild seen_set from deque to bound memory
                        if len(seen_set) > DEDUP_CAP * 1.1:
                            seen_set = set(seen)
                        cache.push_liq({
                            "ts": ts,
                            "coin": coin_key,
                            "side": "SELL" if side_raw == "sell" else "BUY",
                            "qty": sz,
                            "price": px,
                            "usd": sz * px,
                        })
                        pushed_total += 1
                time.sleep(INTER_COIN_SLEEP_S)

            cache.last_update["okx_liq_poll"] = time.time()
            if pushed_total or fetched_ok == 0:
                log.info("okx liq REST cycle: %d coins ok / %d total, %d new events in %.1fs",
                         fetched_ok, len(coins), pushed_total, time.time() - cycle_start)
            backoff = 1.0
        except Exception:
            log.exception("okx_liq_poller loop failure")
            backoff = min(MAX_BACKOFF_S, backoff * 2)

        slept = 0.0
        target = POLL_INTERVAL_S * backoff
        while slept < target and not stop_event.is_set():
            time.sleep(0.5)
            slept += 0.5


def start(cache) -> tuple[threading.Thread, threading.Event]:
    stop_event = threading.Event()
    t = threading.Thread(target=_run_loop, args=(cache, stop_event),
                         name="okx_liq_poller", daemon=True)
    t.start()
    log.info("okx_liq_poller started (interval %ds)", POLL_INTERVAL_S)
    return t, stop_event


def run_in_thread(cache) -> None:
    """Daemon-thread entrypoint. Matches the oi_poller / hlp_poller convention."""
    start(cache)
