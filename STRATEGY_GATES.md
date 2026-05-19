# Strategy Gates — honest backtest (Session 1.5)

Generated: 2026-05-19T03:46:07.821557Z

| Strategy | n | WR | PF | OOS PF | Status |
|---|---|---|---|---|---|
| **uzt_rev** | **41** | **68.3%** | **6.92** | **6.92** | **PAPER** (live validation framework: `VALIDATION_UZT_REV.md`) |
| vsq | - | 0.0% | 0.00 | 0.00 | **ERROR** |
| fd1 | - | 0.0% | 0.00 | 0.00 | **ERROR** |
| lh1 | - | 0.0% | 0.00 | 0.00 | **ERROR** |
| range_fade | - | 0.0% | 0.00 | 0.00 | **ERROR** |

## Gate rules

- **GREEN**: PF ≥ 1.4 AND OOS PF ≥ 1.0 → port as planned
- **YELLOW**: 1.0 ≤ PF < 1.4 OR OOS PF < 1.0 → port but flag `audit_status: PROVISIONAL`, no live capital
- **RED**: PF < 1.0 → DO NOT port; add to SPEC §4 Dead Engine Registry

## Live promotion gates (post-paper)

Live capital promotion is governed per-strategy by a dedicated validation doc:

- **uzt_rev** → `VALIDATION_UZT_REV.md` (canary 2.5% → 5% → full, halt at rolling-20 PF < 1.5)

Backtest PFs above are **NOT** sufficient for live promotion on their own. The validation doc applies a multiple-comparisons discount and requires rolling live-PF gates at each phase boundary.
