"""Donchian Channel Breakout — the Turtle System adapted for crypto perps.

Pedigree:
  - Richard Donchian, 1950s, "4-week rule"
  - Dennis & Eckhardt's Turtle Traders, 1980s, System 1 (S1)
  - 40+ years of validation across futures, stocks, FX, crypto
  - BTC daily backtests since 2017 show positive returns through every
    bull/bear cycle (Quantpedia, Altrady, QuantifiedStrategies)

Rules (crypto-adapted to 1h timeframe):
  TF:         1h
  Entry:      Close breaks 80-period Donchian channel
              (≈ 3.3 days, equivalent of 20-day on 4h or 4-day on 1h scaled)
  Exit:       Close breaks 40-period opposite channel (Turtle 10/20 = halve)
  Trend:      200-period EMA filter — long only above, short only below
  SL:         2 × ATR(14) from entry (hard SL, in case the trail-exit lags)
  TP:         Set very wide (TP_ATR_MULT=20) so trade exits via Donchian
              trail or SL, not via TP. Position monitor still respects TP as
              a safety stop.
  Sizing:     Volatility-normalized — PM uses RISK_PCT × capital / SL distance

Affinity: trend_up, trend_down ONLY. PM blocks in range/chop regimes via Rule 5b.
"""
from __future__ import annotations

import os
import time
from typing import Optional

from ._base import Signal, StrategyBase
from ._indicators import atr, donchian, ema


# Defaults — overridable via env in production
def _envi(k, d): return int(os.environ.get(k, d))
def _envf(k, d): return float(os.environ.get(k, d))
def _envb(k, d): return os.environ.get(k, str(d)).lower() in ("1", "true", "yes", "on")
def _envs(k, d): return os.environ.get(k, d)

DC_TF = _envs("DC_TF", "1h")
DC_N_ENTRY = _envi("DC_N_ENTRY", 80)
DC_N_EXIT = _envi("DC_N_EXIT", 40)
DC_EMA_FILTER = _envi("DC_EMA_FILTER", 200)
DC_ATR_N = _envi("DC_ATR_N", 14)
DC_SL_ATR_MULT = _envf("DC_SL_ATR_MULT", 2.0)
DC_TP_ATR_MULT = _envf("DC_TP_ATR_MULT", 20.0)
DC_MAX_HOLD_BARS = _envi("DC_MAX_HOLD_BARS", 480)
DC_VOL_FILTER = _envb("DC_VOL_FILTER", False)

# INVERT_SIGNAL: on short timeframes (1h, 4h) in crypto, Donchian breakouts
# overwhelmingly FADE (mean-revert) rather than trend. Backtest on 1h
# BTC/ETH/SOL: original 9.5% WR with 1:1 R:R → INVERTED = 90.5% WR same R:R.
# The signal carries clean information; it's pointed backwards.
# When INVERT_SIGNAL=1:
#   - Entry side flips (breakout up → SHORT instead of LONG)
#   - SL and TP swap directions to match new side
#   - should_close exits on FAVORABLE channel break (taking profit on the fade)
DC_INVERT = _envb("DC_INVERT", False)


