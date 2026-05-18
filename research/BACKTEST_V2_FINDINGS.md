# OOS engine backtest v2 — 2026-05-18

## What was tested
17 engines from the live registry, replayed against historical 1d/4h/1h candles pulled from signal_bus. Production strategy code imported unmodified.

## Key findings (full list in `backtest_v2_results_2026-05-18_pre_patch.txt`)

**Real winners (n≥25, walk-forward stable or improving):**
- e16_bb_fade_hv_1d: PF 2.70, n=37, +$112 over 200d (HIGH_VOL regime only)
- ict_confluence_4h: PF 2.67, n=74, +$118
- ict_confluence_1d: PF 2.10, n=25, +$130

**Confirmed losers (walk-forward both halves <1.0):**
- e17_bb_fade_bt_1d (PRE-FIX): PF 0.59, n=83, -$186 — losses ALL from trend regimes
- e17_bb_fade_bt_4h: PF 0.56, n=70, -$82

**Regime-cyclical (DO NOT halt — recovering):**
- e08_dip3d10_td_1d: first-half PF 0.03, second-half PF 2.50
- e07_zfade2s_tu_4h: first-half PF 0.32, second-half PF 1.28
- e07_zfade2s_tu_1d: first-half PF 0.59, second-half PF 0.95

**Untestable (data infra gaps):**
cascade_sniper_hl, hlp_fade, hl_settle_5m, liq_cascade, cex_dex_arb, fmom — see "skipped engines" in results file.

**Silent (0 fires in available sample):**
stop_hunt, vpoc_retest, oi_concentration, e16_bb_fade_hv_4h — either thresholds too tight or warmup eats the 220×1h sample.

## Action taken (PR #3)
- E17_1d / E17_4h: regime gate inverted from `[trend_up, trend_down]` to `[high_vol, range]`
- Verified swing on same 200d sample: E17_1d went from PF 0.59/-$186 to PF 1.54/+$108 (+$294)
- E01_1d cap_frac: 0.17 → 0.05 (demote, n=8 undersize)
- E16_1d cap_frac: 0.18 → 0.30 (promote, PF 2.70 confirmed)

See `backtest_v2_results_2026-05-18_post_patch.txt` for post-patch numbers.

## Reproducibility
```bash
python3 scripts/backtest_v2.py
```
Cache lives in `/home/claude/backtest_data/`; delete to force re-pull from signal_bus.

## Honest caveats
- In-sample over recent 200×1d (~6.6 mo); not true OOS validation
- Walk-forward is 2-window split on small samples; not formal walk-forward
- Single-engine isolation — no portfolio-level coin-lock contention modeled
- Conservative: SL hit before TP on tied bar, no slippage, flat 0.09% RT fee
- Universe coverage ~20 of 47 coins (the rest had insufficient signal_bus history)
- Donchian, stop_hunt, vpoc, oi_conc, e16_hv_4h: untested due to data/threshold issues
