"""OKX REST backfill — pulls last N bars per (coin, tf) on signal-bus boot.

Without this, strategies wait days for klines to accumulate via the WS stream
because OKX (and most exchanges) only push CURRENT-bar updates after subscribe,
not historical bars. PM regime detector needs 80 BTC 1h bars; vsq needs 31+.
"""
from __future__ import annotations

import logging
import time
from typing import Iterable

import httpx

from .cache import Cache


log = logging.getLogger("okx_rest_backfill")


# Internal TF tag → OKX bar string
_OKX_BAR = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"}


def _backfill_one(coin: str, tf: str, bars: int = 200) -> list[dict]:
    inst = f"{coin}-USDT-SWAP"
    bar = _OKX_BAR.get(tf)
    if not bar:
        return []
    url = "https://www.okx.com/api/v5/market/history-candles"
    out: list[dict] = []
    after: int | None = None
    with httpx.Client(timeout=20) as c:
        while len(out) < bars:
            params: dict = {"instId": inst, "bar": bar, "limit": "100"}
            if after is not None:
                params["after"] = str(after)
            try:
                r = c.get(url, params=params)
                r.raise_for_status()
            except Exception as e:
                log.warning("okx rest backfill %s %s: %s", coin, tf, e)
                break
            rows = (r.json() or {}).get("data") or []
            if not rows:
                break
            for row in rows:
                try:
                    out.append({
                        "open_ts": int(row[0]),
                        "open": float(row[1]),
                        "high": float(row[2]),
                        "low": float(row[3]),
                        "close": float(row[4]),
                        "volume": float(row[5]),
                        "closed": True,
                    })
                except (ValueError, IndexError):
                    continue
            oldest = int(rows[-1][0])
            after = oldest
            time.sleep(0.12)  # be polite
    # OKX returns newest first; we want oldest→newest
    out.sort(key=lambda b: b["open_ts"])
    return out[-bars:]


def backfill_all(coins: Iterable[str], cache: Cache,
                 tfs: Iterable[str] = ("1m", "5m", "15m", "1h", "4h", "1d"),
                 bars: int = 200) -> int:
    """Synchronous bulk backfill. Returns total bars loaded."""
    total = 0
    for coin in coins:
        for tf in tfs:
            arr = _backfill_one(coin, tf, bars)
            if not arr:
                continue
            for b in arr:
                cache.push_kline(coin, tf, b)
            total += len(arr)
            log.info("backfilled %s/%s: %d bars", coin, tf, len(arr))
    return total
