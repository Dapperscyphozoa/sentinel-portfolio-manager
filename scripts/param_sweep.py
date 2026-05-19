"""
scripts/param_sweep.py — OOS parameter sweep for 5 RED engines.

For each (engine, param_grid), patch class-attr or env-var, run honest backtest
via the existing backtest_harness, parse the result, compare to baseline.

Output: /tmp/sweep_results/<engine>_sweep.json + console summary
"""
from __future__ import annotations
import os, sys, json, re, time, subprocess, itertools
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = Path("/tmp/sweep_results")
OUT_DIR.mkdir(exist_ok=True)

BUS = ""


def run_backtest(strategy: str, days: int = 90, env: dict | None = None,
                 class_overrides: dict | None = None, universe: str = None) -> dict:
    """Run backtest_harness for a strategy; return parsed dict.

    `class_overrides` patches class attrs by monkey-patching before harness
    imports the strategy. Implemented via a wrapper script + sys.argv.
    """
    # If class overrides needed, write a tiny pre-loader and invoke via -c
    if class_overrides:
        # Build a shim that imports the strategy module, sets attrs, then
        # re-execs backtest_harness as if called directly.
        preload = ["import sys", f"sys.path.insert(0, {repr(str(ROOT))})"]
        # Find which module the strategy lives in
        # All oos engines live in oos_engines; donchian in its own file; uzt_rev separate.
        try:
            from strategy_runner import runner as _r
            _r._load_registered()
            cls = next((c for c in _r.REGISTRY if c.NAME == strategy), None)
            if cls is None:
                return {"error": f"strategy {strategy} not registered"}
            module = cls.__module__
        except Exception as e:
            return {"error": f"import failed: {e}"}
        preload += [f"import {module} as M",
                    f"_cls = next(c for c in dir(M) if hasattr(getattr(M,c,None),'NAME') and getattr(M,c).NAME == {repr(strategy)})",
                    "_cls = getattr(M, _cls)"]
        for k, v in class_overrides.items():
            preload.append(f"_cls.{k} = {v!r}")
        preload.append("import runpy")
        preload.append("sys.argv = ['backtest_harness', '--strategy', " + repr(strategy) +
                       ", '--days', " + repr(str(days)) +
                       (", '--universe', " + repr(universe) if universe else "") +
                       ", '--bus', " + repr(BUS) + "]")
        preload.append(f"runpy.run_path({repr(str(ROOT / 'scripts/backtest_harness.py'))}, run_name='__main__')")
        cmd = [sys.executable, "-c", "; ".join(preload)]
    else:
        cmd = [sys.executable, str(ROOT / "scripts/backtest_harness.py"),
               "--strategy", strategy, "--days", str(days), "--bus", BUS]
        if universe:
            cmd += ["--universe", universe]

    cmd_env = dict(os.environ)
    if env:
        cmd_env.update(env)

    try:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                           timeout=600, env=cmd_env)
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}

    if r.returncode != 0:
        return {"error": "returncode " + str(r.returncode), "stderr": r.stderr[-500:]}

    # Parse JSON block from stdout (harness emits at end)
    out = r.stdout.strip()
    # Find last { ... } block
    last_brace = out.rfind("{")
    if last_brace < 0:
        return {"error": "no json", "stdout": out[-500:]}
    try:
        # Try parse from last { to end
        for start in range(last_brace, -1, -1):
            if out[start] == "{":
                try:
                    return json.loads(out[start:])
                except Exception:
                    continue
        return {"error": "json parse failed"}
    except Exception as e:
        return {"error": f"parse: {e}"}


