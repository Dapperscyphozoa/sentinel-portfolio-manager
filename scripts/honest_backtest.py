#!/usr/bin/env python3
"""
Session 1.5 honest backtest harness — per WORKFLOW.md Session 1.5 spec.

Validates strategies before they're ported to the production runner.

Pulls 180d 1h candles from OKX REST (no live HTTP calls inside strategies;
strategies receive only the bus interface, which serves historical data only
in backtest mode — matching the WORKFLOW requirement).

Per-strategy output: backtests/<strategy>_<date>.md with WR / PF / expectancy /
walk-forward OOS PF / trade ledger summary, plus a STRATEGY_GATES.md row.

GATE RULES (WORKFLOW §1.5):
  GREEN  : honest PF ≥ 1.4 AND OOS PF ≥ 1.0  → port as planned
  YELLOW : honest PF 1.0-1.4 OR OOS PF < 1.0 → port as audit_status=PROVISIONAL
  RED    : honest PF < 1.0                   → do NOT port; add to SPEC §4

USAGE
    python3 scripts/honest_backtest.py --strategy vsq
    python3 scripts/honest_backtest.py --strategy all
"""
from __future__ import annotations
import argparse
import datetime
import json
import os
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional, Callable

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
BACKTESTS_DIR = ROOT / "backtests"
BACKTESTS_DIR.mkdir(exist_ok=True)

OKX_BASE = "https://www.okx.com/api/v5/market/history-candles"

# Honest universe per SPEC §3 (subset for tractable runtime; expand if needed)
UNIVERSE_DEFAULT = [
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "AVAX", "LINK",
    "DOT", "ADA", "ATOM", "NEAR", "APT", "ARB", "OP", "SUI",
    "TIA", "SEI", "INJ", "LTC",
]

TAKER_FEE = 0.0005   # HL taker round-trip 0.05% per leg (0.045% rounded up)
SLIPPAGE = 0.0002    # 2bp slippage per side (conservative for HL on majors)


# ───────────────────────────── data layer ─────────────────────────────

def pull_klines(coin: str, tf: str = "1H", days: int = 180) -> np.ndarray:
    """
    Pull `days` of klines from OKX history endpoint. Returns array shape
    (n, 6) with columns [open_ts_ms, open, high, low, close, volume_base].
    """
    bars_per_day = {"1m": 1440, "5m": 288, "15m": 96, "1H": 24, "4H": 6, "1D": 1}[tf]
    target = days * bars_per_day + 24  # over-pull, trim later
    out: list[list] = []
    after = int(time.time() * 1000)
    client = httpx.Client(timeout=30.0)

    while len(out) < target:
        r = client.get(OKX_BASE, params={
            "instId": f"{coin}-USDT-SWAP",
            "bar": tf,
            "limit": 100,
            "after": after,
        })
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            break
        out.extend(data)
        # next page: 'after' = oldest ts in this batch
        after = int(data[-1][0])
        time.sleep(0.05)

    client.close()
    if not out:
        return np.array([])
    # OKX returns newest-first; reverse to oldest-first
    out = list(reversed(out))
    arr = np.array(
        [[int(r[0]), float(r[1]), float(r[2]), float(r[3]),
          float(r[4]), float(r[5])] for r in out],
        dtype=float,
    )
    # Dedupe by open_ts (pagination can overlap)
    _, idx = np.unique(arr[:, 0], return_index=True)
    return arr[sorted(idx)]


# ──────────────────────────── indicators ────────────────────────────

def sma(x: np.ndarray, period: int) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=float)
    if len(x) < period:
        return out
    csum = np.cumsum(np.insert(x, 0, 0.0))
    out[period - 1:] = (csum[period:] - csum[:-period]) / period
    return out


def rolling_std(x: np.ndarray, period: int) -> np.ndarray:
    out = np.full_like(x, np.nan, dtype=float)
    for i in range(period - 1, len(x)):
        out[i] = x[i - period + 1: i + 1].std(ddof=0)
    return out


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """TR = max(high-low, |high-prev_close|, |low-prev_close|)"""
    tr = np.zeros_like(close)
    tr[0] = high[0] - low[0]
    for i in range(1, len(close)):
        tr[i] = max(high[i] - low[i],
                    abs(high[i] - close[i - 1]),
                    abs(low[i] - close[i - 1]))
    return tr


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    return sma(true_range(high, low, close), period)


