"""Stage 1 honest backtest gate — per sentinel council 2026-05-18.

Three categories of Stage 1 engines, three distinct gating paths:

  CATEGORY A — historical data fully available (council Q2):
    - (none active — cross_coin_zscore killed 2026-05-19, see SPEC §4)
    GATE: 90d walk-forward (60/30 split), n ≥ 150 trades,
          bt_PF ≥ 1.4 AND OOS PF ≥ 1.0 → GREEN, eligible for canary

  CATEGORY B — proxy backtest + live paper required (council Q1, hybrid Q8):
    - hl_cvd_aggressor        (Binance CVD proxy + HL live paper)
    GATE: proxy bt_PF ≥ 1.2 AND live paper n ≥ 30, rolling-PF ≥ 1.5

  CATEGORY C — no proxy possible, live-paper-only:
    - hl_whale_frontrun       (HL leaderboard is unique to HL)
    - hl_vault_predict        (HLP rebalance mechanics unique)
    GATE: n=50 live closures (n=30 for vault), rolling-PF ≥ 1.5 (≥2.0 for vault)

OUTPUTS:
  backtests/stage1_gate_<YYYYMMDD>.md   — verdict per engine
  STAGE1_GATES.md                       — live updated gate status
  Monitor routine consumes STAGE1_GATES.md to auto-promote engines.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from typing import Optional


HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, ".."))
BACKTESTS_DIR = os.path.join(ROOT, "backtests")
GATES_PATH = os.path.join(ROOT, "STAGE1_GATES.md")

os.makedirs(BACKTESTS_DIR, exist_ok=True)


# ───────────────────────────────────────────────────────────────────────
# Engine categorization per council Q1
# ───────────────────────────────────────────────────────────────────────

CATEGORY_A = {     # historical backtest only — engines whose required data
                   # is replayable in HistoricalBus (klines + funding)
    # cross_coin_zscore was the only Cat A entry; KILLED 2026-05-19 (SPEC §4).
    # Category A now empty until a new historical-backtestable engine is added.
}
# REASSIGNED (2026-05-18): liq_cluster_hunt + funding_triangulation moved to Category C.
# Reason: HistoricalBus does not have historical liq feed (Binance forceOrder archive
# is not freely available) or HL hourly funding history. Until the harness is extended,
# these engines can only be gated via live paper accumulation.

CATEGORY_B = {     # proxy backtest + live paper
    "hl_cvd_aggressor": {
        "days": 60, "min_n_bt": 100, "gate_bt_pf": 1.2,
        "gate_live_n": 30, "gate_live_pf": 1.5,
        "proxy_note": "Binance CVD 1s aggregation",
    },
    # hl_depth_shock REMOVED 2026-05-22 (n=9 WR 22% PF 0.32 net -$0.69).
}

CATEGORY_C = {     # live-paper only
    "hl_whale_frontrun": {
        "gate_live_n": 50, "gate_live_pf": 1.5,
        "rationale": "HL leaderboard is unique to HL — no proxy possible",
    },
    "hl_vault_predict": {
        "gate_live_n": 30, "gate_live_pf": 2.0,
        "rationale": "HLP rebalance mechanics unique; tighter PF since rebalances are rare",
    },
    # Reclassified from Category A (no historical liq/funding archive in HistoricalBus)
    "liq_cluster_hunt": {
        "gate_live_n": 40, "gate_live_pf": 1.5,
        "rationale": "Binance forceOrder archive not freely available; live-paper only",
    },
}


# ───────────────────────────────────────────────────────────────────────
# Live-paper closure querying
# ───────────────────────────────────────────────────────────────────────

PM_URL = os.environ.get("PM_URL", "https://spm-pm.onrender.com")
RUNNER_URL = os.environ.get("STRATEGY_RUNNER_URL",
                            "https://spm-strategy-runner.onrender.com")


def fetch_paper_closures(engine: str, limit: int = 2000) -> list[dict]:
    """Fetch paper closures for one engine.

    Paper = extras_json.live is False (the cap_frac=0 engines we're gating).
    """
    url = f"{RUNNER_URL.rstrip('/')}/closures?limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            all_rows = json.loads(r.read())
    except Exception as e:
        print(f"   ✗ fetch closures failed: {e}")
        return []

    out = []
    for row in all_rows:
        if row.get("strategy") != engine:
            continue
        try:
            ex = json.loads(row.get("extras_json", "{}") or "{}")
            if isinstance(ex, dict) and ex.get("live") is False:
                out.append(row)
        except Exception:
            # If extras malformed but engine matches and cap_frac=0 in registry, count it
            out.append(row)
    return out


def compute_paper_stats(closures: list[dict], rolling_n: int = 30) -> dict:
    """Compute rolling-PF and aggregate stats from paper closure list."""
    if not closures:
        return {"n": 0, "wr": 0.0, "pf": 0.0, "rolling_pf": 0.0,
                "rolling_n": 0, "pnl_total": 0.0}

    # Sort by close_ts ascending
    closures_sorted = sorted(closures, key=lambda r: float(r.get("close_ts", 0)))

    pnls = [float(r.get("pnl_usd", 0) or 0) for r in closures_sorted]
    wins = sum(1 for p in pnls if p > 0)
    win_sum = sum(p for p in pnls if p > 0)
    loss_sum = -sum(p for p in pnls if p <= 0)
    pf = win_sum / loss_sum if loss_sum > 0 else (999.0 if win_sum > 0 else 0.0)

    # Rolling on last N
    recent = pnls[-rolling_n:] if len(pnls) > rolling_n else pnls
    r_win = sum(p for p in recent if p > 0)
    r_loss = -sum(p for p in recent if p <= 0)
    r_pf = r_win / r_loss if r_loss > 0 else (999.0 if r_win > 0 else 0.0)

    return {
        "n": len(pnls),
        "wr": wins / len(pnls),
        "pf": pf,
        "rolling_pf": r_pf,
        "rolling_n": len(recent),
        "pnl_total": sum(pnls),
        "max_drawdown_pct": _max_dd_pct(pnls),
    }


def _max_dd_pct(pnls: list[float]) -> float:
    if not pnls:
        return 0.0
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    return max_dd / abs(peak) if peak > 0 else 0.0


# ───────────────────────────────────────────────────────────────────────
# Historical backtest dispatch (delegates to backtest_harness.py)
# ───────────────────────────────────────────────────────────────────────

def run_historical_backtest(strategy: str, days: int) -> Optional[dict]:
    """Dispatch to scripts/backtest_harness.py and parse JSON result."""
    cmd = [sys.executable, os.path.join(HERE, "backtest_harness.py"),
           "--strategy", strategy, "--days", str(days)]
    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "strategy": strategy}

    out_lines = r.stdout.strip().splitlines()
    parsed: dict = {}
    buf = []
    for line in out_lines:
        buf.append(line)
        try:
            parsed = json.loads("\n".join(buf))
        except Exception:
            continue
    if not parsed:
        return {"error": "no_json_output",
                "stdout_tail": r.stdout[-1000:], "stderr_tail": r.stderr[-1000:]}
    return parsed


# ───────────────────────────────────────────────────────────────────────
# Gate logic
# ───────────────────────────────────────────────────────────────────────

def classify_a(bt: dict, min_n: int, gate_pf: float, gate_oos: float) -> str:
    n = bt.get("all", {}).get("n", 0)
    pf = bt.get("all", {}).get("pf", 0.0)
    oos_pf = bt.get("oos", {}).get("pf", 0.0)
    if n < min_n:
        return f"YELLOW_LOW_N (n={n} < {min_n})"
    if pf < 1.0:
        return f"RED (PF={pf:.2f})"
    if pf >= gate_pf and oos_pf >= gate_oos:
        return f"GREEN (PF={pf:.2f}, OOS={oos_pf:.2f})"
    return f"YELLOW (PF={pf:.2f}, OOS={oos_pf:.2f})"


def classify_c(paper: dict, gate_n: int, gate_pf: float) -> str:
    n = paper["n"]
    rpf = paper["rolling_pf"]
    if n < gate_n:
        return f"NEEDS_DATA (n={n}/{gate_n})"
    if rpf >= gate_pf:
        return f"GREEN (n={n}, rolling-PF={rpf:.2f})"
    return f"RED (n={n}, rolling-PF={rpf:.2f} < {gate_pf})"


def classify_b(bt: dict, paper: dict, cfg: dict) -> str:
    bt_pf = bt.get("all", {}).get("pf", 0.0) if bt else 0.0
    bt_oos = bt.get("oos", {}).get("pf", 0.0) if bt else 0.0
    bt_n = bt.get("all", {}).get("n", 0) if bt else 0

    bt_ok = (bt_pf >= cfg["gate_bt_pf"] and bt_oos >= 1.0 and bt_n >= cfg["min_n_bt"])
    paper_ok = (paper["n"] >= cfg["gate_live_n"] and
                paper["rolling_pf"] >= cfg["gate_live_pf"])

    if bt_ok and paper_ok:
        return f"GREEN (bt_PF={bt_pf:.2f}, paper n={paper['n']} rPF={paper['rolling_pf']:.2f})"
    if bt_ok and paper["n"] < cfg["gate_live_n"]:
        return f"NEEDS_LIVE_DATA (bt OK, paper n={paper['n']}/{cfg['gate_live_n']})"
    if not bt_ok and paper_ok:
        return f"YELLOW_BT (paper OK, bt_PF={bt_pf:.2f} bt_OOS={bt_oos:.2f})"
    return f"PENDING (bt_PF={bt_pf:.2f}, paper n={paper['n']} rPF={paper['rolling_pf']:.2f})"


# ───────────────────────────────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-bt", action="store_true",
                    help="Skip historical backtests (only refresh live-paper stats)")
    ap.add_argument("--engine", default="",
                    help="Run only this specific engine")
    args = ap.parse_args()

    results: list[dict] = []

    # ── Category A — historical only ──
    print("\n══════ CATEGORY A — historical backtest ══════")
    for eng, cfg in CATEGORY_A.items():
        if args.engine and args.engine != eng:
            continue
        print(f"\n[A] {eng}")
        if args.skip_bt:
            print("    (skipped per --skip-bt)")
            results.append({"engine": eng, "category": "A", "status": "SKIPPED"})
            continue
        bt = run_historical_backtest(eng, cfg["days"])
        if not bt or "error" in bt:
            err = bt.get("error", "unknown") if bt else "no_response"
            print(f"    ✗ backtest error: {err}")
            results.append({"engine": eng, "category": "A", "status": f"ERROR ({err})", "bt": bt})
            continue
        status = classify_a(bt, cfg["min_n"], cfg["gate_pf"], cfg["gate_oos"])
        print(f"    {status}")
        results.append({
            "engine": eng, "category": "A", "status": status,
            "bt_n": bt.get("all", {}).get("n", 0),
            "bt_pf": bt.get("all", {}).get("pf", 0.0),
            "bt_wr": bt.get("all", {}).get("wr", 0.0),
            "bt_oos_pf": bt.get("oos", {}).get("pf", 0.0),
        })

    # ── Category B — proxy backtest + live paper ──
    print("\n══════ CATEGORY B — proxy backtest + live paper ══════")
    for eng, cfg in CATEGORY_B.items():
        if args.engine and args.engine != eng:
            continue
        print(f"\n[B] {eng}  ({cfg['proxy_note']})")
        # Run historical backtest (uses Binance proxy via HistoricalBus)
        bt = None
        if not args.skip_bt:
            bt = run_historical_backtest(eng, cfg["days"])
            if bt and "error" in bt:
                print(f"    ⚠ proxy backtest error: {bt['error']}")
                bt = None
        # Live paper accumulation
        closures = fetch_paper_closures(eng)
        paper = compute_paper_stats(closures)
        print(f"    paper: n={paper['n']} wr={paper['wr']*100:.1f}% "
              f"rolling-PF={paper['rolling_pf']:.2f} pnl=${paper['pnl_total']:+.2f}")
        if bt:
            print(f"    proxy bt: n={bt.get('all',{}).get('n',0)} "
                  f"PF={bt.get('all',{}).get('pf',0):.2f} "
                  f"OOS={bt.get('oos',{}).get('pf',0):.2f}")
        status = classify_b(bt, paper, cfg)
        print(f"    {status}")
        results.append({
            "engine": eng, "category": "B", "status": status,
            "bt": bt, "paper": paper,
        })

    # ── Category C — live paper only ──
    print("\n══════ CATEGORY C — live paper only ══════")
    for eng, cfg in CATEGORY_C.items():
        if args.engine and args.engine != eng:
            continue
        print(f"\n[C] {eng}  ({cfg['rationale']})")
        closures = fetch_paper_closures(eng)
        paper = compute_paper_stats(closures)
        print(f"    paper: n={paper['n']} wr={paper['wr']*100:.1f}% "
              f"rolling-PF={paper['rolling_pf']:.2f} pnl=${paper['pnl_total']:+.2f}")
        status = classify_c(paper, cfg["gate_live_n"], cfg["gate_live_pf"])
        print(f"    {status}")
        results.append({
            "engine": eng, "category": "C", "status": status,
            "paper": paper,
        })

    # ── Write STAGE1_GATES.md ──
    write_gates_md(results)

    # ── JSON output ──
    print("\n\n══════ JSON SUMMARY ══════")
    print(json.dumps([_simple(r) for r in results], indent=2))


def _simple(r: dict) -> dict:
    out = {k: v for k, v in r.items() if k not in ("bt", "paper")}
    paper = r.get("paper")
    if paper:
        out["paper_n"] = paper["n"]
        out["paper_rolling_pf"] = paper["rolling_pf"]
        out["paper_pnl"] = paper["pnl_total"]
    bt = r.get("bt")
    if bt and isinstance(bt, dict):
        out["bt_pf"] = bt.get("all", {}).get("pf", 0)
        out["bt_n"] = bt.get("all", {}).get("n", 0)
    return out


def write_gates_md(results: list[dict]) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    lines = [
        "# Stage 1 Gates — honest backtest + live paper accumulation",
        "",
        f"Generated: {ts}",
        "",
        "Per sentinel council 2026-05-18: 3 categories of Stage 1 engines, 3 gating paths.",
        "",
        "## Gate rules",
        "",
        "**Category A (historical data available):** 90d walk-forward, n ≥ 150 trades, ",
        "bt_PF ≥ 1.4 AND OOS PF ≥ 1.0 → GREEN, eligible for canary 0.025 cap_frac",
        "",
        "**Category B (HL data + Binance proxy):** proxy bt_PF ≥ 1.2 AND live paper n ≥ 30 ",
        "with rolling-PF ≥ 1.5 → GREEN",
        "",
        "**Category C (HL-unique, live-paper only):** n=30-50 live closures with ",
        "rolling-PF ≥ 1.5-2.0 → GREEN",
        "",
        "**Promotion ladder (post-GREEN):** n=30 @ rolling-PF≥1.5 → canary 0.025 cap_frac · ",
        "n=75 @ rolling-PF≥2.0 → 0.05 · n=150 @ rolling-PF≥1.8 sustained → full registry cap.",
        "",
        "## Current status",
        "",
        "| Engine | Cat | bt_n | bt_PF | paper_n | rolling_PF | Status |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in results:
        eng = r["engine"]
        cat = r["category"]
        bt_n = r.get("bt_n", "—")
        bt_pf = f"{r.get('bt_pf', 0):.2f}" if r.get("bt_pf") else "—"
        paper = r.get("paper", {})
        paper_n = paper.get("n", "—") if paper else "—"
        r_pf = f"{paper.get('rolling_pf', 0):.2f}" if paper else "—"
        if cat == "B" and r.get("bt"):
            bt_data = r["bt"]
            bt_n = bt_data.get("all", {}).get("n", "—") if isinstance(bt_data, dict) else "—"
            bt_pf = f"{bt_data.get('all', {}).get('pf', 0):.2f}" if isinstance(bt_data, dict) else "—"
        lines.append(f"| {eng} | {cat} | {bt_n} | {bt_pf} | {paper_n} | {r_pf} | **{r['status']}** |")

    lines.extend([
        "",
        "## Next actions (auto-promote when gates pass)",
        "",
        "Monitor routine `auto_4loss_demote.py` extended to handle Stage 1 gate progression:",
        "- Each cycle, re-evaluate this gate.",
        "- If engine passes GREEN AND not already canary: promote to cap_frac 0.025 via Render API.",
        "- If engine passes 0.05 ladder: bump cap_frac.",
        "- If engine fails (RED): demote + sentinel audit.",
        "",
    ])

    with open(GATES_PATH, "w") as f:
        f.write("\n".join(lines))
    print(f"\nWrote {GATES_PATH}")


if __name__ == "__main__":
    main()
