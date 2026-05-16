#!/usr/bin/env python3
"""HL-native honest backtest.

Unlike scripts/honest_backtest.py (which used OKX historical), this backtester
pulls candles DIRECTLY from Hyperliquid via Info.candles_snapshot. The whole
point: vsq/lh1/range_fade backtests on OKX showed YELLOW/GREEN but LIVE PAPER
on HL is running negative. The cross-venue assumption is suspect. This is the
fix: backtest on the same venue we trade on.

Models honestly:
  - Fees: HL taker 0.045% per side
  - Slippage: 0.02% per side (conservative for $25-50 notional on majors)
  - Position sizing: RISK_PCT × wallet / SL_distance
  - Compounding: wallet grows/shrinks per closure (Kelly-relevant)

Usage:
    python3 scripts/hl_backtest.py --strategy donchian --coins BTC,ETH,SOL --days 90 --risk 0.10
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


HL_INFO_URL = "https://api.hyperliquid.xyz/info"
FEE_PER_SIDE = 0.00045   # HL taker
SLIPPAGE_PER_SIDE = 0.0002


def fetch_hl_candles(coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
    """Fetch HL candles via Info endpoint. Returns oldest→newest."""
    import httpx
    out: list[dict] = []
    cursor = start_ms
    with httpx.Client(timeout=30) as c:
        while cursor < end_ms:
            body = {
                "type": "candleSnapshot",
                "req": {"coin": coin, "interval": interval,
                        "startTime": cursor, "endTime": min(cursor + 5000 * 3600_000, end_ms)},
            }
            try:
                r = c.post(HL_INFO_URL, json=body)
                r.raise_for_status()
            except Exception as e:
                print(f"  HL fetch error for {coin}: {e}", file=sys.stderr)
                break
            chunk = r.json() or []
            if not chunk:
                break
            for row in chunk:
                out.append({
                    "open_ts": int(row.get("t", row.get("T", 0))),
                    "open": float(row["o"]),
                    "high": float(row["h"]),
                    "low": float(row["l"]),
                    "close": float(row["c"]),
                    "volume": float(row.get("v", 0)),
                })
            new_cursor = out[-1]["open_ts"] + 3600_000
            if new_cursor <= cursor:
                break
            cursor = new_cursor
            time.sleep(0.15)
    # de-dupe by ts
    seen = set()
    uniq: list[dict] = []
    for b in out:
        if b["open_ts"] in seen:
            continue
        seen.add(b["open_ts"])
        uniq.append(b)
    return uniq


class HistoricalBus:
    """Replay bus: returns only candles up to-and-including index `cursor`."""
    def __init__(self, candles_by_coin: dict[str, list[dict]]):
        self._all = candles_by_coin
        self.cursor: dict[str, int] = {c: 0 for c in candles_by_coin}

    def candles(self, coin: str, tf: str, n: int = 200) -> list[dict]:
        i = self.cursor.get(coin, 0)
        return self._all.get(coin, [])[max(0, i - n + 1): i + 1]

    def advance(self, coin: str) -> bool:
        cur = self.cursor.get(coin, 0)
        if cur + 1 < len(self._all.get(coin, [])):
            self.cursor[coin] = cur + 1
            return True
        return False

    def has_more(self, coin: str) -> bool:
        return self.cursor.get(coin, 0) + 1 < len(self._all.get(coin, []))


@dataclass
class Trade:
    coin: str
    is_long: bool
    open_ts: int
    open_px: float
    sl_px: float
    tp_px: float
    size_coin: float
    risk_usd: float
    close_ts: Optional[int] = None
    close_px: Optional[float] = None
    close_reason: Optional[str] = None
    pnl_usd: Optional[float] = None


def backtest(strategy_cls, coins: list[str], candles_by_coin: dict[str, list[dict]],
             starting_capital: float, risk_pct: float) -> dict:
    capital = starting_capital
    bus = HistoricalBus(candles_by_coin)
    open_trades: dict[str, Trade] = {}
    closed: list[Trade] = []
    equity_curve: list[tuple[int, float]] = []

    # Advance each coin's cursor to where strategy has enough history
    min_history = 250
    for c in coins:
        bus.cursor[c] = min(min_history, len(candles_by_coin[c]) - 1)

    while any(bus.has_more(c) for c in coins):
        for coin in coins:
            if not bus.has_more(coin):
                continue
            bus.advance(coin)
            bars = bus.candles(coin, "1h", n=500)
            if len(bars) < 250:
                continue
            cur_bar = bars[-1]
            cur_ts = cur_bar["open_ts"]
            cur_px = cur_bar["close"]
            high = cur_bar["high"]
            low = cur_bar["low"]

            # Manage open trade for this coin
            if coin in open_trades:
                t = open_trades[coin]
                hit_sl = (t.is_long and low <= t.sl_px) or (not t.is_long and high >= t.sl_px)
                hit_tp = (t.is_long and high >= t.tp_px) or (not t.is_long and low <= t.tp_px)
                if hit_sl:
                    close_px = t.sl_px
                    reason = "sl"
                elif hit_tp:
                    close_px = t.tp_px
                    reason = "tp"
                else:
                    # Strategy-defined exit
                    sc, sr = strategy_cls.should_close(
                        {"coin": coin, "is_long": t.is_long}, bus
                    )
                    if sc:
                        close_px = cur_px
                        reason = sr or "strategy_exit"
                    else:
                        close_px = None
                        reason = None

                if close_px is not None:
                    # Apply slippage + fees
                    slip = SLIPPAGE_PER_SIDE * close_px * (1 if t.is_long else -1)
                    eff_close = close_px - slip if t.is_long else close_px + slip
                    gross = (eff_close - t.open_px) * t.size_coin * (1 if t.is_long else -1)
                    fees = FEE_PER_SIDE * t.size_coin * (t.open_px + eff_close)
                    pnl = gross - fees
                    t.close_ts = cur_ts
                    t.close_px = eff_close
                    t.close_reason = reason
                    t.pnl_usd = pnl
                    capital += pnl
                    closed.append(t)
                    del open_trades[coin]
                    equity_curve.append((cur_ts, capital))

            # Look for new signal (only if no open trade on this coin)
            if coin in open_trades:
                continue
            sig = strategy_cls.evaluate(coin, bus)
            if sig is None:
                continue
            # Position sizing: risk_pct × capital / SL distance
            sl_dist = abs(sig.ref_price - sig.sl_px)
            if sl_dist <= 0 or capital <= 0:
                continue
            risk_usd = capital * risk_pct
            size_coin = risk_usd / sl_dist
            # Slippage on entry
            entry_slip = SLIPPAGE_PER_SIDE * sig.ref_price * (1 if sig.is_long else -1)
            eff_open = sig.ref_price + entry_slip if sig.is_long else sig.ref_price - entry_slip
            t = Trade(
                coin=coin, is_long=sig.is_long, open_ts=cur_ts,
                open_px=eff_open, sl_px=sig.sl_px, tp_px=sig.tp_px,
                size_coin=size_coin, risk_usd=risk_usd,
            )
            open_trades[coin] = t

    # Stats
    n = len(closed)
    wins = [t for t in closed if (t.pnl_usd or 0) > 0]
    losses = [t for t in closed if (t.pnl_usd or 0) <= 0]
    win_rate = len(wins) / n if n else 0
    gross_win = sum(t.pnl_usd for t in wins) if wins else 0
    gross_loss = -sum(t.pnl_usd for t in losses) if losses else 0
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float("inf") if gross_win > 0 else 0)
    total_pnl = capital - starting_capital
    return_pct = total_pnl / starting_capital
    avg_win = (gross_win / len(wins)) if wins else 0
    avg_loss = (gross_loss / len(losses)) if losses else 0
    doubles = math.log(capital / starting_capital, 2) if capital > 0 else 0

    return {
        "starting_capital": starting_capital,
        "final_capital": capital,
        "total_pnl_usd": total_pnl,
        "return_pct": return_pct,
        "doublings": doubles,
        "n_trades": n,
        "win_rate": win_rate,
        "profit_factor": pf,
        "avg_win_usd": avg_win,
        "avg_loss_usd": avg_loss,
        "wins": len(wins),
        "losses": len(losses),
        "trades_per_double": (math.log(2) / math.log(1 + return_pct / n) if n > 0 and return_pct > 0 else None),
        "equity_curve_tail": equity_curve[-20:],
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--strategy", default="donchian")
    p.add_argument("--coins", default="BTC,ETH,SOL")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--risk", type=float, default=0.10)
    p.add_argument("--capital", type=float, default=491.24)
    p.add_argument("--out", default="backtests")
    p.add_argument("--tf", default="1h", help="HL candle interval: 1m,5m,15m,1h,4h,1d")
    p.add_argument("--label", default="", help="tag for output filename")
    args = p.parse_args()

    # Load strategy
    mod = __import__(f"strategy_runner.strategies.{args.strategy}", fromlist=["*"])
    strategy_cls = None
    from strategy_runner.strategies._base import StrategyBase
    for name in dir(mod):
        obj = getattr(mod, name)
        if isinstance(obj, type) and issubclass(obj, StrategyBase) and obj is not StrategyBase and obj.NAME:
            strategy_cls = obj
            break
    if strategy_cls is None:
        print(f"FAIL: no strategy class found in {args.strategy}")
        return 2

    coins = [c.strip().upper() for c in args.coins.split(",")]
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.days * 86400_000

    print(f"backtest target: HL {strategy_cls.NAME} on {coins} for {args.days}d, risk={args.risk*100:.1f}%/trade, cap=${args.capital}")
    print()
    bars_by_coin: dict[str, list[dict]] = {}
    for coin in coins:
        print(f"  fetching HL {args.tf} candles for {coin}...")
        bars = fetch_hl_candles(coin, args.tf, start_ms, end_ms)
        print(f"    got {len(bars)} bars")
        bars_by_coin[coin] = bars

    res = backtest(strategy_cls, coins, bars_by_coin, args.capital, args.risk)
    res["params"] = {
        "tf": args.tf, "risk_pct": args.risk, "days": args.days,
        "coins": coins, "starting_capital": args.capital,
        "strategy_env": {k: v for k, v in os.environ.items() if k.startswith("DC_")},
    }

    print()
    print(f"=== RESULT: {strategy_cls.NAME} ===")
    print(f"  starting:    ${res['starting_capital']:.2f}")
    print(f"  final:       ${res['final_capital']:.2f}")
    print(f"  pnl:         ${res['total_pnl_usd']:.2f}  ({res['return_pct']*100:+.1f}%)")
    print(f"  doublings:   {res['doublings']:.2f}x  (target: 17 to $50M)")
    print(f"  n trades:    {res['n_trades']}")
    print(f"  win rate:    {res['win_rate']*100:.1f}%")
    print(f"  profit fact: {res['profit_factor']:.2f}")
    print(f"  avg win:     ${res['avg_win_usd']:.2f}")
    print(f"  avg loss:    ${res['avg_loss_usd']:.2f}")
    if res.get("trades_per_double"):
        print(f"  trades/dbl:  {res['trades_per_double']:.1f}")

    # Persist
    os.makedirs(args.out, exist_ok=True)
    lbl = f"_{args.label}" if args.label else ""
    fname = f"{args.out}/{strategy_cls.NAME}_HL_{args.tf}{lbl}_{time.strftime('%Y%m%d_%H%M')}.json"
    with open(fname, "w") as f:
        json.dump(res, f, indent=2, default=str)
    print(f"\n  saved: {fname}")


if __name__ == "__main__":
    main()