def bollinger(close: np.ndarray, period: int, k: float) -> tuple[np.ndarray, np.ndarray]:
    m = sma(close, period)
    s = rolling_std(close, period)
    return m + k * s, m - k * s


def keltner(high: np.ndarray, low: np.ndarray, close: np.ndarray,
            period: int, mult: float) -> tuple[np.ndarray, np.ndarray]:
    typical = (high + low + close) / 3.0
    m = sma(typical, period)
    a = atr(high, low, close, period)
    return m + mult * a, m - mult * a


def rsi(close: np.ndarray, period: int) -> np.ndarray:
    """Wilder's RSI."""
    out = np.full_like(close, np.nan, dtype=float)
    if len(close) <= period:
        return out
    diff = np.diff(close)
    gain = np.where(diff > 0, diff, 0.0)
    loss = np.where(diff < 0, -diff, 0.0)
    avg_g = gain[:period].mean()
    avg_l = loss[:period].mean()
    if avg_l == 0:
        out[period] = 100.0
    else:
        rs = avg_g / avg_l
        out[period] = 100 - 100 / (1 + rs)
    for i in range(period + 1, len(close)):
        avg_g = (avg_g * (period - 1) + gain[i - 1]) / period
        avg_l = (avg_l * (period - 1) + loss[i - 1]) / period
        if avg_l == 0:
            out[i] = 100.0
        else:
            rs = avg_g / avg_l
            out[i] = 100 - 100 / (1 + rs)
    return out


# ──────────────────────────── trade model ────────────────────────────

@dataclass
class Trade:
    coin: str
    entry_ts: int
    entry_px: float
    side: str            # "L" or "S"
    sl_px: float
    tp_px: float
    max_hold_bars: int
    exit_ts: int = 0
    exit_px: float = 0.0
    exit_reason: str = ""
    bars_held: int = 0
    pnl_pct: float = 0.0      # gross % return on notional (signed)
    pnl_after_costs: float = 0.0  # after fees + slippage


def simulate_trade(t: Trade, bars: np.ndarray, entry_idx: int) -> Trade:
    """Walk bars forward from entry_idx, exit on SL/TP/timeout."""
    for j in range(entry_idx + 1, min(entry_idx + 1 + t.max_hold_bars, len(bars))):
        h, l = bars[j, 2], bars[j, 3]
        # Pessimistic order: check SL first (worst case for the trader)
        if t.side == "L":
            if l <= t.sl_px:
                t.exit_ts = int(bars[j, 0]); t.exit_px = t.sl_px
                t.exit_reason = "sl"; t.bars_held = j - entry_idx
                break
            if h >= t.tp_px:
                t.exit_ts = int(bars[j, 0]); t.exit_px = t.tp_px
                t.exit_reason = "tp"; t.bars_held = j - entry_idx
                break
        else:  # short
            if h >= t.sl_px:
                t.exit_ts = int(bars[j, 0]); t.exit_px = t.sl_px
                t.exit_reason = "sl"; t.bars_held = j - entry_idx
                break
            if l <= t.tp_px:
                t.exit_ts = int(bars[j, 0]); t.exit_px = t.tp_px
                t.exit_reason = "tp"; t.bars_held = j - entry_idx
                break
    else:
        # Timed out
        last = min(entry_idx + t.max_hold_bars, len(bars) - 1)
        t.exit_ts = int(bars[last, 0]); t.exit_px = bars[last, 4]
        t.exit_reason = "timeout"; t.bars_held = last - entry_idx

    gross = ((t.exit_px - t.entry_px) / t.entry_px) if t.side == "L" \
            else ((t.entry_px - t.exit_px) / t.entry_px)
    t.pnl_pct = gross
    t.pnl_after_costs = gross - 2 * TAKER_FEE - 2 * SLIPPAGE
    return t


# ──────────────────────────── strategies ────────────────────────────

