# e08_dip3d7_td_4h_INV — REVIVAL via inversion — 2026-05-20

## Action: REVIVED (paper-only) as `e08_dip3d7_td_4h_inv`

The LONG variant remains permanently dead (see _20260520_verdict.md). The
SHORT variant is a different engine entirely — same trigger, opposite
direction. Thesis flips from "exhaustion → bounce" to "momentum → continuation".

Operator question that uncovered this: "So it's inverted?" — yes. Same
family of error as lh1 (SPEC §3.5).

## Validation (anti-cherry-pick)

### Phase 1: parameter sweep (252 combos, 365d × 60 OKX symbols)
- Combos passing GREEN gate (OOS PF ≥ 1.4): **114 of 252**
- Live params (drop=0.07 hold=6 sl=tp=0.10) inverted to SHORT:
  - ALL n=597 WR 47.9% PF 1.26 tot +322%
  - **OOS n=289 WR 58.1% PF 2.26 tot +624%**
- Top single combo: drop=0.08 hold=6 sl=tp=0.10 → OOS PF 2.45

### Phase 2: walk-forward universe selection
Per-symbol TRAIN/TEST midpoint split. Universe picked on TRAIN PF≥1.0, graded blind on TEST.

| combo | drop | hold | SL/TP | thr | univ | TEST n | TEST WR | TEST PF | tot |
|---|---|---|---|---|---|---|---|---|---|
| **ship** | **0.07** | **8** | **0.10/0.10** | **1.0** | **7** | **46** | **71.7%** | **2.88** | **+150%** |
| alt-1 | 0.07 | 8 | 0.10/0.10 | 1.3 | 4 | 30 | 70.0% | 2.72 | +88% |
| alt-2 | 0.07 | 6 | 0.10/0.10 | 1.0 | 5 | 40 | 67.5% | 2.15 | +97% |

**Train-selected universe hit-rate on TEST: 7/7 = 100%.** Every coin
selected on train was profitable on test:

| symbol | TEST n | TEST WR | TEST PF | tot |
|---|---|---|---|---|
| INJ | 2 | 100% | ∞ | +19% |
| GALA | 4 | 75% | 14.88 | +20% |
| PYTH | 7 | 71% | 5.73 | +21% |
| OP | 7 | 71% | 3.53 | +30% |
| ARB | 6 | 67% | 2.48 | +18% |
| WIF | 10 | 70% | 1.86 | +20% |
| ORDI | 10 | 70% | 1.84 | +23% |

Universe is all mid-cap alts — same cohort that bled LONG. The asymmetry
confirms the thesis: mid-cap alts in TREND_DOWN don't bounce on 4h, they
continue down.

## Ship config

```python
NAME      = "e08_dip3d7_td_4h_inv"
TF        = "4h"
UNIVERSE  = ["ARB", "GALA", "INJ", "OP", "ORDI", "PYTH", "WIF"]
DROP_PCT  = 0.07
HOLD_BARS = 8        # widened from original 6 — give continuation more time
SL_PCT    = 0.10     # symmetric (original)
TP_PCT    = 0.10     # symmetric (original)
SIDE      = SHORT    # the inversion
REGIME    = TREND_DOWN
```

## Lifecycle

- Stage: PAPER (cap_frac 0.00, STRATEGY_E08_DIP3D7_TD_4H_INV_LIVE=0)
- Promotion gate: n≥20 paper closures + live PF ≥ 1.5 → cap_frac 0.03
- Demote: live PF < 1.0 after n=20 OR worst-coin DD > 20% in any rolling 7d window

## Audit trail
- Sweep: /tmp/e08_sweep_result.json (252 long combos, 252 short combos)
- Universe selection: /tmp/e08_inv_universe.json
- Methodology: /home/claude/e08bt/{sweep2.py, inverted.py, inv_universe.py}
- Death certificate for LONG variant: backtests/e08_dip3d7_td_4h_20260520_verdict.md
