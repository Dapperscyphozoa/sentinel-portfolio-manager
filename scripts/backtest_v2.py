#!/usr/bin/env python3
"""
Backtest v2 — extends backtest_oos.py with:
  • All 16 candles-only live engines (1d, 4h, 1h timeframes)
  • Walk-forward split (2 windows: first-half vs second-half)
  • Per-regime stratification at signal time
  • Honest reporting of which engines couldn't be tested (data deps)
"""
from __future__ import annotations
import json
import pickle
import sys
import time
from pathlib import Path

import httpx

REPO = "/home/claude/sentinel-portfolio-manager"
sys.path.insert(0, REPO)

# Production imports (unmodified)
from strategy_runner.strategies.oos_engines import (  # noqa: E402
    E01_zfade_3s_TU_1d, E07_zfade_2s_TU_1d, E08_dip3d_10_TD_1d,
    E09_pump3d_10_TD_1d, E16_bb_fade_HV_1d, E17_bb_fade_BT_1d,
    E01_zfade_3s_TU_4h, E07_zfade_2s_TU_4h, E08_dip3d_7_TD_4h,
    E16_bb_fade_HV_4h, E17_bb_fade_BT_4h,
    DEFAULT_UNIVERSE, _regime,
)
from strategy_runner.strategies.stop_hunt import StopHunt  # noqa: E402
from strategy_runner.strategies.vpoc_retest import VPOCRetest  # noqa: E402
from strategy_runner.strategies.oi_concentration import OIConcentration  # noqa: E402
from strategy_runner.strategies.ict_confluence import ICT_Confluence_4h, ICT_Confluence_1d  # noqa: E402
from strategy_runner.strategies.donchian import Donchian  # noqa: E402

BASE = "https://core-o21t.onrender.com/signal_bus"
N_BARS = 500
CACHE_DIR = Path("/home/claude/backtest_data")
CACHE_DIR.mkdir(exist_ok=True)

EQUITY = 480.0
NOTIONAL_FRAC = 0.25
FEE_PCT_RT = 0.0009

ENGINES = [
    # (class, hold_bars_attr_or_int)
    (E01_zfade_3s_TU_1d, "_HOLD_BARS"),
    (E07_zfade_2s_TU_1d, "_HOLD_BARS"),
    (E08_dip3d_10_TD_1d, "_HOLD_BARS"),
    (E09_pump3d_10_TD_1d, "_HOLD_BARS"),
    (E16_bb_fade_HV_1d, "_HOLD_BARS"),
    (E17_bb_fade_BT_1d, "_HOLD_BARS"),
    (E01_zfade_3s_TU_4h, "_HOLD_BARS"),
    (E07_zfade_2s_TU_4h, "_HOLD_BARS"),
    (E08_dip3d_7_TD_4h, "_HOLD_BARS"),
    (E16_bb_fade_HV_4h, "_HOLD_BARS"),
    (E17_bb_fade_BT_4h, "_HOLD_BARS"),
    (StopHunt, None),
    (VPOCRetest, None),
    (OIConcentration, None),
    (ICT_Confluence_4h, None),
    (ICT_Confluence_1d, None),
    (Donchian, None),
]

SKIPPED_ENGINES_WITH_REASON = [
    ("cascade_sniper_hl", "needs liq stream + markprice history (liq feed dead)"),
    ("hlp_fade", "needs HLP positioning history (only current snapshots cached)"),
    ("hl_settle_5m", "needs funding + markprice history (5m TF + multi-deps)"),
    ("liq_cascade", "needs liq stream (feed dead — DATA_VENUE=okx, subscribe failed)"),
    ("cex_dex_arb", "needs multi-venue funding + markprice history"),
    ("fmom", "needs 48h funding history (only ~34h cached, REST backfill not impl)"),
]


# ─────────────────────── Data layer ────────────────────────────────

