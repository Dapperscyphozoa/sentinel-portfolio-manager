"""SESSION 4 GATE — Chainlink Aggregator Validation.

This script is the kill switch for cl_predictor + endgame. Both strategies
MUST NOT trade live until this validation passes:

  Hard acceptance criteria:
    n >= 100,000 paired samples
    median |diff_bps| <= 5.0
    p95    |diff_bps| <= 15.0
    p99    |diff_bps| <= 30.0
    no rolling-1h window with median > 10bps

If criteria not met:
  - Keep collecting until n=100k; if median plateaus above 5bps, the
    structural-edge hypothesis is FALSIFIED and both strategies die.
  - In that case: only maker_quote + cross_asset + reflexivity_emitter ship.

Data source: poly_signal_bus must run for ≥7 days in non-trading mode to
accumulate ≥100k samples in poly_cl_validation. Then run this script.

Usage:
  python -m scripts.cl_aggregator_validate [--min-n 100000] [--report]
  python -m scripts.cl_aggregator_validate --bucket-by-hour

Output: PASS/FAIL with full distribution stats, plus optional CSV dump.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from typing import Optional

# Add repo root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from common.poly_persistence import connect_poly


HARD_LIMITS = {
    "min_n": 100_000,
    "median_bps_max": 5.0,
    "p95_bps_max": 15.0,
    "p99_bps_max": 30.0,
    "rolling_hour_median_bps_max": 10.0,
}


def percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return float("inf")
    n = len(sorted_vals)
    k = max(0, min(n - 1, int(p * (n - 1))))
    return sorted_vals[k]


def collect_samples(asset: Optional[str] = None,
                     since_ts: Optional[float] = None,
                     min_venues: int = 5) -> list[dict]:
    conn = connect_poly()
    try:
        sql = "SELECT ts, asset, cl_actual, cl_predicted, diff_bps, n_venues" \
              " FROM poly_cl_validation WHERE n_venues >= ?"
        params: list = [min_venues]
        if asset:
            sql += " AND asset=?"; params.append(asset)
        if since_ts:
            sql += " AND ts > ?"; params.append(since_ts)
        sql += " ORDER BY ts"
        rows = conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.close()
    return [
        {"ts": r[0], "asset": r[1], "actual": r[2], "predicted": r[3],
         "diff_bps": r[4], "n_venues": r[5]}
        for r in rows
    ]


def evaluate(samples: list[dict]) -> dict:
    if not samples:
        return {"pass": False, "reason": "no samples", "n": 0}

    abs_diffs = sorted(abs(s["diff_bps"]) for s in samples)
    n = len(abs_diffs)
    median = abs_diffs[n // 2]
    p95 = percentile(abs_diffs, 0.95)
    p99 = percentile(abs_diffs, 0.99)
    mean = sum(abs_diffs) / n
    signed = [s["diff_bps"] for s in samples]
    bias = sum(signed) / n

    # Rolling-1h windows
    rolling_violations = 0
    if len(samples) >= 100:
        samples_sorted = sorted(samples, key=lambda s: s["ts"])
        window: list[float] = []
        window_ts: list[float] = []
        for s in samples_sorted:
            window.append(abs(s["diff_bps"]))
            window_ts.append(s["ts"])
            cutoff = s["ts"] - 3600
            while window_ts and window_ts[0] < cutoff:
                window_ts.pop(0); window.pop(0)
            if len(window) >= 50:
                wmed = sorted(window)[len(window) // 2]
                if wmed > HARD_LIMITS["rolling_hour_median_bps_max"]:
                    rolling_violations += 1

    checks = {
        "n": n >= HARD_LIMITS["min_n"],
        "median": median <= HARD_LIMITS["median_bps_max"],
        "p95":    p95 <= HARD_LIMITS["p95_bps_max"],
        "p99":    p99 <= HARD_LIMITS["p99_bps_max"],
        "rolling_hour": rolling_violations == 0,
    }
    return {
        "pass": all(checks.values()),
        "checks": checks,
        "n": n,
        "median_bps": median,
        "mean_abs_bps": mean,
        "p95_bps": p95,
        "p99_bps": p99,
        "max_bps": abs_diffs[-1] if abs_diffs else None,
        "signed_bias_bps": bias,
        "rolling_hour_violations": rolling_violations,
        "limits": HARD_LIMITS,
    }


def per_asset_summary(samples: list[dict]) -> dict:
    by_asset: dict[str, list[dict]] = {}
    for s in samples:
        by_asset.setdefault(s["asset"], []).append(s)
    return {a: evaluate(ss) for a, ss in by_asset.items()}


def by_venue_count_buckets(samples: list[dict]) -> dict:
    """Distribution conditional on number of venues alive at sample time."""
    buckets: dict[int, list[float]] = {}
    for s in samples:
        buckets.setdefault(s["n_venues"], []).append(abs(s["diff_bps"]))
    out = {}
    for k, vals in sorted(buckets.items()):
        vals.sort()
        n = len(vals)
        out[k] = {
            "n": n,
            "median": vals[n // 2] if n else None,
            "p95": percentile(vals, 0.95),
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", help="filter to single asset (BTC/ETH)")
    parser.add_argument("--since", type=float, help="ts cutoff in unix seconds")
    parser.add_argument("--min-venues", type=int, default=5)
    parser.add_argument("--bucket-by-venues", action="store_true")
    parser.add_argument("--report", action="store_true",
                        help="write CSV to ./cl_validation_report.csv")
    parser.add_argument("--json", action="store_true",
                        help="emit JSON to stdout (for CI consumption)")
    args = parser.parse_args()

    samples = collect_samples(asset=args.asset, since_ts=args.since,
                               min_venues=args.min_venues)
    result = evaluate(samples)

    if not args.asset:
        result["per_asset"] = per_asset_summary(samples)
    if args.bucket_by_venues:
        result["by_venue_count"] = by_venue_count_buckets(samples)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_human(result)

    if args.report:
        _write_csv(samples, "cl_validation_report.csv")
        print("wrote cl_validation_report.csv")

    return 0 if result["pass"] else 1


def _print_human(r: dict) -> None:
    status = "PASS ✓" if r["pass"] else "FAIL ✗"
    print(f"\n=== CL Aggregator Validation: {status} ===")
    print(f"n = {r['n']:,}")
    print(f"median |diff| = {r['median_bps']:.3f} bps   (limit ≤ {r['limits']['median_bps_max']})")
    print(f"mean |diff|   = {r['mean_abs_bps']:.3f} bps")
    print(f"p95  |diff|   = {r['p95_bps']:.3f} bps   (limit ≤ {r['limits']['p95_bps_max']})")
    print(f"p99  |diff|   = {r['p99_bps']:.3f} bps   (limit ≤ {r['limits']['p99_bps_max']})")
    print(f"max  |diff|   = {r.get('max_bps')}")
    print(f"signed bias   = {r['signed_bias_bps']:+.3f} bps")
    print(f"rolling-1h violations = {r['rolling_hour_violations']}")
    print("checks:", json.dumps(r["checks"], indent=2))
    if "per_asset" in r:
        print("\nPer-asset:")
        for a, ar in r["per_asset"].items():
            print(f"  {a}: n={ar['n']:,} median={ar['median_bps']:.2f}bps "
                  f"p95={ar['p95_bps']:.2f}bps pass={ar['pass']}")
    if "by_venue_count" in r:
        print("\nBy venue count:")
        for k, v in r["by_venue_count"].items():
            print(f"  {k} venues: n={v['n']:,} median={v['median']:.2f}bps p95={v['p95']:.2f}bps")
    if not r["pass"]:
        print("\nINTERPRETATION:")
        if r["n"] < r["limits"]["min_n"]:
            print("  Need more samples. Keep bus running, re-run when n >= 100k.")
        if r["median_bps"] > r["limits"]["median_bps_max"]:
            print("  ❌ Median error too high. cl_predictor + endgame edge "
                  "hypothesis is suspect. Either the venue set differs from "
                  "the real DON, the weighting is wrong, or the API is delayed.")
            print("  Next steps:")
            print("    1. Inspect by_venue_count buckets — does median drop at n=7?")
            print("    2. Try volume-weighted median instead of equal weights")
            print("    3. Verify CL_FEED_BTC / CL_FEED_ETH IDs match Chainlink docs")
            print("    4. If median doesn't get under 5bps: KILL cl_predictor + endgame.")


def _write_csv(samples: list[dict], path: str) -> None:
    with open(path, "w") as f:
        f.write("ts,asset,actual,predicted,diff_bps,n_venues\n")
        for s in samples:
            f.write(f"{s['ts']},{s['asset']},{s['actual']},{s['predicted']},"
                    f"{s['diff_bps']},{s['n_venues']}\n")


if __name__ == "__main__":
    sys.exit(main())
