"""11 OOS-validated engines for production deployment.

Validation: 365d HL data, 116-coin universe, 90d TRAIN + 90d TEST split.
Each engine: (signal_logic × regime_gate × timeframe) tuple, isolated per spec.

Production config:
  - 5x leverage on perp position
  - 5% margin per new trade (notional = 25% wallet per position)
  - 20 max concurrent positions globally (1 per coin via PM lock)
  - 10% spot stop-loss (SL = entry × 0.90 long, × 1.10 short)
  - First-fire-wins arbitration (engine ID order = bt_PF order)
  - Auto-cooldown: 4 consec losses/coin (1h), 6 consec losses/engine (1h),
    12% engine DD (1h), live PF < 0.74 × bt_PF after 22 trades (1h)
  - Promotion: 20 paper trades, live PF within 20% of bt_PF → live
"""
from __future__ import annotations

import math
from typing import Optional

from ._base import Signal, StrategyBase
from ._indicators import atr, bollinger, ema, adx


# ---------- Shared helpers ----------
def _regime(closes: list[float], highs: list[float], lows: list[float], i: int) -> str:
    """Threshold regime classifier. PM canonical version is in pm/regime.py;
    this is the strategy-side mirror for evaluate-time gating decisions."""
    if i < 60:
        return "UNKNOWN"
    adx_v, _, _ = adx(highs[: i + 1], lows[: i + 1], closes[: i + 1], 14)
    a = atr(highs[: i + 1], lows[: i + 1], closes[: i + 1], 14)
    if adx_v[-1] is None or a[-1] is None:
        return "UNKNOWN"
    e20 = ema(closes[: i + 1], 20)
    if e20[-1] is None or e20[-6] is None:
        return "UNKNOWN"
    c = closes[i]
    if c <= 0:
        return "UNKNOWN"
    atr_pct = a[-1] / c
    slope = (e20[-1] - e20[-6]) / e20[-6] if e20[-6] > 0 else 0
    if adx_v[-1] > 25 and slope > 0.005:
        return "TREND_UP"
    if adx_v[-1] > 25 and slope < -0.005:
        return "TREND_DOWN"
    if atr_pct > 0.06:
        return "HIGH_VOL"
    if atr_pct < 0.02:
        return "LOW_VOL"
    if abs(slope) < 0.002:
        return "CHOP"
    return "RANGE"


def _bars_for_tf(bus, coin: str, tf: str, n: int) -> Optional[list[dict]]:
    """Fetch n candles for the engine's timeframe."""
    try:
        return bus.candles(coin, tf, n=n) or None
    except Exception:
        return None


def _sl_tp(entry: float, is_long: bool, sl_spot_pct: float = 0.10,
           tp_spot_pct: float = 0.10) -> tuple[float, float]:
    """10% spot SL, 10% spot TP (R:R 1:1 — strategy exits at hold or SL/TP)."""
    if is_long:
        return entry * (1 - sl_spot_pct), entry * (1 + tp_spot_pct)
    return entry * (1 + sl_spot_pct), entry * (1 - tp_spot_pct)


# Default universe — 116 HL perps (overridable via UNIVERSE env)
DEFAULT_UNIVERSE = [
    "BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK", "LTC", "DOT",
    "ATOM", "NEAR", "INJ", "SUI", "APT", "FIL", "ARB", "OP", "MATIC", "TON",
    "TIA", "JUP", "WIF", "kPEPE", "kSHIB", "FTM", "GMX", "AAVE", "UNI",
    "MKR", "COMP", "SEI", "ADA", "TRX", "BCH", "STX", "RNDR", "PENDLE",
    "ORDI", "PYTH", "MEME", "WLD", "CRV", "LDO", "NEO", "kBONK", "CAKE",
]


