# Sentinel Portfolio Manager — Claude Code Build Workflow

This is the **session-by-session build plan** for Claude Code. Read SPEC.md first; this doc tells you the order, the deliverable per session, and how to verify each session is complete before moving on.

**Working agreement:**
- Each session has a single goal. Do not advance to the next until acceptance criteria pass.
- Commit at the end of every session with the prefix from `Commit:` below.
- If you discover a SPEC.md ambiguity, stop, write the question in `OPEN_QUESTIONS.md`, and ask the operator before guessing.
- Never reintroduce a strategy from SPEC.md §4 (Dead Engine Registry).

---

## Pre-flight (operator does this once, ~30 min)

Operator (not Claude Code) executes:

```bash
# 1. Create the repo (operator runs on local or via GitHub UI)
gh repo create Dapperscyphozoa/sentinel-portfolio-manager --public

# 2. Halt legacy bleeders
curl -X POST -H "Authorization: Bearer $RENDER_TOKEN" \
  https://api.render.com/v1/services/srv-d826vjrrjlhs738frnsg/suspend   # trend-rider-v1
curl -X POST -H "Authorization: Bearer $RENDER_TOKEN" \
  https://api.render.com/v1/services/srv-d7vk8ad7vvec73dhpmrg/suspend   # vol-squeeze-fade

# 3. Backup /closures from every legacy engine
for svc in precog fsp-v1 liq-heatmap-v1 funding-div-v1 range-fade-v1 range-breakout-v1 strategies-bundle-v1; do
  curl -sS "https://${svc}.onrender.com/closures?limit=10000" > legacy-data/${svc}-closures-$(date -u +%Y%m%d).json
done
```

Then hand SPEC.md + WORKFLOW.md to Claude Code via `claude-code` CLI:

```bash
cd sentinel-portfolio-manager
git clone https://github.com/Dapperscyphozoa/sentinel-portfolio-manager.git .
cp ../SPEC.md ../WORKFLOW.md .
git add -A && git commit -m "spec: initial SPEC + WORKFLOW"
git push
claude-code .  # or whatever the invocation is for current Claude Code
# Then prompt: "Read SPEC.md and WORKFLOW.md. Begin Session 1."
```

---

## Session 1 — Scaffolding & repo skeleton

**Goal:** create the empty directory structure, `requirements.txt`, `render.yaml` placeholder, and a working `common/` layer.

**Deliverables:**
- Directory tree per SPEC.md §2.3
- `requirements.txt` with: `httpx`, `websockets`, `numpy`, `pandas`, `hyperliquid-python-sdk`, `eth_account`, `requests`, `apscheduler`
- `render.yaml` with 4 service stubs (no real envs yet)
- `common/persistence.py` — SQLite schema (signals, trades, closures, halts), `init_db()`
- `common/config.py` — env loader pattern
- `common/halt.py` — halt state object + token check
- `common/pm_client.py` — `check()`, `register_cloid()`, `regime()`, `attribution()`
- `common/bus_client.py` — `candles()`, `liq()`, `funding()`, `markprice()`, `hl_account()`, `hl_fills()`, `hl_positions()`
- `common/hl_exchange.py` — wraps `hyperliquid-python-sdk` for market/limit orders + cloid prefix hashing

**Acceptance:**
- `python3 -c "from common import persistence, config, halt, pm_client, bus_client, hl_exchange"` succeeds
- `pytest tests/test_common.py -k "schema or cloid"` passes
- `render.yaml` parses (`python3 -c "import yaml; yaml.safe_load(open('render.yaml'))"`)

**Commit:** `feat(scaffold): repo skeleton + common modules`

---

## Session 1.5 — Honest backtest validation gate (sentinel-required)

**Goal:** Before porting 4 questionable strategies (vsq, fd1, lh1, range_fade), prove their edge with honest signal-bus historical Binance data. Sentinel audit identified these 4 as having weak/unverified backtests and warned against porting without revalidation.

**Operator runs this manually before Session 4** — or Claude Code runs it as a standalone session after Session 3 if the operator wants automation.

**Deliverables:**
- `scripts/honest_backtest.py`:
  - Pulls 90d historical klines from signal-bus
  - Replays per strategy module's `evaluate()` against historical bars
  - **No module globals, no live HTTP calls inside strategies** — strategies receive only the bus interface, which itself only serves historical data in backtest mode
  - Outputs: per-strategy WR, PF, expectancy, OOS PF (walk-forward 45d train + 45d test), trade-by-trade JSON
