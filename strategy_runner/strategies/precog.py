"""precog — HL Precog webhook signal consumer (SPEC §3.7).

Listens for inbound signals from the HL Precog service via webhook. The runner's
HTTP server exposes POST /precog/webhook (auth: shared HMAC secret); valid
inbound payloads are queued for the next scan. A Precog signal is a tipped
direction call with confidence + suggested levels.

The strategy itself does NOT scan; it consumes the queue. We expose evaluate()
as the contract entry-point so the registry can include precog; it pops the
oldest pending event per coin and returns it as a Signal.
"""
from __future__ import annotations

import collections
import logging
import os
import threading
import time
from typing import Optional

from ._base import Signal, StrategyBase


log = logging.getLogger("precog")


_LOCK = threading.Lock()
_QUEUE: dict[str, collections.deque] = collections.defaultdict(lambda: collections.deque(maxlen=8))


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def enqueue(coin: str, payload: dict) -> None:
    """Called by server.py's webhook handler after HMAC verification."""
    with _LOCK:
        _QUEUE[coin.upper()].append({"ts": time.time(), "payload": payload})


def queue_stats() -> dict[str, int]:
    with _LOCK:
        return {k: len(v) for k, v in _QUEUE.items()}


class Precog(StrategyBase):
    NAME = "precog"
    CLOID_PREFIX = "prcog_"
    AFFINITY = ["range", "chop", "trend_up", "trend_down"]
    TF = "5m"
    # universe inferred from HL coin universe; webhook can target any coin
    UNIVERSE = [
        "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK", "LTC", "NEAR",
        "SUI", "APT", "ARB", "OP", "INJ", "SEI", "TIA", "WIF", "JUP", "DOT",
    ]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        min_conf = _f("PRECOG_MIN_CONFIDENCE", 0.65)
        max_age_s = _f("PRECOG_MAX_AGE_SEC", 300)
        sl_pct_fallback = _f("PRECOG_SL_PCT_FALLBACK", 0.012)
        tp_pct_fallback = _f("PRECOG_TP_PCT_FALLBACK", 0.030)
        max_hold = int(_f("PRECOG_MAX_HOLD_BARS", 24))

        with _LOCK:
            dq = _QUEUE.get(coin)
            if not dq:
                return None
            # drop stale
            now = time.time()
            while dq and (now - dq[0]["ts"]) > max_age_s:
                dq.popleft()
            if not dq:
                return None
            event = dq.popleft()

        p = event["payload"] or {}
        side = (p.get("side") or "").upper()
        if side not in ("B", "BUY", "LONG", "A", "SELL", "SHORT"):
            return None
        is_long = side in ("B", "BUY", "LONG")
        conf = float(p.get("confidence", 0.0) or 0.0)
        if conf < min_conf:
            return None

        mark = bus.markprice(coin)
        ref = float(p.get("ref_price") or mark.get("hl_mid") or mark.get("binance_mid") or 0)
        if ref <= 0:
            return None

        sl_px = float(p.get("sl_px") or (ref * (1 - sl_pct_fallback) if is_long else ref * (1 + sl_pct_fallback)))
        tp_px = float(p.get("tp_px") or (ref * (1 + tp_pct_fallback) if is_long else ref * (1 - tp_pct_fallback)))

        return Signal(
            coin=coin,
            side="B" if is_long else "A",
            is_long=is_long,
            ref_price=ref,
            sl_px=sl_px,
            tp_px=tp_px,
            max_hold_bars=int(p.get("max_hold_bars") or max_hold),
            fire_ts=time.time() * 1000,
            fire_reason=str(p.get("reason") or "precog_webhook"),
            extras={"confidence": conf, "source": "precog_webhook", "payload": p},
        )