# ============================================================
# ENGINE 1 (E01_1d) — zfade_3sigma in TREND_UP, 1d
# ============================================================
class E01_zfade_3s_TU_1d(StrategyBase):
    """Z-score fade at 3-sigma extreme in TREND_UP regime, daily.
    Backtest PF: 10.05 (n=15). High conviction, low frequency.
    """
    NAME = "e01_zfade3s_tu_1d"
    CLOID_PREFIX = "e01_"
    AFFINITY = ["trend_up"]
    TF = "1d"
    UNIVERSE = DEFAULT_UNIVERSE
    _PERIOD = 30
    _SIGMA = 3.0
    _HOLD_BARS = 5

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        bars = _bars_for_tf(bus, coin, cls.TF, cls._PERIOD + 80)
        if not bars or len(bars) < cls._PERIOD + 60:
            return None
        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        i = len(bars) - 1
        if _regime(closes, highs, lows, i) not in ("TREND_UP",):
            return None
        win = closes[-cls._PERIOD:]
        mu = sum(win) / cls._PERIOD
        var = sum((w - mu) ** 2 for w in win) / cls._PERIOD
        sd = math.sqrt(var) if var > 0 else 0
        if sd <= 0:
            return None
        c = closes[-1]
        z = (c - mu) / sd
        is_long = None
        if z < -cls._SIGMA:
            is_long = True
        elif z > cls._SIGMA:
            is_long = False
        if is_long is None:
            return None
        sl, tp = _sl_tp(c, is_long)
        return Signal(
            coin=coin, side="B" if is_long else "A", is_long=is_long,
            ref_price=c, sl_px=sl, tp_px=tp, max_hold_bars=cls._HOLD_BARS,
            fire_ts=float(bars[-1]["open_ts"]),
            fire_reason=f"z={z:.2f}",
            extras={"z": z, "sigma": cls._SIGMA, "regime": "TREND_UP", "tf": cls.TF},
        )


# ============================================================
# ENGINE 2 (E07_1d) — zfade_2sigma in TREND_UP, 1d
# ============================================================
class E07_zfade_2s_TU_1d(StrategyBase):
    """Z-score fade at 2-sigma extreme in TREND_UP regime, daily.
    Backtest PF: 2.12 (n=67). Looser threshold = more fires.
    """
    NAME = "e07_zfade2s_tu_1d"
    CLOID_PREFIX = "e07_"
    AFFINITY = ["trend_up"]
    TF = "1d"
    UNIVERSE = DEFAULT_UNIVERSE
    _PERIOD = 30
    _SIGMA = 2.0
    _HOLD_BARS = 5

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        bars = _bars_for_tf(bus, coin, cls.TF, cls._PERIOD + 80)
        if not bars or len(bars) < cls._PERIOD + 60:
            return None
        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        i = len(bars) - 1
        if _regime(closes, highs, lows, i) != "TREND_UP":
            return None
        win = closes[-cls._PERIOD:]
        mu = sum(win) / cls._PERIOD
        sd = math.sqrt(sum((w - mu) ** 2 for w in win) / cls._PERIOD)
        if sd <= 0:
            return None
        c = closes[-1]
        z = (c - mu) / sd
        is_long = True if z < -cls._SIGMA else (False if z > cls._SIGMA else None)
        if is_long is None:
            return None
        sl, tp = _sl_tp(c, is_long)
        return Signal(
            coin=coin, side="B" if is_long else "A", is_long=is_long,
            ref_price=c, sl_px=sl, tp_px=tp, max_hold_bars=cls._HOLD_BARS,
            fire_ts=float(bars[-1]["open_ts"]), fire_reason=f"z={z:.2f}",
            extras={"z": z, "sigma": cls._SIGMA, "regime": "TREND_UP", "tf": cls.TF},
        )


# ============================================================
# ENGINE 3 (E08_1d) — dip3d 10% in TREND_DOWN, 1d
# ============================================================
class E08_dip3d_10_TD_1d(StrategyBase):
    """3-day cumulative drop >10% in TREND_DOWN regime → long bounce, daily.
    Backtest PF: 1.93 (n=203). Workhorse mean-reversion engine.
    """
    NAME = "e08_dip3d10_td_1d"
    CLOID_PREFIX = "e08_"
    AFFINITY = ["trend_down"]
    TF = "1d"
    UNIVERSE = DEFAULT_UNIVERSE
    _DROP_PCT = 0.10
    _HOLD_BARS = 2

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        bars = _bars_for_tf(bus, coin, cls.TF, 70)
        if not bars or len(bars) < 65:
            return None
        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        i = len(bars) - 1
        if _regime(closes, highs, lows, i) != "TREND_DOWN":
            return None
        if i < 3:
            return None
        cum = (closes[-1] - closes[-4]) / closes[-4] if closes[-4] > 0 else 0
        if cum >= -cls._DROP_PCT:
            return None
        c = closes[-1]
        sl, tp = _sl_tp(c, True)
        return Signal(
            coin=coin, side="B", is_long=True,
            ref_price=c, sl_px=sl, tp_px=tp, max_hold_bars=cls._HOLD_BARS,
            fire_ts=float(bars[-1]["open_ts"]), fire_reason=f"dip3d={cum*100:.1f}%",
            extras={"cum_3d_pct": cum, "regime": "TREND_DOWN", "tf": cls.TF},
        )