- Run for: `vsq`, `fd1`, `lh1` (inverted version), `range_fade`
- Commit `backtests/<strategy>_<date>.md` for each

**Gate rules** (write to `STRATEGY_GATES.md`):

| Honest PF | Action |
|---|---|
| ≥ 1.4, OOS PF ≥ 1.0 | GREEN — port in Sessions 5-7 as planned |
| 1.0–1.4 OR OOS PF < 1.0 | YELLOW — port but flag in PM registry as `audit_status: PROVISIONAL`, no canary promotion, no live capital |
| < 1.0 | RED — do NOT port. Add to SPEC.md §4 (Dead Engine Registry). Find a replacement strategy or proceed with one fewer strategy. |

**Acceptance:**
- Honest backtest results committed for all 4 strategies
- `STRATEGY_GATES.md` lists GREEN/YELLOW/RED status per strategy
- Sessions 5-7 strategy list adjusted to exclude any RED entries

**Commit:** `gate(backtest): honest validation for vsq/fd1/lh1/range_fade per sentinel audit`

---

## Session 2 — signal-bus: Binance side

**Goal:** Binance WS subscriber operational, HTTP endpoints serve klines + liq + funding.

**Deliverables:**
- `signal_bus/binance_ws.py`:
  - Subscribes to combined stream for SPEC §5.1 set (klines 1m/5m/15m/1h + `!forceOrder@arr` + markPrice)
  - In-memory ring buffer: 1000 bars/coin/TF, 24h of liq events
  - Auto-reconnect with exponential backoff (min 1s, max 60s)
- `signal_bus/cache.py`:
  - Hourly SQLite flush of klines
  - 5-min flush of liq events
  - Cold-start: load last 24h from SQLite on boot
- `signal_bus/server.py`:
  - `GET /health` — WS status, cache sizes, last-update timestamps
  - `GET /candles/{coin}/{tf}?n=N` — returns array of OHLCV dicts
  - `GET /liq?since=<ms>&coin=<optional>` — returns liq events
  - `GET /funding/{coin}?hours=N` — for Binance (HL funding added in Session 3)
  - `GET /markprice/{coin}` — current Binance + (later) HL mid

**Acceptance:**
- Local dev: WS connects, `/health` shows `ws_alive.binance=true`, `/candles/BTC/1h?n=10` returns 10 bars within 30s of startup
- After 1h of running, SQLite has ≥360 BTC 1m bars cached, ≥60 1h bars
- No 429 errors in logs (this is the whole point)
- Deploy to Render. After 24h: `/health` still alive, WS uptime >95%.

**Commit:** `feat(signal-bus): Binance WS klines + liq + markprice + HTTP API`

---

## Session 3 — signal-bus: HL side

**Goal:** HL WS subscribed, account/fills/positions served. Cross-venue markPrice complete.

**Deliverables:**
- `signal_bus/hl_ws.py`:
  - WS to `wss://api.hyperliquid.xyz/ws`
  - Subscribe `userFills`, `webData2` for `HL_AGENT_WALLET`
  - Maintain in-memory state: open positions, recent fills, account value
- `signal_bus/server.py` additions:
  - `GET /hl/account` — `{value, margin_used, positions}`
  - `GET /hl/fills?since=<ms>` — fills array
  - `GET /hl/positions` — open positions array
- `signal_bus/server.py` `/markprice` now returns `{hl_mid, binance_mid}` (OKX + Bybit deferred to Session 11)
- `signal_bus/cache.py`: fills persistence (SQLite)

**Acceptance:**
- `/hl/account` returns operator's actual wallet value (verify against HL UI)
- A test fill (manual on HL) appears in `/hl/fills?since=...` within 5s
- `/markprice/BTC` returns both `hl_mid` and `binance_mid`
- Deploy. After 24h: HL fills are 100% caught (compare against HL UI fills log).

**Commit:** `feat(signal-bus): HL WS fills/positions/account + cross-venue markprice`

---

## Session 4 — strategy-runner skeleton + first strategy (`fsp`)

**Goal:** end-to-end: signal-bus → runner → PM check → HL order. Validate with `fsp` (smallest strategy, cleanest port).

**Deliverables:**
- `strategy_runner/strategies/_base.py` — `Signal` dataclass, `StrategyBase` ABC per SPEC §6.1
- `strategy_runner/runner.py` — registry, scan dispatch per SPEC §6.2
- `strategy_runner/trader.py`:
  - `open(signal, size_usd, cloid_prefix)` — places HL order, persists to SQLite
  - `position_loop()` — every 60s, check open trades for SL/TP/timeout via bus markprice, close if hit
