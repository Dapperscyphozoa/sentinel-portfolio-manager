# SPM Engine Decision Matrix — 2026-05-22

Source data:
- Live attribution from `/strategy/attribution` (all-time, all engines)
- Honest walk-forward backtest: 180d, 30-coin universe (BTC/ETH/SOL/BNB/+27 alts), 50/50 IS/OOS split via date
- Backtest harness: `scripts/backtest_harness.py` using Binance fapi + spot-archive fallback

## Combined verdicts

| Engine | Live n | Live WR | Live Net $ | BT All n | BT All PF | **BT OOS PF** | OOS WR | Final Verdict |
|---|---|---|---|---|---|---|---|---|
| **ict_confluence_4h** | 2 | 100% | +0.47 | 423 | 2.94 | **3.23** | 58% | 🟢 **WORKHORSE — increase capital** |
| **ict_confluence_1d** | 0 | — | — | 34 | 5.00 | **5.34** | 71% | 🟢 **promote to higher cap** |
| **e09_pump3d10_td_1d** | 0 | — | — | 42 | 5.99 | **8.39** | 94% | 🟢 **PROMOTE — best OOS edge** |
| **e16_bb_fade_hv_1d** | 0 | — | — | 32 | 3.30 | **3.12** | 74% | 🟢 keep live |
| **hlp_fade** | 10 | 40% | +0.28 | 0* | — | — | — | 🟡 marginal+ live; HL-data BT not possible |
| **liq_cascade** | 2 | 50% | -0.03 | — | — | — | — | 🟡 break-even, thin |
| **stop_hunt** | 0 | — | — | 42 | 1.58 | **0** | 0% | 🔴 **OOS decay** — no recent fires |
| **donchian** | 0 | — | — | 525 | 1.47 | **0** | 0% | 🔴 **OOS decay** — fires died in recent window |
| **e08_dip3d7_td_4h_inv** | 0 | — | — | 113 | 1.77 | **0.30** | 46% | 🔴 **KILL** — IS→OOS collapse |
| **e08_dip3d7_td_4h** (ghost) | 11 | 0% | -9.98 | — | — | — | — | 🔴 **ALREADY DEAD** — ghost in DB |
| **funding_triangulation** | 14 | 29% | -1.00 | — | — | — | — | 🔴 **DEMOTE** — losing live |
| **fmom** | 19 | 42% | -0.80 | 0* | — | — | — | 🔴 **DEMOTE** — losing live |
| **hl_settle_5m** | 94 | 52% | -3.48 | 0* | — | — | — | 🔴 **DEMOTE** — large n, fee drag eats edge |
| **hl_depth_shock** | 9 | 22% | -0.69 | 0* | — | — | — | 🔴 **DEMOTE** — 22% WR confirmed |
| **hl_whale_frontrun** | 1 | 0% | -0.29 | 0* | — | — | — | ⚠️ too thin, monitor |
| **e17_bb_fade_bt_1d** (ghost) | 1 | 0% | -1.15 | — | — | — | — | 🔴 already off |
| **e08_dip3d10_td_1d** (ghost) | 2 | 50% | -1.27 | — | — | — | — | 🔴 already off |
| **uzt_rev** | 0 | — | — | 0* | — | — | — | 🟡 LIVE but no fires yet; BT needs HL data |
| **oi_concentration** | 0 | — | — | 0* | — | — | — | 🟡 LIVE, BT needs HL OI |
| **vpoc_retest** | 0 | — | — | 0* | — | — | — | 🟡 LIVE, BT needs HL data |

`*` = Binance-data harness cannot fire HL-specific engines (need spm-signal-bus HL feed for honest BT of these).

## Aggregate live PnL by verdict tier

| Tier | Engines | Total live n | Total net $ |
|---|---|---|---|
| 🟢 GREEN | ict_confluence_4h, ict_confluence_1d, e09_pump3d10_td_1d, e16_bb_fade_hv_1d | 2 | +0.47 |
| 🟡 YELLOW | hlp_fade, liq_cascade, uzt_rev, oi_concentration, vpoc_retest | 12 | +0.25 |
| 🔴 RED (still live) | fmom, funding_triangulation, hl_settle_5m, hl_depth_shock, stop_hunt, donchian, e08_dip3d7_td_4h_inv, hl_whale_frontrun | 137 | -6.74 |
| 🔴 GHOST (dead but in DB) | e08_dip3d7_td_4h, e08_dip3d10_td_1d, e17_bb_fade_bt_1d | 14 | -12.40 |

**Net portfolio: -$18.42 (cumulative since attribution started)**

The 8 RED-still-live engines are the bleeding source. Halting them and increasing capital on the 4 GREEN engines flips trajectory.

## Action items (env var changes on Render)

### IMMEDIATE HALT (zero impact, all bleeders)
```
STRATEGY_E08_DIP3D7_TD_4H_INV_ENABLED=0   # OOS PF 0.30
STRATEGY_FMOM_LIVE=0                       # demote to paper: 42% WR live
STRATEGY_FUNDING_TRIANGULATION_LIVE=0      # demote: 29% WR live
STRATEGY_HL_DEPTH_SHOCK_LIVE=0             # demote: 22% WR live
STRATEGY_HL_SETTLE_5M_LIVE=0               # demote: 52% WR but -$3.48 = fee drag
STRATEGY_HL_WHALE_FRONTRUN_LIVE=0          # n=1, 0% WR
STRATEGY_STOP_HUNT_LIVE=0                  # OOS zero fires, decay
STRATEGY_DONCHIAN_LIVE=0                   # OOS zero fires, decay
```

### CONFIRM LIVE (already enabled, verify they fire)
```
STRATEGY_ICT_CONFLUENCE_4H_LIVE=1          # ✓ already firing, OOS PF 3.23
STRATEGY_ICT_CONFLUENCE_1D_LIVE=1          # OOS PF 5.34
STRATEGY_E09_PUMP3D10_TD_1D_LIVE=1         # OOS PF 8.39 — gold
STRATEGY_E16_BB_FADE_HV_1D_LIVE=1          # OOS PF 3.12
```

### MONITOR (insufficient data either way)
```
hlp_fade, liq_cascade, uzt_rev, oi_concentration, vpoc_retest
```

## Landing page fix (deployed commit c527fdf)

- Hardcoded LIVE_ENGINES set removed — now derives from `/strategy/health` registry.live.
- StrategyBase.info() augmented with `enabled`, `live`, `stage` from env.
- Bleeders bubble to top within each tier (most-negative first).
- Default view now all-time (since=0). Toggle to post-bug-only via header link.
- GHOST tier added — engines no longer in registry but still have closures in DB.

After redeploy the 6 hidden bleeders (funding_triangulation, hl_depth_shock, fmom, donchian, vpoc_retest, e16_bb_fade_hv_4h) will correctly render as LIVE with their live PnL.

## What still needs HL-data honest BT (Binance harness can't do these)

- uzt_rev (needs HL liq + OI + CVD)
- hlp_fade, hlp_decoder (needs HL HLP NAV)
- hl_settle_5m, hl_depth_shock, hl_cvd_aggressor (needs HL orderbook/CVD)
- oi_concentration, funding_triangulation (needs HL OI + funding diff)
- vpoc_retest (needs HL volume profile)
- liq_cluster_hunt (needs HL liq cluster data)

**Unblock path:** resume `spm-signal-bus` on Render (srv-d840pnpkh4rs73cpj66g currently suspended) — then `backtest_harness.py --bus https://spm-signal-bus.onrender.com ...` will serve HL-specific historical data.