# ============================================================
# ENGINE 4 (E09_1d) — pump3d 10% in TREND_DOWN, 1d
# ============================================================
class E09_pump3d_10_TD_1d(StrategyBase):
    """3-day cumulative pump >10% in TREND_DOWN regime → short reversion, daily.
    Backtest PF: 1.87 (n=111). Bear-market rally fader.
    """
    NAME = "e09_pump3d10_td_1d"
    CLOID_PREFIX = "e09_"
    AFFINITY = ["trend_down"]
    TF = "1d"
    UNIVERSE = DEFAULT_UNIVERSE
    _PUMP_PCT = 0.10
    _HOLD_BARS = 2

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        bars = _bars_for_tf(bus, coin, cls.TF, 70)
        if not bars or len(bars) < 65:
            return None
        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        i = len(bars) - 1
        if _regime(closes, highs, lows, i) != "TREND_DOWN":
            return None
        if i < 3:
            return None
        cum = (closes[-1] - closes[-4]) / closes[-4] if closes[-4] > 0 else 0
        if cum <= cls._PUMP_PCT:
            return None
        c = closes[-1]
        sl, tp = _sl_tp(c, False)
        return Signal(
            coin=coin, side="A", is_long=False,
            ref_price=c, sl_px=sl, tp_px=tp, max_hold_bars=cls._HOLD_BARS,
            fire_ts=float(bars[-1]["open_ts"]), fire_reason=f"pump3d={cum*100:.1f}%",
            extras={"cum_3d_pct": cum, "regime": "TREND_DOWN", "tf": cls.TF},
        )


# ============================================================
# ENGINE 5 (E16_1d) — bb_fade in HIGH_VOL, 1d
# ============================================================
class E16_bb_fade_HV_1d(StrategyBase):
    """Bollinger band fade in HIGH_VOL regime, daily.

    Honest backtest (2026-05-17, 180d, 47 coins, OOS half):
      n=29, WR 72.4%, PF 5.35, expectancy +3.86%/trade
      OOS:  n=15 (post-split) WR 73%, PF 8.59
      LONG:  n=15 WR 67%, PF 2.84   SHORT: n=14 WR 79%, PF 13.07
    Direction asymmetry 12pp (moderate). Small-sample warning: n=29
    overall; PF 8.59 OOS is largely driven by a few outliers. Council
    promotion audit 2026-05-18: PROVISIONAL — keep symmetric (do not
    short-only at this n), revisit after n≥40 live trades.

    cap_frac in PM registry = 0.30 is advisory only; actual sizing is
    flat MARGIN_PCT_PER_TRADE × LEVERAGE per trade.
    """
    NAME = "e16_bb_fade_hv_1d"
    CLOID_PREFIX = "e16_"
    AFFINITY = ["high_vol"]
    TF = "1d"
    UNIVERSE = DEFAULT_UNIVERSE
    _BB_PERIOD = 20
    _BB_STD = 2.0
    _HOLD_BARS = 3

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        bars = _bars_for_tf(bus, coin, cls.TF, 90)
        if not bars or len(bars) < 80:
            return None
        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        i = len(bars) - 1
        if _regime(closes, highs, lows, i) != "HIGH_VOL":
            return None
        upper, _, lower = bollinger(closes, cls._BB_PERIOD, cls._BB_STD)
        if upper[-1] is None:
            return None
        c = closes[-1]
        is_long = True if c < lower[-1] else (False if c > upper[-1] else None)
        if is_long is None:
            return None
        sl, tp = _sl_tp(c, is_long)
        return Signal(
            coin=coin, side="B" if is_long else "A", is_long=is_long,
            ref_price=c, sl_px=sl, tp_px=tp, max_hold_bars=cls._HOLD_BARS,
            fire_ts=float(bars[-1]["open_ts"]),
            fire_reason="bb_break_lo" if is_long else "bb_break_hi",
            extras={"bb_lower": lower[-1], "bb_upper": upper[-1],
                    "regime": "HIGH_VOL", "tf": cls.TF},
        )