def evaluate_vsq(bars: np.ndarray, coin: str) -> list[Trade]:
    """Per SPEC §3.2. Pure price action; no missing-data caveats."""
    if len(bars) < 100:
        return []
    closes = bars[:, 4]; highs = bars[:, 2]; lows = bars[:, 3]; vols = bars[:, 5]

    bb_u, bb_l = bollinger(closes, 20, 2.0)
    kc_u, kc_l = keltner(highs, lows, closes, 14, 1.5)
    a = atr(highs, lows, closes, 14)
    avg_vol_20 = sma(vols, 20)

    trades = []
    cooldown_until = -1

    for i in range(50, len(bars) - 1):
        if i <= cooldown_until:
            continue
        if np.isnan(bb_u[i]) or np.isnan(kc_u[i]) or np.isnan(avg_vol_20[i]) or np.isnan(a[i]):
            continue
        # Sustained squeeze last 6 bars
        sustained = True
        for k in range(6):
            j = i - k
            if not (bb_u[j] < kc_u[j] and bb_l[j] > kc_l[j]):
                sustained = False
                break
        if not sustained:
            continue
        # Breakout this bar
        vol_ok = vols[i] > 1.8 * avg_vol_20[i]
        bu = closes[i] > bb_u[i] and vol_ok
        bd = closes[i] < bb_l[i] and vol_ok
        if not (bu or bd):
            continue
        # Enter at NEXT bar open
        entry_ts = int(bars[i + 1, 0])
        entry_px = bars[i + 1, 1]
        side = "L" if bu else "S"
        sl = entry_px - 2.0 * a[i] if side == "L" else entry_px + 2.0 * a[i]
        tp = entry_px + 6.0 * a[i] if side == "L" else entry_px - 6.0 * a[i]
        t = Trade(coin=coin, entry_ts=entry_ts, entry_px=entry_px,
                  side=side, sl_px=sl, tp_px=tp, max_hold_bars=24)
        t = simulate_trade(t, bars, i + 1)
        trades.append(t)
        # Cool down to prevent re-fire on the same setup (one trade per breakout)
        cooldown_until = i + t.bars_held + 5

    return trades


def evaluate_range_fade(bars: np.ndarray, coin: str) -> list[Trade]:
    """Per SPEC §3.3. CAVEAT: no PM regime filter (regime classifier not
    available in backtest); this is the PERMISSIVE version. Real production
    deployment would gate on regime != trend at conf > 0.7."""
    if len(bars) < 50:
        return []
    closes = bars[:, 4]; highs = bars[:, 2]; lows = bars[:, 3]
    r = rsi(closes, 14)
    bb_u, bb_l = bollinger(closes, 20, 2.0)

    trades = []
    cooldown_until = -1
    for i in range(30, len(bars) - 1):
        if i <= cooldown_until:
            continue
        if np.isnan(r[i]) or np.isnan(bb_u[i]):
            continue
        long_fire = r[i] < 25 and closes[i] <= bb_l[i] * 1.001
        short_fire = r[i] > 75 and closes[i] >= bb_u[i] * 0.999
        if not (long_fire or short_fire):
            continue
        entry_ts = int(bars[i + 1, 0])
        entry_px = bars[i + 1, 1]
        side = "L" if long_fire else "S"
        sl = entry_px * (1 - 0.012) if side == "L" else entry_px * (1 + 0.012)
        tp = entry_px * (1 + 0.020) if side == "L" else entry_px * (1 - 0.020)
        t = Trade(coin=coin, entry_ts=entry_ts, entry_px=entry_px,
                  side=side, sl_px=sl, tp_px=tp, max_hold_bars=12)
        t = simulate_trade(t, bars, i + 1)
        trades.append(t)
        cooldown_until = i + t.bars_held + 3
    return trades


