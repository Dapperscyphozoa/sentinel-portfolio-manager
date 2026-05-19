# Sentinel Portfolio Manager — Project Specification v2.1

**Supersedes:** SPEC.md v2.0 (2026-05-18) in its entirety, and folds in `SPEC_v2.1_DELTA.md`.
**Updated:** 2026-05-19 against live `pm/pretrade.py:ENGINE_REGISTRY` (commit 60bf719) and live `spm-strategy-runner` /health.
**Mission:** unchanged. Single, lean, bloat-free algorithmic trading stack on Hyperliquid perps with Binance/OKX as signal venues and HL as execution venue.

This v2.1 codifies what the system **actually is** as of 2026-05-19 — the registry, the engines, the lifecycle rules. Anything not in this document is not in scope. If this SPEC diverges from `pm/pretrade.py:ENGINE_REGISTRY`, **the code wins** and this SPEC must be updated.

---

## §0 What changed since v2.0

- **UZT_REV v3 resurrected and shipped.** v2.0 declared UZT RED based on the v1 implementation (honest PF 0.18, postmortem at `references/uzt_postmortem.md`). The operator subsequently rebuilt the strategy from scratch as **reversal-only, single TP=5R, signal SL, no BE move, no partials, 40-bar time stop, Asia 00–05 UTC blocked, 16-coin curated universe** (commit `48fa280`). Honest backtest n=41, WR 68.3%, PF 6.92, three-sample consistency (90×20 PF 5.18 → 120×20 PF 5.69 → 120×30 PF 6.92). Registered live in PM 2026-05-19 (commit `6b99998`) at `cap_frac=0.05`. Formal phase-gated promotion contract in `VALIDATION_UZT_REV.md`.
- **e08_dip3d7_td_4h archived** (commit `6c77c8a`). Was GREEN 0.10 in v2.0 §3.1. PM registry retains the entry at cap_frac 0.10 but the strategy file is gone; runner does not load it. **Code-and-spec divergence flagged — see §3.10.**
- **cex_dex_arb and cascade_sniper_hl** archived in same commit (`6c77c8a`).
- **cross_coin_zscore killed** 2026-05-19 (commit `f4f54c6`) — sentinel CRITICAL unanimous, honest PF 0.99 over 90d × 10 pairs (n=223). Already noted in v2.0 §4.
- **Tier 1 engines activated:** `hlp_fade` (0.03), `stop_hunt` (0.02), `vpoc_retest` (0.03), `oi_concentration` (0.02) now live with capital. `fmom` remains at 0.00 paper-pending.
- **Stage 1 HL-specific engines** added at cap_frac 0.00 PROVISIONAL_NEW_ENGINE_PAPER: `hl_cvd_aggressor`, `funding_triangulation`, `liq_cluster_hunt`, `hl_whale_frontrun`, `hl_depth_shock`, `hl_vault_predict`. None backtested yet (`bt_n=0`).
- **4-loss workflow** (was DELTA): cooldown.demote_engine → monitor.auto_4loss_demote → sentinel audit + paper demote → auto-promote on 4 paper wins. Codified in §7.3 below.
- Sentinel scope (operator 2026-05-18, reaffirmed): consulted for edge development, loss reduction, profit increase, WR increase ONLY. Not consulted for live/paper sizing decisions or registry curation.

---

## §1 Architecture (unchanged from v1.0 / v2.0)

4 Render services: `signal-bus`, `strategy-runner`, `pm`, `monitor`. See v1.0 §2 for diagram. No structural change.

Additionally: a `sniper` micro-service runs the cascade-sniper-hl Variant B. Out of scope for this SPEC; documented in `DEPLOY_SNIPER.md`.

---

## §3 Engine Registry (replaces v2.0 §3 entirely)

The authoritative registry lives in `pm/pretrade.py:ENGINE_REGISTRY`. This SPEC table mirrors that source. **If they diverge, code wins.**

