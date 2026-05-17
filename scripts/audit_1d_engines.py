#!/usr/bin/env python3
"""Run honest backtest for every 1d engine in parallel, write summary table."""
import os, subprocess, json, sys, concurrent.futures, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Current 1d engines (from /pm/engines)
ENGINES_1D = [
    ("e08_dip3d10_td_1d",   1.93),
    ("e08_dip3d7_td_4h",    1.5),
    ("e07_zfade2s_tu_4h",   2.5),
    ("e01_zfade3s_tu_4h",   5.0),
    ("e17_bb_fade_bt_4h",   1.3),
    ("donchian",            0.0),  # also re-audit on Binance for completeness
]

DAYS = int(os.environ.get("DAYS", 180))


def run_one(name, claimed_pf):
    t0 = time.time()
    env = {**os.environ, "BACKTEST_DATA_VENUE": "binance"}
    cmd = [sys.executable, os.path.join(ROOT, "scripts/backtest_harness.py"),
           "--strategy", name, "--days", str(DAYS)]
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           timeout=1800, env=env)
        # Parse last JSON
        out = r.stdout.strip()
        # Find JSON object at end
        depth, end = 0, -1
        for i in range(len(out) - 1, -1, -1):
            if out[i] == '}':
                if depth == 0: end = i
                depth += 1
            elif out[i] == '{':
                depth -= 1
                if depth == 0:
                    j = json.loads(out[i:end+1])
                    return {"name": name, "claimed_pf": claimed_pf,
                            "n": j.get("all", {}).get("n", 0),
                            "wr": j.get("all", {}).get("wr", 0),
                            "honest_pf": j.get("all", {}).get("pf", 0),
                            "expectancy": j.get("all", {}).get("expectancy", 0),
                            "is_pf": j.get("is", {}).get("pf", 0),
                            "is_n": j.get("is", {}).get("n", 0),
                            "oos_pf": j.get("oos", {}).get("pf", 0),
                            "oos_n": j.get("oos", {}).get("n", 0),
                            "sec": time.time() - t0}
        return {"name": name, "error": "no_json", "stderr": r.stderr[-500:],
                "stdout_tail": out[-500:]}
    except subprocess.TimeoutExpired:
        return {"name": name, "error": "timeout"}
    except Exception as e:
        return {"name": name, "error": str(e)}


print(f"Backtesting {len(ENGINES_1D)} engines × {DAYS}d (OKX data) in parallel...\n")
with concurrent.futures.ProcessPoolExecutor(max_workers=4) as ex:
    futures = {ex.submit(run_one, n, p): n for n, p in ENGINES_1D}
    results = []
    for f in concurrent.futures.as_completed(futures):
        r = f.result()
        results.append(r)
        if "error" in r:
            print(f"  ✗ {r['name']}: {r['error']}")
        else:
            verdict = ("RED" if r["honest_pf"] < 1.0 else
                       "YELLOW" if r["honest_pf"] < 1.4 else "GREEN")
            ratio = (r["honest_pf"] / r["claimed_pf"]) if r["claimed_pf"] else 0
            print(f"  ✓ {r['name']:<28} claim={r['claimed_pf']:>5.2f}  "
                  f"honest={r['honest_pf']:>5.2f}  n={r['n']:>3} WR={r['wr']*100:>4.0f}%  "
                  f"ratio={ratio:.2f}  [{verdict}]  ({r['sec']:.0f}s)")

# Summary
results_ok = [r for r in results if "error" not in r]
results_ok.sort(key=lambda x: -x["honest_pf"])

print("\n" + "═"*100)
print(f"{'engine':<28} {'claim':>6} {'honest':>7} {'ratio':>6} {'n':>4} {'WR%':>5} "
      f"{'IS-PF':>6} {'OOS-PF':>7} {'verdict':<8}")
print("─"*100)
for r in results_ok:
    ratio = (r["honest_pf"] / r["claimed_pf"]) if r["claimed_pf"] else 0
    verdict = ("RED" if r["honest_pf"] < 1.0 else
               "YELLOW" if r["honest_pf"] < 1.4 else
               "GREEN" if r["oos_pf"] >= 1.0 else "YELLOW")
    print(f"{r['name']:<28} {r['claimed_pf']:>6.2f} {r['honest_pf']:>7.2f} "
          f"{ratio:>6.2f} {r['n']:>4} {r['wr']*100:>5.0f} "
          f"{r['is_pf']:>6.2f} {r['oos_pf']:>7.2f} {verdict:<8}")

print("\nLegend: claim=bt_pf from registry. honest=full-period PF. ratio=honest/claim.")
print("        IS=in-sample (first half) PF, OOS=out-of-sample (second half).")
print("        RED=honest<1.0 (no edge). YELLOW=1.0-1.4 (weak). GREEN=>=1.4 & OOS>=1.0.")

# Persist
out_path = os.path.join(ROOT, "backtests", f"HONEST_AUDIT_{time.strftime('%Y%m%d_%H%M')}.json")
os.makedirs(os.path.dirname(out_path), exist_ok=True)
json.dump({"days": DAYS, "venue": "okx", "engines": results}, open(out_path, "w"), indent=2)
print(f"\nFull results: {out_path}")