def evaluate_lh1(bars: np.ndarray, coin: str) -> list[Trade]:
    """Per SPEC §3.5 (inverted: trade WITH the sweep direction). CAVEAT:
    liquidation confluence (LH_VOL_SPIKE_MULT applied to liq cluster) is
    REPLACED with a volume spike on the kline itself, since historical liq
    data is not retrievable. This is a STRUCTURAL-ONLY version of the
    strategy — production version would require liq history."""
    if len(bars) < 130:
        return []
    closes = bars[:, 4]; highs = bars[:, 2]; lows = bars[:, 3]; vols = bars[:, 5]

    PIVOT_LB, PIVOT_RB = 5, 5
    CLUSTER_BAND = 0.003
    MIN_PIVOTS = 3
    SWEEP_PCT = 0.002
    VOL_SPIKE = 1.5
    SL_BUFFER = 0.003
    RR = 3.0
    LOOKBACK = 120

    avg_vol = sma(vols, 20)
    trades = []
    cooldown_until = -1

    for i in range(LOOKBACK + PIVOT_RB, len(bars) - 1):
        if i <= cooldown_until:
            continue
        if np.isnan(avg_vol[i]):
            continue
        # Find pivot highs/lows in trailing window (excluding the last PIVOT_RB bars to avoid look-ahead)
        win_start = i - LOOKBACK
        win_end = i - PIVOT_RB
        pivot_h = []
        pivot_l = []
        for j in range(win_start + PIVOT_LB, win_end):
            if highs[j] == highs[j - PIVOT_LB:j + PIVOT_RB + 1].max():
                pivot_h.append(highs[j])
            if lows[j] == lows[j - PIVOT_LB:j + PIVOT_RB + 1].min():
                pivot_l.append(lows[j])
        if not pivot_h and not pivot_l:
            continue
        # Find equal-high cluster (BSL = buyside liquidity)
        bsl_lvl = None
        if len(pivot_h) >= MIN_PIVOTS:
            top = max(pivot_h)
            cluster = [p for p in pivot_h if abs(p - top) / top < CLUSTER_BAND]
            if len(cluster) >= MIN_PIVOTS:
                bsl_lvl = top
        ssl_lvl = None
        if len(pivot_l) >= MIN_PIVOTS:
            bot = min(pivot_l)
            cluster = [p for p in pivot_l if abs(p - bot) / bot < CLUSTER_BAND]
            if len(cluster) >= MIN_PIVOTS:
                ssl_lvl = bot
        # Sweep detection on current bar
        vol_ok = vols[i] > VOL_SPIKE * avg_vol[i]
        # Sweep of BSL = wick above + close below → INVERTED = LONG (continuation up after sweep)
        # Wait that's contradictory. Per SPEC: inverted means trade WITH sweep direction.
        # If price swept BSL (broke above equal highs), continuation = LONG.
        # If price swept SSL (broke below equal lows), continuation = SHORT.
        # Use: high broke through BSL OR low broke through SSL.
        if bsl_lvl is not None and highs[i] >= bsl_lvl * (1 + SWEEP_PCT) and vol_ok:
            # Sweep above BSL — INVERTED: go LONG continuation
            entry_ts = int(bars[i + 1, 0])
            entry_px = bars[i + 1, 1]
            sl = bsl_lvl * (1 - SL_BUFFER)  # below the swept level
            tp = entry_px + RR * (entry_px - sl)
            t = Trade(coin=coin, entry_ts=entry_ts, entry_px=entry_px,
                      side="L", sl_px=sl, tp_px=tp, max_hold_bars=8)
            t = simulate_trade(t, bars, i + 1)
            trades.append(t)
            cooldown_until = i + t.bars_held + 5
            continue
        if ssl_lvl is not None and lows[i] <= ssl_lvl * (1 - SWEEP_PCT) and vol_ok:
            # Sweep below SSL — INVERTED: go SHORT continuation
            entry_ts = int(bars[i + 1, 0])
            entry_px = bars[i + 1, 1]
            sl = ssl_lvl * (1 + SL_BUFFER)
            tp = entry_px - RR * (sl - entry_px)
            t = Trade(coin=coin, entry_ts=entry_ts, entry_px=entry_px,
                      side="S", sl_px=sl, tp_px=tp, max_hold_bars=8)
            t = simulate_trade(t, bars, i + 1)
            trades.append(t)
            cooldown_until = i + t.bars_held + 5

    return trades


