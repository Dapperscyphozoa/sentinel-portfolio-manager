"""precog_pivot_rsi — clean port of precog.signal() from precog-hl repo.

PARKED in _archived/. Do NOT add to runner.STRATEGY_REGISTRY until it
passes the deploy-gate (see references/precog_revisit.md).

Original code: 14,621 lines (precog-hl/precog.py). Distilled to the
actual signal kernel: pivot detection + RSI threshold + wick rejection
filter, optional chase-gate for select coins, 30-min cooldown.

Status: parked 2026-05-17 after OOS WF (30d, 6 majors, 15m):
    TRAIN n=69 WR=42% PF=0.90 ret -4.8%
    TEST  n=44 WR=43% PF=1.34 ret +10.8%
    -> "test-only edge", train losing, n below noise floor for credible PF claim.
Sentinel kill: 3 of 5 voters (87% conf). Resurrect only via the deploy
gate documented in references/precog_revisit.md.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from .._base import Signal, StrategyBase


# ---------- helpers ----------
def _rma(a: list, n: int) -> list:
    r = [None] * len(a)
    seed = [x for x in a[:n] if x is not None]
    if len(seed) < n:
        return r
    s = sum(seed) / n
    r[n - 1] = s
    for i in range(n, len(a)):
        if a[i] is None:
            r[i] = s
            continue
        s = (s * (n - 1) + a[i]) / n
        r[i] = s
    return r


def _rsi(closes: list[float], n: int = 14) -> list:
    gains = [0.0] * len(closes)
    losses = [0.0] * len(closes)
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains[i] = max(d, 0)
        losses[i] = max(-d, 0)
    ag = _rma(gains, n)
    al = _rma(losses, n)
    out = [None] * len(closes)
    for i in range(len(closes)):
        if ag[i] is None:
            continue
        out[i] = 100 if al[i] == 0 else 100 - 100 / (1 + ag[i] / al[i])
    return out


CHASE_GATE_COINS = {"BTC", "BNB", "DOT", "ATOM", "SUI", "LDO", "INJ",
                    "UMA", "ALGO", "BLUR", "VVV", "APE", "OP", "TON",
                    "TIA", "LTC", "MOODENG", "AR", "GALA", "VIRTUAL"}
CHASE_LOOKBACK = 20


def _chase_gate_ok(side: str, price: float, closes: list[float], i: int) -> bool:
    """Block entries that are chasing a recent extreme (price already
    moved >2% in trade direction over CHASE_LOOKBACK bars)."""
    if i < CHASE_LOOKBACK:
        return True
    base = closes[i - CHASE_LOOKBACK]
    if base <= 0:
        return True
    move = (price - base) / base
    if side == "BUY" and move > 0.02:
        return False
    if side == "SELL" and move < -0.02:
        return False
    return True


# ---------- strategy ----------
class PrecogPivotRsi(StrategyBase):
    """Pivot + RSI + wick rejection (port of precog.signal()).

    BUY when: last bar is local pivot low over LB lookback AND RSI < RL
              AND lower wick > WICK_RATIO * body AND not chasing
    SELL when: mirror image with pivot high + RSI > RH + upper wick

    Defaults preserve the live precog config: LB=10, RH=70, RL=30,
    WICK_RATIO=1.2, CD_MS=30min, SL=1.5%, TP=3.75% (1:2.5 R:R).
    """
    NAME = "precog_pivot_rsi"
    CLOID_PREFIX = "prcog_"
    AFFINITY = ["range", "chop", "trend_up", "trend_down"]
    TF = "15m"
    UNIVERSE = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE",
                "AVAX", "LINK", "ADA", "DOT", "ATOM", "NEAR", "INJ",
                "SUI", "APT", "FIL", "ARB", "OP", "TON", "TIA"]

    _LB = int(os.environ.get("PRECOG_LB", "10"))
    _RH = float(os.environ.get("PRECOG_RSI_HI", "70"))
    _RL = float(os.environ.get("PRECOG_RSI_LO", "30"))
    _WICK_RATIO = float(os.environ.get("PRECOG_WICK_RATIO", "1.2"))
    _CD_BARS = int(os.environ.get("PRECOG_CD_BARS", "2"))  # 30min @ 15m = 2 bars
    _SL_PCT = float(os.environ.get("PRECOG_SL_PCT", "0.015"))
    _TP_PCT = float(os.environ.get("PRECOG_TP_PCT", "0.0375"))
    _MAX_HOLD_BARS = int(os.environ.get("PRECOG_MAX_HOLD", "96"))  # 24h @ 15m

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        bars = bus.candles(coin, cls.TF, n=cls._LB + 40) or None
        if not bars or len(bars) < cls._LB + 20:
            return None
        opens = [b["open"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        closes = [b["close"] for b in bars]
        i = len(bars) - 1
        rsi = _rsi(closes, 14)
        if rsi[i] is None:
            return None
        bar_range = highs[i] - lows[i]
        if bar_range <= 0:
            return None
        is_pivot_high = highs[i] == max(highs[i - cls._LB:i + 1])
        is_pivot_low = lows[i] == min(lows[i - cls._LB:i + 1])
        sell_ok = is_pivot_high and rsi[i] > cls._RH
        buy_ok = is_pivot_low and rsi[i] < cls._RL
        body = abs(closes[i] - opens[i])
        if body > 0:
            upper_wick = highs[i] - max(opens[i], closes[i])
            lower_wick = min(opens[i], closes[i]) - lows[i]
            if sell_ok and upper_wick < cls._WICK_RATIO * body:
                sell_ok = False
            if buy_ok and lower_wick < cls._WICK_RATIO * body:
                buy_ok = False
        if coin in CHASE_GATE_COINS:
            if sell_ok and not _chase_gate_ok("SELL", closes[i], closes, i):
                sell_ok = False
            if buy_ok and not _chase_gate_ok("BUY", closes[i], closes, i):
                buy_ok = False
        if not (sell_ok or buy_ok):
            return None
        is_long = bool(buy_ok)
        ref = closes[i]
        if is_long:
            sl = ref * (1 - cls._SL_PCT)
            tp = ref * (1 + cls._TP_PCT)
        else:
            sl = ref * (1 + cls._SL_PCT)
            tp = ref * (1 - cls._TP_PCT)
        return Signal(
            coin=coin,
            side="B" if is_long else "A",
            is_long=is_long,
            ref_price=ref,
            sl_px=sl,
            tp_px=tp,
            max_hold_bars=cls._MAX_HOLD_BARS,
            fire_ts=time.time() * 1000,
            fire_reason=f"precog_pivot_{'low' if is_long else 'high'}_rsi{rsi[i]:.0f}",
            extras={
                "lb": cls._LB,
                "rsi": round(rsi[i], 1),
                "wick_ratio": round((lower_wick if is_long else upper_wick) / body, 2) if body > 0 else 0,
                "tf": cls.TF,
            },
        )
