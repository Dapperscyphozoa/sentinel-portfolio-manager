# Session 1.5 — replication run 2026-05-18

This folder is a **replication** of the original Session 1.5 honest backtest
that was committed on 2026-05-16 (commit `af387ff`). It is informational; it
does **not** overturn the canonical gates in `STRATEGY_GATES.md` (committed
2026-05-16) unless the operator chooses to act on the new findings.

## Why a replication

The replication was initiated under the assumption that Session 1.5 had not
yet been run. Mid-execution, the prior committed gates were discovered. The
results are kept because they used a **wider time window** (180d vs the
original 90d) and a **different harness** (direct OKX REST + HL funding REST
calls, in `scripts/honest_backtest.py`, vs the original `scripts/backtest_harness.py`
with cursor-gated `HistoricalBus`).

## Side-by-side

| Strategy | Original (2026-05-16) | Replication (2026-05-18) | Delta |
|---|---|---|---|
| `vsq` | n=251, PF 1.46 GREEN, OOS 1.18 | n=377, PF 1.14 YELLOW, OOS 1.37 | Aggregate PF dropped 22%; OOS *improved* (1.18→1.37). Larger window picked up worse aggregate but better recent. |
| `lh1` | n=1012, PF 1.32 YELLOW, OOS 1.22 | n=95, PF 1.72 YELLOW, OOS 0.82 | Trade count collapsed (1012→95) — replication used a structural-only sweep detector without liq confluence. Different test, not directly comparable. |
| `range_fade` | n=266 (30d/8c), PF 1.25 YELLOW, OOS 1.11 | n=794 (90d/11c), PF 0.65 RED, OOS 0.68 | Larger sample reverses verdict. Replication is statistically more meaningful (n=794 vs n=266). **Strongly suggests RED is the right call.** |
| `fd1` | n=818, PF 0.85 RED, OOS 0.78 | n=150, PF 1.25 YELLOW, OOS 1.51 | Replication uses a **different signal definition** — endpoint-to-endpoint diff vs the original's linear slope + freshness check. Two different strategies share the name. Not directly comparable. |

## Recommended actions

1. **`range_fade`**: replication's n=794 vs original's n=266 makes the RED verdict more credible. Consider demoting from YELLOW to RED in the canonical gates. (Currently YELLOW; this would move it to SPEC §4 alongside fd1.)
2. **`vsq`**: replication confirms GREEN→YELLOW direction. The 180d sample weakens the case for canary promotion. Suggests holding vsq at paper for another 30d of live observation before any capital deployment.
3. **`lh1`**: replication is not comparable (different signal). No action. Re-run with full liq confluence after Bybit liq subscriber ships.
4. **`fd1`**: replication is not comparable (different signal definition). Original (slope+freshness) interpretation is what's in the archived production code; honor the RED gate. If operator wants to revisit, define which interpretation is the actual production target and re-run.

## Caveats specific to this replication run

- `lh1`: structural-only — no liq confluence layer (historical liq data unavailable for backfill).
- `range_fade`: no PM regime filter applied (permissive version; expect production PF slightly higher with filter).
- `fd1`: endpoint-diff signal, not slope-based; does not match the archived production fd1.
- `vsq`: clean test, no caveats.

## Files

- `vsq.md`, `lh1.md`, `range_fade.md`, `fd1.md` — per-strategy reports from this replication
- Reproduce with: `python3 scripts/honest_backtest.py --strategy <name> --days 180`
