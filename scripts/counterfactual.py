"""Counterfactual analysis for proposed profit/cost adjustments.

Reads backtests/*.jsonl (the honest-backtest output across every engine),
recomputes net P&L under each proposed adjustment, and emits per-engine
and aggregate deltas vs the taker-fee baseline.

This module is offline-only. It does not touch live trading, the PM gate,
or production state. Run:

    python3 scripts/counterfactual.py

Output: JSON to stdout (consumed by ADJUSTMENTS_REPORT.md generation).
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone


BACKTESTS_DIR = os.path.join(os.path.dirname(__file__), "..", "backtests")

# HL fee schedule — matches strategy_runner/trader.py:867-868
TAKER = 0.00045   # 0.045% per side
MAKER = 0.00015   # 0.015% per side
RT_TAKER = 2 * TAKER             # 0.090% round-trip both legs taker (baseline)
RT_HALF_MAKER = MAKER + TAKER    # 0.060% maker entry + taker exit (proposed)


@dataclass
class Fire:
    engine: str
    coin: str
    is_long: bool
    open_ts: int          # ms
    close_ts: int         # ms
    pnl_pct: float        # gross price-move %, no fees
    close_reason: str
    # Optional fields — present only in JSONLs produced by the harness AFTER
    # the #4/#9 instrumentation patch landed. Empty / 0 for legacy JSONLs.
    fire_reason: str = ""
    vol_24h_usd: float = 0.0


def _engine_name_from_file(fname: str) -> str:
    # uzt_20260518.jsonl -> uzt
    # e09_pump3d10_td_1d_20260517.jsonl -> e09_pump3d10_td_1d
    # donchian_HL_1h_inv_3coin_365d_20260516_0827.json (JSON file, skipped)
    stem = fname.replace(".jsonl", "")
    parts = stem.split("_")
    while parts and parts[-1].isdigit():
        parts.pop()
    return "_".join(parts) or stem


def load_fires() -> list[Fire]:
    fires: list[Fire] = []
    for fname in sorted(os.listdir(BACKTESTS_DIR)):
        if not fname.endswith(".jsonl"):
            continue
        engine = _engine_name_from_file(fname)
        path = os.path.join(BACKTESTS_DIR, fname)
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                fires.append(Fire(
                    engine=engine,
                    coin=d["coin"],
                    is_long=bool(d["is_long"]),
                    open_ts=int(d["open_ts"]),
                    close_ts=int(d["close_ts"]),
                    pnl_pct=float(d["pnl_pct"]),
                    close_reason=str(d.get("close_reason", "unknown")),
                    fire_reason=str(d.get("fire_reason", "") or ""),
                    vol_24h_usd=float(d.get("vol_24h_usd", 0.0) or 0.0),
                ))
    return fires


def stats(fires: list[Fire], rt_fee: float = RT_TAKER) -> dict:
    """n, win-rate, profit-factor, sum of net pnl% — under given round-trip fee."""
    if not fires:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "net_sum": 0.0, "gross_sum": 0.0}
    nets = [f.pnl_pct - rt_fee for f in fires]
    wins = sum(x for x in nets if x > 0)
    losses_abs = sum(-x for x in nets if x < 0)
    if losses_abs == 0:
        pf = float("inf") if wins > 0 else 0.0
    else:
        pf = wins / losses_abs
    n_wins = sum(1 for x in nets if x > 0)
    return {
        "n": len(fires),
        "wr": n_wins / len(fires),
        "pf": pf,
        "net_sum": sum(nets),
        "gross_sum": sum(f.pnl_pct for f in fires),
    }


def by_engine(fires: list[Fire]) -> dict[str, list[Fire]]:
    out: dict[str, list[Fire]] = defaultdict(list)
    for f in fires:
        out[f.engine].append(f)
    return dict(out)


# ─────────────────────────── Adjustments ───────────────────────────


def adj1_maker_entry(fires: list[Fire]) -> dict:
    """#1 — Switch entry from taker to maker.

    Round-trip fee drops from 2*TAKER (0.090%) to MAKER+TAKER (0.060%).
    Savings per fire: 0.030% of notional regardless of outcome.
    """
    eng_groups = by_engine(fires)
    rows = []
    for eng, fs in sorted(eng_groups.items()):
        base = stats(fs, RT_TAKER)
        proposed = stats(fs, RT_HALF_MAKER)
        rows.append({
            "engine": eng,
            "n": base["n"],
            "baseline_net_pct": base["net_sum"] * 100,
            "proposed_net_pct": proposed["net_sum"] * 100,
            "delta_net_pct": (proposed["net_sum"] - base["net_sum"]) * 100,
            "baseline_pf": base["pf"],
            "proposed_pf": proposed["pf"],
        })
    total_base = stats(fires, RT_TAKER)
    total_prop = stats(fires, RT_HALF_MAKER)
    return {
        "name": "Maker entry (entry MAKER, exit TAKER)",
        "savings_bps_per_fire": (RT_TAKER - RT_HALF_MAKER) * 10000,
        "total_fires": len(fires),
        "baseline_net_pct_total": total_base["net_sum"] * 100,
        "proposed_net_pct_total": total_prop["net_sum"] * 100,
        "delta_net_pct_total": (total_prop["net_sum"] - total_base["net_sum"]) * 100,
        "baseline_pf_total": total_base["pf"],
        "proposed_pf_total": total_prop["pf"],
        "by_engine": rows,
    }


def _split_by_time(fires: list[Fire], frac: float = 0.5) -> tuple[list[Fire], list[Fire]]:
    """Split chronologically per-engine. Returns (in_sample, out_of_sample)."""
    eng_groups = by_engine(fires)
    is_, oos = [], []
    for eng, fs in eng_groups.items():
        fs_sorted = sorted(fs, key=lambda f: f.open_ts)
        cut = int(len(fs_sorted) * frac)
        is_.extend(fs_sorted[:cut])
        oos.extend(fs_sorted[cut:])
    return is_, oos


def adj3_by_coin_pruning(fires: list[Fire], min_n: int = 10, max_pf: float = 1.0) -> dict:
    """#3 — Drop (engine, coin) pairs with n >= min_n and net PF < max_pf.

    Walk-forward variant: fit dead-pair set on the first half (IS), evaluate
    on the second half (OOS). An in-sample-overfit version is reported too
    so the reader can see the gap.
    """
    def _evaluate(train: list[Fire], test: list[Fire]) -> dict:
        pair_groups_train: dict[tuple[str, str], list[Fire]] = defaultdict(list)
        for f in train:
            pair_groups_train[(f.engine, f.coin)].append(f)
        dead_pairs = set()
        dead_pairs_detail = []
        for (eng, coin), fs in pair_groups_train.items():
            s = stats(fs, RT_TAKER)
            if s["n"] >= min_n and s["pf"] < max_pf:
                dead_pairs.add((eng, coin))
                dead_pairs_detail.append({
                    "engine": eng, "coin": coin, "is_n": s["n"],
                    "is_pf": s["pf"], "is_net_pct": s["net_sum"] * 100,
                })
        kept = [f for f in test if (f.engine, f.coin) not in dead_pairs]
        dropped = [f for f in test if (f.engine, f.coin) in dead_pairs]
        base = stats(test, RT_TAKER)
        after = stats(kept, RT_TAKER)
        return {
            "dead_pairs_in_train": len(dead_pairs),
            "test_fires_total": len(test),
            "test_fires_dropped": len(dropped),
            "test_baseline_net_pct": base["net_sum"] * 100,
            "test_proposed_net_pct": after["net_sum"] * 100,
            "test_delta_net_pct": (after["net_sum"] - base["net_sum"]) * 100,
            "test_baseline_pf": base["pf"],
            "test_proposed_pf": after["pf"],
            "dead_pairs_detail": sorted(dead_pairs_detail, key=lambda r: r["is_net_pct"])[:15],
        }

    is_fires, oos_fires = _split_by_time(fires, frac=0.5)
    walk_forward = _evaluate(is_fires, oos_fires)
    in_sample_overfit = _evaluate(fires, fires)  # fit and test on same data
    return {
        "name": f"By-coin pruning (n>={min_n}, PF<{max_pf})",
        "rule": f"drop (engine, coin) pairs with n>={min_n} and live PF<{max_pf}",
        "walk_forward_50_50": walk_forward,
        "in_sample_overfit_warning": {
            "delta_net_pct": in_sample_overfit["test_delta_net_pct"],
            "note": "Same-data fit-and-eval. Reported only to show the overfit gap vs walk-forward.",
        },
    }


def adj6_clean_only(fires: list[Fire]) -> dict:
    """#6 — Exclude noisy closures from gating math.

    In backtest data, all closes are sl/tp/timeout/eod (no force_close /
    reconciled_off_book), so this is a null counterfactual offline.
    Reported as evidence that this adjustment requires LIVE closure data.
    """
    reasons: dict[str, int] = defaultdict(int)
    for f in fires:
        reasons[f.close_reason] += 1
    noisy_reasons = {"force_close:audit_red", "reconciled_off_book", "manual"}
    clean = [f for f in fires if f.close_reason not in noisy_reasons]
    base = stats(fires, RT_TAKER)
    after = stats(clean, RT_TAKER)
    return {
        "name": "Clean-only stats (drop noisy closures)",
        "close_reasons_seen": dict(reasons),
        "fires_total": len(fires),
        "fires_after_clean_filter": len(clean),
        "baseline_pf_total": base["pf"],
        "proposed_pf_total": after["pf"],
        "delta_net_pct_total": (after["net_sum"] - base["net_sum"]) * 100,
        "note": "Backtest closes are simulator-clean. Counterfactual is null offline.",
    }


def adj8_utc_hour_blackout(fires: list[Fire], min_n_per_hour: int = 5) -> dict:
    """#8 — Approximation of pre-event blackout via UTC-hour analysis.

    Walk-forward: fit losing-hour set per engine on first half, evaluate on
    second half. The literal proposal needs an event calendar; this is the
    closest offline proxy and is reported with explicit overfit warning.
    """
    def _hour(f: Fire) -> int:
        return datetime.fromtimestamp(f.open_ts / 1000, tz=timezone.utc).hour

    def _evaluate_engine(train: list[Fire], test: list[Fire]) -> dict | None:
        per_hour_train: dict[int, list[Fire]] = defaultdict(list)
        for f in train:
            per_hour_train[_hour(f)].append(f)
        block_set = set()
        for h, hs in per_hour_train.items():
            s = stats(hs, RT_TAKER)
            if s["n"] >= min_n_per_hour and s["pf"] < 1.0:
                block_set.add(h)
        if not block_set:
            return None
        kept = [f for f in test if _hour(f) not in block_set]
        base = stats(test, RT_TAKER)
        after = stats(kept, RT_TAKER)
        return {
            "blocked_hours_utc": sorted(block_set),
            "test_n_before": base["n"],
            "test_n_after": after["n"],
            "test_pf_before": base["pf"],
            "test_pf_after": after["pf"],
            "test_delta_net_pct": (after["net_sum"] - base["net_sum"]) * 100,
        }

    rows = []
    is_fires, oos_fires = _split_by_time(fires, frac=0.5)
    is_by_eng = by_engine(is_fires)
    oos_by_eng = by_engine(oos_fires)
    for eng in sorted(set(is_by_eng) & set(oos_by_eng)):
        r = _evaluate_engine(is_by_eng[eng], oos_by_eng[eng])
        if r is None:
            continue
        if r["test_n_before"] < 20:  # require enough OOS to read
            continue
        r["engine"] = eng
        rows.append(r)
    return {
        "name": "UTC-hour blackout (walk-forward 50/50)",
        "approach": "Per engine: identify hours with IS n>=5 and PF<1.0; block in OOS half.",
        "caveat": "Proxy for event blackout. Real implementation needs an event calendar.",
        "engines_with_walk_forward_signal": len(rows),
        "by_engine": sorted(rows, key=lambda r: -r["test_delta_net_pct"]),
    }


def adj4_fire_reason_pruning(fires: list[Fire], min_n: int = 15, max_pf: float = 1.0) -> dict:
    """#4 — Drop (engine, fire_reason) pairs with n >= min_n and PF < max_pf.

    Same walk-forward shape as #3, but partitioned by fire_reason. Requires
    JSONLs produced by the instrumented harness; older files report
    'no_data_yet'.
    """
    tagged = [f for f in fires if f.fire_reason]
    if not tagged:
        return {
            "name": f"By fire_reason pruning (n>={min_n}, PF<{max_pf})",
            "status": "no_data_yet",
            "note": "No fires in backtests/*.jsonl carry fire_reason. Re-run honest_backtest.py "
                    "with the instrumented harness, then re-run this analysis.",
        }
    is_fires, oos_fires = _split_by_time(tagged, frac=0.5)
    pair_groups_train: dict[tuple[str, str], list[Fire]] = defaultdict(list)
    for f in is_fires:
        pair_groups_train[(f.engine, f.fire_reason)].append(f)
    dead = set()
    dead_detail = []
    for (eng, fr), fs in pair_groups_train.items():
        s = stats(fs, RT_TAKER)
        if s["n"] >= min_n and s["pf"] < max_pf:
            dead.add((eng, fr))
            dead_detail.append({"engine": eng, "fire_reason": fr,
                                "is_n": s["n"], "is_pf": s["pf"],
                                "is_net_pct": s["net_sum"] * 100})
    kept = [f for f in oos_fires if (f.engine, f.fire_reason) not in dead]
    base = stats(oos_fires, RT_TAKER)
    after = stats(kept, RT_TAKER)
    return {
        "name": f"By fire_reason pruning (n>={min_n}, PF<{max_pf})",
        "dead_pairs_in_train": len(dead),
        "oos_fires_total": len(oos_fires),
        "oos_fires_dropped": len(oos_fires) - len(kept),
        "oos_baseline_net_pct": base["net_sum"] * 100,
        "oos_proposed_net_pct": after["net_sum"] * 100,
        "oos_delta_net_pct": (after["net_sum"] - base["net_sum"]) * 100,
        "oos_baseline_pf": base["pf"],
        "oos_proposed_pf": after["pf"],
        "dead_pairs_detail": sorted(dead_detail, key=lambda r: r["is_net_pct"])[:15],
    }


def adj9_liquidity_floor(fires: list[Fire], floors_usd: tuple[float, ...] = (50e6, 200e6, 500e6)) -> dict:
    """#9 — Tiered liquidity floor.

    For each floor, drop fires below the threshold and compute aggregate
    delta. Requires JSONLs produced by the instrumented harness.
    """
    tagged = [f for f in fires if f.vol_24h_usd > 0]
    if not tagged:
        return {
            "name": "Tiered liquidity floor",
            "status": "no_data_yet",
            "note": "No fires in backtests/*.jsonl carry vol_24h_usd. Re-run honest_backtest.py "
                    "with the instrumented harness, then re-run this analysis.",
        }
    base = stats(tagged, RT_TAKER)
    rows = []
    for floor in floors_usd:
        kept = [f for f in tagged if f.vol_24h_usd >= floor]
        s = stats(kept, RT_TAKER)
        rows.append({
            "floor_usd": floor,
            "n_kept": s["n"],
            "n_dropped": base["n"] - s["n"],
            "net_pct": s["net_sum"] * 100,
            "pf": s["pf"],
            "delta_net_pct_vs_baseline": (s["net_sum"] - base["net_sum"]) * 100,
        })
    return {
        "name": "Tiered liquidity floor",
        "n_with_volume_data": len(tagged),
        "baseline_net_pct": base["net_sum"] * 100,
        "baseline_pf": base["pf"],
        "tiers": rows,
    }


# ─────────────────────────── Untestable items ───────────────────────────


UNTESTABLE = {
    "#5 — Regime affinity confidence threshold tune": {
        "blocker": "No regime classification timeline in backtest data. Regime is computed live by pm/regime.py.",
        "next_step": "Snapshot /regime endpoint every 5min for 7+ days, then overlay on per-fire timestamps to compute per-confidence-bucket PF.",
    },
    "#7 — Funding inclusion in net P&L": {
        "blocker": "No HL funding history archive (per BACKTEST_QUEUE.md infrastructure follow-up §2). signal_bus persistence is accumulating from 2026-05-19; usable in ~14 days.",
        "next_step": "Wait for signal_bus funding archive, then recompute closures.pnl_usd += funding_paid/received per hour held.",
    },
    "#10 — Kronos gate on UNTESTED tier": {
        "blocker": "Kronos is a transformer ML gate; runs on live signal stream. test_kronos_gate.py exists but no historical inference output to overlay.",
        "next_step": "Activate Kronos in paper mode on UNTESTED engines for 30+ days; compare signal acceptance rate vs paper PF delta.",
    },
}


def main() -> None:
    fires = load_fires()
    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "fires_loaded": len(fires),
        "engines_loaded": sorted(by_engine(fires).keys()),
        "baseline_total": stats(fires, RT_TAKER),
        "adjustments": {
            "1_maker_entry": adj1_maker_entry(fires),
            "3_by_coin_pruning_n10_pf1": adj3_by_coin_pruning(fires, min_n=10, max_pf=1.0),
            "3b_by_coin_pruning_n15_pf1": adj3_by_coin_pruning(fires, min_n=15, max_pf=1.0),
            "4_fire_reason_pruning": adj4_fire_reason_pruning(fires, min_n=15, max_pf=1.0),
            "6_clean_only": adj6_clean_only(fires),
            "8_utc_hour_blackout": adj8_utc_hour_blackout(fires),
            "9_liquidity_floor": adj9_liquidity_floor(fires),
        },
        "untestable_offline": UNTESTABLE,
    }
    json.dump(report, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
