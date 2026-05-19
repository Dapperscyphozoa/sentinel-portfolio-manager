# UZT_REV v3 — Live Validation Framework

**Strategy:** `uzt_rev` (Unified Zone Trading, reversal-only, ship config v3)
**Backtest of record:** n=41, WR 68.3%, PF 6.92, expectancy +1.71R/trade
**Three-sample consistency:** 90d×20 PF 5.18 → 120d×20 PF 5.69 → 120d×30 PF 6.92 (monotonic)
**Status as of 2026-05-19:** paper mode, data starvation fix shipped (commit `e785898`), full 16/16 universe at 1000-bar gate, awaiting first paper fire.

This document is the **enforceable** validation contract for promoting `uzt_rev` from paper → live → full capital. Each phase has explicit entry criteria, exit criteria, and a halt trigger that takes precedence over everything else.

---

## Phase 0 — Paper sanity (now)

**Purpose:** Prove the signal-bus → strategy-runner → PM gate → trader pipeline works end-to-end before committing real capital. Not a statistical test — `n=5` is too small to validate edge, only the *plumbing*.

| Gate | Threshold | Action if hit |
|---|---|---|
| Paper fires recorded | ≥ 5 | Eligible for Phase 1 review |
| Time since last fire | > 14 days | Investigate (data gap? gate bug?) |
| Signal extras_json well-formed | 100% of fires | (no action — just check) |
| HL paper wallet attribution working | 100% of fires | Required before LIVE=1 |

**Exit:** ≥ 5 paper fires AND extras_json contains `zone_side`, `path: "REV"`, `tp_r: 5.0`, `audit_status: "PROVISIONAL"`.
**Setting:** `STRATEGY_UZT_REV_LIVE=0`, `STRATEGY_UZT_REV_ENABLED=1`.
**Expected duration:** with ~10 fires/month backtest cadence and a 16-coin universe, expect 5 fires in 10–20 days.

---

## Phase 1 — Live canary

**Purpose:** Statistical validation begins. Money is real but bounded.

| Setting | Value |
|---|---|
| `STRATEGY_UZT_REV_LIVE` | `1` |
| `capital_fraction` | `0.025` (~$12.27 on $491 wallet) |
| Leverage | 5× (default) |
| Max loss per trade | ~$12 (stop is signal-SL, not %) |
| Max concurrent positions | 1 (uzt_rev only — PM `coin_conc_max=1`) |

**Promotion to Phase 2:**
- `n ≥ 20` closed live trades, AND
- rolling-20 PF ≥ 2.0

**Halt triggers (any one fires → `POST /halt/uzt_rev`):**
- rolling-20 PF < 1.5 once n ≥ 10
- Account drawdown > 5% (already enforced globally by `monitor/drawdown_watch`)
- 4 consecutive losses (already enforced by `pm.cooldown` 4-loss rule)
- Any single trade losing > 1.5× signal SL (slippage anomaly — likely venue issue)

**Demotion (one-way → SPEC §4):**
- `n ≥ 30` AND rolling-30 PF < 1.0 → permanent demote, add to Dead Engine Registry

---

## Phase 2 — Scaled canary

| Setting | Value |
|---|---|
| `capital_fraction` | `0.05` (~$24.55) |

**Promotion to Phase 3:**
- `n ≥ 50` closed live trades, AND
- rolling-50 PF ≥ 3.0

**Halt triggers:** same as Phase 1, computed on rolling-20 window.

---

## Phase 3 — Full allocation

| Setting | Value |
|---|---|
| `capital_fraction` | per PM registry max-share calc (current 0.05 reserved; revisit at this stage based on other-engine performance) |

**Continuous monitoring:**
- rolling-50 PF < 2.0 → demote one tier (Phase 2 sizing)
- rolling-50 PF < 1.0 → halt + post-mortem

---

## Why these specific thresholds

**Break-even WR at TP=5R is 16.7%.** Backtest WR is 68.3% — a 51 percentage-point cushion. The phase-gate PFs (2.0 / 3.0) are deliberately discounted from the backtest PF 6.92 to account for:

1. **Multiple-comparisons inflation** from the 28-variant exit-policy sweep (per sentinel audit). Honest post-sweep estimate is closer to PF 3.0–4.0, not 6.92.
2. **Survivorship bias** in per-coin tier ranking (16/30 coins selected for positive Total R).
3. **Walk-forward used a fixed split**, not rolling — regime-shift exposure is partly uncaptured.
4. **Backtest-to-live degradation** norm of 30–50% — live PF tends to be backtest × 0.5–0.7.

So the gates assume PF discounted by ~50% from backtest, then add safety margin. PF ≥ 2.0 at n=20 = "still well above break-even after honest discount". PF ≥ 3.0 at n=50 = "live performance approaches the discounted-backtest estimate".

**Halt at rolling-20 PF < 1.5** with `n ≥ 10`: this is well below break-even (PF 1.0) so it gives room for normal variance but pulls the plug if structural regime change happens.

---

## What is NOT covered by this framework

- **Validation of other strategies** — each engine in `pm/pretrade.py` needs its own per-engine validation doc. `uzt_rev` is the canary; others follow once it proves the rail.
- **Pre-trade overlays** (regime affinity, coin concentration, halt tokens) — those are in `pm/pretrade.py` and continue to apply.
- **Tooling to auto-enforce these gates** — currently the rolling PF must be checked manually via `monitor/routines/demotion_candidates.py` (daily). Auto-halt-on-PF-breach is deferred to a separate session; until then, **manual operator review is required** at each phase boundary.

---

## How to read this in 30 seconds

| Phase | Capital | n threshold | PF threshold | Halt |
|---|---|---|---|---|
| 0 paper | $0 | 5 fires | (plumbing only) | n/a |
| 1 canary | 2.5% (~$12/trade) | 20 closures | ≥ 2.0 | rolling-20 PF < 1.5 |
| 2 scaled | 5.0% (~$25/trade) | 50 closures | ≥ 3.0 | rolling-20 PF < 1.5 |
| 3 full | 5%+ | continuous | rolling-50 ≥ 2.0 | rolling-50 PF < 1.0 |