### 3.1 GREEN — real edge, live capital allocated

| Engine | Honest PF | n | cap_frac | TF | Affinity | Notes |
|---|---|---|---|---|---|---|
| `hl_settle_5m` | 1.85 | 55 | 0.20 | 5m | all regimes | Most-tested live. Promoted 2026-05-18 post short-only fix + denylist + TP 0.4% fee cleanup. |
| `ict_confluence_4h` | 3.18 | — | 0.15 | 4h | all regimes | Council-trimmed from 0.25 → 0.15. OOS PF 1.37 on longs (asymmetric, kept SHORT_ONLY=0). Routed through `live_safety`. |
| `e09_pump3d10_td_1d` | 2.20 | 26 | 0.10 | 1d | trend_down | n=26; over-allocated risk. Re-eval at n=50. |
| `uzt_rev` (v3) | **6.92** | 41 | **0.05** | 1h | trend_up/down/range/chop | **NEW.** Reversal-only ship config. 16-coin universe (UNI ETH ATOM FIL BNB LTC NEAR SOL APT ARB WIF DOGE DOT SUI APE AVAX). Single TP=5R, signal SL, no BE, no partials, 40-bar time stop, Asia 00–05 UTC blocked. Phase-gated promotion per `VALIDATION_UZT_REV.md`. Paper-mode awaiting first fire as of 2026-05-19. |
| `ict_confluence_1d` | 3.35 | — | 0.05 | 1d | all regimes | Paper-only via live_safety. |
| `e16_bb_fade_hv_1d` | 5.35 | 29 | 0.05 | 1d | high_vol | Council-trimmed from 0.10 (n=29 too thin). |
| `e01_zfade3s_tu_1d` | 1.29 | — | 0.05 | 1d | trend_up | |

**Subtotal: 0.65**

### 3.2 WATCH — green by PF but suspect IS/OOS or undersize n

(Currently merged into GREEN above — `e09_pump3d10_td_1d`, `e16_bb_fade_hv_1d`, `e01_zfade3s_tu_1d`.)

### 3.3 YELLOW — marginal (PF 1.0–1.4), paper mode only

| Engine | Honest PF | cap_frac | Notes |
|---|---|---|---|
| `e17_bb_fade_bt_1d` | 1.21 | 0.01 | |
| `e07_zfade2s_tu_1d` | 1.01 | 0.02 | |
| `e08_dip3d10_td_1d` | 0.50 / OOS 1.85 | 0.02 | OKX-positive Binance-suspect; trimmed for stop_hunt. |
| `e07_zfade2s_tu_4h` | 1.22 | 0.06 | |
| `e01_zfade3s_tu_4h` | 1.20 | 0.02 | |

**Paper-mode enforcement:** `STRATEGY_<NAME>_LIVE=0` env in `spm-strategy-runner`.

### 3.4 UNTESTED / new-engine paper — low-weight monitoring

| Engine | bt_pf (est) | bt_n | cap_frac | Affinity | Notes |
|---|---|---|---|---|---|
| `liq_cascade` | 1.30 | — | 0.05 | trend_up/down | Event-driven, sentinel-born, no walk-forward yet. |
| `e16_bb_fade_hv_4h` | 1.50 | 1 | 0.02 | high_vol | n=1 backtest; monitor only. |
| `hl_cvd_aggressor` | 2.20 | 0 | 0.00 | trend_up/down/range | World-first HL CVD aggressor flow. Paper pending honest backtest. |
| `funding_triangulation` | 2.00 | 0 | 0.00 | all regimes | HL funding vs Binance/OKX consensus, single-leg HL execution. |
| `liq_cluster_hunt` | 2.60 | 0 | 0.00 | all regimes | Predict sweep path from stacked liq cluster + round-number alignment. |
| `hl_whale_frontrun` | 3.20 | 0 | 0.00 | all regimes | World-first: copy new opens from top-20 HL wallets. Highest est edge. |
| `hl_depth_shock` | 2.10 | 0 | 0.00 | range/chop/high_vol | Fade bid/ask depth shocks before price catches down. |
| `hl_vault_predict` | 3.00 | 0 | 0.00 | all regimes | Anticipate HLP imminent rebalance from NAV-vs-mark divergence rate. |

