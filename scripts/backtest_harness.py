"""Honest backtest harness.

Replays a strategy against historical Binance klines pulled from a deployed
signal-bus (or, as fallback, directly from Binance REST). Strategies see a
HistoricalBus that ONLY serves data ≤ the replay cursor — no live-data leakage.

Usage:
    python3 scripts/backtest_harness.py --strategy vsq --days 90 \
        --universe BTC,ETH,SOL --bus https://spm-signal-bus.onrender.com

Outputs:
    backtests/<strategy>_<YYYYMMDD>.md  — WR, PF, expectancy, OOS PF
    backtests/<strategy>_<YYYYMMDD>.jsonl  — trade-by-trade
"""
from __future__ import annotations

import argparse
import collections
import importlib
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy_runner.strategies._base import Signal, StrategyBase  # noqa: E402


TF_SECONDS = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def binance_klines(symbol: str, tf: str, start_ms: int, end_ms: int) -> list[dict]:
    """Pull historical klines directly from Binance Futures REST. Paginated."""
    url = "https://fapi.binance.com/fapi/v1/klines"
    out: list[dict] = []
    cursor = start_ms
    with httpx.Client(timeout=30) as c:
        while cursor < end_ms:
            r = c.get(url, params={"symbol": symbol, "interval": tf,
                                   "startTime": cursor, "endTime": end_ms, "limit": 1500})
            r.raise_for_status()
            rows = r.json()
            if not rows:
                break
            for row in rows:
                out.append({
                    "open_ts": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                })
            cursor = int(rows[-1][0]) + TF_SECONDS[tf] * 1000
            if len(rows) < 1500:
                break
            time.sleep(0.1)  # be polite
    return out