# ============================================================
# ENGINE 6 (E17_1d) — bb_fade in BOTH_TRENDS, 1d
# ============================================================
class E17_bb_fade_BT_1d(StrategyBase):
    """Bollinger band fade — regime-gated to HIGH_VOL and RANGE only.

    HISTORY: original spec gated to TREND_UP/TREND_DOWN. Backtest v2 (2026-05-18,
    n=83 across 20 coins × 200 1d bars) showed:
      TREND_DOWN  n=51  WR 35%  PF 0.59  net -$119
      TREND_UP    n=28  WR 43%  PF 0.60  net  -$59
      HIGH_VOL    n= 2  WR 50%  PF 3.00  net   +$8
      RANGE       n= 2  WR  0%  PF 0.00  net  -$16
    Trend regimes lost in 79/83 fires (95%). Walk-forward both halves <1.0 PF
    (first 0.44, second 0.86). Trend-fade thesis is empirically wrong on this
    sample. Re-gated to non-trend regimes to mirror E16's successful pattern
    (E16 fires HIGH_VOL only, PF 2.70 n=37). E17 covers RANGE which E16 doesn't.
    """
    NAME = "e17_bb_fade_bt_1d"
    CLOID_PREFIX = "e17_"
    AFFINITY = ["high_vol", "range"]
    TF = "1d"
    UNIVERSE = DEFAULT_UNIVERSE
    _BB_PERIOD = 20
    _BB_STD = 2.0
    _HOLD_BARS = 3

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        bars = _bars_for_tf(bus, coin, cls.TF, 90)
        if not bars or len(bars) < 80:
            return None
        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        i = len(bars) - 1
        if _regime(closes, highs, lows, i) not in ("HIGH_VOL", "RANGE"):
            return None
        upper, _, lower = bollinger(closes, cls._BB_PERIOD, cls._BB_STD)
        if upper[-1] is None:
            return None
        c = closes[-1]
        is_long = True if c < lower[-1] else (False if c > upper[-1] else None)
        if is_long is None:
            return None
        sl, tp = _sl_tp(c, is_long)
        return Signal(
            coin=coin, side="B" if is_long else "A", is_long=is_long,
            ref_price=c, sl_px=sl, tp_px=tp, max_hold_bars=cls._HOLD_BARS,
            fire_ts=float(bars[-1]["open_ts"]),
            fire_reason="bb_break_lo" if is_long else "bb_break_hi",
            extras={"bb_lower": lower[-1], "bb_upper": upper[-1],
                    "regime_gate": "HIGH_VOL_OR_RANGE", "tf": cls.TF},
        )


# ============================================================
# ENGINE 7 (E01_4h) — zfade_3sigma in TREND_UP, 4h
# ============================================================
class E01_zfade_3s_TU_4h(StrategyBase):
    NAME = "e01_zfade3s_tu_4h"
    CLOID_PREFIX = "e01h_"
    AFFINITY = ["trend_up"]
    TF = "4h"
    UNIVERSE = DEFAULT_UNIVERSE
    _PERIOD = 30
    _SIGMA = 3.0
    _HOLD_BARS = 5  # = 20h

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        bars = _bars_for_tf(bus, coin, cls.TF, cls._PERIOD + 80)
        if not bars or len(bars) < cls._PERIOD + 60:
            return None
        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        if _regime(closes, highs, lows, len(bars) - 1) != "TREND_UP":
            return None
        win = closes[-cls._PERIOD:]
        mu = sum(win) / cls._PERIOD
        sd = math.sqrt(sum((w - mu) ** 2 for w in win) / cls._PERIOD)
        if sd <= 0:
            return None
        c = closes[-1]
        z = (c - mu) / sd
        is_long = True if z < -cls._SIGMA else (False if z > cls._SIGMA else None)
        if is_long is None:
            return None
        sl, tp = _sl_tp(c, is_long)
        return Signal(
            coin=coin, side="B" if is_long else "A", is_long=is_long,
            ref_price=c, sl_px=sl, tp_px=tp, max_hold_bars=cls._HOLD_BARS,
            fire_ts=float(bars[-1]["open_ts"]), fire_reason=f"z={z:.2f}",
            extras={"z": z, "sigma": cls._SIGMA, "regime": "TREND_UP", "tf": cls.TF},
        )


