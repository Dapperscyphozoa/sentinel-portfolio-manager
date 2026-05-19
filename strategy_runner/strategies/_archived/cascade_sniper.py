"""Cascade Sniper — Binance liq detection → HL execution.

Council 5/6 unanimous PICK over ICT, listings, arb, unlocks.
Variant B per /tmp/audit/sniper_select.txt.

THESIS:
  Binance liq stream is 10-30× denser than HL's. When $500K+ of liquidations
  consolidate in 30 seconds for one coin, price has momentum.
  - In TREND regime: RIDE the cascade (same direction as forced flow)
  - In RANGE regime: FADE the cascade (opposite, mean-revert)
  Hold ≤3 minutes. Hard SL. No averaging-down.

CASCADE DEFINITION:
  Sum of USD-notional liqs in last LIQ_WINDOW_S seconds, segregated by side.
  side='SELL' = long position forced to sell  (dominant means longs nuked)
  side='BUY'  = short position forced to buy  (dominant means shorts nuked)
  Cascade fires when dominant_side_usd >= LIQ_USD_MIN.

ANTI-SPAM:
  After firing on coin X, suppress further signals on X for COOLDOWN_S seconds.

LIVE-SAFETY INTEGRATION:
  Signal routes through common.live_safety: tightened risk, circuit breakers,
  daily halts, kill switch. Same controls as ICT live deploy.

KILLSHOT RISK (council unanimous): HL halts trading during volatility cascades,
preventing exit. Mitigations: hard SL at 0.4-0.6%, max_hold 180s, position cap.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

from ._base import Signal, StrategyBase

log = logging.getLogger("cascade_sniper")


# Per-coin last fire time for anti-spam (process-local cooldown)
_last_fire: dict[str, float] = {}


def _f(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


class CascadeSniperHL(StrategyBase):
    """Binance liquidation cascade sniper, HL execution venue."""

    NAME = "cascade_sniper_hl"
    CLOID_PREFIX = "casc_"
    AFFINITY = ["high_vol", "trend_up", "trend_down", "range", "chop"]
    TF = "1m"   # nominal — actually event-driven via bus.liq()
    # Top-30 HL perps by volume; expand cautiously
    UNIVERSE = [
        "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "AVAX", "LINK", "LTC", "DOT",
        "ADA", "ATOM", "NEAR", "ARB", "OP", "SUI", "APT", "TON", "TIA", "INJ",
        "SEI", "BCH", "TRX", "AAVE", "UNI", "MKR", "FIL", "WIF", "kPEPE", "JUP",
    ]

    # Cascade detection thresholds (env-overridable)
    LIQ_USD_MIN = 500_000          # min dominant-side liq USD in window
    LIQ_WINDOW_S = 30              # seconds to consolidate liqs
    COOLDOWN_S = 60                # anti-spam: 60s suppression after fire
    # Regime classifier (HL 1h SMA-14)
    REGIME_TREND_PCT = 0.020       # close vs 14-bar SMA > 2% = trend_up
    # Entry/exit per regime
    RIDE_TP_PCT = 0.008            # 0.8% take-profit in trend regime
    RIDE_SL_PCT = 0.004            # 0.4% stop in trend regime → 2:1 R:R
    FADE_TP_PCT = 0.012            # 1.2% in range regime
    FADE_SL_PCT = 0.006            # 0.6% in range regime → 2:1 R:R
    MAX_HOLD_BARS = 3              # 3 × 1m = 3 minutes (close on next candle bias)

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        # Env-overridable
        liq_min = _f(f"CASC_LIQ_USD_MIN", cls.LIQ_USD_MIN)
        win_s = int(_f(f"CASC_LIQ_WINDOW_S", cls.LIQ_WINDOW_S))
        cooldown_s = int(_f(f"CASC_COOLDOWN_S", cls.COOLDOWN_S))

        # Anti-spam: skip if recently fired on this coin
        now = time.time()
        last = _last_fire.get(coin, 0.0)
        if now - last < cooldown_s:
            return None

        # 1) Pull recent liqs from signal-bus
        try:
            now_ms = int(now * 1000)
            since_ms = now_ms - win_s * 1000
            liqs = bus.liq(since_ms=since_ms, coin=coin) or []
        except Exception:
            log.exception("liq fetch failed for %s", coin)
            return None
        if not liqs:
            return None

        # 2) Consolidate by side. Binance forceOrder field 'S' (side):
        #    'SELL' = long position liquidated  (forced market-sell)
        #    'BUY'  = short position liquidated (forced market-buy)
        long_liqs_usd = sum(
            float(l.get("usd", 0)) for l in liqs
            if str(l.get("side", "")).upper() == "SELL"
        )
        short_liqs_usd = sum(
            float(l.get("usd", 0)) for l in liqs
            if str(l.get("side", "")).upper() == "BUY"
        )
        dominant_usd = max(long_liqs_usd, short_liqs_usd)
        if dominant_usd < liq_min:
            return None
        dominant_side = "long" if long_liqs_usd > short_liqs_usd else "short"

        # 3) Current mark price (HL — execution venue)
        try:
            mp = bus.markprice(coin) or {}
            cur = mp.get("hl_mid") or mp.get("binance_mid")
            if cur is None:
                return None
            cur = float(cur)
            if cur <= 0:
                return None
        except Exception:
            log.exception("markprice fetch failed for %s", coin)
            return None

        # 4) Regime classification (HL 1h, SMA-14)
        regime = cls._classify_regime(coin, bus, cur)

        # 5) Direction: ride in trend, fade in range/chop
        is_trend_regime = regime in ("trend_up", "trend_down")
        if is_trend_regime:
            # RIDE: long-liqs (SELL pressure) → keep selling → go SHORT
            #       short-liqs (BUY pressure) → keep buying → go LONG
            is_long = (dominant_side == "short")
            mode = "ride"
            tp_pct, sl_pct = cls.RIDE_TP_PCT, cls.RIDE_SL_PCT
        else:
            # FADE: long-liqs cascade exhausted → buy the dip → LONG
            #       short-liqs cascade exhausted → sell the top → SHORT
            is_long = (dominant_side == "long")
            mode = "fade"
            tp_pct, sl_pct = cls.FADE_TP_PCT, cls.FADE_SL_PCT

        # 6) Build signal
        sl_px = cur * (1 - sl_pct) if is_long else cur * (1 + sl_pct)
        tp_px = cur * (1 + tp_pct) if is_long else cur * (1 - tp_pct)

        # Record fire time for anti-spam
        _last_fire[coin] = now

        return Signal(
            coin=coin,
            side="B" if is_long else "A",
            is_long=is_long,
            ref_price=cur,
            sl_px=sl_px,
            tp_px=tp_px,
            max_hold_bars=cls.MAX_HOLD_BARS,
            fire_ts=now_ms,
            fire_reason=f"casc_{mode}_{dominant_side}_${int(dominant_usd/1000)}k_{regime}",
            extras={
                "dominant_side": dominant_side,
                "dominant_usd": dominant_usd,
                "long_liqs_usd": long_liqs_usd,
                "short_liqs_usd": short_liqs_usd,
                "n_liq_events": len(liqs),
                "window_s": win_s,
                "regime": regime,
                "mode": mode,
                "ride_or_fade_tp": tp_pct,
                "ride_or_fade_sl": sl_pct,
            },
        )

    @classmethod
    def _classify_regime(cls, coin: str, bus, cur_price: float) -> str:
        """Simple regime classifier: HL 1h SMA-14 vs current price."""
        try:
            bars = bus.candles(coin, "1h", n=24) or []
            if len(bars) < 14:
                return "unknown"
            closes = [float(b["close"]) for b in bars[-14:]]
            sma = sum(closes) / len(closes)
            if sma <= 0:
                return "unknown"
            deviation = (cur_price - sma) / sma
            if deviation > cls.REGIME_TREND_PCT:
                return "trend_up"
            if deviation < -cls.REGIME_TREND_PCT:
                return "trend_down"
            return "range"
        except Exception:
            log.exception("regime classification failed for %s", coin)
            return "unknown"


def reset_cooldowns():
    """Test utility: clear anti-spam state."""
    global _last_fire
    _last_fire = {}
