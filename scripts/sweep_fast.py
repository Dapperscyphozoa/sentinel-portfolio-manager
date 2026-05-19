"""Fast OOS sweep targeting the 5 RED engines.

Smaller days, smaller universe — designed for sandbox time budget.
Uses OKX direct (no signal-bus dependency).
"""
from __future__ import annotations
import os, sys, json, time, subprocess
from pathlib import Path

ROOT = Path("/home/claude/spm")
sys.path.insert(0, str(ROOT))

# (engine, days, universe_csv|None, configs[(label, env, class_over)])
SWEEPS = [
    # donchian — already swept upstream; one more with widely-different TF + INVERT
    ("donchian", 60, "BTC,ETH,SOL,AVAX,LINK,DOGE", [
        ("baseline_60d",      {},                                              None),
        ("INVERT=1",          {"DC_INVERT": "1"},                              None),
        ("INVERT+SL=1",       {"DC_INVERT": "1", "DC_SL_ATR_MULT": "1.0"},     None),
        ("INVERT+TP=ATR3",    {"DC_INVERT": "1", "DC_TP_ATR_MULT": "3.0"},     None),
    ]),
    # cross_coin_zscore — 30d to fit time budget; smaller universe
    ("cross_coin_zscore", 30, "ETH,SOL,BNB,AVAX,LINK,DOGE", [
        ("baseline",          {},                                              None),
        ("Z=1.5",             {"CCZ_Z_THRESHOLD": "1.5"},                      None),
        ("Z=2.5",             {"CCZ_Z_THRESHOLD": "2.5"},                      None),
        ("Z=3.0_LB=120",      {"CCZ_Z_THRESHOLD": "3.0", "CCZ_LOOKBACK_BARS": "120"}, None),
        ("RR=3:1",            {"CCZ_TP_PCT": "0.024"},                         None),
    ]),
    # e08_dip3d7_td_4h — focus on drop threshold (the known sweet spot)
    ("e08_dip3d7_td_4h", 90, "BTC,ETH,SOL,AVAX,LINK,DOGE,ARB,APT,DOT,SUI", [
        ("baseline_drop=0.07", {},                                             {"_DROP_PCT": 0.07}),
        ("drop=0.15",          {},                                             {"_DROP_PCT": 0.15}),
        ("drop=0.20",          {},                                             {"_DROP_PCT": 0.20, "_HOLD_BARS": 24}),
        ("drop=0.25",          {},                                             {"_DROP_PCT": 0.25, "_HOLD_BARS": 24}),
    ]),
    # e17_bb_fade_bt_4h
    ("e17_bb_fade_bt_4h", 90, "BTC,ETH,SOL,AVAX,LINK,DOGE,ARB,APT,DOT,SUI", [
        ("baseline_BB20_2.0",  {},                                             None),
        ("BB14_2.0",           {},                                             {"_BB_PERIOD": 14}),
        ("BB20_2.5",           {},                                             {"_BB_STD": 2.5}),
        ("BB30_2.0",           {},                                             {"_BB_PERIOD": 30}),
    ]),
    # e08_dip3d10_td_1d
    ("e08_dip3d10_td_1d", 180, "BTC,ETH,SOL,AVAX,LINK,DOGE,ARB,APT,DOT,SUI", [
        ("baseline_drop=0.10", {},                                             {"_DROP_PCT": 0.10}),
        ("drop=0.15",          {},                                             {"_DROP_PCT": 0.15}),
        ("drop=0.20",          {},                                             {"_DROP_PCT": 0.20}),
        ("drop=0.25_hold=5",   {},                                             {"_DROP_PCT": 0.25, "_HOLD_BARS": 5}),
    ]),
]


