# Profit/Cost Adjustments — Offline Counterfactual Report

**Generated:** 2026-05-19
**Harness:** `scripts/counterfactual.py`
**Data:** `backtests/*.jsonl` — 7,893 simulated fires across 20 engines
**Scope:** No live-trading code is modified by this analysis.

## What this commit ships

The verdicts below were validated offline. Code changes in this commit:

- **#3 By-coin pruning** — new `pm/pretrade.py` gate behind `ENABLE_BY_COIN_PRUNE=0` (default OFF). 6 unit tests added covering default-off, dead-pair rejection, sample-size floor, PF threshold, noisy-closure exclusion, and engine allowlist.
- **#4 fire_reason instrumentation** — `scripts/backtest_harness.py` now persists `fire_reason`, `extras`, and `vol_24h_usd` per fire. `scripts/counterfactual.py` activates `adj4_fire_reason_pruning` and `adj9_liquidity_floor` when the new fields are present (legacy JSONLs report `no_data_yet`).
- **#6 Clean-closure hygiene** — `monitor/routines/daily_report.py` and `core/mer.py` now pass `?clean_only=1` to `/attribution`. `pm/server.py` local fallback `/attribution` honors `clean_only` parameter (was silently ignored). `pm/attribution.by_strategy()` defaults `clean_only=True`.
- **#9 24h-volume instrumentation** — same harness patch as #4 above.

**Not shipped (requires operator approval / live-path change):**
- **#1 Maker entry** — fee bookkeeping already supports `extras.maker_only_recommended` (see `strategy_runner/trader.py:874`), but actual order routing still calls `hl.market_open`. A real implementation requires adding `limit_open(post_only=True, fallback_taker_after_ms=N)` to `common/hl_exchange.py`, integrating fill-timeout logic into `trader.py`, and operator review of the unfilled-rate impact. Separate PR.

**To enable #3 in production (after operator review):**
```bash
# Per-service env vars on spm-pm
ENABLE_BY_COIN_PRUNE=1
BY_COIN_PRUNE_MIN_N=15
BY_COIN_PRUNE_MAX_PF=1.0
BY_COIN_PRUNE_ENGINES=hl_settle_5m,ict_confluence_4h,...  # explicit allowlist
```
Start with the allowlist set to one GREEN engine with ≥50 closures, monitor for 7 days, expand from there.

---

## What this report does

For each of the 10 proposed adjustments to lower cost / increase profit (excluding sizing and risk management), I ran an offline counterfactual against every honest-backtest JSONL in `backtests/`. Where a rule had to be fit (e.g. "drop loser pairs"), I used **walk-forward 50/50**: fit on the chronologically-first half, evaluate on the second half. An in-sample-overfit number is reported beside the walk-forward result so the gap is visible.

Some adjustments cannot be tested with the current data — those are flagged with the specific blocker and the next step needed to test them.

## Baseline

| | Value |
|---|---|
| Fires across all backtests | 7,893 |
| Engines covered | 20 |
| Aggregate net-pct (after taker fees both legs) | **−3.74%** sum |
| Aggregate PF | **0.955** |

(The aggregate is below 1.0 because the JSONL set includes archived/RED engines like `donchian`, `cross_coin_zscore`, and `e17_bb_fade_bt_4h`. The verdicts below are per-engine where it matters.)

---

## Verdict matrix

| # | Adjustment | Verdict | Headline impact |
|---|---|---|---|
| 1 | Maker entry (entry maker, exit taker) | **SHIP** | +236.8% net-pct across 7,893 fires = +30 bps/fire, no overfit risk |
| 2 | Live PF degradation gate recalibration | **NEEDS FORWARD** | Not offline-testable |
| 3 | By-coin pruning (n ≥ 15, PF < 1.0) | **SHIP W/ CAUTION** | WF OOS PF 1.23 → 1.43, +223.8% — but drops 53% of OOS fires |
| 3a | By-coin pruning (n ≥ 10, PF < 1.0) | **REJECT** | WF OOS delta −43.3% — overfit at this threshold |
| 4 | fire_reason variant pruning | **NEEDS INSTRUMENTATION** | Backtest harness strips `extras_json.fire_reason` |
| 5 | Regime-affinity confidence tune (0.7 → 0.5) | **NEEDS FORWARD** | No regime timeline in backtest data |
| 6 | Exclude noisy closures from all gating math | **SHIP (LIVE only)** | Right policy; null offline (all simulated closes are clean) |
| 7 | Funding inclusion in net P&L | **NEEDS FORWARD** | No HL funding archive; ~14d wait per BACKTEST_QUEUE.md |
| 8 | Pre-event blackout (UTC-hour proxy) | **CONDITIONAL — per engine only** | 6 of 11 engines positive OOS; 5 worse OOS |
| 9 | Tiered liquidity floor (50M / 200M / 500M) | **NEEDS INSTRUMENTATION** | 24h volume not persisted in JSONLs |
| 10 | Kronos gate on UNTESTED tier | **NEEDS FORWARD** | Live ML gate; no historical inference output |