- `strategy_runner/server.py`:
  - `GET /health`, `/state`, `/closures`, `/signals`
  - `POST /halt/<name>` (X-Halt-Token), `POST /halt/all`
- `strategy_runner/strategies/fsp.py` — implement per SPEC §3.1, using `bus.funding(coin, hours=4)` instead of direct HL calls
- Deploy to Render, paper mode (`STRATEGY_FSP_LIVE=0`)
- PM service stays as legacy for now — pointed via `PM_URL` env

**Acceptance:**
- `fsp` registered, scan loop runs every 5min
- Manual trigger: inject a synthetic extreme-funding event in test mode → `fsp` fires, signal recorded, paper trade opened, SL/TP set, position monitor tracks it
- Side-by-side with legacy `fsp-v1` service for 24h: if real funding extreme fires, both should fire the same signal (within 1 scan cycle = ≤5min). If only one fires, debug.
- After 24h: `fsp` in new runner has ≥1 signal recorded with full `extras_json`, or `≥1 cycles run with no firing conditions present` (acceptable if no real extreme in window).

**Commit:** `feat(strategy-runner): scaffold + fsp port; side-by-side validated`

---

## Session 5 — Port `range_fade` + `range_bo`

**Goal:** two more strategies, simple price-action.

**Deliverables:**
- `strategy_runner/strategies/range_fade.py` per SPEC §3.3
- `strategy_runner/strategies/range_breakout.py` per SPEC §3.4
- Unit tests: synthetic candles → assert correct fire/no-fire

**Acceptance:**
- Both strategies scan every 5min
- Side-by-side with legacy `range-fade-v1` and `range-breakout-v1` services for 24h: if either fires in either system, the other should too (or document why not — usually different universe or scan timing).
- No 429s in logs anywhere (signal-bus is doing its job)

**Commit:** `feat(strategy-runner): range_fade + range_bo ported`

---

## Session 6 — Port `vsq` + honest re-backtest

**Goal:** finally let vsq run with adequate data. AND re-validate its PF 3.04 claim with honest signal-bus historical Binance data.

**Deliverables:**
- `strategy_runner/strategies/vsq.py` per SPEC §3.2
- `scripts/backtest_harness.py`:
  - Pulls 90d of historical klines from signal-bus (or directly from Binance REST as fallback)
  - Replays through strategy `evaluate()` with synthetic time → produces trades, computes WR/PF/expectancy
  - **Critical:** no live calls from inside strategies during backtest — bus client points at historical replay endpoint
- Run `python3 scripts/backtest_harness.py --strategy vsq --days 90 --universe BTC,ETH,SOL,...`
- Report results: if honest PF ≥ 1.4, proceed. If <1.0, file `VSQ_AUDIT.md` with findings before promoting.

**Acceptance:**
- vsq deployed paper
- Honest 90d backtest report committed (`backtests/vsq_<date>.md`)
- If PF<1.0: strategy stays enabled but flagged "fictional_backtest" in PM registry, no canary promotion until reparameterized

**Commit:** `feat(strategy-runner): vsq port + honest backtest harness; result documented`

---

## Session 7 — Port `fd1` + `lh1`

**Goal:** complete the canary-tier batch.

**Deliverables:**
- `strategy_runner/strategies/fd1.py` per SPEC §3.6
- `strategy_runner/strategies/lh1.py` per SPEC §3.5 (inverted version)
- Side-by-side validation 24h each

**Acceptance:**
- Both firing or both quiescent (no orphan fires)
- Inversion logic in lh1 verified: a synthetic SSL sweep produces a LONG signal (legacy original would produce SHORT)

**Commit:** `feat(strategy-runner): fd1 + lh1 (inverted) ported`

---

## Session 8 — Port `precog`

**Goal:** the largest port — multi-system confluence with structural gate.

**Deliverables:**
- `strategy_runner/strategies/precog.py`:
  - Port struct_n monotonic-pivot gate from legacy `precog-hl/confluence_engine.py`
  - Port confluence variants (BTC_WALL, OI, CVD, OBI, SNIPER) — keep CONF_MIN_SYS=2
  - HL-specific data via bus (`bus.hl_walls(coin)`, `bus.hl_oi(coin)`, `bus.hl_cvd(coin)`) — these endpoints need to be added to signal-bus in this session
