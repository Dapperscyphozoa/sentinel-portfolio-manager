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
                 bars: int = 200, max_workers: int = 4) -> int:
    """Concurrent bulk backfill. Returns total bars loaded.

    2026-05-21: previously serial — 24 coins × 6 TFs × ~1.5s/combo = 3.6min total,
    but every Render deploy restarts the bus mid-backfill, leaving coins past
    #14 with zero historical bars. Solution: parallelize across coins (max 4
    concurrent workers; OKX REST 20/sec/IP soft limit easily survives this).
    Per-coin all-TF batch keeps progress meaningful per worker, so even if
    interrupted, complete-coin units have been written.

    2026-05-22: max_workers parameterized. Default 4 for first-ever boot
    (speed matters when SQLite is empty); callers can pass 1 to avoid the
    parallel-JSON-parse memory spike that OOM'd spm-bus on starter plan.
    """
    import concurrent.futures as cf
    coins = list(coins)
    tfs = tuple(tfs)
    total = 0

    def _backfill_coin_all_tfs(coin: str) -> int:
        n = 0
        for tf in tfs:
            try:
                arr = _backfill_one(coin, tf, bars)
            except Exception:
                log.exception("backfill %s/%s crashed", coin, tf)
                continue
            if not arr:
                continue
            try:
                for b in arr:
                    cache.push_kline(coin, tf, b)
                n += len(arr)
                log.info("backfilled %s/%s: %d bars", coin, tf, len(arr))
            except Exception:
                log.exception("backfill push %s/%s crashed", coin, tf)
        # 2026-05-21: per-coin flush so partial backfill survives next deploy.
        # Without this, if deploy interrupts after coin K of 24, coins 1..K
        # had memory-only state that gets lost on restart even though they
        # successfully backfilled.
        try:
            cache.flush_klines()
        except Exception:
            log.exception("per-coin backfill flush failed for %s", coin)
        return n

    with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_backfill_coin_all_tfs, c): c for c in coins}
        for f in cf.as_completed(futs):
            coin = futs[f]
            try:
                n = f.result()
                total += n
                log.info("backfill done for %s: %d bars total", coin, n)
            except Exception:
                log.exception("backfill thread crashed for %s", coin)
    return total