**Implement now:** #1, #6
**Implement after per-engine validation:** #3 (n ≥ 15 only), #8 (selective)
**Build infrastructure first:** #4, #9
**Wait for forward-paper data:** #2, #5, #7, #10

---

## #1 — Maker entry (post-only entries, taker exits unchanged)

**Verdict: SHIP.** Pure cost reduction with no edge assumption.

Round-trip fee drops from 2 × 0.045% = **0.090%** (current) to 0.015% + 0.045% = **0.060%** (proposed). Savings per fire = **30 bps of notional**, applied to every fire regardless of outcome.

| | Baseline (taker both legs) | Proposed (maker entry) |
|---|---|---|
| Aggregate net-pct (7,893 fires) | −3.74% | **+233.05%** |
| Aggregate PF | 0.955 | 0.983 |
| Per-fire savings | — | +30 bps |

**Caveats not modelable offline:**
- Post-only limit fills are not guaranteed. If 20% of intended fires never fill at the limit price, the savings are reduced proportionally, and we lose the opportunity cost on any fires that would have been profitable.
- Existing code already supports the toggle: `extras.maker_only_recommended` is read in `strategy_runner/trader.py:874`. The plumbing exists — only the per-engine setting is missing.

**Next step:** Roll out via env flag per engine (default OFF), monitor unfilled-rate via existing trader logging for 7 days before flipping all engines.

---

## #3 — By-coin pruning

**Verdict: SHIP at `n ≥ 15, PF < 1.0`. Reject `n ≥ 10`.**

The proposal: in `pm` pre-trade, reject if the (engine, coin) pair has ≥ N historical fires with live PF < 1.0.

Walk-forward 50/50 test: fit the dead-pair set on the chronologically-first half of each engine's fires, evaluate by simulating the rule on the second half.

| Threshold | IS fantasy (overfit) | OOS walk-forward |
|---|---|---|
| n ≥ 10, PF < 1.0 | +1,717.97% | **−43.30%** (worse OOS) |
| n ≥ 15, PF < 1.0 | +1,432.03% | **+223.84%** (better OOS) |

The gap between the in-sample fantasy and the walk-forward result is the overfit tax. At `n ≥ 10`, the rule fits noise; at `n ≥ 15`, real losing pairs are stable enough across halves to be safely dropped.

**Caveats:**
- `n ≥ 15` cuts **53.6%** of OOS fires (2,118 of 3,952). Lower trade count means less opportunity for compounding and more sensitivity to a single bad month.
- The 33 dead pairs identified are concentrated in `cross_coin_zscore`, `e17_bb_fade_bt_4h`, `fd1`, `lh1` — the engines already known to be marginal or RED.
- This rule applied to a GREEN engine could prematurely cull a pair that's mid-drawdown rather than dead. Test on GREEN engines (`hl_settle_5m`, `ict_confluence_4h`) with `n ≥ 30, PF < 0.7` before broad rollout.

**Next step:** Implement as `pm` gate behind `ENABLE_BY_COIN_PRUNE` env (default OFF). Apply only to engines with ≥ 50 closed fires. Re-evaluate every 30 days against fresh data.

---

## #6 — Exclude noisy closures from all gating math

**Verdict: SHIP (live data only).**

Backtest JSONLs contain only simulator-clean closes (`timeout`, `sl`, `tp`, `eod`) — by construction the simulator never force-closes. The offline counterfactual is therefore **null**: delta = 0.000%.

But the adjustment is real and shippable against the live `closures` table: filter `close_reason NOT IN ('force_close:audit_red', 'reconciled_off_book', 'manual', 'force_close:%')` consistently across:
- live PF degradation gate (`monitor/routines/`)
- 4-loss demote counter (`monitor/routines/auto_4loss_demote`)
- `by_coin` / `by_reason` attribution
- promotion gates (per `pm/promotion_gate.py`)

The promotion gate already excludes noisy closes — verify the other three call sites do too. This is a code hygiene task, not a feature.

---

## #8 — UTC-hour blackout (event-blackout proxy)

**Verdict: CONDITIONAL — per-engine only, do NOT apply universally.**

The proposal targets event calendars (CPI/FOMC/funding settles). No event calendar is available offline. The closest proxy: per-engine, identify UTC hours where IS PF < 1.0 with n ≥ 5, block them in OOS.

OOS results by engine, sorted by OOS delta:

| Engine | OOS n before | OOS n after | OOS PF before → after | OOS Δ net-pct |
|---|---|---|---|---|
| cross_coin_zscore | 1310 | 110 | 0.69 → 0.49 | **+113.56%** |
| fd1 | 409 | 82 | 0.73 → 0.72 | **+76.38%** |
| e01_zfade3s_tu_4h | 49 | 32 | 0.99 → 1.83 | **+23.13%** |
| lh1 | 506 | 319 | 0.79 → 0.74 | +5.09% |
| vsq | 126 | 80 | 1.08 → 1.20 | +4.44% |
| range_fade | 133 | 97 | 0.88 → 0.92 | +3.24% |
| donchian | 90 | 70 | 1.45 → 1.40 | −9.65% |
| e08_dip3d10_td_1d | 70 | 0 | 1.14 → 0.00 | −23.66% |
| e07_zfade2s_tu_4h | 212 | 139 | 1.10 → 1.03 | −24.57% |
| e17_bb_fade_bt_4h | 487 | 241 | 1.24 → 1.41 | −48.46% |
| e08_dip3d7_td_4h | 316 | 59 | 1.89 → 0.80 | **−506.69%** |