- Signal-bus additions for HL-specific data:
  - `bus.hl_walls(coin)` — top bid/ask walls
  - `bus.hl_oi(coin)` — open interest series
  - `bus.hl_cvd(coin)` — cumulative volume delta
- Deploy paper mode

**Acceptance:**
- precog firing at expected ~3-10/day on FINAL config universe
- Compare first 20 fires against legacy precog service — same coin + side + within 1 scan window
- Structural gate verified: when last 3 pivot lows ascending, longs are NOT blocked; when descending, longs ARE blocked

**Commit:** `feat(strategy-runner): precog port with struct_n gate + HL confluence endpoints`

---

## Session 9 — `liq_cascade` with Binance liq stream

**Goal:** activate the killer feature — Binance liquidation feed instead of HL's sparse one.

**Deliverables:**
- `strategy_runner/strategies/liq_cascade.py` per SPEC §3.8
- Verify signal-bus `/liq` endpoint returns Binance forceOrder events with proper schema
- Backtest 90d using `bus.liq_historical(coin, since=T)` — confirm cascade events 10-30× denser than HL would have provided
- Deploy paper mode

**Acceptance:**
- `/liq?since=<24h ago>` returns ≥1000 events across universe (HL would have returned ~50)
- liq_cascade fires within 5s of a qualifying cascade event (event-driven, not scan-driven)
- Regime-aware direction verified: in trend_down regime, long-liq cascade produces SELL; in range, produces BUY

**Commit:** `feat(strategy-runner): liq_cascade with Binance liq stream`

---

## Session 10 — `cex_dex_arb` with 4-venue basis

**Goal:** rebuild this with rigorous honest backtest. Past PF claims were fictional (look-ahead bias); this version is bulletproof.

**Deliverables:**
- Signal-bus additions: OKX + Bybit WS markPrice subscribers, `/markprice/{coin}` now returns `{hl_mid, binance_mid, okx_mid, bybit_mid}`
- `strategy_runner/strategies/cex_dex_arb.py` per SPEC §3.9
- Honest backtest harness mod:
  - `bus.markprice_historical(venue, coin, ts)` — replay historical from each venue's REST
  - Run 90d backtest with this — no module-global caches, no live calls
- Report: committed to `backtests/cex_dex_arb_<date>.md`. If PF <1.2, strategy stays disabled in production until reparameterized.

**Acceptance:**
- 4 venue WS feeds healthy
- Honest backtest committed
- If PF≥1.2 paper, deploy paper mode
- If PF<1.2: write findings, do not deploy, flag in OPEN_QUESTIONS.md

**Commit:** `feat(strategy-runner): cex_dex_arb 4-venue + honest backtest`

---

## Session 11 — New PM (slim rewrite)

**Goal:** drop the 38 dead registry entries; PM is now lean.

**Deliverables:**
- `pm/server.py` — HTTP server with `/check`, `/regime`, `/lifecycle/<engine>`, `/lifecycle/promote`, `/attribution`, `/halt/<engine>`
- `pm/registry.py` — exactly 9 entries per SPEC §7.1
- `pm/pretrade_gate.py` — port Rule 5b verbatim including `trend_direction_aware` patch
- `pm/lifecycle.py` — stage transitions (paper → canary → full), capital_fraction scaling
- `pm/attribution.py` — consumes `bus.hl_fills()` instead of subscribing HL WS directly
- Deploy. Point `spm-strategy-runner` at it via `PM_URL` env.

**Acceptance:**
- All 9 strategies in registry, none of the 38 zombies
- Pre-trade gate denies a known-bad trade (test: try opening BTC LONG when regime=trend_down at conf 1.0 + strategy has no trend_direction_aware → denied with reason=`regime_affinity_mismatch`)
- Same trade with `trend_direction_aware: True` → allowed at half size
- Capital_fraction allocations sum to 1.00

**Commit:** `feat(pm): slim rewrite — 9 entries, Rule 5b preserved`

---

## Session 12 — Monitor service + Claude Code routines

**Goal:** autonomous health checks firing via Anthropic Messages API.

**Deliverables:**
- `monitor/claude_client.py` per SPEC §8.1
- `monitor/scheduler.py`:
  - apscheduler-driven loop
  - Daily-budget guard: track spend in SQLite, halt firing if >$5/day