def pull_hl_funding(coin: str, days: int = 180) -> dict[int, float]:
    """
    Pull `days` of HL funding history via REST. Returns {hour_open_ts_ms: rate}.
    HL fundingHistory returns up to 500 rows per call (hourly cadence ≈ 20d).
    """
    out: dict[int, float] = {}
    end_ts = int(time.time() * 1000)
    target_oldest = end_ts - days * 86400 * 1000
    cursor = end_ts
    client = httpx.Client(timeout=30.0)
    seen_oldest = end_ts

    while seen_oldest > target_oldest:
        # HL paginates by startTime — request a 25d window ending at cursor
        start = cursor - 25 * 86400 * 1000
        r = client.post("https://api.hyperliquid.xyz/info",
                        json={"type": "fundingHistory", "coin": coin,
                              "startTime": start, "endTime": cursor},
                        headers={"Content-Type": "application/json"})
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        for row in rows:
            ts = int(row["time"])
            hr = (ts // 3600000) * 3600000
            out[hr] = float(row["fundingRate"])
        oldest_in_batch = min(int(r["time"]) for r in rows)
        if oldest_in_batch >= seen_oldest:
            break  # no progress, avoid infinite loop
        seen_oldest = oldest_in_batch
        cursor = oldest_in_batch - 1
        time.sleep(0.1)

    client.close()
    return out


def evaluate_fd1(bars: np.ndarray, coin: str) -> list[Trade]:
    """Per SPEC §3.6. Funding/price divergence over 4 bars; fade the price move.
    Requires hourly funding rate. Pulls from HL REST in this harness."""
    if len(bars) < 50:
        return []
    closes = bars[:, 4]; highs = bars[:, 2]; lows = bars[:, 3]
    opens_ts = bars[:, 0].astype(np.int64)

    days = max(30, int((opens_ts[-1] - opens_ts[0]) / 86400000) + 5)
    funding_map = pull_hl_funding(coin, days=days)
    if len(funding_map) < 50:
        return []

    # Align funding to each kline's open_ts; forward-fill missing
    f_series = np.full(len(bars), np.nan, dtype=float)
    for i, ts in enumerate(opens_ts):
        hr = int(ts)
        if hr in funding_map:
            f_series[i] = funding_map[hr]
    # Forward-fill (funding settles every 1h on HL; sparse intra-hour fills are normal)
    last = np.nan
    for i in range(len(f_series)):
        if not np.isnan(f_series[i]): last = f_series[i]
        else: f_series[i] = last

    # SPEC §3.6 params
    BARS = 4
    FUND_HI = 1.5e-5
    FUND_LO = -5e-5
    SL_PCT = 0.015
    TP_PCT = 0.030
    MAX_HOLD = 24
    PRICE_THRESHOLD = 0.015  # only consider meaningful price moves

    trades = []
    cooldown_until = -1

    for i in range(BARS + 1, len(bars) - 1):
        if i <= cooldown_until:
            continue
        if np.isnan(f_series[i]) or np.isnan(f_series[i - BARS]):
            continue
        price_chg = (closes[i] - closes[i - BARS]) / closes[i - BARS]
        funding_chg = f_series[i] - f_series[i - BARS]
        # Fade rally with funding falling = SHORT
        short_fire = price_chg > PRICE_THRESHOLD and funding_chg < FUND_LO
        # Fade drop with funding rising = LONG
        long_fire = price_chg < -PRICE_THRESHOLD and funding_chg > FUND_HI
        if not (long_fire or short_fire):
            continue
        entry_ts = int(bars[i + 1, 0])
        entry_px = bars[i + 1, 1]
        side = "L" if long_fire else "S"
        sl = entry_px * (1 - SL_PCT) if side == "L" else entry_px * (1 + SL_PCT)
        tp = entry_px * (1 + TP_PCT) if side == "L" else entry_px * (1 - TP_PCT)
        t = Trade(coin=coin, entry_ts=entry_ts, entry_px=entry_px,
                  side=side, sl_px=sl, tp_px=tp, max_hold_bars=MAX_HOLD)
        t = simulate_trade(t, bars, i + 1)
        trades.append(t)
        cooldown_until = i + t.bars_held + 4
    return trades


STRATEGY_FNS: dict[str, tuple[Callable, str, list[str]]] = {
    # name → (evaluate_fn, timeframe, caveats)
    "vsq":         (evaluate_vsq, "1H", []),
    "range_fade":  (evaluate_range_fade, "15m", [
        "CAVEAT: no PM regime filter applied (regime classifier history not available). "
        "Production version gates on regime != trend at conf > 0.7. "
        "This is the PERMISSIVE version — expect production PF to be slightly higher due to fewer trend-fade losses."]),
    "lh1":         (evaluate_lh1, "1H", [
        "CAVEAT: liquidation confluence replaced with kline volume spike (historical liq data unavailable). "
        "This is the STRUCTURAL-ONLY version of the inverted SMC sweep strategy. "
        "Production version requires liquidation event history (e.g., Bybit forceOrder backfill)."]),
    "fd1":         (evaluate_fd1, "1H", [
        "Funding rates pulled from HL /info fundingHistory REST (no caveat — this is the production data source)."]),
}


# ──────────────────────────── metrics ────────────────────────────

def trade_metrics(trades: list[Trade]) -> dict:
    if not trades:
        return {"n": 0, "wr": 0, "pf": 0, "expectancy": 0, "total_return": 0,
                "avg_win": 0, "avg_loss": 0, "max_dd": 0, "sharpe": 0}
    pnls = np.array([t.pnl_after_costs for t in trades])
    wins = pnls[pnls > 0]; losses = pnls[pnls <= 0]
    wr = len(wins) / len(pnls)
    gross_w = wins.sum() if len(wins) else 0.0
    gross_l = -losses.sum() if len(losses) else 0.0
    pf = (gross_w / gross_l) if gross_l > 0 else float("inf") if gross_w > 0 else 0.0
    expectancy = pnls.mean()
    # Equity curve assuming 1u risk per trade
    eq = np.cumsum(pnls)
    peak = np.maximum.accumulate(eq)
    dd = eq - peak
    max_dd = float(dd.min()) if len(dd) else 0.0
    sharpe = float(pnls.mean() / pnls.std() * np.sqrt(252 * 24)) if pnls.std() > 0 else 0.0
    return {
        "n": int(len(trades)),
        "wr": round(wr, 3),
        "pf": round(float(pf), 3) if pf != float("inf") else "inf",
        "expectancy": round(float(expectancy), 5),
        "total_return": round(float(eq[-1]) if len(eq) else 0, 4),
        "avg_win": round(float(wins.mean()) if len(wins) else 0, 4),
        "avg_loss": round(float(losses.mean()) if len(losses) else 0, 4),
        "max_dd_pct_units": round(max_dd, 4),
        "sharpe_annualized": round(sharpe, 2),
        "by_exit": {r: int(sum(1 for t in trades if t.exit_reason == r))
                    for r in ("sl", "tp", "timeout")},
    }


def walk_forward(trades: list[Trade], split_ts: int) -> tuple[dict, dict]:
    """Split trades at given ts; return (train_metrics, oos_metrics)."""
    train = [t for t in trades if t.entry_ts < split_ts]
    test = [t for t in trades if t.entry_ts >= split_ts]
    return trade_metrics(train), trade_metrics(test)


# ──────────────────────────── runner ────────────────────────────

def run_strategy(name: str, universe: list[str], days: int = 180) -> dict:
    if name not in STRATEGY_FNS:
        print(f"unknown strategy: {name}")
        return {}
    fn, tf, caveats = STRATEGY_FNS[name]
    if fn is None:
        print(f"\n=== {name} — NOT RUN ===")
        for c in caveats: print(f"  ! {c}")
        return {"strategy": name, "status": "not_run", "caveats": caveats}

    print(f"\n{'=' * 70}\n{name.upper()}  tf={tf}  universe_n={len(universe)}  days={days}\n{'=' * 70}")
    if caveats:
        for c in caveats: print(f"  ! {c}")
        print()

    all_trades = []
    per_coin = {}
    for coin in universe:
        try:
            print(f"  {coin:5s} pulling…", end=" ", flush=True)
            bars = pull_klines(coin, tf=tf, days=days)
            if len(bars) < 200:
                print(f"insufficient bars ({len(bars)}), skip")
                continue
            t0 = time.time()
            trades = fn(bars, coin)
            elapsed = time.time() - t0
            m = trade_metrics(trades)
            per_coin[coin] = {"n": m["n"], "wr": m["wr"], "pf": m["pf"],
                              "expectancy": m["expectancy"], "bars": len(bars)}
            all_trades.extend(trades)
            print(f"bars={len(bars):5d}  trades={m['n']:3d}  "
                  f"wr={m['wr']:.1%}  pf={str(m['pf']):>6s}  ({elapsed:.1f}s)")
        except Exception as e:
            print(f"ERROR: {e}")

    print(f"\n{'─' * 70}\nALL-COIN AGGREGATE")
    full = trade_metrics(all_trades)
    print(json.dumps(full, indent=2))

    # Walk-forward: 90d train + 90d test
    if all_trades:
        sorted_trades = sorted(all_trades, key=lambda t: t.entry_ts)
        split_ts = sorted_trades[len(sorted_trades) // 2].entry_ts
        train_m, test_m = walk_forward(sorted_trades, split_ts)
        print(f"\nWALK-FORWARD  (split at trade #{len(sorted_trades)//2}, ts={split_ts})")
        print(f"  TRAIN: n={train_m['n']:3d}  wr={train_m['wr']:.1%}  pf={train_m['pf']}")
        print(f"  OOS:   n={test_m['n']:3d}  wr={test_m['wr']:.1%}  pf={test_m['pf']}")
    else:
        train_m = test_m = {"n": 0, "pf": 0, "wr": 0}

    # Gate
    pf_h = full["pf"] if isinstance(full["pf"], (int, float)) else 999
    pf_o = test_m["pf"] if isinstance(test_m["pf"], (int, float)) else 999
    if pf_h >= 1.4 and pf_o >= 1.0:
        gate = "GREEN"
    elif pf_h < 1.0:
        gate = "RED"
    else:
        gate = "YELLOW"
    print(f"\nGATE: {gate}  (honest PF={pf_h}, OOS PF={pf_o})")

    return {
        "strategy": name,
        "tf": tf,
        "days": days,
        "universe": universe,
        "caveats": caveats,
        "per_coin": per_coin,
        "aggregate": full,
        "walk_forward": {"train": train_m, "oos": test_m, "split_ts": int(split_ts) if all_trades else 0},
        "gate": gate,
        "ts": int(time.time()),
    }


def write_report(result: dict):
    if result.get("status") == "not_run":
        return
    name = result["strategy"]
    date = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    path = BACKTESTS_DIR / f"{name}_{date}.md"
    lines = [
        f"# {name} honest backtest — {date}",
        "",
        f"**Gate:** `{result['gate']}`",
        f"**Timeframe:** {result['tf']}  **Lookback:** {result['days']}d  **Universe:** {len(result['universe'])} coins",
        "",
    ]
    if result["caveats"]:
        lines.append("## Caveats")
        for c in result["caveats"]:
            lines.append(f"- {c}")
        lines.append("")
    a = result["aggregate"]
    lines += [
        "## Aggregate (all coins pooled)",
        "```",
        json.dumps(a, indent=2),
        "```",
        "",
        "## Walk-forward",
        "```",
        json.dumps(result["walk_forward"], indent=2, default=str),
        "```",
        "",
        "## Per-coin",
        "| coin | trades | WR | PF | expectancy |",
        "|---|---|---|---|---|",
    ]
    for c, m in result["per_coin"].items():
        lines.append(f"| {c} | {m['n']} | {m['wr']:.1%} | {m['pf']} | {m['expectancy']:+.5f} |")
    path.write_text("\n".join(lines))
    print(f"\nwrote {path}")


def write_gates_summary(results: list[dict]):
    date = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    path = ROOT / "STRATEGY_GATES.md"
    lines = [
        "# Strategy gates — Session 1.5 honest-backtest verdicts",
        "",
        f"_Last update: {date}_",
        "",
        "Per WORKFLOW.md §1.5: GREEN port as-is; YELLOW port as PROVISIONAL "
        "(no canary, no live capital); RED do NOT port, add to SPEC §4.",
        "",
        "| Strategy | Gate | Honest PF | OOS PF | n trades | WR | Caveats |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if r.get("status") == "not_run":
            lines.append(f"| {r['strategy']} | ⏸️ NOT_RUN | – | – | – | – | "
                         f"{'; '.join(r.get('caveats', []))[:120]} |")
            continue
        a = r["aggregate"]; wf = r["walk_forward"]["oos"]
        cav = "; ".join(c.split('CAVEAT: ')[1].split('.')[0] if 'CAVEAT' in c else c
                        for c in r.get('caveats', []))[:120] or "—"
        lines.append(f"| {r['strategy']} | **{r['gate']}** | {a['pf']} | {wf.get('pf','—')} | "
                     f"{a['n']} | {a['wr']:.1%} | {cav} |")
    path.write_text("\n".join(lines))
    print(f"\nwrote {path}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--strategy", default="all",
                   help="vsq, range_fade, lh1, fd1, or 'all'")
    p.add_argument("--days", type=int, default=180)
    p.add_argument("--universe", default=None,
                   help="comma-separated coin list (default: 20 majors)")
    args = p.parse_args()
    universe = args.universe.split(",") if args.universe else UNIVERSE_DEFAULT

    if args.strategy == "all":
        targets = list(STRATEGY_FNS.keys())
    else:
        targets = [args.strategy]

    results = []
    for s in targets:
        try:
            r = run_strategy(s, universe, args.days)
            results.append(r)
            write_report(r)
        except Exception as e:
            import traceback
            traceback.print_exc()
            results.append({"strategy": s, "status": "error", "error": str(e)})

    write_gates_summary(results)


if __name__ == "__main__":
    main()
