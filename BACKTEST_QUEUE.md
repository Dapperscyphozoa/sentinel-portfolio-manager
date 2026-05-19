# Stage 1 Honest Backtest Queue — Phase 13 work plan

**Authorised:** operator 2026-05-19.
**Scope:** the six HL-specific Stage 1 engines added to `pm/pretrade.py:ENGINE_REGISTRY` at `capital_fraction=0.00`, `audit_status: PROVISIONAL_NEW_ENGINE_PAPER`. None have been honest-backtested. This document fixes the run order, data dependencies, and pass/fail gates before any of them gets `cap_frac > 0`.

## Order of execution (descending estimated edge)

| # | Engine | bt_pf (est) | Data deps | Notes |
|---|---|---|---|---|
| 1 | `hl_whale_frontrun` | 3.20 | signal-bus `whale_poller` (top-20 HL wallets, 30s poll). 90d accumulation required. | World-first. Highest est edge. Tests viability of "copy big HL longs/shorts at entry". |
| 2 | `hl_vault_predict` | 3.00 | signal-bus `hlp_poller` (HLP NAV vs mark, 5s poll). 90d minimum. | Anticipate HLP imminent rebalance from NAV-vs-mark divergence rate. |
| 3 | `liq_cluster_hunt` | 2.60 | signal-bus liq events (Binance `!forceOrder@arr` + HL `liquidations`). Already accumulating; needs ≥30d. | Predict sweep path from stacked liq cluster + round-number alignment. |
| 4 | `hl_cvd_aggressor` | 2.20 | signal-bus HL trade tape (aggressor side per print). Tape buffer needs ≥30d. | CVD aggressor flow on HL specifically (not Binance CVD). |
| 5 | `hl_depth_shock` | 2.10 | signal-bus L2 book snapshots (1s). Storage cost is significant; 30d minimum. | Fade bid/ask depth shocks. Already has 1 open live position at runtime — that's an inflight test, not a backtest. |
| 6 | `funding_triangulation` | 2.00 | HL funding + Binance funding + OKX funding, all time-aligned. signal-bus has Binance + OKX; HL funding via existing `funding/{coin}` endpoint. ≥30d. | Cross-venue divergence single-leg HL execution. |

## Methodology (per engine — non-negotiable)

Each engine must pass **all** of the following before `cap_frac > 0` is set in `pm/pretrade.py:ENGINE_REGISTRY`:

1. **Walk-forward split**: 60/30/30 days minimum. IS for parameter tune (if any), OOS-1 and OOS-2 for validation. PF must be ≥ 1.4 on each split independently. Three-sample consistency (PF monotonically rising or flat across IS → OOS-1 → OOS-2) required for any cap_frac > 0.05.
2. **Sample-size floor**: n ≥ 30 closed trades on OOS-2. Below 30, decision is "extend window" or "shelve".
3. **No live-data leakage**: the backtest harness pulls from signal-bus historical endpoints only. No live HTTP calls inside the engine module during replay. The harness asserts this.
4. **Per-coin tier ranking**: same gate as UZT_REV — drop any coin with negative Total R from the live universe before deployment.
5. **Regime bucketing**: PF computed in vol-tercile × funding-sign buckets. Engine must not have a bucket with PF < 0.5 (catastrophic regime). If it does, add regime gate to engine code before deploying.
6. **Multi-policy exit sweep** (if engine has tuneable exit): 20+ exit variants tested, single chosen. Document chosen variant in `backtests/<engine>_<date>.md`.

## Acceptance gates (per Phase 13 engine ship)

| Gate | Threshold | Action |
|---|---|---|
| Honest OOS PF (both splits) | ≥ 1.4 | Eligible for cap_frac 0.02 canary |
| Honest OOS PF (both splits) | ≥ 2.0 + n ≥ 50 + consistency | Eligible for cap_frac 0.05 |
| Honest OOS PF (any split) | < 1.0 | Engine moves to SPEC §4 Dead Registry, file archived |
| Honest OOS PF (any split) | 1.0–1.4 | Engine stays cap_frac 0.00 paper, re-run after parameter sweep |

## Execution mechanism

Each engine: run `python3 scripts/honest_backtest.py --strategy <name> --days 90 --walk-forward 60-30 --universe <listed-coins>` and commit:

- `backtests/<engine>_<date>.md` — summary table (per the existing convention)
- `backtests/<engine>_<date>.jsonl` — per-trade detail

Then either:
- **Pass** → PR that bumps `cap_frac` in `pm/pretrade.py` + flips `STRATEGY_<NAME>_ENABLED=1` env on `spm-strategy-runner` + updates SPEC §3.4 → §3.1 promotion
- **Fail** → PR that adds to SPEC §4 Dead Registry + moves strategy file to `strategy_runner/strategies/_archived/`

## Data readiness check before kickoff

Before running engine #1 (`hl_whale_frontrun`), verify:

```
curl -sS https://spm-signal-bus.onrender.com/health \
  | jq '.last_update | {whale_poll, hlp_poller}'
```

Both must show non-zero timestamps and < 60s old. If `whale_poll = 0`, the poller is dead; engine #1 cannot proceed until fixed.

## Sequencing rule

Run engines **strictly in order**. No parallel honest-backtest runs.
- One engine at a time keeps the harness output deterministic.
- If engine #N fails the gate, do not skip ahead — failure conditions often reveal data-availability gaps that affect later engines too.
- Estimated wall time: ~30 min per engine (90d × 16 coins × 5m resolution), or ~3h total queue.

## Out of scope for Phase 13

- Live deployment of any of these six engines (that's Phase 14+ once gated).
- Building new data sources — every engine listed has its data dep already in `signal-bus` (verified via `pm/pretrade.py` notes column).
- Re-running honest backtests for already-GREEN engines (separate task; see SPEC §11 Phase 16).
- Touching `uzt_rev` — it has its own validation contract (`VALIDATION_UZT_REV.md`).

---

**END Phase 13 work plan**

When all six engines have a verdict (pass→promote or fail→dead), Phase 13 is complete. Update SPEC §11 to mark `[x]`.
