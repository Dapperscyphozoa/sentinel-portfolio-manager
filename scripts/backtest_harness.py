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


# ----- Binance (primary, geo-restricted in some regions) -----

def binance_klines(symbol: str, tf: str, start_ms: int, end_ms: int) -> list[dict]:
    """Pull historical klines from Binance REST. Tries fapi (perps) first;
    on 451 geo-block, falls back to data-api.binance.vision (spot, archive
    endpoint). Spot and perp prices on Binance are typically within
    0.1-0.3% — close enough for backtest signal validation.
    """
    hosts = [
        ("https://fapi.binance.com/fapi/v1/klines", "perp"),
        ("https://data-api.binance.vision/api/v3/klines", "spot-archive"),
    ]
    out: list[dict] = []
    last_err: Exception | None = None
    for url, kind in hosts:
        cursor = start_ms
        out = []
        try:
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
                    time.sleep(0.1)
            if out:
                return out
        except httpx.HTTPStatusError as e:
            last_err = e
            if e.response.status_code != 451:
                raise
            # Geo-blocked, try next host
            continue
    if last_err:
        raise last_err
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


# ----- OKX (fallback for restricted regions) -----

_OKX_TF = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H", "4h": "4H", "1d": "1D"}


def okx_klines(symbol: str, tf: str, start_ms: int, end_ms: int) -> list[dict]:
    """OKX historical klines. /api/v5/market/history-candles for deep history.

    OKX returns data NEWEST first; we reverse to oldest-first. Pagination uses
    `after` cursor (returns data with ts < after). Page size 100.
    """
    inst = symbol.replace("USDT", "-USDT-SWAP")
    okx_bar = _OKX_TF.get(tf)
    if not okx_bar:
        return []
    url = "https://www.okx.com/api/v5/market/history-candles"
    out: list[dict] = []
    after = end_ms
    with httpx.Client(timeout=30) as c:
        while after > start_ms:
            r = c.get(url, params={"instId": inst, "bar": okx_bar,
                                   "after": str(after), "limit": "100"})
            r.raise_for_status()
            d = r.json()
            rows = d.get("data") or []
            if not rows:
                break
            for row in rows:
                ts = int(row[0])
                if ts < start_ms:
                    continue
                out.append({
                    "open_ts": ts,
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                })
            # Cursor backward
            oldest = int(rows[-1][0])
            if oldest <= start_ms:
                break
            after = oldest
            time.sleep(0.12)
    out.sort(key=lambda b: b["open_ts"])
    return out


