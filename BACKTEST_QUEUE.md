# Stage 1 Honest Backtest Queue — Phase 13 work plan (REVISED 2026-05-19)

**STATUS: SUPERSEDED.** Authoritative document is `STAGE1_GATES.md` (committed 2026-05-18). This file is retained for context and the per-engine telemetry/promotion ladder; the actual gate table and the "wait 14 days" action plan live in `STAGE1_GATES.md`.

**Why this got rediscovered:** The original Phase 13 plan (BACKTEST_QUEUE v1, 2026-05-19) assumed historical backtest was feasible for the 6 Stage 1 engines after the OKX REST liq fix. Rechecking against the previous day's sentinel-council work showed all 6 were already reclassified to Category C (live-paper-only) because:

- liq_cluster_hunt → no Binance forceOrder historical archive available
- funding_triangulation → no HL hourly funding archive available
- hl_whale_frontrun, hl_vault_predict, hl_cvd_aggressor, hl_depth_shock → HL-unique data with no historical proxy

**Action:** all 6 Stage 1 engines are already `STRATEGY_<NAME>_ENABLED=1` on `spm-strategy-runner`. They are scanning live in paper mode (no per-engine `_LIVE` set, global `LIVE_TRADING` unset → default False per `trader.py`). Re-eval per `STAGE1_GATES.md`:

| Engine | Cat | Promotion gate |
|---|---|---|
| `hl_cvd_aggressor` | C | n=30 paper, rolling-PF ≥ 1.5 |
| `hl_depth_shock` | C | n=30 paper, rolling-PF ≥ 1.5 |
| `hl_whale_frontrun` | C | n=50 paper, rolling-PF ≥ 1.5 |
| `hl_vault_predict` | C | n=30 paper, rolling-PF ≥ 2.0 |
| `liq_cluster_hunt` | C | n=40 paper, rolling-PF ≥ 1.5 |
| `funding_triangulation` | C | n=30 paper, rolling-PF ≥ 1.5 |

Estimated time to first gate: **14–21 days** of live paper accumulation. Monitor routine `auto_4loss_demote` already polls daily and updates `STAGE1_GATES.md`.

---

## Promotion ladder (post-GREEN, per STAGE1_GATES.md)

- n=30 @ rolling-PF ≥ 1.5 → canary 0.025 cap_frac
- n=75 @ rolling-PF ≥ 2.0 → 0.05
- n=150 @ rolling-PF ≥ 1.8 sustained → full registry cap

## Infrastructure follow-up (recommended by STAGE1_GATES.md, est 3–5 days)

To enable historical-backtest gating for future strategies:
1. Build historical liq feed — current OKX REST cold-load gives ~25h; need archive of `signal_bus/cache.flush_liqs()` SQLite output, which is **already accumulating passively** since 2026-05-19 deploy. In 14d we'll have 14d of liq archive.
2. Build HL funding rate historical via signal-bus persistence (already pulling, just need to archive).
3. Build Binance L2 depth proxy historical (Bybit has free 30d L2 archive).
4. Build CVD historical via Binance aggTrade replay (free, 30d history).

---

## Original v1 methodology (retained for reference; applies once historical data exists)

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