def run_one(engine: str, days: int, universe: str, env: dict, class_over: dict) -> dict:
    """Run backtest with optional class-attr override (via preload shim)."""
    py = sys.executable
    preload_parts = ["import sys", f"sys.path.insert(0, {repr(str(ROOT))})"]
    if class_over:
        # Find the module
        try:
            from strategy_runner import runner as _r
            _r._load_registered()
            cls = next((c for c in _r.REGISTRY if c.NAME == engine), None)
            if cls is None:
                return {"error": f"strategy {engine} not registered"}
            module = cls.__module__
        except Exception as e:
            return {"error": f"import failed: {e}"}
        preload_parts += [
            f"import {module} as M",
            "_target=None",
            f"_target = next((getattr(M,a) for a in dir(M) if hasattr(getattr(M,a,None),'NAME') and getattr(M,a).NAME == {repr(engine)}), None)",
        ]
        for k, v in class_over.items():
            preload_parts.append(f"_target.{k} = {v!r}")
    preload_parts += [
        "import runpy",
        f"sys.argv = ['backtest_harness','--strategy',{repr(engine)},'--days',{repr(str(days))},'--universe',{repr(universe)}]",
        f"runpy.run_path({repr(str(ROOT / 'scripts/backtest_harness.py'))}, run_name='__main__')",
    ]
    cmd_env = dict(os.environ)
    cmd_env["BACKTEST_DATA_VENUE"] = "okx"
    cmd_env.update(env)
    try:
        r = subprocess.run([py, "-c", "; ".join(preload_parts)], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=420, env=cmd_env)
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    if r.returncode != 0:
        return {"error": f"rc={r.returncode}", "stderr": r.stderr[-400:]}
    out = r.stdout.strip()
    # Find last JSON
    for start in range(len(out) - 1, -1, -1):
        if out[start] == "{":
            try:
                return json.loads(out[start:])
            except Exception:
                continue
    return {"error": "no_json", "out": out[-300:]}


def cell(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict): return default
        cur = cur.get(k, default)
    return cur


def fmt(p):
    try:
        if p == float("inf"): return "inf"
        return f"{float(p):.2f}"
    except: return str(p)


def main():
    rows = []
    for engine, days, univ, configs in SWEEPS:
        print(f"\n{'='*78}")
        print(f"  {engine}  ({days}d, U={len(univ.split(','))})")
        print(f"{'='*78}")
        for label, env, cls in configs:
            t0 = time.time()
            res = run_one(engine, days, univ, env or {}, cls)
            elapsed = time.time() - t0
            if "error" in res:
                print(f"  {label:24}  ERROR {res['error']:30}  ({elapsed:.0f}s)")
                rows.append({"engine":engine,"label":label,"error":res["error"]})
                continue
            n = cell(res, "all_trades", "n", default=0)
            wr = cell(res, "all_trades", "wr", default=0)
            pf = cell(res, "all_trades", "pf", default=0)
            oos_n = cell(res, "oos", "n", default=0)
            oos_pf = cell(res, "oos", "pf", default=0)
            print(f"  {label:24}  n={n:>4}  WR={(wr*100 if wr<2 else wr):5.1f}%  "
                  f"PF={fmt(pf):>6}  OOS(n={oos_n:>3}) PF={fmt(oos_pf):>6}  ({elapsed:.0f}s)")
            rows.append({"engine":engine,"label":label,"n":n,"wr":wr,"pf":pf,
                         "oos_n":oos_n,"oos_pf":oos_pf})
    # Save
    out = Path("/tmp/sweep_fast.json")
    out.write_text(json.dumps(rows, indent=2, default=str))
    print(f"\nWrote {out}")
    # Winners
    print("\n" + "="*78)
    print("  WINNERS (PF≥1.4 AND OOS≥1.0 AND n≥20)")
    print("="*78)
    winners = [r for r in rows if "error" not in r and isinstance(r.get("pf"),(int,float))
               and r["pf"]>=1.4 and isinstance(r.get("oos_pf"),(int,float)) and r["oos_pf"]>=1.0
               and r["n"]>=20]
    if not winners:
        print("  NONE")
    else:
        for w in winners:
            print(f"  {w['engine']:25} {w['label']:24}  PF={fmt(w['pf'])} OOS={fmt(w['oos_pf'])} n={w['n']}")


if __name__ == "__main__":
    main()