def okx_funding(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    """OKX funding rate history. /api/v5/public/funding-rate-history."""
    inst = symbol.replace("USDT", "-USDT-SWAP")
    url = "https://www.okx.com/api/v5/public/funding-rate-history"
    out: list[dict] = []
    after = end_ms
    with httpx.Client(timeout=30) as c:
        while after > start_ms:
            r = c.get(url, params={"instId": inst, "after": str(after), "limit": "100"})
            r.raise_for_status()
            d = r.json()
            rows = d.get("data") or []
            if not rows:
                break
            for row in rows:
                ts = int(row.get("fundingTime", 0))
                if ts < start_ms:
                    continue
                out.append({"ts": ts, "rate": float(row.get("fundingRate", "0"))})
            oldest = int(rows[-1].get("fundingTime", 0))
            if oldest <= start_ms or oldest == after:
                break
            after = oldest
            time.sleep(0.12)
    out.sort(key=lambda r: r["ts"])
    return out


def signal_bus_klines(symbol: str, tf: str, start_ms: int, end_ms: int) -> list[dict]:
    """Pull historical klines from core's /signal_bus/candles endpoint.
    
    This is Binance data ingested through the production signal-bus, so it
    matches what the engines see live — eliminating venue-confounding that
    council flagged when OKX was used as a Binance proxy.
    
    Coin string is symbol without USDT suffix (e.g. 'BTC' not 'BTCUSDT').
    """
    coin = symbol.replace("USDT", "")
    bus = os.environ.get("BACKTEST_SIGNAL_BUS_URL", "https://core-o21t.onrender.com")
    # Bus returns last N bars; estimate N needed for the window
    tf_s = TF_SECONDS[tf]
    n_needed = min(int((end_ms - start_ms) / 1000 / tf_s) + 50, 1000)
    url = f"{bus}/signal_bus/candles/{coin}/{tf}"
    with httpx.Client(timeout=30) as c:
        r = c.get(url, params={"n": n_needed})
        r.raise_for_status()
        bars = r.json()
    # Filter to requested window, normalize schema (already matches)
    out = []
    for b in bars:
        ts = int(b.get("open_ts", b.get("ts", 0)))
        if start_ms <= ts <= end_ms:
            out.append({
                "open_ts": ts,
                "open": float(b["open"]),
                "high": float(b["high"]),
                "low": float(b["low"]),
                "close": float(b["close"]),
                "volume": float(b.get("volume", 0)),
            })
    out.sort(key=lambda b: b["open_ts"])
    return out


# ----- Dispatch by venue env var -----

def fetch_klines(symbol: str, tf: str, start_ms: int, end_ms: int) -> list[dict]:
    venue = os.environ.get("BACKTEST_DATA_VENUE", "binance").lower()
    if venue == "signal_bus":
        return signal_bus_klines(symbol, tf, start_ms, end_ms)
    if venue == "okx":
        return okx_klines(symbol, tf, start_ms, end_ms)
    return binance_klines(symbol, tf, start_ms, end_ms)


def fetch_funding(symbol: str, start_ms: int, end_ms: int) -> list[dict]:
    venue = os.environ.get("BACKTEST_DATA_VENUE", "binance").lower()
    if venue == "okx":
        return okx_funding(symbol, start_ms, end_ms)
    return binance_funding(symbol, start_ms, end_ms)


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
        """Return hourly forward-filled funding rates for [cursor - hours, cursor].

        Settled funding (Binance/OKX: every 4-8h) is interpolated to hourly cadence
        by carrying forward the most recent rate. This matches the production
        signal-bus behavior, which receives the live funding rate field on the
        Binance markPrice@1s stream and so effectively sees an hourly+ cadence.
        """
        full = self.funding_h.get(coin) or []
        if not full:
            return []
        end_ms = self.cursor_ms
        start_ms = end_ms - hours * 3600_000
        # Find last rate before start_ms to carry forward
        last_rate: float | None = None
        for r in full:
            if r["ts"] <= start_ms:
                last_rate = float(r["rate"])
            else:
                break
        out: list[dict] = []
        idx = 0
        ts = start_ms
        while ts <= end_ms:
            # Advance idx past any settled funding events ≤ ts
            while idx < len(full) and full[idx]["ts"] <= ts:
                last_rate = float(full[idx]["rate"])
                idx += 1
            if last_rate is not None:
                out.append({"ts": ts, "rate": last_rate})
            ts += 3600_000
        return out

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
    """Load a strategy class by NAME. Handles module/NAME drift cleanly.

    Tries each candidate module name; in each, returns the class whose
    NAME attribute matches the requested name (NOT first match — multiple
    engines per module is common, e.g. oos_engines.py has 11).
    """
    # Hard mapping for known NAME → module-file mismatches.
    name_to_modules: dict[str, list[str]] = {
        "range_bo": ["range_breakout", "range_bo"],
        "range_breakout": ["range_breakout"],
        # e01..e17 all live in oos_engines.py
        **{f"e{n:02d}_{x}": ["oos_engines"] for n in (1,7,8,9,16,17)
           for x in ("zfade3s_tu_1d","zfade2s_tu_1d","dip3d10_td_1d","pump3d10_td_1d",
                     "bb_fade_hv_1d","bb_fade_bt_1d","zfade3s_tu_4h","zfade2s_tu_4h",
                     "dip3d7_td_4h","bb_fade_hv_4h","bb_fade_bt_4h")},
        "ict_confluence_4h": ["ict_confluence"],
        "ict_confluence_1d": ["ict_confluence"],
        "cascade_sniper_hl": ["cascade_sniper"],
    }
    candidates = name_to_modules.get(name, [name])
    last_err: Exception | None = None
    for mod_name in candidates:
        try:
            mod = importlib.import_module(f"strategy_runner.strategies.{mod_name}")
        except ImportError as e:
            last_err = e
            continue
        # Prefer exact NAME match; fall back to first concrete subclass
        match = None
        first = None
        for attr in dir(mod):
            obj = getattr(mod, attr)
            if (isinstance(obj, type) and issubclass(obj, StrategyBase)
                    and obj is not StrategyBase and getattr(obj, "NAME", None)):
                if obj.NAME == name:
                    match = obj
                    break
                if first is None:
                    first = obj
        if match:
            return match
        if first and len(candidates) == 1:  # only-candidate fallback
            return first
    raise SystemExit(f"strategy {name!r} not found (last error: {last_err})")


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

    venue = os.environ.get("BACKTEST_DATA_VENUE", "binance")
    print(f"backtest {args.strategy} tf={tf} universe={len(universe)} days={args.days} venue={venue}", flush=True)
    klines_by_coin: dict[str, list[dict]] = {}
    funding_by_coin: dict[str, list[dict]] = {}
    for coin in universe:
        sym = f"{coin}USDT"
        print(f"  fetching {sym}...", flush=True)
        try:
            klines_by_coin[coin] = fetch_klines(sym, tf, start_ms, end_ms)
        except Exception as e:
            print(f"    klines failed: {e}")
            klines_by_coin[coin] = []
        if args.strategy in ("fsp", "fd1"):
            try:
                funding_by_coin[coin] = fetch_funding(sym, start_ms, end_ms)
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