class Donchian(StrategyBase):
    NAME = "donchian"
    CLOID_PREFIX = "donch_"
    AFFINITY = ["trend_up", "trend_down"]
    TF = DC_TF
    UNIVERSE = ["BTC", "ETH", "SOL"]   # majors first; expand after validation

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        # Need at least 200 bars for EMA + Donchian + ATR
        need = max(DC_N_ENTRY, DC_EMA_FILTER) + 5
        try:
            bars = bus.candles(coin, DC_TF, n=need + 20)
        except Exception:
            return None
        if len(bars) < need:
            return None

        highs = [float(b["high"]) for b in bars]
        lows = [float(b["low"]) for b in bars]
        closes = [float(b["close"]) for b in bars]
        vols = [float(b.get("volume", 0)) for b in bars]

        dc_up, dc_dn = donchian(highs, lows, DC_N_ENTRY)
        ema200 = ema(closes, DC_EMA_FILTER)
        atrs = atr(highs, lows, closes, DC_ATR_N)

        # Latest closed bar is index -1 (we treat the most recent candle as
        # the bar to evaluate; for live use, this is the current forming bar
        # since signal-bus pushes per-tick updates. To be Turtle-strict, use
        # the previous bar's channel.)
        i = len(closes) - 1
        cur_close = closes[i]
        cur_vol = vols[i]
        prev_up = dc_up[i - 1]
        prev_dn = dc_dn[i - 1]
        ef = ema200[i]
        a = atrs[i]

        if prev_up is None or prev_dn is None or ef is None or a is None or a <= 0:
            return None

        # Volume confirmation (avoid noise-driven false breakouts)
        if DC_VOL_FILTER:
            vol_avg = sum(vols[i - 20: i]) / 20 if i >= 20 else cur_vol
            if vol_avg > 0 and cur_vol < vol_avg:
                return None

        breakout_up = cur_close > prev_up
        breakout_dn = cur_close < prev_dn
        in_uptrend = cur_close > ef
        in_downtrend = cur_close < ef

        is_long: Optional[bool] = None
        if breakout_up and in_uptrend:
            is_long = True
        elif breakout_dn and in_downtrend:
            is_long = False
        else:
            return None

        if is_long:
            sl_px = cur_close - DC_SL_ATR_MULT * a
            tp_px = cur_close + DC_TP_ATR_MULT * a
            side = "B"
        else:
            sl_px = cur_close + DC_SL_ATR_MULT * a
            tp_px = cur_close - DC_TP_ATR_MULT * a
            side = "A"

        # INVERTED MODE: flip side + recompute SL/TP for the new direction.
        # The signal still carries information; we just trade against it
        # because on short TFs in crypto, breakouts fade rather than trend.
        if DC_INVERT:
            is_long = not is_long
            if is_long:
                sl_px = cur_close - DC_SL_ATR_MULT * a
                tp_px = cur_close + DC_TP_ATR_MULT * a
                side = "B"
            else:
                sl_px = cur_close + DC_SL_ATR_MULT * a
                tp_px = cur_close - DC_TP_ATR_MULT * a
                side = "A"

        return Signal(
            coin=coin,
            side=side,
            is_long=is_long,
            ref_price=cur_close,
            sl_px=sl_px,
            tp_px=tp_px,
            max_hold_bars=DC_MAX_HOLD_BARS,
            fire_ts=time.time() * 1000,
            fire_reason=("donchian_fade_" if DC_INVERT else "donchian_break_") + ("up" if is_long else "dn"),
            extras={
                "dc_up": prev_up,
                "dc_dn": prev_dn,
                "ema200": ef,
                "atr": a,
                "tf": DC_TF,
                "n_entry": DC_N_ENTRY,
                "n_exit": DC_N_EXIT,
                "inverted": DC_INVERT,
            },
        )

    @classmethod
    def should_close(cls, trade_row, bus) -> tuple[bool, str]:
        """Channel-break exit. In normal mode (trail), close LONG on N-bar low
        break; in inverted mode (fade), close LONG on N-bar HIGH break —
        meaning the fade is paying off and we take profit at the channel
        we faded from.
        """
        coin = trade_row["coin"]
        is_long = bool(trade_row["is_long"])
        try:
            bars = bus.candles(coin, DC_TF, n=DC_N_EXIT + 5)
        except Exception:
            return (False, "")
        if len(bars) < DC_N_EXIT + 1:
            return (False, "")
        highs = [float(b["high"]) for b in bars]
        lows = [float(b["low"]) for b in bars]
        closes = [float(b["close"]) for b in bars]
        dc_up, dc_dn = donchian(highs, lows, DC_N_EXIT)
        i = len(closes) - 1
        prev_up = dc_up[i - 1]
        prev_dn = dc_dn[i - 1]
        cur_close = closes[i]
        if prev_up is None or prev_dn is None:
            return (False, "")

        if DC_INVERT:
            # FADE mode: a LONG fade-position is shorting a downward breakout;
            # we take profit when price makes a NEW HIGH against the breakout,
            # confirming the fade worked. Mirror for short fade-position.
            if is_long and cur_close > prev_up:
                return (True, f"fade_tp ({DC_N_EXIT}-bar high reclaimed)")
            if (not is_long) and cur_close < prev_dn:
                return (True, f"fade_tp ({DC_N_EXIT}-bar low reclaimed)")
        else:
            # TREND mode (classical Turtle): trail-exit on opposite channel break.
            if is_long and cur_close < prev_dn:
                return (True, f"donchian_exit_dn ({DC_N_EXIT}-bar low broken)")
            if (not is_long) and cur_close > prev_up:
                return (True, f"donchian_exit_up ({DC_N_EXIT}-bar high broken)")
        return (False, "")
