# Stage 1 Honest Backtest Queue — Phase 13 work plan (REVISED 2026-05-19)

**Authorised:** operator 2026-05-19.
**Scope:** the six HL-specific Stage 1 engines added to `pm/pretrade.py:ENGINE_REGISTRY` at `capital_fraction=0.00`, `audit_status: PROVISIONAL_NEW_ENGINE_PAPER`. None have been honest-backtested. This document fixes the run order, data dependencies, and pass/fail gates before any of them gets `cap_frac > 0`.

## REVISION 2026-05-19 — data-availability reality

After the liq-stream fix (commit `aae4aa9` — OKX REST poller into `cache.push_liq()`), historical liq data IS now available (~25h depth via REST cold-load). But other HL-specific data streams (whale positions, L2 book, NAV-vs-mark, HL trade-tape CVD) accumulate **prospectively only** — they are not backfillable from HL REST.

| Stream | Backfillable? | Current depth | Effect on Phase 13 |
|---|---|---|---|
| OKX liqs (REST) | Yes | ~25h cold-load; ongoing | `liq_cluster_hunt` honest-backtestable NOW |
| Cross-venue funding | Yes (existing endpoint) | 30d historical via funding API | `funding_triangulation` honest-backtestable NOW |
| HL whale positions | **No** (deque-only) | from-boot accumulation | `hl_whale_frontrun` needs ≥30d live paper, **not backtestable** |
| HL L2 book snapshots | **No** | from-boot accumulation | `hl_depth_shock` needs ≥30d live paper, **not backtestable** |
| HL HLP NAV-vs-mark | **No** | from-boot accumulation | `hl_vault_predict` needs ≥30d live paper, **not backtestable** |
| HL trade tape CVD | **No** | from-boot accumulation (600 events/coin) | `hl_cvd_aggressor` needs ≥30d live paper, **not backtestable** |

**Conclusion:** the v1 BACKTEST_QUEUE.md ordering by est-PF was wrong because it ignored data availability. **Reordering below puts backtestable engines first and shifts the unbackfillable engines to a "live paper accumulation" track that runs in parallel.**

## Order of execution (revised — backtestable first)

| # | Engine | bt_pf (est) | Data ready? | Action |
|---|---|---|---|---|
| 1 | `liq_cluster_hunt` | 2.60 | **YES** (OKX REST liqs, 25h cold + ongoing) | Honest backtest NOW. Walk-forward 18h/4h split as bootstrap; full 60/30/30 once 90d of liq history accumulates. |
| 2 | `funding_triangulation` | 2.00 | **YES** (binance/okx/HL funding 30d via existing endpoints) | Honest backtest NOW. Full walk-forward immediately. |
| --- | --- | --- | --- | --- |
| 3 | `hl_whale_frontrun` | 3.20 | **NO** (whale deque from-boot only) | **Live paper accumulation**. Set `STRATEGY_HL_WHALE_FRONTRUN_ENABLED=1`, `STRATEGY_HL_WHALE_FRONTRUN_LIVE=0`. Re-eval at n≥30 paper fires or 30d, whichever first. |
| 4 | `hl_vault_predict` | 3.00 | **NO** (NAV-vs-mark from-boot only) | Live paper accumulation. Same protocol. |
| 5 | `hl_cvd_aggressor` | 2.20 | **NO** (HL trade tape 600 events/coin in-memory) | Live paper accumulation. Same protocol. |
| 6 | `hl_depth_shock` | 2.10 | **NO** (L2 book from-boot only) | Live paper accumulation. Same protocol. |

## Methodology (per backtestable engine — non-negotiable)

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
