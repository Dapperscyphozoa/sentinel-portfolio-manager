"""Session 1.5 gate.

Runs honest backtest for: vsq, fd1, lh1, range_fade.
Writes STRATEGY_GATES.md with GREEN/YELLOW/RED status.

Requires deployed signal-bus OR Binance REST (this script uses the latter via
backtest_harness's binance_klines fallback).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime


HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, ".."))


GATE_STRATEGIES = ["vsq", "fd1", "lh1", "range_fade"]


def classify(pf: float, oos_pf: float) -> str:
    if pf < 1.0:
        return "RED"
    if pf >= 1.4 and oos_pf >= 1.0:
        return "GREEN"
    return "YELLOW"


def run_one(strategy: str, days: int = 90) -> dict:
    cmd = [sys.executable, os.path.join(HERE, "backtest_harness.py"),
           "--strategy", strategy, "--days", str(days)]
    print(f"\n=== {strategy} ===")
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=3600)
    # parse last JSON object from stdout
    out = r.stdout.strip().splitlines()
    j: dict = {}
    buf = []
    for line in out:
        buf.append(line)
        try:
            j = json.loads("\n".join(buf))
        except Exception:
            continue
    if not j:
        print(r.stdout[-2000:])
        print(r.stderr[-2000:])
        return {"strategy": strategy, "status": "ERROR", "msg": "no json output"}
    pf = j.get("all", {}).get("pf", 0.0)
    oos_pf = j.get("oos", {}).get("pf", 0.0)
    return {
        "strategy": strategy,
        "pf": pf,
        "oos_pf": oos_pf,
        "wr": j.get("all", {}).get("wr", 0.0),
        "n": j.get("all", {}).get("n", 0),
        "status": classify(pf, oos_pf),
        "report": j.get("report"),
    }


def main():
    results = []
    for s in GATE_STRATEGIES:
        try:
            results.append(run_one(s))
        except Exception as e:
            results.append({"strategy": s, "status": "ERROR", "msg": str(e)})

    out_path = os.path.join(ROOT, "STRATEGY_GATES.md")
    with open(out_path, "w") as f:
        f.write(f"# Strategy Gates — honest backtest (Session 1.5)\n\n")
        f.write(f"Generated: {datetime.utcnow().isoformat()}Z\n\n")
        f.write("| Strategy | n | WR | PF | OOS PF | Status |\n")
        f.write("|---|---|---|---|---|---|\n")
        for r in results:
            f.write(f"| {r['strategy']} | {r.get('n', '-')} | {r.get('wr', 0)*100:.1f}% | "
                    f"{r.get('pf', 0):.2f} | {r.get('oos_pf', 0):.2f} | **{r['status']}** |\n")
        f.write("\n## Gate rules\n\n")
        f.write("- **GREEN**: PF ≥ 1.4 AND OOS PF ≥ 1.0 → port as planned\n")
        f.write("- **YELLOW**: 1.0 ≤ PF < 1.4 OR OOS PF < 1.0 → port but flag `audit_status: PROVISIONAL`, no live capital\n")
        f.write("- **RED**: PF < 1.0 → DO NOT port; add to SPEC §4 Dead Engine Registry\n")
    print(json.dumps(results, indent=2))
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