def pull_tf(tf: str, universe):
    """Pull `tf` candles for all coins; cache per TF."""
    cache = CACHE_DIR / f"bars_{tf}.pkl"
    if cache.exists():
        with open(cache, "rb") as f:
            return pickle.load(f)
    print(f"  pulling {len(universe)} coins × {tf} from {BASE} ...")
    data = {}
    with httpx.Client(timeout=20.0) as c:
        for coin in universe:
            try:
                r = c.get(f"{BASE}/candles/{coin}/{tf}", params={"n": N_BARS})
                bars = r.json() if r.status_code == 200 else []
                if bars and len(bars) >= 100:
                    data[coin] = bars
            except Exception:
                pass
    with open(cache, "wb") as f:
        pickle.dump(data, f)
    return data


class HistoricalBus:
    """Per-TF mock bus that serves bars[0..current_idx] only."""
    def __init__(self, bars_by_tf: dict):
        self._all = bars_by_tf       # {tf: {coin: bars}}
        self._t = {}                 # {(tf, coin): idx}
    
    def set_time(self, tf: str, coin: str, idx: int):
        self._t[(tf, coin)] = idx
    
    def candles(self, coin, tf, n=200):
        coins = self._all.get(tf, {})
        if coin not in coins:
            return []
        t = self._t.get((tf, coin), 0)
        bars = coins[coin]
        end = min(t + 1, len(bars))
        return bars[max(0, end - n):end]


# ─────────────────────── Trade simulator ────────────────────────────

def simulate_trade(bars, entry_idx, entry_px, sl_px, tp_px, is_long, max_hold):
    n = len(bars)
    last_close = entry_px
    for k in range(1, max_hold + 1):
        j = entry_idx + k
        if j >= n:
            return last_close, j - 1, "no_data", k - 1
        b = bars[j]
        hi, lo, cl = b["high"], b["low"], b["close"]
        if is_long:
            if lo <= sl_px: return sl_px, j, "sl", k
            if hi >= tp_px: return tp_px, j, "tp", k
        else:
            if hi >= sl_px: return sl_px, j, "sl", k
            if lo <= tp_px: return tp_px, j, "tp", k
        last_close = cl
    return last_close, entry_idx + max_hold, "time", max_hold


def regime_at(bars, idx):
    """Classify regime at index idx using the production _regime helper."""
    if idx < 60:
        return "UNKNOWN"
    closes = [b["close"] for b in bars[:idx+1]]
    highs = [b["high"] for b in bars[:idx+1]]
    lows = [b["low"] for b in bars[:idx+1]]
    try:
        return _regime(closes, highs, lows, idx)
    except Exception:
        return "UNKNOWN"


def backtest_engine(eng_cls, hold_attr, bars_by_tf: dict, bus: HistoricalBus, warmup=80):
    tf = eng_cls.TF
    bars_for_tf = bars_by_tf.get(tf, {})
    if not bars_for_tf:
        return {"engine": eng_cls.NAME, "tf": tf, "trades": [],
                "skipped": f"no {tf} data cached"}
    # Resolve hold bars: class attribute or signal field
    hold_fallback = None
    if hold_attr and hasattr(eng_cls, hold_attr):
        hold_fallback = getattr(eng_cls, hold_attr)
    
    universe = getattr(eng_cls, "UNIVERSE", DEFAULT_UNIVERSE)
    universe = [c for c in universe if c in bars_for_tf]
    
    trades = []
    for coin in universe:
        bars = bars_for_tf[coin]
        n = len(bars)
        if n < warmup + 5:
            continue
        open_until = -1
        for t in range(warmup, n - 2):
            if t <= open_until:
                continue
            bus.set_time(tf, coin, t)
            try:
                sig = eng_cls.evaluate(coin, bus)
            except Exception:
                continue
            if sig is None:
                continue
            hold = getattr(sig, "max_hold_bars", None) or hold_fallback or 24
            hold = min(hold, n - t - 1)
            if hold < 1:
                continue
            entry = sig.ref_price
            exit_px, exit_idx, reason, held = simulate_trade(
                bars, t, entry, sig.sl_px, sig.tp_px, sig.is_long, hold)
            risk = abs(entry - sig.sl_px)
            if risk == 0:
                continue
            r_gross = ((exit_px - entry) if sig.is_long else (entry - exit_px)) / risk
            pct_gross = ((exit_px - entry) if sig.is_long else (entry - exit_px)) / entry
            risk_pct = risk / entry
            fee_R = FEE_PCT_RT / risk_pct if risk_pct > 0 else 0
            r_net = r_gross - fee_R
            pct_net = pct_gross - FEE_PCT_RT
            reg = regime_at(bars, t)
            trades.append({
                "coin": coin, "entry_idx": t, "exit_idx": exit_idx, "held": held,
                "R_net": r_net, "pct_net": pct_net, "reason": reason,
                "regime": reg, "is_long": sig.is_long,
            })
            open_until = exit_idx
    return {"engine": eng_cls.NAME, "tf": tf, "trades": trades}