All Stage 1 entries carry `audit_status: PROVISIONAL_NEW_ENGINE_PAPER`, `min_n_for_gate: 30`. Honest backtest required before any cap_frac > 0.

### 3.5 RED — halted (honest PF < 1.0 or fictional backtest)

| Engine | Honest PF | Reason | cap_frac |
|---|---|---|---|
| `e17_bb_fade_bt_4h` | 0.86 | Negative expectancy | 0.00 |
| `donchian` | 0.01 | Catastrophic | 0.00 |
| `cex_dex_arb` | 0.00 | Look-ahead bias unfixable on perps; file archived | 0.00 |
| `cascade_sniper_hl` | 0.00 | v1 dead; v2 in `sniper` service; file archived | 0.00 |

### 3.6 TIER 1 — built, mostly shipped

The Council #1 set. cap_frac changed from "0.00 awaiting approval" in v2.0 to active values 2026-05-18.

| Engine | Backtest PF | TF | Affinity | cap_frac | Status |
|---|---|---|---|---|---|
| `hlp_fade` | 2.50 | — | all regimes | **0.03** | Council #1 pick, world-first HLP vault fade. **ACTIVATED 2026-05-18**, council 3/5 YES with caveat: validate /hlp poll latency < 1s before first live fire. |
| `stop_hunt` | 3.00 | — | range/chop/high_vol | **0.02** | S/R wick-sweep + reversal. **ACTIVATED 2026-05-18** after news-spike ATR filter added (`STOPH_NEWS_SPIKE_ATR_MULT=3.0`). |
| `oi_concentration` | 2.75 | — | high_vol/range/chop | **0.02** | Pre-cascade detector. **ACTIVATED 2026-05-18** after real OI feed wired via `signal_bus.oi_poller`. |
| `vpoc_retest` | 1.90 | — | all regimes | **0.03** | Naked weekly POC magnet. **ACTIVATED 2026-05-18**, council 5/5 unanimous. |
| `fmom` | 1.75 | — | all regimes | 0.00 | Funding momentum (2nd-derivative). 3 critical bugs fixed 2026-05-18 (10–30× over-firing). Paper-pending-validation. |

### 3.7 LEGACY (archived in `strategy_runner/strategies/_archived/`)

Files present: `fsp.py`, `vsq.py`, `range_fade.py`, `range_breakout.py`, `lh1.py`, `fd1.py`, `precog.py`, `precog_pivot_rsi.py`, `cascade_sniper.py`, `cex_dex_arb.py`. Not loaded at runtime. STRATEGY_GATES.md documents the post-honest-backtest verdicts:

- `vsq` GREEN (honest PF 1.46) — archived because superseded by ICT/e-series.
- `lh1` YELLOW (honest PF 1.32) — archived; consider re-port if per-coin allowlist applied.
- `range_fade` YELLOW (honest PF 1.25) — re-run with full universe before any revival.
- `fd1` RED (honest PF 0.85) — permanently dead.
- `fsp`, `range_breakout`, `precog` — archived; never honest-audited under v2 framework; no revival plan.

### 3.8 Invariant

`sum(cap_frac for all engines in ENGINE_REGISTRY) == 1.00 ± 0.02`, asserted at module load (`promotion_gate.py`). Any rebalance must preserve this invariant.

