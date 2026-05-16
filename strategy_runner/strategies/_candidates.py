"""Council-proposed candidate strategies for Round 1 backtesting.

These are research candidates — NOT yet wired into runner.py. If any survive
the backtest filter (PF > 1.3 over 365d), they get promoted to their own
module and unit-tested.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from ._base import Signal, StrategyBase
from ._indicators import (
    adx, atr, bb_width, bollinger, donchian, ema, keltner, rsi, sma, vwrsi,
)


def _envf(k, d): return float(os.environ.get(k, d))


# ============================================================================
# CANDIDATE 1: Volatility-Adjusted RSI Divergence (Qwen3 Coder, round 1)
# Long: RSI(14)<30 AND close>EMA(200) AND ATR(14) > ATR_prev × 1.05
# Mean-reversion in healthy uptrend during volatility expansion.
# ============================================================================
class VolRSI(StrategyBase):
    NAME = "vol_rsi"
    CLOID_PREFIX = "vrsi_"
    AFFINITY = ["trend_up", "trend_down", "range", "chop"]
    TF = "1h"
    UNIVERSE = ["BTC", "ETH", "SOL"]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        try:
            bars = bus.candles(coin, "1h", n=250)
        except Exception:
            return None
        if len(bars) < 220:
            return None
        highs = [float(b["high"]) for b in bars]
        lows = [float(b["low"]) for b in bars]
        closes = [float(b["close"]) for b in bars]
        rsis = rsi(closes, 14)
        emas = ema(closes, 200)
        atrs = atr(highs, lows, closes, 14)
        if rsis[-1] is None or emas[-1] is None or atrs[-1] is None or atrs[-2] is None:
            return None
        r = rsis[-1]
        e = emas[-1]
        a = atrs[-1]
        a_prev = atrs[-2]
        c = closes[-1]
        vol_rising = a > a_prev * 1.05
        is_long: Optional[bool] = None
        if r < 30 and c > e and vol_rising:
            is_long = True
        elif r > 70 and c < e and vol_rising:
            is_long = False
        else:
            return None
        sl = c - 2.5 * a if is_long else c + 2.5 * a
        tp = c + 3.5 * a if is_long else c - 3.5 * a
        return Signal(
            coin=coin, side="B" if is_long else "A", is_long=is_long,
            ref_price=c, sl_px=sl, tp_px=tp,
            max_hold_bars=120, fire_ts=time.time() * 1000,
            fire_reason="vol_rsi_" + ("long" if is_long else "short"),
            extras={"rsi": r, "ema200": e, "atr": a, "atr_prev": a_prev},
        )


# ============================================================================
# CANDIDATE 2: Volatility Compression Breakout (Qwen3 235B)
# Long: BB_width(20) < SMA(BB_width,50) AND close > upper_BB(20) AND volume>avg
# 4h timeframe. BB squeeze → expansion breakout with volume confirmation.
# ============================================================================
class BBSqueeze(StrategyBase):
    NAME = "bb_squeeze"
    CLOID_PREFIX = "bbsq_"
    AFFINITY = ["trend_up", "trend_down", "range", "chop"]
    TF = "4h"
    UNIVERSE = ["BTC", "ETH", "SOL"]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        try:
            bars = bus.candles(coin, "4h", n=150)
        except Exception:
            return None
        if len(bars) < 80:
            return None
        highs = [float(b["high"]) for b in bars]
        lows = [float(b["low"]) for b in bars]
        closes = [float(b["close"]) for b in bars]
        vols = [float(b.get("volume", 0)) for b in bars]
        upper, mid, lower = bollinger(closes, 20, 2.0)
        bbw = bb_width(closes, 20, 2.0)
        atrs = atr(highs, lows, closes, 14)
        if upper[-1] is None or lower[-1] is None or bbw[-1] is None or atrs[-1] is None:
            return None
        # BB width below its own 50-bar SMA — squeeze regime
        bbw_valid = [x for x in bbw[-50:] if x is not None]
        if len(bbw_valid) < 30:
            return None
        bbw_avg = sum(bbw_valid) / len(bbw_valid)
        is_squeezed = bbw[-1] < bbw_avg
        vol_valid = [v for v in vols[-20:] if v > 0]
        vol_avg = sum(vol_valid) / len(vol_valid) if vol_valid else 0
        vol_confirm = vols[-1] > vol_avg
        c = closes[-1]
        a = atrs[-1]
        is_long: Optional[bool] = None
        if is_squeezed and c > upper[-1] and vol_confirm:
            is_long = True
        elif is_squeezed and c < lower[-1] and vol_confirm:
            is_long = False
        else:
            return None
        sl = c - 1.5 * a if is_long else c + 1.5 * a
        tp = c + 3.0 * a if is_long else c - 3.0 * a
        return Signal(
            coin=coin, side="B" if is_long else "A", is_long=is_long,
            ref_price=c, sl_px=sl, tp_px=tp,
            max_hold_bars=60, fire_ts=time.time() * 1000,
            fire_reason="bb_squeeze_" + ("up" if is_long else "dn"),
            extras={"bbw": bbw[-1], "bbw_avg": bbw_avg, "vol_ratio": vols[-1] / vol_avg if vol_avg > 0 else 0},
        )


# ============================================================================
# CANDIDATE 3: Keltner Squeeze Momentum (Mistral Large)
# Long: close > KeltnerUpper(20) AND ADX>25 AND +DI>-DI AND ATR > SMA(ATR,20)
# 4h. Classic ADX-confirmed breakout with directional movement filter.
# ============================================================================
class KeltnerADX(StrategyBase):
    NAME = "keltner_adx"
    CLOID_PREFIX = "kadx_"
    AFFINITY = ["trend_up", "trend_down"]
    TF = "4h"
    UNIVERSE = ["BTC", "ETH", "SOL"]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        try:
            bars = bus.candles(coin, "4h", n=150)
        except Exception:
            return None
        if len(bars) < 80:
            return None
        highs = [float(b["high"]) for b in bars]
        lows = [float(b["low"]) for b in bars]
        closes = [float(b["close"]) for b in bars]
        kup, kmid, klow = keltner(highs, lows, closes, 20, 2.0)
        atrs = atr(highs, lows, closes, 14)
        adxs, pdis, mdis = adx(highs, lows, closes, 14)
        if kup[-1] is None or atrs[-1] is None or adxs[-1] is None or pdis[-1] is None or mdis[-1] is None:
            return None
        # ATR rising filter
        atr_valid = [a for a in atrs[-20:] if a is not None]
        if len(atr_valid) < 15:
            return None
        atr_avg = sum(atr_valid) / len(atr_valid)
        atr_rising = atrs[-1] > atr_avg
        c = closes[-1]
        a = atrs[-1]
        is_long: Optional[bool] = None
        if c > kup[-1] and adxs[-1] > 25 and pdis[-1] > mdis[-1] and atr_rising:
            is_long = True
        elif c < klow[-1] and adxs[-1] > 25 and mdis[-1] > pdis[-1] and atr_rising:
            is_long = False
        else:
            return None
        sl = c - 1.5 * a if is_long else c + 1.5 * a
        tp = c + 3.0 * a if is_long else c - 3.0 * a
        return Signal(
            coin=coin, side="B" if is_long else "A", is_long=is_long,
            ref_price=c, sl_px=sl, tp_px=tp,
            max_hold_bars=60, fire_ts=time.time() * 1000,
            fire_reason="keltner_adx_" + ("up" if is_long else "dn"),
            extras={"adx": adxs[-1], "pdi": pdis[-1], "mdi": mdis[-1], "kup": kup[-1], "klow": klow[-1]},
        )


# ============================================================================
# CANDIDATE 4: VWRSI + EMA stack (Codestral)
# Long: VWRSI(14)<20 AND close>EMA(50) AND close<EMA(200)
# 1h. Volume-confirmed oversold bounce below 200 EMA but above 50 EMA.
# ============================================================================
class VWRSI_EMA(StrategyBase):
    NAME = "vwrsi_ema"
    CLOID_PREFIX = "vwre_"
    AFFINITY = ["trend_up", "trend_down", "range", "chop"]
    TF = "1h"
    UNIVERSE = ["BTC", "ETH", "SOL"]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        try:
            bars = bus.candles(coin, "1h", n=250)
        except Exception:
            return None
        if len(bars) < 220:
            return None
        highs = [float(b["high"]) for b in bars]
        lows = [float(b["low"]) for b in bars]
        closes = [float(b["close"]) for b in bars]
        vols = [float(b.get("volume", 1.0)) for b in bars]
        vwr = vwrsi(closes, vols, 14)
        e50 = ema(closes, 50)
        e200 = ema(closes, 200)
        atrs = atr(highs, lows, closes, 14)
        if vwr[-1] is None or e50[-1] is None or e200[-1] is None or atrs[-1] is None:
            return None
        c = closes[-1]
        a = atrs[-1]
        is_long: Optional[bool] = None
        # Long: oversold VWRSI, below LT trend but bouncing above ST trend
        if vwr[-1] < 20 and c > e50[-1] and c < e200[-1]:
            is_long = True
        elif vwr[-1] > 80 and c < e50[-1] and c > e200[-1]:
            is_long = False
        else:
            return None
        sl = c - 1.5 * a if is_long else c + 1.5 * a
        tp = c + 2.0 * a if is_long else c - 2.0 * a
        return Signal(
            coin=coin, side="B" if is_long else "A", is_long=is_long,
            ref_price=c, sl_px=sl, tp_px=tp,
            max_hold_bars=72, fire_ts=time.time() * 1000,
            fire_reason="vwrsi_ema_" + ("long" if is_long else "short"),
            extras={"vwrsi": vwr[-1], "ema50": e50[-1], "ema200": e200[-1]},
        )


# ============================================================================
# CANDIDATE 5: Volatility Spike Mean Reversion at EMA200 (Qwen3 Coder, round 2)
# Long: close < EMA(200) AND low touched EMA(200) AND wide bar (range > 1.5×ATR)
# 1h. After a wide bar that pierces and recovers from EMA200, expect bounce.
# ============================================================================
class VolSpikeEMA(StrategyBase):
    NAME = "volspike_ema"
    CLOID_PREFIX = "vspk_"
    AFFINITY = ["trend_up", "trend_down", "range", "chop"]
    TF = "1h"
    UNIVERSE = ["BTC", "ETH", "SOL"]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        try:
            bars = bus.candles(coin, "1h", n=250)
        except Exception:
            return None
        if len(bars) < 220:
            return None
        highs = [float(b["high"]) for b in bars]
        lows = [float(b["low"]) for b in bars]
        closes = [float(b["close"]) for b in bars]
        emas = ema(closes, 200)
        atrs = atr(highs, lows, closes, 14)
        if emas[-1] is None or atrs[-1] is None:
            return None
        c = closes[-1]
        h = highs[-1]
        l = lows[-1]
        e = emas[-1]
        a = atrs[-1]
        wide_bar = (h - l) > 1.5 * a
        is_long: Optional[bool] = None
        # Long: above EMA200 (uptrend) and price spike DOWN through EMA200
        # then recovered. Mean reversion bounce expected.
        if c > e and l <= e and wide_bar:
            is_long = True
        elif c < e and h >= e and wide_bar:
            is_long = False
        else:
            return None
        sl = c - 2.5 * a if is_long else c + 2.5 * a
        tp = c + 3.0 * a if is_long else c - 3.0 * a
        return Signal(
            coin=coin, side="B" if is_long else "A", is_long=is_long,
            ref_price=c, sl_px=sl, tp_px=tp,
            max_hold_bars=72, fire_ts=time.time() * 1000,
            fire_reason="volspike_ema_" + ("long" if is_long else "short"),
            extras={"ema200": e, "atr": a, "range_ratio": (h - l) / a if a > 0 else 0},
        )


# Convenience: list of all candidates
ALL_CANDIDATES = [VolRSI, BBSqueeze, KeltnerADX, VWRSI_EMA, VolSpikeEMA]