- `monitor/routines/`:
  - `silent_engines.py` — checks each strategy's fire rate vs expected
  - `fee_drag.py` — `fees / |gross_pnl|` per strategy
  - `regime_shift.py` — detect btc-hmm transitions, suggest size_mult changes
  - `pnl_attribution.py` — per-coin per-strategy bleed identification
  - `promotion_candidates.py` — paper engines with n≥50, PF>1.4
  - `demotion_candidates.py` — live engines with PF < 0.7 × backtest PF after 50 closures
  - `drawdown_watch.py` — if account DD >5%, halt all (THIS is the only auto-action routine)
  - `sentinel_audit.py` — on-demand, invokes the installed sentinel skill
- `monitor/server.py`:
  - `GET /health` — last routine runs, spend stats
  - `POST /audit` — triggers `sentinel_audit` with payload
  - `GET /reports/<routine>?since=<ts>` — historical routine outputs
- Deploy. First 24h must produce ≥6 routine reports persisted to `/var/data/reports/`.

**Acceptance:**
- Each routine fires successfully ≥1 time and JSON output is parseable
- Daily spend tracked correctly (sum of token usage × per-model price)
- `drawdown_watch` end-to-end test: artificially set account value to trigger 5% DD → routine fires → halt_all executes → all 9 strategies show `halted: true`
- After drill, operator restarts manually (no auto-resume)

**Commit:** `feat(monitor): Claude routines + autonomous drawdown halt`

---

## Session 13 — Decommission legacy + final cutover

**Goal:** old stack gone, new stack is the only stack.

**Deliverables:**
- For each legacy service (precog, fsp-v1, lh1, fd1, range-fade-v1, range-breakout-v1, strategies-bundle-v1, vsf, trend-rider-v1, btc-hmm-regime, portfolio-manager-legacy):
  - Final `/closures` + `/state` snapshot → `legacy-data/<svc>-<date>.json`
  - Halt the service
  - Wait 24h for any in-flight orders to settle
  - Delete the service from Render
- Update existing dashboard (`quant-stack-dashboard-phbu.onrender.com`):
  - Repoint at `spm-pm.onrender.com`
  - Update FULL_API_ENGINES list to the 9 new strategies
- Tag every legacy GitHub repo with `archived-pre-rebuild` and add `ARCHIVED: see sentinel-portfolio-manager` to its README

**Acceptance:**
- Render dashboard shows 4 services in the spm-* prefix; no legacy services running
- `legacy-data/` populated with one JSON file per old service
- Existing dashboard renders all 9 strategies with live data from new PM
- Operator can place a manual order via HL UI and see it attributed in `monitor/reports/pnl_attribution/`

**Commit:** `chore(decommission): legacy stack retired; new stack is canonical`

---

## Verification rituals (run after every session)

**Side-by-side validation rules (sentinel-corrected for low-frequency strategies):**

24h matching windows fail for low-frequency strategies (fsp ≈1/wk, precog ≈3-10/day, liq_cascade event-driven). Use this tiered approach:

| Strategy fires/day (expected) | Validation method |
|---|---|
| ≥ 10/day | 24h side-by-side: ≥3 matching fires required to cut over |
| 1-10/day | 72h side-by-side: ≥2 matching fires required |
| < 1/day  | Skip side-by-side. Instead: replay 30d historical via `scripts/honest_backtest.py` and `scripts/replay_strategy.py`; require new implementation produces same trade list as a separate legacy-port script reading the legacy code. Operator approval required to cut over without live matching. |

```bash
# Before committing:
python3 -m py_compile $(git ls-files '*.py')        # syntax check
pytest tests/                                        # all unit tests
python3 -c "import yaml; yaml.safe_load(open('render.yaml'))"  # blueprint valid

# After deploying (each affected service):
curl -sS https://spm-<svc>.onrender.com/health      # liveness
curl -sS https://spm-<svc>.onrender.com/state       # state shape

# Cross-service smoke test (post Session 4):
curl -sS https://spm-signal-bus.onrender.com/candles/BTC/1h?n=5 | jq '.[0]'
curl -sS https://spm-strategy-runner.onrender.com/state | jq '.open_trades | length'
```

## Open questions are tracked in `OPEN_QUESTIONS.md`

Anything Claude Code is unsure about: append to that file with timestamp, context, and recommended resolution. Operator reviews between sessions.

## Done is done

When Session 13 acceptance passes:
- Tag the repo `v1.0.0`
- Operator updates Anthropic memory: replace MULTICA description with `sentinel-portfolio-manager` description
- This document and SPEC.md remain canonical; future changes go through proper PR review

**END OF WORKFLOW**