Current sum tally (2026-05-19):
- GREEN: 0.20 + 0.15 + 0.10 + 0.05 + 0.05 + 0.05 + 0.05 = **0.65**
- YELLOW (paper-LIVE=0 but cap reserved): 0.01 + 0.02 + 0.02 + 0.06 + 0.02 = **0.13**
- UNTESTED: 0.05 + 0.02 = **0.07**
- TIER 1 activated: 0.03 + 0.02 + 0.02 + 0.03 = **0.10**
- RED + Stage 1 paper + e08_dip3d7_td_4h ghost: 0.10 + 0.00×many = **0.10**
- **Total: ~1.05** — over invariant. Rebalance required (see §11 Phase 12 below).

### 3.9 Runner vs PM registry divergence (live audit 2026-05-19)

`spm-strategy-runner /health` lists 26 active strategies. `pm/pretrade.py:ENGINE_REGISTRY` lists 25. Diff:

- **In PM, not in runner:** `uzt_rev` (file `uzt_rev.py` present but not loaded by `runner.py`; latest build `dep-d862hii8qa3s73ai2g9g` may pick it up). `e08_dip3d7_td_4h` (file archived but PM entry retained with cap_frac 0.10 — see §3.10).
- **In runner, not in PM:** none confirmed; all 26 runner entries map to PM registry rows or to archived files retaining strategy-side scan loops.
- **Open positions on halted strategies (4 of 5 currently open):** `cross_coin_zscore` (2), `hl_settle_5m` (1, despite GREEN — halt is on at runner level), `hl_depth_shock` (1, cap_frac 0.00). Halt blocks new fires; existing positions ride their brackets. **Acceptable per spec, not a bug.**

### 3.10 Ghost entry: `e08_dip3d7_td_4h`

The PM registry retains this engine at `cap_frac=0.10` with `bt_pf=0.93`, with a comment claiming OOS PF 2.01 promotion. The strategy **file is archived** (commit `6c77c8a` "archive(3 dead engines)"). Therefore:

- PM gate will allow signals if any are submitted, but runner cannot generate them (no module).
- The cap_frac 0.10 contributes to the over-invariant total in §3.8.

**Required action:** decide either (a) delete the PM entry and rebalance, or (b) restore the file and run honest re-backtest under v2.1 gate. Pending operator decision. Until resolved, treat the entry as ghost.

---

## §4 Dead Engine Registry (additive to v1.0 §4)

In addition to v1.0 §4 dead engines, add:

| Engine | Failure |
|---|---|
| `UZT_REV` v1 (Lesson #2 implementation, May 2026) | RED per §1.5 honest-backtest gate: PF 0.18, n=21, 30d × 4 majors. Postmortem at `references/uzt_postmortem.md`. **Superseded by `uzt_rev` v3** (now GREEN, see §3.1) — v3 is a different design (reversal-only, single TP=5R, 16-coin universe) and is **not** a revival of v1. |
| `donchian` | Backtest PF 0.01 — catastrophic |
| `cascade_sniper_hl` (v1) | Replaced by `sniper` service Variant B (separate deploy). v1 fictional PF, dead. File archived (commit `6c77c8a`). |
| `cex_dex_arb` | Look-ahead bias unfixable on perps. File archived (commit `6c77c8a`). |
| `e08_dip3d7_td_4h` | File archived (commit `6c77c8a`). Ghost PM registry entry pending cleanup — see §3.10. |
| `e17_bb_fade_bt_4h` | Honest PF 0.86 |
| `fd1` | Honest PF 0.85, OOS 0.78 |
| `cross_coin_zscore` | KILLED 2026-05-19 (commit `f4f54c6`). Honest PF 0.99 over 90d × 10 pairs (n=223). Sentinel council CRITICAL unanimous (4/4 valid voters, 100% confidence): thesis broken — crypto perp pair ratios are not cointegrated at 5m timescale; alts-vs-alts pairs (ARB/ETH, SUI/SOL, etc.) follow BTC beta and don't mean-revert. Even maximal-salvage estimate (2 cointegrated pairs, 1h TF, ADF gate, z-cross exit) projected PF ≤ 1.6, worst engine in green tier. Archived per operator directive. Do NOT re-register. |

Do not re-port any of these. **`uzt_rev` v3 is not a revival of v1 UZT_REV — it is a different strategy with the same prefix.**

---

## §7 Portfolio Manager v2.1 (replaces v2.0 §7)

### 7.1 Gate rules (codified in `pm/pretrade.py:check()`)

Order of evaluation:
1. `STRATEGY_<NAME>_ENABLED=0` env → reject `strategy_disabled`
2. `PM_FORCE_HALT_<NAME>=1` env → reject `halt_forced`
3. `BLOCKED_COINS` env (comma-sep) → reject `coin_blocked_operator`
4. **Global coin lock** — 1 position per coin across all engines → reject `coin_locked`
5. **Global concurrent cap** — `MAX_OPEN_POSITIONS` (default 20) → reject `max_open_global`
6. **Regime affinity** — if regime confidence > 0.7 and regime not in engine affinity → reject `regime_mismatch:<regime>`
7. **Cooldown checks** (§7.3 below) — reject `coin_cooldown` or `engine_cooldown` or `engine_paper_demoted`
8. **Sizing** — fixed `MARGIN_PCT_PER_TRADE × LEVERAGE` (default 5% × 5× = 25% notional/trade)
9. **Live-safety gate** for `{ict_confluence_4h, ict_confluence_1d, cascade_sniper_hl, uzt_rev}` — ATR-aware size override
10. Pass → return `CheckResult(allow=True, size_usd, "ok", bt_pf=<engine_pf>)`

### 7.2 Sizing

- `LEVERAGE = 5` (constant; not per-engine)
- `MARGIN_PCT_PER_TRADE = 0.05` (5% margin per fire)
- → notional = 25% of wallet per trade
- `MIN_TRADE_USD = 10` floor
- `MAX_MARGIN_FRAC = 1.0` (no over-margining)

cap_frac is **advisory only** for current production — used by dashboard and the promotion gate. Current production sizing is flat. Per-engine cap_frac sizing planned for v2.2.

### 7.3 Auto-cooldown rules + 4-loss workflow (folded in from SPEC_v2.1_DELTA)

| Trigger | Action | Duration |
|---|---|---|
| 4 consec losses on same (engine, coin) | Coin cooldown | 1h rolling |
| **4 consec losses on engine** | **Paper demote** (workflow below) | Permanent until reinstated |
| Engine drawdown > 12% over last 50 trades | Engine cooldown | 1h rolling |
| Live PF < 0.74 × backtest PF after n ≥ 22 | Engine cooldown | 1h rolling |

**4-loss permanent-demote workflow:**

```
  4 consec losses on engine
   ↓
  cooldown.demote_engine()  (PM-internal flag, paper_demoted=true)
   ↓
  monitor.auto_4loss_demote routine detects new row (polls every 5 min)
   ↓
  Step 1: fires sentinel audit on the engine's source code
          - Pulls source from GitHub raw
          - Uses haiku model via internal claude_client (~$0.005/audit)
          - Writes audit report to /var/data/audits/<engine>_<ts>.md
   ↓
  Step 2: flips STRATEGY_<NAME>_LIVE=0 via Render API on strategy-runner
   ↓
  Engine now runs in PAPER MODE (signals fire and record, no live HL orders)
   ↓
  Auto-promote watch (every 5 min):
    If last 4 PAPER closures all have pnl_usd > 0:
      - Flip STRATEGY_<NAME>_LIVE=1 via Render API
      - POST /reinstate/<engine> with X-Halt-Token (clears cooldown flag)
      - Record promotion in seen_demotions.promoted_ts
```

**Rule constants:**

| Constant | Value | Location |
|---|---|---|
| `CONSEC_LOSS_ENGINE` | 4 | `common/cooldown.py` |
| `WIN_STREAK_FOR_PROMOTE` | 4 | `monitor/routines/auto_4loss_demote.py` |
| Monitor poll interval | 300s | `monitor/server.py` |
| Sentinel audit model | `claude-haiku-4-5-20251001` | `monitor/claude_client.py` |
| Daily API budget | $5 | `DAILY_API_BUDGET_USD` env |

**Manual override:** `POST /reinstate/<engine>` (X-Halt-Token), or flip `STRATEGY_<NAME>_LIVE=1` env on strategy-runner.

**Fail-soft semantics:** if Render API or GitHub or claude_client fails, the demote/promote step proceeds without that piece; never blocks lifecycle.

### 7.4 Promotion (lifecycle)

- Paper → Live: n ≥ 20 closures AND live PF within 20% of backtest PF → operator approves promotion.
- Live → Full: n ≥ 50 closures AND live PF ≥ 0.85 × backtest PF.
- Any → halted: triggers in §7.3 above.

**Per-engine override:** `uzt_rev` follows `VALIDATION_UZT_REV.md` phase gates instead of the generic Paper→Live→Full ladder.

---

## §11 Migration plan (replaces v2.0 §11)

Phases 1–10 of v1.0 / v2.0 SPEC are **done**. Remaining:

- [x] Phase 11: 4-loss workflow + auto-audit + paper-win promotion (commit set in original DELTA)
- [ ] **Phase 12: cap_frac invariant rebalance.** Current sum 1.05; over-invariant. Decide: trim YELLOW (paper-mode caps that are advisory only) or delete ghost entries (e08_dip3d7_td_4h) to bring sum to 1.00 ± 0.02. **Awaits operator approval.**
- [ ] **Phase 13: Stage 1 honest backtest sweep.** Six HL-specific engines at `bt_n=0` need walk-forward honest backtests before any cap_frac > 0. Order: `hl_whale_frontrun` (highest est edge 3.20), `hl_vault_predict` (3.00), `liq_cluster_hunt` (2.60), `hl_cvd_aggressor` (2.20), `hl_depth_shock` (2.10), `funding_triangulation` (2.00).
- [ ] **Phase 14: `uzt_rev` v3 paper validation.** Per `VALIDATION_UZT_REV.md` Phase 0: ≥ 5 paper fires with well-formed `extras_json` before promoting to live. As of 2026-05-19 09:00 UTC, awaiting first paper fire.
- [ ] **Phase 15: ghost-entry cleanup.** Resolve `e08_dip3d7_td_4h` per §3.10. Either restore file + re-backtest, or delete PM entry.
- [ ] **Phase 16: edge-improvement sentinel cycle.** Per-engine sentinel consult focused on (a) WR improvement, (b) drawdown reduction, (c) PF lift. Output: `edge_audit/<name>.md`.

---

## §12 Acceptance criteria for v2.1 ship

- [x] SPEC.md replaced with this v2.1 in repo `main`
- [x] `SPEC_v2.1_DELTA.md` content folded into §7.3 above; delta file deletable
- [x] 4-loss-demote patch merged (v2.1 DELTA tracked it complete)
- [ ] cap_frac invariant rebalanced to 1.00 ± 0.02 (Phase 12)
- [ ] `uzt_rev` first paper fire recorded (Phase 14)
- [ ] At least 1 sentinel edge consult completed per GREEN engine
- [ ] Ghost `e08_dip3d7_td_4h` entry resolved (Phase 15)

---

## §13 Out of scope v2.1

- Per-engine custom leverage (still flat 5× for v2.1)
- Per-engine custom margin pct (still flat 5%; per-engine planned v2.2)
- Multi-account routing
- Re-port of any archived legacy engine without honest re-audit
- Revival of UZT v1 (the failed Lesson #2 implementation) — `uzt_rev` v3 is a different strategy, not a revival
- HL spot trading

---

**END OF SPEC v2.1**

If ambiguous, ask the operator. If conflicting with `pm/pretrade.py:ENGINE_REGISTRY`, the code wins — update this SPEC.
