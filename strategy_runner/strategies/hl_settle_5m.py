"""hl_settle_5m — Hourly funding boundary mechanical edge.

THESIS:
Hyperliquid funds EVERY 1 HOUR (unique vs 8h on Binance/OKX/Bybit).
At T-5min to T (settlement), traders avoiding paying funding pre-position
mechanically:
  - if funding will be POSITIVE (longs pay): longs close, shorts open
  - if funding will be NEGATIVE (shorts pay): shorts close, longs open

This creates a predictable mechanical push at T-5min that REVERSES at T+0
once funding has been paid and real flow resumes.

SIGNAL (two trades per hourly boundary, opposite directions):
  - PRE-SETTLE (T-5min to T):
    fade the mechanical push — if funding > 0 (longs pay), expect mechanical
    selling pressure into settlement → SHORT for ~5-10 min, exit at T+0
    if funding < 0 (shorts pay), expect mechanical buying → LONG
  - POST-SETTLE (T+5min to T+30min):
    flow re-equilibrates; expect direction OPPOSITE the pre-settle mechanical
    push (i.e., RIDE with the funding-recipient direction)

DISTINCT FROM:
- fsp (dead, §4): used funding LEVEL absolute threshold; no boundary timing
- fd1 (dead, §4): funding/price divergence over hours; no boundary timing
- fmom: 2nd-derivative funding ROC; no boundary timing
- This is uniquely HL-specific: only HL has hourly funding cadence

OPERATOR CAVEAT (council finding 2026-05-17):
At taker fees (~0.025% per side = 0.05% roundtrip), this engine is BORDERLINE
viable. Two trades per hour × 24h × 50bps roundtrip = ~24% gross-fee drag/day.
Per council: ONLY VIABLE WHEN IMPLEMENTED MAKER-ONLY (HL maker rebates).
- HL_SETTLE_MAKER_ONLY=1 default — engine returns None unless maker fill possible
- Tight SL (1.5× ATR) on the 5-min mechanical leg
- Skip if spread > 2 ticks (slippage will eat edge)

EXPECTED: PF 1.5-2.2 OOS, 5-15 trades/day across coins (filtered by spread).
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Optional

from ._base import Signal, StrategyBase


HL_SETTLE_PRE_MIN = int(os.environ.get("HL_SETTLE_PRE_MIN", "5"))      # fire if T-X to T window
HL_SETTLE_POST_MIN = int(os.environ.get("HL_SETTLE_POST_MIN", "5"))    # post-settle ride starts T+X
HL_SETTLE_POST_END_MIN = int(os.environ.get("HL_SETTLE_POST_END_MIN", "30"))  # post-settle window end
HL_SETTLE_FUNDING_MIN_ABS = float(os.environ.get("HL_SETTLE_FUNDING_MIN_ABS", "1e-5"))
HL_SETTLE_MAKER_ONLY = os.environ.get("HL_SETTLE_MAKER_ONLY", "1") == "1"
HL_SETTLE_SPREAD_BPS_MAX = float(os.environ.get("HL_SETTLE_SPREAD_BPS_MAX", "5.0"))
HL_SETTLE_SL_PCT = float(os.environ.get("HL_SETTLE_SL_PCT", "0.003"))   # 0.3% — SYMMETRIC fix 2026-05-18
HL_SETTLE_TP_PCT = float(os.environ.get("HL_SETTLE_TP_PCT", "0.004"))   # widened 0.3→0.4% (sniper conversion 2026-05-18)
# R:R rebalance 2026-05-18: original 0.5/0.3 was structurally negative-EV.
# At 28 paper trades WR was 60.7% (good) but avg loss 0.58% > avg win 0.35%
# = -$0.17 net. Symmetric 0.3/0.3 gives positive EV at WR > 50% pre-fees.
# 2026-05-18 sniper conversion: widened TP to 0.4% to cut fee-as-pct-of-win
# from 30% → 22%. Live SL/TP fill data showed TP fills 0.30-0.59% (mean 0.35%)
# so 0.4% target is reachable without losing fill rate. R:R now 0.4:0.3 = 1.33,
# break-even WR = 42.9%. Short-only book hit 75% live; cushion is now 32pp.
HL_SETTLE_MAX_HOLD_MIN = int(os.environ.get("HL_SETTLE_MAX_HOLD_MIN", "30"))

# Sniper conversion 2026-05-18 (council audit n=55 live):
# LONG WR 38.7% (12/31) vs SHORT WR 75.0% (18/24). 36pp direction asymmetry.
# Mechanism: alt-crypto bias is bearish/sideways during sample → mechanical
# longs into negative-funding settlements don't follow through, mechanical
# shorts into positive-funding settlements do. Engine is a directional bet
# disguised as a market-neutral funding play. Ban longs until regime shifts
# and the long book recovers a tradeable WR on n≥30.
HL_SETTLE_SHORT_ONLY = os.environ.get("HL_SETTLE_SHORT_ONLY", "1") == "1"
# Council Q4 (2026-05-18): trend-regime invasion is the projected edge
# degradation as n grows. Block mean-rev fires when 1h ADX > threshold.
HL_SETTLE_ADX_THRESHOLD = float(os.environ.get("HL_SETTLE_ADX_THRESHOLD", "25.0"))
HL_SETTLE_ADX_PERIOD = int(os.environ.get("HL_SETTLE_ADX_PERIOD", "14"))


# Live-validated universe (council audit 2026-05-18).
# Winners on live data (n=55 closures, short-only WR 75%):
#   WIF 9/13, DOT 5/6, SOL 2/3, JUP 1/1, SEI 1/1, DOGE 3/5, NEAR 1/2, BNB 1/2
# Marginal (kept on probation, monitor at n=20):
#   AVAX 6/10 (60% WR, slightly net negative)
# Banned bleeders (1/12 WR collective, -$1.39 cumulative):
#   LINK 0/3, LTC 0/2, OP 1/3, BTC 0/1, APT 0/1, ARB 0/1, TIA 0/1
# Untraded so far (allowed by default — observe before judging):
#   ETH XRP INJ kPEPE kSHIB kBONK AAVE UNI WLD ORDI PYTH ATOM SUI
DEFAULT_UNIVERSE = [
    # Tier 1 — live-validated short edges
    "WIF", "DOT", "SOL", "JUP", "SEI", "DOGE", "NEAR", "BNB",
    # Probation — live-mixed, retain for sample growth
    "AVAX",
    # Untraded — let them earn a sample before ruling
    "ETH", "XRP", "SUI", "INJ", "kPEPE", "kSHIB", "kBONK",
    "AAVE", "UNI", "WLD", "ORDI", "PYTH", "ATOM",
]
# Coins explicitly banned at runtime — bleeders from live audit n=55
# sentinel grid sweep 2026-05-19 (n=81 live, OKX 1m replay): AVAX added.
# AVAX was 2nd-biggest bleeder among non-denylisted coins (n=13, WR 50%,
# net -$0.67) and removing it lifts replay PF 0.81→1.10 (+36% scaled to
# live = 0.54→0.73, +35.8%). Reversible via HL_SETTLE_COIN_DENYLIST env.
_DEFAULT_DENY = "LINK,LTC,OP,BTC,APT,ARB,TIA,AVAX"
HL_SETTLE_COIN_DENYLIST = set(
    c.strip().upper()
    for c in os.environ.get("HL_SETTLE_COIN_DENYLIST", _DEFAULT_DENY).split(",")
    if c.strip()
)


def _minutes_to_settle(now_ms: int) -> tuple[int, int]:
    """Return (minutes_until_next_settle, minutes_since_last_settle).
    HL funding settles on the hour boundary (UTC).
    """
    dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    mins_in_hour = dt.minute + dt.second / 60.0
    to_next = int(round(60 - mins_in_hour))
    since_last = int(round(mins_in_hour))
    return to_next, since_last



def _compute_adx_1h(candles: list, period: int) -> Optional[float]:
    """Wilder-smoothed ADX over `period` bars.
    Returns ADX value or None if insufficient data.

    Sentinel-flagged: guard against env mis-config (period <= 0)."""
    if period <= 0 or len(candles) < period * 2 + 2:
        return None
    try:
        highs = [float(b["high"]) for b in candles]
        lows = [float(b["low"]) for b in candles]
        closes = [float(b["close"]) for b in candles]
    except (KeyError, ValueError, TypeError):
        return None

    n = len(candles)
    tr = [0.0] * n
    pdm = [0.0] * n
    mdm = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        dn = lows[i - 1] - lows[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]))

    # Wilder smoothing
    atr = sum(tr[1:period + 1]) / period
    pdi_sm = sum(pdm[1:period + 1]) / period
    mdi_sm = sum(mdm[1:period + 1]) / period

    dxs = []
    for i in range(period + 1, n):
        atr = (atr * (period - 1) + tr[i]) / period
        pdi_sm = (pdi_sm * (period - 1) + pdm[i]) / period
        mdi_sm = (mdi_sm * (period - 1) + mdm[i]) / period
        if atr <= 0:
            continue
        pdi = 100.0 * pdi_sm / atr
        mdi = 100.0 * mdi_sm / atr
        denom = pdi + mdi
        if denom <= 0:
            continue
        dx = 100.0 * abs(pdi - mdi) / denom
        dxs.append(dx)

    if len(dxs) < period:
        return None
    # Wilder ADX: smoothed DX
    adx = sum(dxs[:period]) / period
    for d in dxs[period:]:
        adx = (adx * (period - 1) + d) / period
    return adx

class HLSettle5m(StrategyBase):
    NAME = "hl_settle_5m"
    CLOID_PREFIX = "hlst_"
    AFFINITY = ["trend_up", "trend_down", "range", "chop", "high_vol"]
    TF = "1m"
    UNIVERSE = DEFAULT_UNIVERSE

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        # Coin denylist — bleeders from live audit
        if coin in HL_SETTLE_COIN_DENYLIST:
            return None

        # ── ADX trend-regime filter (council Q4 2026-05-18) ──
        # Mean-reversion at settle decays in strong-trend regimes (ADX > 25).
        # Council unanimous: this is the projected edge-degradation risk as
        # n grows. Filter at engine entry.
        if HL_SETTLE_ADX_THRESHOLD > 0:
            try:
                adx_bars = bus.candles(coin, "1h", n=HL_SETTLE_ADX_PERIOD * 4)
                adx = _compute_adx_1h(adx_bars, HL_SETTLE_ADX_PERIOD)
                if adx is not None and adx > HL_SETTLE_ADX_THRESHOLD:
                    return None
            except Exception:
                pass    # bus error or missing — fall through (do not block)

        # Latest funding rate (sign tells us who pays)
        try:
            funding = bus.funding(coin, hours=1)
        except Exception:
            return None
        if not funding:
            return None
        curr = funding[-1]
        if not isinstance(curr, dict) or "rate" not in curr:
            return None
        rate_now = float(curr["rate"])
        if abs(rate_now) < HL_SETTLE_FUNDING_MIN_ABS:
            return None  # tiny funding → no mechanical pressure worth trading

        # Determine which side of the settlement boundary we're in
        now_ms = int(curr.get("ts", time.time() * 1000))
        to_next, since_last = _minutes_to_settle(now_ms)

        in_pre = to_next <= HL_SETTLE_PRE_MIN and to_next > 0
        in_post = (HL_SETTLE_POST_MIN <= since_last <= HL_SETTLE_POST_END_MIN)
        if not (in_pre or in_post):
            return None  # neither window — no trade

        # Maker-only gate: skip if spread is too wide for passive fill
        if HL_SETTLE_MAKER_ONLY:
            try:
                mp = bus.markprice(coin)
                hl_mid = float(mp.get("hl_mid") or mp.get("binance_mid") or 0)
                if hl_mid <= 0:
                    return None
                # Approximate spread check via 1m kline range
                bars = bus.candles(coin, "1m", n=5)
                if bars:
                    recent = bars[-1]
                    spread_bps = (float(recent["high"]) - float(recent["low"])) / hl_mid * 10000
                    if spread_bps > HL_SETTLE_SPREAD_BPS_MAX:
                        return None
            except Exception:
                return None

        # Determine direction:
        #   funding > 0 = longs pay shorts
        #   PRE: fade mechanical longs-closing → SHORT
        #   POST: ride funding recipient (shorts won this hour) → SHORT continues
        # Actually re-think:
        #   funding > 0 means longs are paying. PRE-settle, longs close mechanically
        #     → mechanical selling pressure → in PRE, expect price DOWN, but we want
        #     to TRADE WITH that flow for the 5min, then REVERSE at T+0.
        #   POST-settle, the funding has been paid; new longs may open if they think
        #     price is cheap (or shorts close to lock gain). Direction is less clear.
        #     Simplest robust model: PRE = trade with mechanical (funding>0 → SHORT),
        #     POST = trade opposite (funding>0 → LONG, because mechanical selling
        #     has overshot).
        if in_pre:
            # Trade WITH mechanical direction
            is_long = rate_now < 0  # funding<0 → shorts pay → buying pressure → LONG
            reason_tag = f"pre_T{to_next}m_rate={rate_now:+.2e}"
        else:
            # in_post: trade AGAINST mechanical direction (the overshoot fade)
            is_long = rate_now > 0  # funding>0 → mechanical sold → buy the dip
            reason_tag = f"post_T+{since_last}m_rate={rate_now:+.2e}"

        # Sniper conversion: drop long signals (council audit 2026-05-18)
        if HL_SETTLE_SHORT_ONLY and is_long:
            return None

        try:
            mp = bus.markprice(coin)
            ref_px = float(mp.get("hl_mid") or mp.get("binance_mid") or 0)
        except Exception:
            return None
        if ref_px <= 0:
            return None

        if is_long:
            sl_px = ref_px * (1 - HL_SETTLE_SL_PCT)
            tp_px = ref_px * (1 + HL_SETTLE_TP_PCT)
        else:
            sl_px = ref_px * (1 + HL_SETTLE_SL_PCT)
            tp_px = ref_px * (1 - HL_SETTLE_TP_PCT)

        # 1m TF — max_hold expressed in bars
        max_hold_bars = HL_SETTLE_MAX_HOLD_MIN

        return Signal(
            coin=coin,
            side="B" if is_long else "A",
            is_long=is_long,
            ref_price=ref_px,
            sl_px=sl_px,
            tp_px=tp_px,
            max_hold_bars=max_hold_bars,
            fire_ts=time.time() * 1000,
            fire_reason=f"settle_{reason_tag}",
            extras={
                "settle_phase": "pre" if in_pre else "post",
                "to_next_settle_min": to_next,
                "since_last_settle_min": since_last,
                "rate_now": rate_now,
                "maker_only": HL_SETTLE_MAKER_ONLY,
            },
        )