# ─────────────────────── Aggregations ──────────────────────────────

def summarize(trades: list, n_bars_sample: int = 200):
    n = len(trades)
    if n == 0:
        return None
    rs = [t["R_net"] for t in trades]
    pcts = [t["pct_net"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    wr = len(wins) / n
    avg_w = sum(wins)/len(wins) if wins else 0
    avg_l = sum(losses)/len(losses) if losses else 0
    exp_r = sum(rs)/n
    gp = sum(wins); gl = -sum(losses)
    pf = (gp/gl) if gl > 0 else (float("inf") if gp > 0 else 0)
    cum = peak = max_dd = 0
    for r in rs:
        cum += r
        if cum > peak: peak = cum
        dd = peak - cum
        if dd > max_dd: max_dd = dd
    notional = EQUITY * NOTIONAL_FRAC
    net_usd = sum(p * notional for p in pcts)
    return {"n": n, "wr": wr, "exp_R": exp_r, "PF": pf,
            "avg_W": avg_w, "avg_L": avg_l, "maxDD_R": max_dd, "net_$": net_usd}


def split_walkforward(trades):
    """Split trades by entry_idx median into first-half and second-half."""
    if len(trades) < 4:
        return None, None
    idxs = sorted(t["entry_idx"] for t in trades)
    mid = idxs[len(idxs)//2]
    first = [t for t in trades if t["entry_idx"] <= mid]
    second = [t for t in trades if t["entry_idx"] > mid]
    return summarize(first), summarize(second)


def stratify_by_regime(trades):
    by = {}
    for t in trades:
        by.setdefault(t["regime"], []).append(t)
    return {reg: summarize(ts) for reg, ts in by.items()}


# ─────────────────────── Output ────────────────────────────────────

def fmt(s):
    if s is None:
        return f"{'—':>7s}  {'—':>5s}  {'—':>6s}  {'—':>5s}"
    return f"{s['n']:>4d}  {s['wr']:>4.0%}  {s['PF']:>5.2f}  {s['exp_R']:>+5.2f}  {s['net_$']:>+7.2f}"


def main():
    print("=" * 100)
    print(f"OOS engine backtest v2 — {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}")
    print(f"Universe: up to {len(DEFAULT_UNIVERSE)} coins / TF")
    print(f"Trade model: open close[t], walk forward, SL/TP/time exit")
    print(f"             fees {FEE_PCT_RT:.4%} round-trip, notional ${EQUITY*NOTIONAL_FRAC:.0f}/trade on ${EQUITY:.0f}")
    print("=" * 100)
    print()
    print("Pulling per-TF data ...")
    bars_by_tf = {}
    for tf in ("1d", "4h", "1h"):
        bars_by_tf[tf] = pull_tf(tf, DEFAULT_UNIVERSE)
        sample_bars = [len(b) for b in bars_by_tf[tf].values()]
        print(f"  {tf}: {len(bars_by_tf[tf])} coins, "
              f"bars min={min(sample_bars)} med={sorted(sample_bars)[len(sample_bars)//2]} "
              f"max={max(sample_bars)}")
    print()
    
    bus = HistoricalBus(bars_by_tf)
    results = []
    print("Backtesting engines ...")
    for eng_cls, hold_attr in ENGINES:
        t0 = time.time()
        try:
            raw = backtest_engine(eng_cls, hold_attr, bars_by_tf, bus)
        except Exception as e:
            print(f"  {eng_cls.NAME:25s}  ERROR: {e}")
            continue
        wall = time.time() - t0
        n = len(raw["trades"])
        skip = raw.get("skipped", "")
        print(f"  {eng_cls.NAME:25s}  tf={raw['tf']:>3s}  n={n:4d}  ({wall:.1f}s) {skip}")
        results.append(raw)
    
    print()
    print("=" * 100)
    print("SUMMARY (full-sample)")
    print("=" * 100)
    print(f"{'engine':25s} {'tf':>3s}   {'n':>4s} {'WR':>4s}  {'PF':>5s}  {'E[R]':>5s}  {'maxDD':>5s}  {'net_$':>7s}")
    print("-" * 75)
    for r in results:
        if not r["trades"]:
            print(f"{r['engine']:25s} {r['tf']:>3s}   --- 0 trades ---")
            continue
        s = summarize(r["trades"])
        print(f"{r['engine']:25s} {r['tf']:>3s}   {s['n']:>4d} {s['wr']:>4.0%}  "
              f"{s['PF']:>5.2f}  {s['exp_R']:>+5.2f}  {s['maxDD_R']:>5.1f}  {s['net_$']:>+7.2f}")
    
    print()
    print("=" * 100)
    print("WALK-FORWARD: first half vs second half (split at median entry_idx)")
    print("=" * 100)
    print(f"{'engine':25s} {'tf':>3s}  | {'first  n  WR    PF   E[R]   net_$':38s} | {'second n  WR    PF   E[R]   net_$':38s}")
    print("-" * 105)
    for r in results:
        if not r["trades"]:
            continue
        a, b = split_walkforward(r["trades"])
        print(f"{r['engine']:25s} {r['tf']:>3s}  | {fmt(a):38s} | {fmt(b):38s}")
    
    print()
    print("=" * 100)
    print("REGIME STRATIFICATION (top 6 engines by full-sample n)")
    print("=" * 100)
    # Sort engines by n desc
    sorted_r = sorted([r for r in results if r["trades"]], 
                      key=lambda r: len(r["trades"]), reverse=True)
    for r in sorted_r[:8]:
        by_reg = stratify_by_regime(r["trades"])
        print(f"\n{r['engine']} (tf={r['tf']}, total n={len(r['trades'])}):")
        print(f"  {'regime':10s}  {'n':>4s}  {'WR':>4s}  {'PF':>5s}  {'E[R]':>5s}  {'net_$':>7s}")
        for reg, s in sorted(by_reg.items(), key=lambda x: -(x[1]['n'] if x[1] else 0)):
            if s is None:
                continue
            print(f"  {reg:10s}  {s['n']:>4d}  {s['wr']:>4.0%}  "
                  f"{s['PF']:>5.2f}  {s['exp_R']:>+5.2f}  {s['net_$']:>+7.2f}")
    
    print()
    print("=" * 100)
    print("SKIPPED ENGINES (untestable — require data not currently captured)")
    print("=" * 100)
    for name, reason in SKIPPED_ENGINES_WITH_REASON:
        print(f"  {name:25s}  {reason}")
    
    print()
    print("=" * 100)
    print("CAVEATS")
    print("=" * 100)
    print("• In-sample over recent 200×1d (~6.6 mo), 205×4h (~34d), 220×1h (~9d).")
    print("• Walk-forward is 2-window split on small samples; not a true OOS validation.")
    print("• Single-engine isolation — no portfolio-level coin-lock contention modeled.")
    print("• Conservative: SL hit before TP on tied bar; no slippage; flat 0.09% RT fee.")
    print("• Universe coverage varies (~20 coins with full 1d, fewer with 4h/1h).")
    print("• Donchian holds up to 480 bars on 1h — most signals can't resolve in 220-bar window.")


if __name__ == "__main__":
    main()