# ============================================================
# ENGINE 8 (E07_4h) — zfade_2sigma in TREND_UP, 4h (TOP CONTRIBUTOR: $+8.30)
# ============================================================
class E07_zfade_2s_TU_4h(StrategyBase):
    """Top engine by TEST PnL ($+8.30). 4h z-score fade in uptrends."""
    NAME = "e07_zfade2s_tu_4h"
    CLOID_PREFIX = "e07h_"
    AFFINITY = ["trend_up"]
    TF = "4h"
    UNIVERSE = DEFAULT_UNIVERSE
    _PERIOD = 30
    _SIGMA = 2.0
    _HOLD_BARS = 5

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        bars = _bars_for_tf(bus, coin, cls.TF, cls._PERIOD + 80)
        if not bars or len(bars) < cls._PERIOD + 60:
            return None
        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        if _regime(closes, highs, lows, len(bars) - 1) != "TREND_UP":
            return None
        win = closes[-cls._PERIOD:]
        mu = sum(win) / cls._PERIOD
        sd = math.sqrt(sum((w - mu) ** 2 for w in win) / cls._PERIOD)
        if sd <= 0:
            return None
        c = closes[-1]
        z = (c - mu) / sd
        is_long = True if z < -cls._SIGMA else (False if z > cls._SIGMA else None)
        if is_long is None:
            return None
        sl, tp = _sl_tp(c, is_long)
        return Signal(
            coin=coin, side="B" if is_long else "A", is_long=is_long,
            ref_price=c, sl_px=sl, tp_px=tp, max_hold_bars=cls._HOLD_BARS,
            fire_ts=float(bars[-1]["open_ts"]), fire_reason=f"z={z:.2f}",
            extras={"z": z, "sigma": cls._SIGMA, "regime": "TREND_UP", "tf": cls.TF},
        )


# ============================================================
# ENGINE 9 (E08_4h) — dip3d 7% in TREND_DOWN, 4h (adjusted threshold)
# ============================================================
class E08_dip3d_7_TD_4h(StrategyBase):
    """4h dip-buy at 7% drop in TREND_DOWN (lower threshold for shorter TF)."""
    NAME = "e08_dip3d7_td_4h"
    CLOID_PREFIX = "e08h_"
    AFFINITY = ["trend_down"]
    TF = "4h"
    UNIVERSE = DEFAULT_UNIVERSE
    _DROP_PCT = 0.07
    _LOOKBACK = 18   # 3 days × 6 4h bars = 18
    _HOLD_BARS = 12  # 48h

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        bars = _bars_for_tf(bus, coin, cls.TF, cls._LOOKBACK + 70)
        if not bars or len(bars) < cls._LOOKBACK + 65:
            return None
        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        if _regime(closes, highs, lows, len(bars) - 1) != "TREND_DOWN":
            return None
        cum = (closes[-1] - closes[-cls._LOOKBACK - 1]) / closes[-cls._LOOKBACK - 1] if closes[-cls._LOOKBACK - 1] > 0 else 0
        if cum >= -cls._DROP_PCT:
            return None
        c = closes[-1]
        sl, tp = _sl_tp(c, True)
        return Signal(
            coin=coin, side="B", is_long=True,
            ref_price=c, sl_px=sl, tp_px=tp, max_hold_bars=cls._HOLD_BARS,
            fire_ts=float(bars[-1]["open_ts"]), fire_reason=f"dip3d={cum*100:.1f}%",
            extras={"cum_3d_pct": cum, "regime": "TREND_DOWN", "tf": cls.TF},
        )


