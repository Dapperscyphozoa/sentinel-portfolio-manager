# Sentinel Portfolio Manager — Project Specification v2.0

**Supersedes:** SPEC.md v1.0 (2026-05-16) in its entirety.
**Author:** updated 2026-05-18 against live repo + live PM registry.
**Mission:** unchanged. Single, lean, bloat-free algorithmic trading stack on Hyperliquid perps with Binance/OKX as signal venues and HL as execution venue.

This v2.0 codifies what the system **actually is** as of 2026-05-18 — the registry, the engines, the lifecycle rules — rather than what the pre-build SPEC v1.0 proposed. Anything not in this document is not in scope.

---

## §0 What changed since v1.0

- Engine count: **9 specced → 20 implemented**. v1.0 assumed only the legacy port set (fsp/vsq/range_fade/range_bo/lh1/fd1/precog/liq_cascade/cex_dex_arb). Reality: a new generation of OOS-validated engines (ict_confluence, e01/e07/e08/e09/e16/e17 series), Tier 1 ship-pending engines (hlp_fade, stop_hunt, vpoc_retest, oi_concentration, fmom), and venue-specific engines (hl_settle_5m, cascade_sniper_hl) have shipped. Legacy 7 archived under `strategy_runner/strategies/_archived/`.
- Lifecycle: GREEN / WATCH / YELLOW / UNTESTED / RED tiers now formal; promotion is council-audited.
- New rule (operator 2026-05-18): **4 consecutive losses on any engine → permanent paper demote** (replaces v1.0's 6-loss 1h cooldown for engine-level signal). The 4-loss-per-coin 1h cooldown is retained.
- UZT_REV failed §1.5 honest-backtest gate (RED). Removed from any ship plan.
- VSQ honest PF revised down from 3.04 claim to 1.46; still GREEN but at lower allocation.
- FD1 RED on honest backtest (PF 0.85 OOS 0.78); permanently disabled.
- Sizing: cap_frac is advisory only. Production sizing is **flat MARGIN_PCT_PER_TRADE (5%) × LEVERAGE (5×)** per fire.
- New PM gate rules: 1 position per coin globally, 20 concurrent positions max, first-fire-wins across engines.
- Sentinel scope (operator 2026-05-18): consulted for edge development, loss reduction, profit increase, WR increase ONLY. Not consulted for live/paper sizing decisions or registry curation.

---

## §1 Architecture (unchanged from v1.0)

4 Render services: `signal-bus`, `strategy-runner`, `pm`, `monitor`. See v1.0 §2 for diagram. No structural change.

Additionally: a `sniper` micro-service is in production for the cascade-sniper-hl variant B implementation. Not in scope for this SPEC; documented separately in `DEPLOY_SNIPER.md`.

---

## §3 Engine Registry (replaces v1.0 §3 entirely)

The authoritative registry lives in `pm/pretrade.py:ENGINE_REGISTRY`. This SPEC table mirrors that source. **If they diverge, code wins.**

### 3.1 GREEN — real edge, live capital allocated

| Engine | Honest PF | n | cap_frac | TF | Affinity | Notes |
|---|---|---|---|---|---|---|
| `hl_settle_5m` | 1.85 | 55 | 0.20 | 5m | all regimes | Most-tested live. Promoted 2026-05-18 post short-only fix + denylist + TP 0.4% fee cleanup. |
| `ict_confluence_4h` | 3.18 | — | 0.15 | 4h | all regimes | Council-trimmed from 0.25 → 0.15. OOS PF 1.37 on longs (asymmetric, kept SHORT_ONLY=0). Routed through `live_safety`. |
| `e09_pump3d10_td_1d` | 2.20 | 26 | 0.10 | 1d | trend_down | n=26; over-allocated risk. Re-eval at n=50. |
| `e08_dip3d7_td_4h` | OOS 2.01 | 191 | 0.10 | 4h | trend_down | Promoted 2026-05-18 after force_close PnL bug fix (commit c5b055d). |
| `ict_confluence_1d` | 3.35 | — | 0.05 | 1d | all regimes | Paper-only via live_safety. |
| `e16_bb_fade_hv_1d` | 5.35 | 29 | 0.05 | 1d | high_vol | Council-trimmed from 0.10 (n=29 too thin). |
| `e01_zfade3s_tu_1d` | 1.29 | — | 0.05 | 1d | trend_up | |

**Subtotal: 0.70**

### 3.2 WATCH — edge signal but IS/OOS divergence or thin n

(Currently merged into GREEN above — `e09_pump3d10_td_1d`, `e16_bb_fade_hv_1d`, `e01_zfade3s_tu_1d`.)

### 3.3 YELLOW — marginal (PF 1.0–1.4), paper mode only

| Engine | Honest PF | cap_frac | Notes |
|---|---|---|---|
| `e17_bb_fade_bt_1d` | 1.21 | 0.01 | |
| `e07_zfade2s_tu_1d` | 1.01 | 0.02 | |
| `e08_dip3d10_td_1d` | 0.50 / OOS 1.85 | 0.07 | OKX-positive Binance-suspect; re-audit pending. |
| `e07_zfade2s_tu_4h` | 1.22 | 0.06 | |
| `e01_zfade3s_tu_4h` | 1.20 | 0.02 | |

**Paper-mode enforcement:** `STRATEGY_<NAME>_LIVE=0` env in `spm-strategy-runner`.

### 3.4 UNTESTED — low-weight monitoring

| Engine | Backtest PF | cap_frac | Notes |
|---|---|---|---|
| `liq_cascade` | 1.30 | 0.10 | Event-driven, sentinel-born, no walk-forward yet. |
| `e16_bb_fade_hv_4h` | 1.50 | 0.02 | n=1 backtest; monitor only. |

### 3.5 RED — halted (honest PF < 1.0 or fictional backtest)

| Engine | Honest PF | Reason | cap_frac |
|---|---|---|---|
| `e17_bb_fade_bt_4h` | 0.86 | Negative expectancy | 0.00 |
| `donchian` | 0.01 | Catastrophic | 0.00 |
| `cex_dex_arb` | 0.00 | Look-ahead bias unfixable on perps | 0.00 |
| `cascade_sniper_hl` | 0.00 | v1 dead; v2 in `sniper` service | 0.00 |

### 3.6 TIER 1 — built, ship-pending (cap_frac = 0.00)

These engines have backtest PF ≥ 1.4 and code shipped, but are paper-pending-validation per the lifecycle gate. **Operator directive 2026-05-18: "everything that has a profitable edge live trading" → these need cap allocation.** Proposed rebalance in §7 below; awaits operator approval.

| Engine | Backtest PF | TF | Affinity | Status |
|---|---|---|---|---|
| `hlp_fade` | 2.50 | — | all regimes | Council #1 pick, world-first. HLP vault fade. Tier 1 #1. |
| `stop_hunt` | 3.00 | — | range/chop/high_vol | S/R wick-sweep + reversal. Tier 1 #4. |
| `oi_concentration` | 2.75 | — | high_vol/range/chop | Pre-cascade detector, v1 vol-proxy. Tier 1 #6. |
| `vpoc_retest` | 1.90 | — | all regimes | Naked weekly POC magnet. Tier 1 #5. |
| `fmom` | 1.75 | — | all regimes | Funding momentum (2nd-derivative). 3 critical bugs fixed 2026-05-18 commit (10–30× over-firing). Paper-pending-validation. |

### 3.7 LEGACY (archived in `strategy_runner/strategies/_archived/`)

`fsp`, `vsq`, `range_fade`, `range_breakout`, `lh1`, `fd1`, `precog`. Not loaded at runtime. STRATEGY_GATES.md documents the post-honest-backtest verdicts:
- `vsq` GREEN (honest PF 1.46) — archived because superseded by ICT/e-series.
- `lh1` YELLOW (honest PF 1.32) — archived; consider re-port if per-coin allowlist applied.
- `range_fade` YELLOW (honest PF 1.25) — re-run with full universe before any revival.
- `fd1` RED (honest PF 0.85) — permanently dead.
- `fsp`, `range_breakout`, `precog` — archived; never honest-audited under v2 framework; no revival plan.

### 3.8 Invariant

`sum(cap_frac for all engines in ENGINE_REGISTRY) == 1.00 ± 0.02`, asserted at module load. Any rebalance must preserve this invariant.

---

## §4 Dead Engine Registry (additive to v1.0 §4)

In addition to v1.0 §4 dead engines, add:

| Engine | Failure |
|---|---|
| `UZT_REV` (Lesson #2 Unified Zone Trading) | RED per §1.5 honest-backtest gate (commit 2026-05-18). Memory locked-ship-config was wrong; honest gate rejected. |
| `donchian` | Backtest PF 0.01 — catastrophic |
| `cascade_sniper_hl` (v1) | Replaced by `sniper` service Variant B (separate deploy). v1 fictional PF, dead. |
| `e17_bb_fade_bt_4h` | Honest PF 0.86 |
| `fd1` | Honest PF 0.85, OOS 0.78 (already RED per STRATEGY_GATES.md but reaffirmed here) |
| `cross_coin_zscore` | KILLED 2026-05-19. Honest PF 0.99 over 90d × 10 pairs (n=223). Sentinel council CRITICAL unanimous (4/4 valid voters, 100% confidence): thesis broken — crypto perp pair ratios are not cointegrated at 5m timescale; alts-vs-alts pairs (ARB/ETH, SUI/SOL, etc.) follow BTC beta and don't mean-revert. Even maximal-salvage estimate (2 cointegrated pairs, 1h TF, ADF gate, z-cross exit) projected PF ≤1.6, worst engine in green tier. Archived per operator directive. Do NOT re-register. |

Do not re-port any of these.

---

## §7 Portfolio Manager v2.0 (replaces v1.0 §7)

### 7.1 Gate rules (codified in `pm/pretrade.py:check()`)

Order of evaluation:
1. `STRATEGY_<NAME>_ENABLED=0` env → reject `strategy_disabled`
2. `PM_FORCE_HALT_<NAME>=1` env → reject `halt_forced`
3. `BLOCKED_COINS` env (comma-sep) → reject `coin_blocked_operator`
4. **Global coin lock** — 1 position per coin across all engines → reject `coin_locked`
5. **Global concurrent cap** — `MAX_OPEN_POSITIONS` (default 20) → reject `max_open_global`
6. **Regime affinity** — if regime confidence > 0.7 and regime not in engine affinity → reject `regime_mismatch:<regime>`
7. **Cooldown checks** (§7.3 below) — reject `coin_cooldown` or `engine_cooldown`
8. **Sizing** — fixed `MARGIN_PCT_PER_TRADE × LEVERAGE` (default 5% × 5× = 25% notional/trade)
9. **Live-safety gate** for `{ict_confluence_4h, ict_confluence_1d, cascade_sniper_hl}` — ATR-aware size override
10. Pass → return `CheckResult(allow=True, size_usd, "ok", bt_pf=<engine_pf>)`

### 7.2 Sizing

- `LEVERAGE = 5` (constant; not per-engine)
- `MARGIN_PCT_PER_TRADE = 0.05` (5% margin per fire)
- → notional = 25% of wallet per trade
- `MIN_TRADE_USD = 10` floor
- `MAX_MARGIN_FRAC = 1.0` (no over-margining)

cap_frac is **advisory only** — used by dashboard and for future weight-based sizing. Current production uses flat sizing.

### 7.3 Auto-cooldown rules

| Trigger | Action | Duration |
|---|---|---|
| 4 consec losses on same (engine, coin) | Coin cooldown | 1h rolling |
| **4 consec losses on engine** (operator 2026-05-18: lowered from 6) | **Paper demote** (permanent until operator reinstates) | Permanent |
| Engine drawdown > 12% over last 50 trades | Engine cooldown | 1h rolling |
| Live PF < 0.74 × backtest PF after n ≥ 22 | Engine cooldown | 1h rolling |

**Paper demote** = `STRATEGY_<NAME>_LIVE` flag forced to 0 in PM state DB. Strategy continues generating signals; runner records them; **no live order placed**. Operator reverses via `POST /reinstate/<engine>` with `X-PM-Auth` header.

### 7.4 Promotion (lifecycle)

- Paper → Live: n ≥ 20 closures AND live PF within 20% of backtest PF → operator approves promotion.
- Live → Full: n ≥ 50 closures AND live PF ≥ 0.85 × backtest PF.
- Any → halted: triggers in §7.3 above.

---

## §11 Migration plan (mostly complete)

Phases 1–6 of v1.0 SPEC are **done**. Remaining:

- [ ] **Phase 8 (new): cap_frac activation for Tier 1 engines** — flip hlp_fade, stop_hunt, oi_concentration, vpoc_retest, fmom from 0.00 to canary allocations. Requires rebalancing existing GREEN tier to preserve invariant. **Awaits operator approval per §3.6.**
- [ ] **Phase 9 (new): 4-loss-permanent-demote implementation** — patch `common/cooldown.py` + `pm/pretrade.py` + add `POST /reinstate/<engine>` endpoint. Code patch in `patches/4_loss_demote.diff`.
- [ ] **Phase 10 (new): edge-improvement sentinel cycle** — each engine in GREEN+WATCH+TIER1 gets a sentinel council consult focused on (a) WR improvement, (b) drawdown reduction, (c) PF lift. Output: per-engine `edge_audit/<name>.md`.
- [ ] Decommission legacy services (v1.0 Phase 7) — partial; legacy engines archived in repo but legacy Render services may still be running. Operator to verify.

---

## §12 Acceptance criteria for v2.0 ship

- [ ] SPEC.md replaced with this v2.0 in repo `main`
- [ ] 4-loss-demote patch merged, tested with synthetic 4-loss sequence on testnet engine
- [ ] Tier 1 cap_frac allocation rebalance approved + deployed
- [ ] `/reinstate/<engine>` endpoint live, auth-gated
- [ ] Monitor's `pnl_attribution` routine updated to show paper-vs-live state per engine
- [ ] At least 1 sentinel edge consult completed per GREEN/WATCH engine

---

## §13 Out of scope v2.0

- Per-engine custom leverage (still flat 5× for v2)
- Per-engine custom margin pct (still flat 5%)
- Multi-account routing
- Re-port of any archived legacy engine without honest re-audit
- UZT_REV revival in any form
- HL spot trading

---

**END OF SPEC v2.0**

If ambiguous, ask the operator. If conflicting with `pm/pretrade.py:ENGINE_REGISTRY`, the code wins — update this SPEC.