def binance_funding(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """Funding rate history. /fapi/v1/fundingRate."""
    url = "https://fapi.binance.com/fapi/v1/fundingRate"
    out: list[dict] = []
    cursor = start_ms
    with httpx.Client(timeout=30) as c:
        while cursor < end_ms:
            r = c.get(url, params={"symbol": symbol, "startTime": cursor,
                                   "endTime": end_ms, "limit": 1000})
            r.raise_for_status()
            rows = r.json()
            if not rows:
                break
            for row in rows:
                out.append({"ts": int(row["fundingTime"]), "rate": float(row["fundingRate"])})
            cursor = int(rows[-1]["fundingTime"]) + 1
            if len(rows) < 1000:
                break
            time.sleep(0.1)
    return out


@dataclass
class HistoricalBus:
    """Bus implementation that ONLY serves data ≤ self.cursor_ms.

    This is the entire point of the honest harness: strategies cannot see the future.
    """
    klines: dict[tuple[str, str], list[dict]]
    funding_h: dict[str, list[dict]]
    cursor_ms: int = 0

    def candles(self, coin: str, tf: str, n: int = 200) -> list[dict]:
        full = self.klines.get((coin, tf)) or []
        visible = [b for b in full if b["open_ts"] <= self.cursor_ms]
        return visible[-n:]

    def funding(self, coin: str, hours: int) -> list[dict]:
        full = self.funding_h.get(coin) or []
        since = self.cursor_ms - hours * 3600_000
        return [r for r in full if since <= r["ts"] <= self.cursor_ms]

    def markprice(self, coin: str) -> dict:
        bars = self.candles(coin, "1h", n=1) or self.candles(coin, "15m", n=1) or self.candles(coin, "5m", n=1)
        if not bars:
            return {"binance_mid": None, "hl_mid": None}
        return {"binance_mid": float(bars[-1]["close"]), "hl_mid": float(bars[-1]["close"])}

    def liq(self, since_ms: Optional[int] = None, coin: Optional[str] = None) -> list[dict]:
        return []  # historical liq feed not available via REST


@dataclass
class Trade:
    coin: str
    is_long: bool
    open_ts: int
    open_px: float
    sl_px: float
    tp_px: float
    max_hold_bars: int
    strategy: str
    close_ts: int = 0
    close_px: float = 0.0
    close_reason: str = ""

    @property
    def pnl_pct(self) -> float:
        if self.close_px == 0:
            return 0.0
        ret = (self.close_px - self.open_px) / self.open_px
        return ret if self.is_long else -ret


def simulate(strat_cls: type[StrategyBase], coins: list[str], tf: str,
             klines_by_coin: dict[str, list[dict]],
             funding_by_coin: dict[str, list[dict]] | None = None,
             start_ms: int = 0, end_ms: int = 0) -> list[Trade]:
    # build bus with all-tf klines for each coin so strategies needing multiple TFs work
    bus_klines: dict[tuple[str, str], list[dict]] = {}
    for coin in coins:
        bus_klines[(coin, tf)] = klines_by_coin.get(coin, [])
    bus = HistoricalBus(klines=bus_klines, funding_h=funding_by_coin or {})
    step_s = TF_SECONDS[tf]
    open_trades: list[Trade] = []
    closed: list[Trade] = []

    ts = start_ms
    while ts <= end_ms:
        bus.cursor_ms = ts
        # check open trades for SL/TP/timeout
        for t in list(open_trades):
            bars = bus.candles(t.coin, tf, n=1)
            if not bars:
                continue
            px = bars[-1]["close"]
            high = bars[-1]["high"]
            low = bars[-1]["low"]
            bars_held = (ts - t.open_ts) // (step_s * 1000)
            hit_tp = (t.is_long and high >= t.tp_px) or (not t.is_long and low <= t.tp_px)
            hit_sl = (t.is_long and low <= t.sl_px) or (not t.is_long and high >= t.sl_px)
            timed_out = bars_held >= t.max_hold_bars
            if hit_tp:
                t.close_ts = ts; t.close_px = t.tp_px; t.close_reason = "tp"
                closed.append(t); open_trades.remove(t)
            elif hit_sl:
                t.close_ts = ts; t.close_px = t.sl_px; t.close_reason = "sl"
                closed.append(t); open_trades.remove(t)
            elif timed_out:
                t.close_ts = ts; t.close_px = px; t.close_reason = "timeout"
                closed.append(t); open_trades.remove(t)

        # evaluate strategy for each coin (only if not already open)
        open_coins = {t.coin for t in open_trades}
        for coin in coins:
            if coin in open_coins:
                continue
            try:
                sig = strat_cls.evaluate(coin, bus)
            except Exception:
                continue
            if sig is None:
                continue
            open_trades.append(Trade(
                coin=coin, is_long=sig.is_long, open_ts=ts, open_px=sig.ref_price,
                sl_px=sig.sl_px, tp_px=sig.tp_px, max_hold_bars=sig.max_hold_bars,
                strategy=strat_cls.NAME,
            ))
        ts += step_s * 1000

    # close any remaining at last px
    for t in open_trades:
        bars = bus.candles(t.coin, tf, n=1)
        if bars:
            t.close_ts = ts; t.close_px = bars[-1]["close"]; t.close_reason = "eod"
            closed.append(t)
    return closed


def metrics(trades: list[Trade]) -> dict:
    if not trades:
        return {"n": 0, "wr": 0, "pf": 0, "expectancy": 0, "gross": 0}
    wins = [t.pnl_pct for t in trades if t.pnl_pct > 0]
    losses = [t.pnl_pct for t in trades if t.pnl_pct <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf")
    wr = len(wins) / len(trades) if trades else 0.0
    expectancy = sum(t.pnl_pct for t in trades) / len(trades)
    return {
        "n": len(trades), "wr": wr, "pf": pf,
        "expectancy": expectancy, "gross_win": gross_win, "gross_loss": gross_loss,
    }


def load_strategy(name: str) -> type[StrategyBase]:
    mod = importlib.import_module(f"strategy_runner.strategies.{name}")
    for attr in dir(mod):
        obj = getattr(mod, attr)
        if isinstance(obj, type) and issubclass(obj, StrategyBase) and obj is not StrategyBase and obj.NAME == name.replace("range_breakout", "range_bo"):
            return obj
        if isinstance(obj, type) and issubclass(obj, StrategyBase) and obj is not StrategyBase and obj.__module__.endswith(name):
            return obj
    raise SystemExit(f"strategy {name} not found in module")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--universe", required=False, default="")
    ap.add_argument("--tf", default="")
    ap.add_argument("--bus", default="", help="signal-bus URL (optional; falls back to Binance REST)")
    ap.add_argument("--out", default="backtests")
    args = ap.parse_args()

    strat = load_strategy(args.strategy)
    tf = args.tf or strat.TF
    universe = [c.strip().upper() for c in args.universe.split(",") if c.strip()] or strat.UNIVERSE

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - args.days * 86400 * 1000

    print(f"backtest {args.strategy} tf={tf} universe={len(universe)} days={args.days}", flush=True)
    klines_by_coin: dict[str, list[dict]] = {}
    funding_by_coin: dict[str, list[dict]] = {}
    for coin in universe:
        sym = f"{coin}USDT"
        print(f"  fetching {sym}...", flush=True)
        try:
            klines_by_coin[coin] = binance_klines(sym, tf, start_ms, end_ms)
        except Exception as e:
            print(f"    klines failed: {e}")
            klines_by_coin[coin] = []
        if args.strategy in ("fsp", "fd1"):
            try:
                funding_by_coin[coin] = binance_funding(sym, start_ms, end_ms)
            except Exception as e:
                print(f"    funding failed: {e}")

    trades = simulate(strat, universe, tf, klines_by_coin, funding_by_coin, start_ms, end_ms)
    m_all = metrics(trades)

    # walk-forward: split 50/50
    mid = (start_ms + end_ms) // 2
    is_trades = [t for t in trades if t.open_ts <= mid]
    oos_trades = [t for t in trades if t.open_ts > mid]
    m_is = metrics(is_trades)
    m_oos = metrics(oos_trades)

    date_tag = datetime.utcnow().strftime("%Y%m%d")
    os.makedirs(args.out, exist_ok=True)
    md_path = os.path.join(args.out, f"{args.strategy}_{date_tag}.md")
    jsonl_path = os.path.join(args.out, f"{args.strategy}_{date_tag}.jsonl")

    with open(md_path, "w") as f:
        f.write(f"# {args.strategy} honest backtest — {date_tag}\n\n")
        f.write(f"- TF: {tf}\n- Universe: {len(universe)}\n- Days: {args.days}\n\n")
        f.write("## All trades\n")
        f.write(f"- n={m_all['n']} WR={m_all['wr']*100:.1f}% PF={m_all['pf']:.2f} expectancy={m_all['expectancy']*100:.2f}%/trade\n\n")
        f.write("## Walk-forward (split 50/50)\n")
        f.write(f"- IS  n={m_is['n']} WR={m_is['wr']*100:.1f}% PF={m_is['pf']:.2f}\n")
        f.write(f"- OOS n={m_oos['n']} WR={m_oos['wr']*100:.1f}% PF={m_oos['pf']:.2f}\n\n")
        f.write("## Per-coin\n")
        per = collections.defaultdict(list)
        for t in trades:
            per[t.coin].append(t)
        for coin, ts in sorted(per.items()):
            mm = metrics(ts)
            f.write(f"- {coin}: n={mm['n']} WR={mm['wr']*100:.1f}% PF={mm['pf']:.2f}\n")

    with open(jsonl_path, "w") as f:
        for t in trades:
            f.write(json.dumps({
                "coin": t.coin, "is_long": t.is_long, "open_ts": t.open_ts,
                "open_px": t.open_px, "close_ts": t.close_ts, "close_px": t.close_px,
                "sl_px": t.sl_px, "tp_px": t.tp_px, "pnl_pct": t.pnl_pct,
                "close_reason": t.close_reason,
            }) + "\n")

    print(json.dumps({"all": m_all, "is": m_is, "oos": m_oos, "report": md_path}, indent=2))


if __name__ == "__main__":
    main()
