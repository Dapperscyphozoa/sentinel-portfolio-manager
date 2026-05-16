"""Round 2 candidates — multi-factor confluence strategies (4 voter + 1 synthesis).
Designed to fire rarely (3-5/mo/coin) with high conviction to escape the
friction-drag failure mode of Round 1.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from ._base import Signal, StrategyBase
from ._indicators import adx, atr, bollinger, donchian, ema, rsi, sma


# Common helper — get an EMA slope: current vs N bars back
def _slope_pos(series, look=5):
    if series[-1] is None or series[-1 - look] is None:
        return False
    return series[-1] > series[-1 - look]


# ============================================================================
# C6: quad_confluence (Qwen3 235B): 4h EMA trend + 1h RSI + vol z-score + Donch
# ============================================================================
class QuadConfluence(StrategyBase):
    NAME = "quad_confluence"
    CLOID_PREFIX = "qcnf_"
    AFFINITY = ["trend_up", "trend_down", "range", "chop"]
    TF = "1h"
    UNIVERSE = ["BTC", "ETH", "SOL"]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        try:
            bars = bus.candles(coin, "1h", n=300)
        except Exception:
            return None
        if len(bars) < 220:
            return None
        highs = [float(b["high"]) for b in bars]
        lows = [float(b["low"]) for b in bars]
        closes = [float(b["close"]) for b in bars]
        vols = [float(b.get("volume", 1.0)) for b in bars]
        # Use long EMAs on 1h as "HTF proxy": EMA80(1h)≈3.3 days, EMA200(1h)≈8.3 days
        e_fast = ema(closes, 50)
        e_slow = ema(closes, 200)
        rsis = rsi(closes, 14)
        atrs = atr(highs, lows, closes, 14)
        if any(x[-1] is None for x in (e_fast, e_slow, rsis, atrs)):
            return None
        # Vol z-score on 20 bars
        v_window = vols[-20:]
        v_mean = sum(v_window) / 20
        v_var = sum((v - v_mean) ** 2 for v in v_window) / 20
        v_std = v_var ** 0.5
        v_z = (vols[-1] - v_mean) / v_std if v_std > 0 else 0
        # 1h Donchian 12-bar breakout (recent half-day high/low)
        dc_up, dc_dn = donchian(highs, lows, 12)
        if dc_up[-2] is None or dc_dn[-2] is None:
            return None
        c = closes[-1]
        a = atrs[-1]
        # Long: HTF trend up + RSI oversold + volume spike + breakout 12-bar high
        trend_up = e_fast[-1] > e_slow[-1] and _slope_pos(e_fast, 5)
        trend_dn = e_fast[-1] < e_slow[-1] and (e_fast[-1] < e_fast[-6])
        is_long: Optional[bool] = None
        if trend_up and rsis[-1] < 35 and v_z > 1.5 and c > dc_up[-2]:
            is_long = True
        elif trend_dn and rsis[-1] > 65 and v_z > 1.5 and c < dc_dn[-2]:
            is_long = False
        else:
            return None
        sl = c - 1.5 * a if is_long else c + 1.5 * a
        tp = c + 3.0 * a if is_long else c - 3.0 * a
        return Signal(coin=coin, side="B" if is_long else "A", is_long=is_long,
                      ref_price=c, sl_px=sl, tp_px=tp,
                      max_hold_bars=72, fire_ts=time.time() * 1000,
                      fire_reason="quad_conf_" + ("long" if is_long else "short"),
                      extras={"rsi": rsis[-1], "vol_z": v_z, "ema_fast": e_fast[-1], "ema_slow": e_slow[-1]})


# ============================================================================
# C7: htf_ltf_rejection (Mistral Large): HTF EMA + Donchian level + LTF bar +
#     volume + RSI div. Simplified without 15m candle pattern.
# ============================================================================
class HTFRejection(StrategyBase):
    NAME = "htf_rejection"
    CLOID_PREFIX = "htfr_"
    AFFINITY = ["trend_up", "trend_down", "range", "chop"]
    TF = "1h"
    UNIVERSE = ["BTC", "ETH", "SOL"]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        try:
            bars = bus.candles(coin, "1h", n=300)
        except Exception:
            return None
        if len(bars) < 220:
            return None
        highs = [float(b["high"]) for b in bars]
        lows = [float(b["low"]) for b in bars]
        closes = [float(b["close"]) for b in bars]
        opens = [float(b["open"]) for b in bars]
        vols = [float(b.get("volume", 1.0)) for b in bars]
        e50 = ema(closes, 50)
        e200 = ema(closes, 200)
        rsis = rsi(closes, 14)
        atrs = atr(highs, lows, closes, 14)
        if any(x[-1] is None for x in (e50, e200, rsis, atrs)):
            return None
        dc_up, dc_dn = donchian(highs, lows, 20)
        if dc_up[-1] is None or dc_dn[-1] is None:
            return None
        # Bar rejection: close near top half of bar's range for long (bullish)
        bar_range = highs[-1] - lows[-1]
        if bar_range <= 0:
            return None
        body_pos = (closes[-1] - lows[-1]) / bar_range  # 0=at low, 1=at high
        # Volume spike
        v_avg = sum(vols[-20:]) / 20
        vol_ok = vols[-1] > 1.5 * v_avg
        # Distance from Donchian level (must be within 1 ATR of band)
        c = closes[-1]
        a = atrs[-1]
        near_low = (lows[-1] <= dc_dn[-1] + 0.3 * a)
        near_high = (highs[-1] >= dc_up[-1] - 0.3 * a)
        is_long: Optional[bool] = None
        if e50[-1] > e200[-1] and near_low and body_pos > 0.6 and vol_ok and rsis[-1] < 40:
            is_long = True
        elif e50[-1] < e200[-1] and near_high and body_pos < 0.4 and vol_ok and rsis[-1] > 60:
            is_long = False
        else:
            return None
        sl = c - 1.5 * a if is_long else c + 1.5 * a
        tp = c + 3.0 * a if is_long else c - 3.0 * a
        return Signal(coin=coin, side="B" if is_long else "A", is_long=is_long,
                      ref_price=c, sl_px=sl, tp_px=tp,
                      max_hold_bars=48, fire_ts=time.time() * 1000,
                      fire_reason="htf_rejection_" + ("long" if is_long else "short"),
                      extras={"e50": e50[-1], "e200": e200[-1], "rsi": rsis[-1], "body_pos": body_pos, "vol_ratio": vols[-1]/v_avg if v_avg > 0 else 0})


# ============================================================================
# C8: trend_reversal_confluence (Qwen3 Coder): 4h EMA200 + 1h RSI + 1h BB
# ============================================================================
class TrendReversal(StrategyBase):
    NAME = "trend_reversal"
    CLOID_PREFIX = "trev_"
    AFFINITY = ["trend_up", "trend_down", "range", "chop"]
    TF = "1h"
    UNIVERSE = ["BTC", "ETH", "SOL"]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        try:
            bars = bus.candles(coin, "1h", n=300)
        except Exception:
            return None
        if len(bars) < 220:
            return None
        highs = [float(b["high"]) for b in bars]
        lows = [float(b["low"]) for b in bars]
        closes = [float(b["close"]) for b in bars]
        vols = [float(b.get("volume", 1.0)) for b in bars]
        e200 = ema(closes, 200)
        rsis = rsi(closes, 14)
        upper, mid, lower = bollinger(closes, 20, 2.0)
        atrs = atr(highs, lows, closes, 14)
        if any(x[-1] is None for x in (e200, rsis, upper, lower, atrs)):
            return None
        v_avg = sum(vols[-20:]) / 20
        vol_ok = vols[-1] > 1.5 * v_avg
        c = closes[-1]
        a = atrs[-1]
        # "4h close above EMA200" approximated as "1h close > EMA200 AND EMA200 slope positive"
        e200_slope_up = e200[-1] > e200[-6] if e200[-6] is not None else False
        e200_slope_dn = e200[-1] < e200[-6] if e200[-6] is not None else False
        is_long: Optional[bool] = None
        # Long: above 200 EMA (trend up) + RSI oversold + touched lower BB + volume spike
        if c > e200[-1] and e200_slope_up and rsis[-1] < 30 and lows[-1] <= lower[-1] and vol_ok:
            is_long = True
        elif c < e200[-1] and e200_slope_dn and rsis[-1] > 70 and highs[-1] >= upper[-1] and vol_ok:
            is_long = False
        else:
            return None
        sl = c - 1.5 * a if is_long else c + 1.5 * a
        tp = c + 3.0 * a if is_long else c - 3.0 * a
        return Signal(coin=coin, side="B" if is_long else "A", is_long=is_long,
                      ref_price=c, sl_px=sl, tp_px=tp,
                      max_hold_bars=72, fire_ts=time.time() * 1000,
                      fire_reason="trend_reversal_" + ("long" if is_long else "short"),
                      extras={"rsi": rsis[-1], "e200": e200[-1]})


# ============================================================================
# C9: htf_ltf_confluence (Codestral): HTF ADX trend + LTF rejection + vol spike +
#     momentum divergence
# ============================================================================
class HTFLTFCfl(StrategyBase):
    NAME = "htf_ltf_cfl"
    CLOID_PREFIX = "htfl_"
    AFFINITY = ["trend_up", "trend_down"]
    TF = "1h"
    UNIVERSE = ["BTC", "ETH", "SOL"]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        try:
            bars = bus.candles(coin, "1h", n=300)
        except Exception:
            return None
        if len(bars) < 220:
            return None
        highs = [float(b["high"]) for b in bars]
        lows = [float(b["low"]) for b in bars]
        closes = [float(b["close"]) for b in bars]
        vols = [float(b.get("volume", 1.0)) for b in bars]
        # HTF approximated by 16-bar ADX (~2/3 day)
        adxs, pdis, mdis = adx(highs, lows, closes, 14)
        rsis = rsi(closes, 14)
        rsis2 = rsi(closes, 5)  # shorter RSI for "LTF divergence proxy"
        atrs = atr(highs, lows, closes, 14)
        if any(x[-1] is None for x in (adxs, pdis, mdis, rsis, rsis2, atrs)):
            return None
        v_avg = sum(vols[-20:]) / 20
        vol_spike = vols[-1] > 2.0 * v_avg
        c = closes[-1]
        a = atrs[-1]
        is_long: Optional[bool] = None
        # Long: ADX>25 trend up + RSI(14)>70 BUT RSI(5)<30 (mean-revert in uptrend) + vol spike
        if adxs[-1] > 25 and pdis[-1] > mdis[-1] and rsis[-1] > 60 and rsis2[-1] < 30 and vol_spike:
            is_long = True
        elif adxs[-1] > 25 and mdis[-1] > pdis[-1] and rsis[-1] < 40 and rsis2[-1] > 70 and vol_spike:
            is_long = False
        else:
            return None
        sl = c - 1.5 * a if is_long else c + 1.5 * a
        tp = c + 3.0 * a if is_long else c - 3.0 * a
        return Signal(coin=coin, side="B" if is_long else "A", is_long=is_long,
                      ref_price=c, sl_px=sl, tp_px=tp,
                      max_hold_bars=48, fire_ts=time.time() * 1000,
                      fire_reason="htf_ltf_" + ("long" if is_long else "short"),
                      extras={"adx": adxs[-1], "rsi": rsis[-1], "rsi5": rsis2[-1]})


# ============================================================================
# C10: claude_synthesis — distilled from all 4 voters
# Components (ALL must align):
#   1. HTF trend filter: EMA50 > EMA200 (uptrend) AND EMA50 rising
#   2. Statistical extreme: close < lower BB(20, 2)
#   3. Momentum: RSI(14) < 30
#   4. Volume confirmation: vol > 1.5 × SMA(vol, 20)
#   5. ATR not collapsing: ATR > 0.5 × SMA(ATR, 20)
# ============================================================================
class ClaudeSynth(StrategyBase):
    NAME = "claude_synth"
    CLOID_PREFIX = "csyn_"
    AFFINITY = ["trend_up", "trend_down", "range", "chop"]
    TF = "1h"
    UNIVERSE = ["BTC", "ETH", "SOL"]

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        try:
            bars = bus.candles(coin, "1h", n=300)
        except Exception:
            return None
        if len(bars) < 220:
            return None
        highs = [float(b["high"]) for b in bars]
        lows = [float(b["low"]) for b in bars]
        closes = [float(b["close"]) for b in bars]
        vols = [float(b.get("volume", 1.0)) for b in bars]
        e50 = ema(closes, 50)
        e200 = ema(closes, 200)
        rsis = rsi(closes, 14)
        upper, mid, lower = bollinger(closes, 20, 2.0)
        atrs = atr(highs, lows, closes, 14)
        if any(x[-1] is None or (len(x) >= 6 and x[-6] is None) for x in (e50, e200, rsis, upper, lower, atrs)):
            return None
        v_avg = sum(vols[-20:]) / 20
        atr_avg = sum(a for a in atrs[-20:] if a is not None) / max(1, len([a for a in atrs[-20:] if a is not None]))
        c = closes[-1]
        a = atrs[-1]
        # Long confluence:
        cond_long = (
            c > e200[-1] and                  # above LT trend
            e50[-1] > e200[-1] and            # MT trend up
            e50[-1] > e50[-6] and             # MT trend rising
            rsis[-1] < 30 and                 # momentum extreme oversold
            lows[-1] <= lower[-1] and         # touched lower BB
            vols[-1] > 1.5 * v_avg and        # volume spike
            a > 0.5 * atr_avg                 # volatility present
        )
        cond_short = (
            c < e200[-1] and
            e50[-1] < e200[-1] and
            e50[-1] < e50[-6] and
            rsis[-1] > 70 and
            highs[-1] >= upper[-1] and
            vols[-1] > 1.5 * v_avg and
            a > 0.5 * atr_avg
        )
        is_long: Optional[bool] = None
        if cond_long:
            is_long = True
        elif cond_short:
            is_long = False
        else:
            return None
        sl = c - 1.5 * a if is_long else c + 1.5 * a
        tp = c + 3.0 * a if is_long else c - 3.0 * a
        return Signal(coin=coin, side="B" if is_long else "A", is_long=is_long,
                      ref_price=c, sl_px=sl, tp_px=tp,
                      max_hold_bars=48, fire_ts=time.time() * 1000,
                      fire_reason="csyn_" + ("long" if is_long else "short"),
                      extras={"rsi": rsis[-1], "e50": e50[-1], "e200": e200[-1], "bb_lower": lower[-1], "bb_upper": upper[-1]})


# All Round 2 candidates
ROUND2 = [QuadConfluence, HTFRejection, TrendReversal, HTFLTFCfl, ClaudeSynth]
