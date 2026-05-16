#!/usr/bin/env python3
"""Batch backtest harness: fetches HL candles ONCE per coin+TF, then runs
every candidate strategy through the same data. Faster than per-strategy
runs, and gives apples-to-apples comparison.

Usage:
    python3 scripts/batch_backtest.py --days 365 --risk 0.05
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.hl_backtest import (
    fetch_hl_candles, HistoricalBus, Trade, FEE_PER_SIDE, SLIPPAGE_PER_SIDE
)
from strategy_runner.strategies._candidates import ALL_CANDIDATES as R1
from strategy_runner.strategies._candidates2 import ROUND2 as R2
ALL_CANDIDATES = R1 + R2


def backtest_one(strategy_cls, coins, candles_by_coin_tf, starting_capital, risk_pct):
    """Backtest one strategy. candles_by_coin_tf is dict of {(coin, tf): [bars]}."""
    capital = starting_capital
    tf = strategy_cls.TF
    # Build a single-tf bus per the strategy's TF
    candles_by_coin = {c: candles_by_coin_tf.get((c, tf), []) for c in coins}
    bus = HistoricalBus(candles_by_coin)
    open_trades = {}
    closed = []
    min_history = 220 if tf == "1h" else 80

    for c in coins:
        bus.cursor[c] = min(min_history, len(candles_by_coin[c]) - 1)

    while any(bus.has_more(c) for c in coins):
        for coin in coins:
            if not bus.has_more(coin):
                continue
            bus.advance(coin)
            bars = candles_by_coin[coin][:bus.cursor[coin] + 1]
            if len(bars) < min_history:
                continue
            cur = bars[-1]
            cur_ts = cur["open_ts"]; cur_px = cur["close"]
            high = cur["high"]; low = cur["low"]
            # manage open trade
            if coin in open_trades:
                t = open_trades[coin]
                hit_sl = (t.is_long and low <= t.sl_px) or (not t.is_long and high >= t.sl_px)
                hit_tp = (t.is_long and high >= t.tp_px) or (not t.is_long and low <= t.tp_px)
                # also enforce max_hold timeout
                bar_h = 3600 if tf == "1h" else 14400
                timed_out = (cur_ts - t.open_ts) >= 60 * bar_h  # 60 bars cap by default
                close_px = None; reason = None
                if hit_sl:
                    close_px, reason = t.sl_px, "sl"
                elif hit_tp:
                    close_px, reason = t.tp_px, "tp"
                elif timed_out:
                    close_px, reason = cur_px, "timeout"
                if close_px is not None:
                    slip = SLIPPAGE_PER_SIDE * close_px * (1 if t.is_long else -1)
                    eff_close = close_px - slip if t.is_long else close_px + slip
                    gross = (eff_close - t.open_px) * t.size_coin * (1 if t.is_long else -1)
                    fees = FEE_PER_SIDE * t.size_coin * (t.open_px + eff_close)
                    pnl = gross - fees
                    t.close_ts = cur_ts; t.close_px = eff_close; t.close_reason = reason; t.pnl_usd = pnl
                    capital += pnl
                    closed.append(t)
                    del open_trades[coin]

            if coin in open_trades:
                continue
            sig = strategy_cls.evaluate(coin, bus)
            if sig is None:
                continue
            sl_dist = abs(sig.ref_price - sig.sl_px)
            if sl_dist <= 0 or capital <= 0:
                continue
            risk_usd = capital * risk_pct
            size_coin = risk_usd / sl_dist
            entry_slip = SLIPPAGE_PER_SIDE * sig.ref_price * (1 if sig.is_long else -1)
            eff_open = sig.ref_price + entry_slip if sig.is_long else sig.ref_price - entry_slip
            open_trades[coin] = Trade(coin, sig.is_long, cur_ts, eff_open, sig.sl_px, sig.tp_px, size_coin, risk_usd)

    n = len(closed)
    wins = [t for t in closed if (t.pnl_usd or 0) > 0]
    losses = [t for t in closed if (t.pnl_usd or 0) <= 0]
    gross_win = sum(t.pnl_usd for t in wins) if wins else 0
    gross_loss = -sum(t.pnl_usd for t in losses) if losses else 0
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0)
    wr = (len(wins) / n) if n else 0
    return_pct = (capital - starting_capital) / starting_capital
    avg_win = (gross_win / len(wins)) if wins else 0
    avg_loss = (gross_loss / len(losses)) if losses else 0
    return {
        "name": strategy_cls.NAME,
        "tf": tf,
        "trades": n,
        "win_rate": wr,
        "profit_factor": pf,
        "return_pct": return_pct,
        "final_capital": capital,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy_R": (wr * avg_win - (1 - wr) * avg_loss) / avg_loss if avg_loss > 0 else 0,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--coins", default="BTC,ETH,SOL")
    p.add_argument("--risk", type=float, default=0.05)
    p.add_argument("--capital", type=float, default=491.24)
    p.add_argument("--out", default="backtests")
    args = p.parse_args()

    coins = [c.strip().upper() for c in args.coins.split(",")]
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.days * 86400_000

    # Figure out which TFs we need
    tfs_needed = set(s.TF for s in ALL_CANDIDATES)
    print(f"Fetching HL candles: {len(coins)} coins × {len(tfs_needed)} TFs × {args.days} days...")
    candles: dict[tuple[str, str], list[dict]] = {}
    for tf in tfs_needed:
        for coin in coins:
            print(f"  {coin} @ {tf}...", end="", flush=True)
            bars = fetch_hl_candles(coin, tf, start_ms, end_ms)
            print(f" {len(bars)} bars")
            candles[(coin, tf)] = bars

    print()
    print(f"=== BATCH BACKTEST: {len(ALL_CANDIDATES)} candidates × {coins} × {args.days}d, risk={args.risk*100}% ===")
    print()

    results = []
    for cls in ALL_CANDIDATES:
        try:
            r = backtest_one(cls, coins, candles, args.capital, args.risk)
            results.append(r)
        except Exception as e:
            print(f"  {cls.NAME}: ERROR {e}")
            import traceback; traceback.print_exc()

    # Print table
    print(f"{'name':<18} {'tf':<4} {'n':<5} {'wr':<6} {'pf':<7} {'ret':<10} {'final':<10}")
    print("-" * 70)
    for r in sorted(results, key=lambda x: -x["return_pct"]):
        verdict = "🟢" if r["profit_factor"] >= 1.3 and r["return_pct"] > 0 else "🔴"
        print(f"{r['name']:<18} {r['tf']:<4} {r['trades']:<5} {r['win_rate']*100:<5.1f}% {r['profit_factor']:<6.2f} {r['return_pct']*100:<+9.1f}% ${r['final_capital']:<9.2f} {verdict}")

    # Save
    os.makedirs(args.out, exist_ok=True)
    fname = f"{args.out}/batch_{time.strftime('%Y%m%d_%H%M')}.json"
    with open(fname, "w") as f:
        json.dump({
            "config": {"days": args.days, "coins": coins, "risk": args.risk, "capital": args.capital},
            "results": results,
        }, f, indent=2, default=str)
    print(f"\nsaved: {fname}")


if __name__ == "__main__":
    main()
