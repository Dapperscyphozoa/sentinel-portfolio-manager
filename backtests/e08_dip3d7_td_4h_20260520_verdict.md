# e08_dip3d7_td_4h — PERMANENT DEATH CERTIFICATE — 2026-05-20

## Disposition: BURIED. Do not resurrect under any parameter combination.

## Honest validation completed
- Data: spm-sentinel-pm (OKX SWAP perps, 4h)
- Window: 365d, 60 symbols
- Method: walk-forward 50/50 split, no module-global caches, isolated TRAIN/TEST

## Phase 1: parameter sweep (252 combos)
- Combos tested: drop ∈ {0.05,0.06,0.07,0.08,0.10,0.12} × hold ∈ {2,3,4,6,8,12} × 7 (SL,TP) pairs
- Combos passing GREEN gate (OOS PF ≥ 1.0): **0 of 252**
- Best OOS PF achieved: 0.94 (drop=0.05 hold=4 sl=0.05 tp=0.15)
- Live config (drop=0.07 hold=6 sl=tp=0.10): OOS PF 0.32 — strategy converged to expected negative EV
- Rank 6 outlier (drop=0.12 hold=12 sl=0.10 tp=0.20) IS PF 11.63 → OOS PF 0.85 = textbook overfit blowup, 13.6× IS/OOS divergence

## Phase 2: walk-forward universe selection (anti-cherry-pick)
- Top 4 combos × 4 PF thresholds × per-symbol TRAIN/TEST split
- Best honest TEST result: rank4 combo at thr=1.1, |U|=16 coins, TEST PF **0.98**, tot −4.9%
- Train-selected universe hit-rate on TEST: 7/16 (43.75%) — indistinguishable from random
- 20260517 backtest's "OOS PF 2.01 on n=191" was sampling noise; does not survive larger-sample replication

## Conclusion
The 3-day-drop mean-reversion thesis is structurally broken on 4h crypto perps. No parameter combination produces positive expectancy. The per-coin variation in PF is noise, not signal. The 4h timeframe does not capture genuine mean-reversion in TREND_DOWN regime — by the time a 5%+ 3-bar drop registers, the downtrend continuation is the dominant prior, not reversion.

## Family disposition
- e08_dip3d10_td_1d (sibling, 1d timeframe): keep paper at cap_frac 0.02 per current registry
- e08_dip3d7_td_4h: ARCHIVED permanently. Source removed from oos_engines.py 2026-05-19, registry entry removed 2026-05-19, dashboard filter added 2026-05-20 (commit d5a02ee), this verdict locks the family's 4h variant.

## Audit trail
- /tmp/e08_sweep_result.json — 252-combo sweep results
- /tmp/e08_universe_select.json — walk-forward universe-selection results
- See sweep2.py + universe_select.py in /home/claude/e08bt/ for methodology