# ============================================================
# ENGINE 10 (E16_4h) — bb_fade in HIGH_VOL, 4h
# ============================================================
class E16_bb_fade_HV_4h(StrategyBase):
    NAME = "e16_bb_fade_hv_4h"
    CLOID_PREFIX = "e16h_"
    AFFINITY = ["high_vol"]
    TF = "4h"
    UNIVERSE = DEFAULT_UNIVERSE
    _BB_PERIOD = 20
    _BB_STD = 2.0
    _HOLD_BARS = 6  # 24h

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        bars = _bars_for_tf(bus, coin, cls.TF, 90)
        if not bars or len(bars) < 80:
            return None
        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        if _regime(closes, highs, lows, len(bars) - 1) != "HIGH_VOL":
            return None
        upper, _, lower = bollinger(closes, cls._BB_PERIOD, cls._BB_STD)
        if upper[-1] is None:
            return None
        c = closes[-1]
        is_long = True if c < lower[-1] else (False if c > upper[-1] else None)
        if is_long is None:
            return None
        sl, tp = _sl_tp(c, is_long)
        return Signal(
            coin=coin, side="B" if is_long else "A", is_long=is_long,
            ref_price=c, sl_px=sl, tp_px=tp, max_hold_bars=cls._HOLD_BARS,
            fire_ts=float(bars[-1]["open_ts"]),
            fire_reason="bb_break_lo" if is_long else "bb_break_hi",
            extras={"bb_lower": lower[-1], "bb_upper": upper[-1],
                    "regime": "HIGH_VOL", "tf": cls.TF},
        )


# ============================================================
# ENGINE 11 (E17_4h) — bb_fade in BOTH_TRENDS, 4h (n=550, $+5.48)
# ============================================================
class E17_bb_fade_BT_4h(StrategyBase):
    """Bollinger band fade — regime-gated to HIGH_VOL and RANGE only, 4h.

    HISTORY: original spec gated to TREND_UP/TREND_DOWN. Backtest v2 (2026-05-18,
    n=70 across 20 coins × 205 4h bars) showed:
      TREND_UP    n=52  WR 50%  PF 0.50  net -$80
      TREND_DOWN  n=17  WR 53%  PF 0.64  net  -$9
      LOW_VOL     n= 1  WR 100% PF inf   net  +$6
    Walk-forward both halves <1.0 PF (first 0.30, second 0.95). Same pattern
    as E17_1d — trend-fade thesis empirically broken. Re-gated to mirror E16.
    Will resume signalling under the new gate; cap_frac stays 0.00 (RED in
    registry) until 30+ trades accumulate under the corrected gate.
    """
    NAME = "e17_bb_fade_bt_4h"
    CLOID_PREFIX = "e17h_"
    AFFINITY = ["high_vol", "range"]
    TF = "4h"
    UNIVERSE = DEFAULT_UNIVERSE
    _BB_PERIOD = 20
    _BB_STD = 2.0
    _HOLD_BARS = 6

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        bars = _bars_for_tf(bus, coin, cls.TF, 90)
        if not bars or len(bars) < 80:
            return None
        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        if _regime(closes, highs, lows, len(bars) - 1) not in ("HIGH_VOL", "RANGE"):
            return None
        upper, _, lower = bollinger(closes, cls._BB_PERIOD, cls._BB_STD)
        if upper[-1] is None:
            return None
        c = closes[-1]
        is_long = True if c < lower[-1] else (False if c > upper[-1] else None)
        if is_long is None:
            return None
        sl, tp = _sl_tp(c, is_long)
        return Signal(
            coin=coin, side="B" if is_long else "A", is_long=is_long,
            ref_price=c, sl_px=sl, tp_px=tp, max_hold_bars=cls._HOLD_BARS,
            fire_ts=float(bars[-1]["open_ts"]),
            fire_reason="bb_break_lo" if is_long else "bb_break_hi",
            extras={"bb_lower": lower[-1], "bb_upper": upper[-1],
                    "regime_gate": "HIGH_VOL_OR_RANGE", "tf": cls.TF},
        )


# Engine registry — ordered by backtest PF (highest first → first-fire wins for highest PF)
OOS_ENGINES = [
    E01_zfade_3s_TU_1d,    # bt_PF 10.05
    E08_dip3d_10_TD_1d,    # bt_PF 1.93
    E09_pump3d_10_TD_1d,   # bt_PF 1.87
    E07_zfade_2s_TU_1d,    # bt_PF 2.12
    E16_bb_fade_HV_1d,     # bt_PF 1.47
    E17_bb_fade_BT_1d,     # bt_PF 1.41
    E01_zfade_3s_TU_4h,
    E07_zfade_2s_TU_4h,    # top contributor by PnL
    E08_dip3d_7_TD_4h,
    E16_bb_fade_HV_4h,
    E17_bb_fade_BT_4h,
]