def cell(d: dict, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
    return cur


def summarize(strategy: str, label: str, result: dict) -> dict:
    """Pull WR, PF, n from harness JSON."""
    all_t = cell(result, "all_trades") or cell(result, "all") or {}
    n = cell(all_t, "n", default=0)
    wr = cell(all_t, "wr", default=0)
    pf = cell(all_t, "pf", default=0)
    oos = cell(result, "walk_forward", "oos") or {}
    oos_n = cell(oos, "n", default=0)
    oos_pf = cell(oos, "pf", default=0)
    return {"strategy": strategy, "label": label, "n": n, "wr": wr, "pf": pf,
            "oos_n": oos_n, "oos_pf": oos_pf, "raw": result}


def fmt_pf(p):
    try:
        if p == float("inf"): return "inf"
        return f"{p:.2f}"
    except Exception:
        return str(p)


# ============================================================================
#  Sweep grids — small, focused, sandbox-time-aware
# ============================================================================

GRIDS = {
    # donchian — only env-driven engine. Try inversion + EMA filter off + tighter exits.
    "donchian": [
        ("baseline", {}, None),
        ("INVERT=1", {"DC_INVERT": "1"}, None),
        ("EMA_FILTER=0", {"DC_EMA_FILTER": "0"}, None),
        ("SL=1.0", {"DC_SL_ATR_MULT": "1.0"}, None),
        ("INVERT+EMA=0", {"DC_INVERT": "1", "DC_EMA_FILTER": "0"}, None),
        ("N_ENTRY=40", {"DC_N_ENTRY": "40", "DC_N_EXIT": "20"}, None),
    ],
    # cross_coin_zscore — KILLED 2026-05-19, see SPEC §4
    "e17_bb_fade_bt_4h": [
        ("baseline", {}, None),
        ("BB_PERIOD=14", None, {"_BB_PERIOD": 14}),
        ("BB_PERIOD=30", None, {"_BB_PERIOD": 30}),
        ("BB_STD=1.8", None, {"_BB_STD": 1.8}),
        ("BB_STD=2.5", None, {"_BB_STD": 2.5}),
        ("HOLD=12", None, {"_HOLD_BARS": 12}),
        ("BB14_STD2.5", None, {"_BB_PERIOD": 14, "_BB_STD": 2.5}),
    ],
    "e08_dip3d7_td_4h": [
        # Prior code-comment sweep showed PFs: drop=5→0.82, 7→0.83, 10→0.72,
        # 12→0.58, 15→0.85, 20→2.66 — confirm with NEW 90d window
        ("baseline (drop=0.07)", None, {"_DROP_PCT": 0.07}),
        ("drop=0.15", None, {"_DROP_PCT": 0.15}),
        ("drop=0.20", None, {"_DROP_PCT": 0.20}),
        ("drop=0.25", None, {"_DROP_PCT": 0.25}),
        ("drop=0.20 hold=24", None, {"_DROP_PCT": 0.20, "_HOLD_BARS": 24}),
    ],
    "e08_dip3d10_td_1d": [
        # baseline n=8 too small. Sweep DROP, HOLD
        ("baseline", {}, None),
        ("drop=0.05", None, {"_DROP_PCT": 0.05}),
        ("drop=0.07", None, {"_DROP_PCT": 0.07}),
        ("drop=0.15", None, {"_DROP_PCT": 0.15}),
        ("drop=0.20", None, {"_DROP_PCT": 0.20}),
        ("hold=5", None, {"_HOLD_BARS": 5}),
    ],
}


def main():
    summary = []
    for engine, configs in GRIDS.items():
        print(f"\n{'='*72}\n  {engine}\n{'='*72}")
        for label, env, cls_over in configs:
            t0 = time.time()
            res = run_backtest(engine, days=90, env=env, class_overrides=cls_over)
            elapsed = time.time() - t0
            if "error" in res:
                print(f"  {label:24}  ERROR {res['error']:30}  ({elapsed:.1f}s)")
                summary.append({"engine": engine, "label": label, "error": res["error"]})
                continue
            s = summarize(engine, label, res)
            print(f"  {label:24}  n={s['n']:>4}  WR={s['wr']:>5.1f}%  "
                  f"PF={fmt_pf(s['pf']):>6}  OOS_PF={fmt_pf(s['oos_pf']):>6}  ({elapsed:.1f}s)")
            summary.append({"engine": engine, "label": label, "n": s["n"],
                            "wr": s["wr"], "pf": s["pf"], "oos_pf": s["oos_pf"]})

    # Save
    out_path = OUT_DIR / f"sweep_summary_{int(time.time())}.json"
    out_path.write_text(json.dumps(summary, indent=2, default=str))
    print(f"\nWrote: {out_path}")

    # Final winners table
    print(f"\n{'='*72}")
    print("  WINNERS (PF >= 1.4 AND OOS PF >= 1.0)")
    print(f"{'='*72}")
    winners = [s for s in summary if "error" not in s and isinstance(s.get("pf"),(int,float))
               and s["pf"] >= 1.4 and isinstance(s.get("oos_pf"),(int,float)) and s["oos_pf"] >= 1.0
               and s["n"] >= 30]
    if not winners:
        print("  NONE — no parameter combo achieved GREEN gate (PF≥1.4 + OOS≥1.0 + n≥30)")
    else:
        for w in winners:
            print(f"  {w['engine']:25} {w['label']:24}  PF={fmt_pf(w['pf'])}  OOS={fmt_pf(w['oos_pf'])}  n={w['n']}")


if __name__ == "__main__":
    main()
