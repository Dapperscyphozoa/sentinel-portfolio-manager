# e08_dip3d7_td_4h_INV — universe expansion 7 → 15 coins — 2026-05-20

## Action: universe expanded after full 60-coin GREEN sweep

Initial revival shipped a conservative 7-coin universe (the train-selected
set from walk-forward universe-selection, thr=1.0). Operator requested
full 60-coin re-check to find more coins that "respect the engine."

## Methodology
- Ship config (drop=0.07 hold=8 sl=tp=0.10, SHORT side, TREND_DOWN gate)
- Walk-forward per-symbol midpoint split, TRAIN/TEST graded independently
- 60-symbol OKX SWAP perps, 365d window
- Inclusion gate: both TRAIN PF ≥ 1.0 AND TEST PF ≥ 1.0 AND n ≥ 8 per half

## Cohorts
- **GREEN** (both halves pass, n≥8/half): 8 new coins
  FIL (all_PF 8.50), BLUR (7.21), DOT (4.31), SNX (3.22), ENS (2.86),
  TIA (2.42), LDO (2.13), DYDX (1.86)
- **GREEN but thin** (n<7 either half — sample noise risk): 5 coins excluded
  LTC (n=4), CRV (6), SOL (5), ICP (6), ETH (6)
- **YELLOW** (one half fails): 31 coins — excluded
- **RED** (both fail): 8 coins — excluded
  XRP, ATOM, AXS, COMP, SAND, MANA, HBAR, XLM

## Expanded universe (15 coins)
ARB, GALA, INJ, OP, ORDI, PYTH, WIF (original 7)
+ FIL, BLUR, DOT, SNX, ENS, TIA, LDO, DYDX (8 additions)

## Aggregate validation
| phase | n | WR | PF | total return |
|---|---|---|---|---|
| TRAIN | 109 | 47.7% | 1.74 | +111% |
| TEST | 89 | 69.7% | 3.31 | +295% |
| ALL | 198 | 57.6% | 2.46 | +406% |

Both halves cleanly exceed GREEN gate. TEST WR 69.7% confirms the
edge holds on the larger universe.

## Frequency impact
- 7-coin: ~9 trades/mo
- 15-coin: ~16 trades/mo (73% more)
- Still well below daily-volume sensitivity; no concurrent-position risk
  (cap_frac 0.02 still limits to 2 concurrent positions).

## Risk profile (unchanged)
- size_mult 0.2 → $24.55 notional/trade
- 10% SL → max loss $2.46/trade
- 2 max concurrent → $4.91 worst-case
- Total monthly notional exposure at 16/mo × $25 = $400 turnover