Five of eleven engines lose money OOS under their own fitted blackouts. The "huge" wins on `cross_coin_zscore` and `fd1` are mostly volume-reduction effects on engines that lose money in every hour — blocking 22 of 24 hours is a roundabout way of just turning the engine off, which is better done explicitly by lowering `cap_frac`.

**Caveat:** The negative cases (`e17`, `e08_dip3d7_td_4h`) show this rule is dangerously overfit at the hour-granularity. Most engines don't have an honest hour-of-day edge — they have signal-quality edges that happen to cluster in some hours, and blocking those hours kills good fires alongside bad ones.

**Next step:** Do NOT roll this out as a blanket rule. For `fd1` and `e01_zfade3s_tu_4h`, where the OOS lift is real and the engine is otherwise marginal, consider an explicit per-engine `blocked_utc_hours` env. For everything else, **wait for the real adjustment** (event-calendar-based blackout via macroeconomic-event feed).

---

## Items not testable against `backtests/*.jsonl`

These items genuinely can't be evaluated with current data. For each: the specific blocker, and the concrete next step to make it testable.

### #2 — Live PF degradation gate recalibration

- **Blocker:** Requires the live `closures` history *plus* the demote-event log in `monitor.db`. Neither is in the repo (production-only SQLite on Render).
- **Next step:** Export `/attribution` snapshots over 30+ days. Replay the `live_pf < 0.74 × bt_pf after n ≥ 22` rule at varying thresholds (0.85x, 0.90x) and n_floors (22, 30, 50). The right answer minimizes false-positive demotes while catching genuine PF decay early.

### #4 — fire_reason variant pruning

- **Blocker:** Backtest JSONLs don't carry `extras_json.fire_reason`. Engines do tag it at runtime (search `strategy_runner/strategies/*.py` for `fire_reason=`), but `backtest_harness.py` discards it on serialization.
- **Next step:** Patch `scripts/backtest_harness.py` to persist `extras_json` on each fire. Re-run `honest_backtest.py`. Then re-run this harness — the `by_reason` slice is then identical in form to `by_coin`.

### #5 — Regime-affinity confidence tune (0.7 → 0.5)

- **Blocker:** No regime classification at each fire's timestamp. Regime is computed live by `pm/regime.py` and not persisted into closures.
- **Next step:** Snapshot `/regime` every 5 minutes for 7+ days. Overlay on per-fire timestamps. Bucket each engine's fires by regime confidence at fire time, compute per-bucket PF. The right threshold is where below-threshold fires consistently have PF < 1.0.

### #7 — Funding inclusion in net P&L

- **Blocker:** No HL funding rate archive yet. `BACKTEST_QUEUE.md` infrastructure follow-up §2 confirms signal_bus is passively accumulating funding history since 2026-05-19; in ~14 days there will be enough to backfill.
- **Next step:** Once 14 days of HL funding history exist, augment `pm/server.py` `closures` write to compute `funding_paid_usd = funding_rate × notional × hours_held / 8` and add to `pnl_usd`. Strategies that profit from harvesting positive funding will show up immediately in `/attribution`.

### #9 — Tiered liquidity floor

- **Blocker:** Backtest JSONLs don't include 24h volume per coin at fire time. Volume is in the Binance kline rows the harness pulls but isn't written through.
- **Next step:** Augment `scripts/backtest_harness.py` to write `vol_24h_usd` per fire. Re-run honest backtests. Then test floor tiers (50M / 200M / 500M) by simply filtering fires below each threshold.

### #10 — Kronos gate on UNTESTED tier

- **Blocker:** Kronos is a transformer ML gate; runs on the live signal stream. `tests/test_kronos_gate.py` validates the gate's interface but produces no historical inference output to overlay on past fires.
- **Next step:** Activate Kronos in paper mode for UNTESTED engines (`hl_cvd_aggressor`, `funding_triangulation`, `liq_cluster_hunt`, `hl_whale_frontrun`, `hl_depth_shock`, `hl_vault_predict`) for 30+ days. Compare paper PF with and without Kronos as a pre-fire gate.

---

## Reproducing this report

```bash
python3 scripts/counterfactual.py > /tmp/cf.json
# Inspect /tmp/cf.json for full per-engine, per-coin detail.
```

The harness is deterministic — re-running on the same JSONLs produces identical output. Re-run after each new honest-backtest batch to refresh.

## What this does NOT prove

- Backtest fires ≠ live fires. Slippage, fill-rate, and partial fills are simulated cleanly here.
- Walk-forward 50/50 is a single split. Two-fold cross-validation would be more robust.
- PF and net-pct are aggregate; they don't speak to drawdown sequencing or per-trade tail risk.

The recommendations above are necessary but not sufficient — each "SHIP" item should be deployed with `LIVE_TRADING=0` first per `WORKFLOW.md` §post-deploy gates.
