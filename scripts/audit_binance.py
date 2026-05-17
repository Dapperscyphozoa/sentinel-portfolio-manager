#!/usr/bin/env python3
"""Council-required: re-audit RED engines on BINANCE data (via signal-bus) 
to remove OKX-venue confounding."""
import os, subprocess, json, sys, concurrent.futures, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# All 1d engines (180d signal-bus depth available)
ENGINES_1D = [
    ("e01_zfade3s_tu_1d",   10.05),
    ("e07_zfade2s_tu_1d",   2.12),
    ("e08_dip3d10_td_1d",   1.93),  # was RED on OKX (0.58) — re-test on Binance
    ("e09_pump3d10_td_1d",  1.87),
    ("e16_bb_fade_hv_1d",   1.47),
    ("e17_bb_fade_bt_1d",   1.41),
    ("ict_confluence_1d",   1.21),
]

def run_one(name, claimed):
    env = {**os.environ, "BACKTEST_DATA_VENUE": "signal_bus"}
    t0 = time.time()
    cmd = [sys.executable, os.path.join(ROOT, "scripts/backtest_harness.py"),
           "--strategy", name, "--days", "180"]
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=1800, env=env)
    out = r.stdout.strip()
    depth, end = 0, -1
    for i in range(len(out)-1,-1,-1):
        if out[i]=='}':
            if depth==0: end=i
            depth+=1
        elif out[i]=='{':
            depth-=1
            if depth==0:
                j = json.loads(out[i:end+1])
                return {"name":name,"claimed":claimed,
                        "n":j.get("all",{}).get("n",0),
                        "wr":j.get("all",{}).get("wr",0),
                        "pf":j.get("all",{}).get("pf",0),
                        "is_pf":j.get("is",{}).get("pf",0),
                        "oos_pf":j.get("oos",{}).get("pf",0),
                        "sec":time.time()-t0}
    return {"name":name,"error":"parse_fail","stderr":r.stderr[-400:]}

print("Re-audit on Binance data (via signal-bus) — 7 × 1d engines, 180d window\n")
with concurrent.futures.ProcessPoolExecutor(max_workers=4) as ex:
    futures = {ex.submit(run_one, n, p): n for n, p in ENGINES_1D}
    results = [f.result() for f in concurrent.futures.as_completed(futures)]

results.sort(key=lambda r: -(r.get("pf",0)))
print(f'{"engine":<28} {"claim":>6} {"BINANCE":>8} {"n":>4} {"WR%":>5} {"IS-PF":>6} {"OOS-PF":>7} verdict')
print("-"*88)
# Load OKX prior result for comparison
import glob
prior = {}
for fn in sorted(glob.glob(os.path.join(ROOT,"backtests/HONEST_AUDIT_*.json"))):
    d = json.load(open(fn))
    if d.get("venue")=="okx":
        for r in d.get("engines",[]):
            prior[r["name"]] = r.get("honest_pf", r.get("pf",0))

for r in results:
    if "error" in r:
        print(f'  {r["name"]:<28} ERROR: {r["error"]}')
        continue
    okx = prior.get(r["name"], None)
    diff = ""
    if okx is not None:
        delta = r["pf"] - okx
        diff = f"  (Δ vs OKX: {okx:+.2f}→{r['pf']:+.2f} = {delta:+.2f})"
    verdict = ("RED" if r["pf"]<1.0 else "YELLOW" if r["pf"]<1.4 else "GREEN")
    print(f'  {r["name"]:<28} {r["claimed"]:>6.2f} {r["pf"]:>8.2f} {r["n"]:>4} {r["wr"]*100:>5.0f} '
          f'{r["is_pf"]:>6.2f} {r["oos_pf"]:>7.2f} {verdict}{diff}')

out_path = os.path.join(ROOT, "backtests", f"BINANCE_AUDIT_{time.strftime('%Y%m%d_%H%M')}.json")
json.dump({"venue":"binance_via_signal_bus","days":180,"engines":results}, open(out_path,"w"), indent=2)
print(f"\nSaved: {out_path}")
