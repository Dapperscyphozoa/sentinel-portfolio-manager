# Sentinel-Poly — Claude Code Build Workflow

Session-by-session build plan for `sentinel-poly`. Read POLY_SPEC.md first; this doc gives the order and acceptance criteria.

**Working agreement:**
- Each session has one goal. Do not advance until acceptance passes.
- Commit with the prefix from `Commit:` below.
- If POLY_SPEC.md is ambiguous, stop and append to `OPEN_QUESTIONS.md`. Do not guess.
- Do not place LIVE Polymarket orders until Session 4 acceptance (cl_aggregator_validate passes 5bps gate).

---

## Pre-flight (operator does, ~20 min)

1. **Generate a fresh Polygon wallet** dedicated to this rail. Do not reuse the HL wallets. Fund with ~$100 USDC.e + ~$10 MATIC for gas.
2. **Register Polymarket API key** via PM dashboard (requires wallet signature). Save: `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE`.
3. **Create AWS account** if not already (operator has one for ad-hoc). Spin a `t3.medium` in us-east-1 (we deploy onto this in Session 6). Save SSH key + public IP.
4. **Branch already exists:** `sentinel-poly` on `Dapperscyphozoa/sentinel-portfolio-manager`. POLY_SPEC.md, POLY_WORKFLOW.md, and poly_signer.rs are already at root.
5. Hand to Claude Code:
   ```
   git clone -b sentinel-poly https://github.com/Dapperscyphozoa/sentinel-portfolio-manager.git
   cd sentinel-portfolio-manager
   claude-code .
   ```
   Then prompt: *"You are on branch `sentinel-poly` of the sentinel-portfolio-manager repo. Read POLY_SPEC.md and POLY_WORKFLOW.md. Do not modify SPM files (SPEC.md, WORKFLOW.md, signal_bus/, strategy_runner/, pm/, monitor/, STRATEGY_GATES.md, VALIDATION_UZT_REV.md). Begin Session 1."*

---

## Session 1 — Scaffolding + poly directories

**Goal:** poly directory skeleton, render.yaml additions for poly-pm + poly-monitor (append, do not replace SPM services).

**Deliverables:**
- New top-level dirs per POLY_SPEC §2.4: `poly_signal_bus/`, `poly_runner/`, `poly_signer/`, `poly_pm/`, `poly_monitor/` with empty `__init__.py` + stub `server.py` files where applicable
- `aws-userdata.sh` at repo root — bootstrap script for us-east-1 t3.medium (apt installs, systemd units for bus/runner/signer)
- **Do not duplicate `common/`.** SPM's `common/persistence.py`, `common/halt.py`, `common/config.py` are reused. If poly needs additional shared utilities, add them as new files (e.g. `common/poly_signer_client.py` for the Unix-socket Python client) — never modify existing common/ files in a way that breaks SPM.
- Append to `render.yaml`: `poly-pm` and `poly-monitor` service blocks. Leave all existing `spm-*` blocks intact.
- Append to `requirements.txt` if needed: `scipy` (for `cl_predictor`'s norm.cdf). Do not remove existing deps.

**Acceptance:**
- `python3 -c "from common import persistence, halt, config"` still works (SPM common/ unchanged)
- `python3 -c "from common.poly_signer_client import sign_order"` works (new file, no breakage)
- `bash aws-userdata.sh` runs to completion in a local Docker Ubuntu 22.04 (no-op safe)
- `python3 -c "import yaml; d=yaml.safe_load(open('render.yaml')); names=[s['name'] for s in d['services']]; assert 'spm-pm' in names and 'poly-pm' in names"`

**Commit:** `feat(scaffold): repo skeleton + common modules + aws bootstrap`

---

## Session 2 — `poly-signal-bus`: CEX multi-venue + cache

**Goal:** subscribe 7 CEX venues, cache 60s of ticks per venue, serve `/cex_consensus`.

**Deliverables:**
- `poly_signal_bus/cex_ws.py`:
  - 7 WS subscribers (Binance, Coinbase, Kraken, Bitstamp, Bitfinex, OKX, Huobi)
  - Each parses to `{venue, asset, ts_ms, mid_price}` and pushes to ring buffer
  - Auto-reconnect with exponential backoff
- `poly_signal_bus/cache.py`:
  - Per-venue-per-asset ring of last 600 ticks (10 min at 1s)
  - SQLite flush every 60s for replay
- `poly_signal_bus/server.py`:
  - `GET /health` — per-venue WS status + last-tick age
  - `GET /cex_consensus/{asset}` — `{venue1: price, venue2: price, ...}` snapshot
  - `GET /candles/{venue}/{asset}/1s?n=N` — for historical replay

**Acceptance:**
- After 5 min uptime, all 7 venues show last-tick age <2s
- `/cex_consensus/BTC` returns 7 prices, all within 30bps of each other in calm market

**Commit:** `feat(bus): 7-venue CEX WS aggregator + cache`

---

## Session 3 — `poly-signal-bus`: Chainlink Data Stream + aggregator replication

**Goal:** subscribe Chainlink Data Stream for BTC/USD and ETH/USD on Polygon. Implement local aggregator that replicates the DON's median-with-trim algorithm.

**Deliverables:**
- `poly_signal_bus/chainlink_stream.py`:
  - Connects to Chainlink Data Stream WS (Polygon Mainnet)
  - For BTC/USD and ETH/USD feeds
  - Parses each report → `{feed_id, ts, benchmark_price, observation_ts, raw_signers}`
- `poly_signal_bus/cl_aggregator.py`:
  - Pure function: `aggregate(cex_consensus: dict[venue, price]) -> float`
  - Algorithm: drop ticks >50bps from median, then re-median the survivors
  - Match the DON's documented aggregation as closely as possible (verify against ChainlinkSecure docs)
- `poly_signal_bus/server.py` additions:
  - `GET /cl_actual/{asset}` — latest Chainlink-reported price
  - `GET /cl_predicted/{asset}` — local aggregator prediction
  - `GET /cl_divergence/{asset}` — `{predicted, actual, diff_bps, last_match_ts}`

**Acceptance:**
- After 1h: `cl_predicted` and `cl_actual` for BTC match within 5bps on ≥95% of samples (logged to SQLite, computed by query)
- If FAIL: write findings to `OPEN_QUESTIONS.md`, do NOT proceed. Either re-tune aggregator or kill `cl_predictor` strategy from POLY_SPEC.

**Commit:** `feat(bus): Chainlink Data Stream + DON aggregator replication`

---

## Session 4 — `cl_aggregator_validate.py` — the 5bps gate

**Goal:** 30 days of historical replay. Prove the gate before committing further engineering.

**Deliverables:**
- `scripts/cl_aggregator_validate.py`:
  - Pulls 30d historical CEX prices from each venue (where REST history available — Binance, Coinbase, Kraken, OKX have it; Bitstamp/Bitfinex/Huobi may need to use signal-bus's own 30d SQLite if available)
  - Pulls 30d historical Chainlink BTC/USD ticks from on-chain (`eth_getLogs` on the aggregator contract; or Chainlink's historical Data Stream archive)
  - For each Chainlink tick: compute what local aggregator would have predicted at the same timestamp using historical CEX prices
  - Output: `{median_abs_error_bps, p95_error_bps, p99_error_bps, n_samples, fail_rate}`

**Gate rule:**
- median_abs_error_bps < 5  AND  p95 < 15  AND  n_samples > 100,000 → PASS
- Anything else → FAIL: kill `cl_predictor` and `endgame` strategies in POLY_SPEC §3 (only `maker_quote`, `cross_asset`, `reflexivity_emitter` survive)

**Acceptance:**
- Validation report committed: `validation/cl_aggregator_<date>.md`
- If PASS: proceed to Session 5
- If FAIL: STOP. Operator decides whether to (a) deepen the aggregator (more venues, refined trim), (b) ship reduced-strategy version, or (c) abort the project.

**Commit:** `gate(validate): cl_aggregator 5bps prediction gate <PASS|FAIL>`

---

## Session 5 — `poly-signal-bus`: Polymarket CLOB + market discovery

**Goal:** subscribe PM CLOB WS for active BTC/ETH 5m markets, poll REST for new market discovery.

**Deliverables:**
- `poly_signal_bus/polymarket_clob.py`:
  - REST poll every 30s: discover active `btc-updown-5m-*` and `eth-updown-5m-*` markets
  - WS subscribe to each active market's CLOB feed (order book deltas, trades)
  - Track per-market state: `{token_id_yes, token_id_no, best_bid_yes, best_ask_yes, ...}`
- `poly_signal_bus/server.py` additions:
  - `GET /market_list` — current active markets with start_ts, end_ts, time_remaining
  - `GET /pm_book/{market_id}` — full book snapshot
  - `GET /implied_prob/{market_id}` — `{yes_mid, no_mid, yes_implied, no_implied}`
- Compute `implied_prob` as midpoint of yes_bid/yes_ask, with sanity check that `yes + no ≈ 1.00 ± fee`

**Acceptance:**
- `/market_list` returns ≥2 active BTC + ≥2 active ETH markets (overlapping windows)
- For a market with >$1k recent volume: book depth `n_levels >= 5` on both sides
- `implied_prob.yes_implied + no_implied` is within 5% of 1.00

**Commit:** `feat(bus): Polymarket CLOB WS + market discovery + implied prob`

---

## Session 6 — `poly-signer` Rust microservice

**Goal:** ship the Rust signer, deploy to us-east-1, verify <5ms sign + <100ms wall-clock.

**Deliverables:**
- `poly_signer/Cargo.toml` per POLY_SPEC §2.4 (or use `/home/claude/poly/poly_signer.rs` as starting point)
- `poly_signer/src/main.rs` (or expanded from skeleton)
- `poly_signer/src/eip712.rs` — verified PolymarketOrder schema against current docs
- `poly_signer/src/submit.rs` — HTTP POST to PM CLOB, L2 HMAC auth headers
- `poly_signer/src/socket.rs` — Unix domain socket server
- `aws-userdata.sh` updated: installs Rust, builds release binary, creates systemd unit `poly-signer.service`
- `tests/test_signer_roundtrip.py` — Python sends test order request over socket, asserts response shape (use Polymarket testnet or signed-but-unsubmitted dry-run flag)

**Acceptance:**
- Deploy signer to us-east-1 t3.medium (manual, via SSH + git clone + cargo build --release)
- Submit 100 sample orders in dry-run mode: median sign_ms < 5, p99 < 15
- Total wall-clock (sign + POST to PM mock or testnet): median < 100ms, p99 < 250ms

**Commit:** `feat(signer): Rust EIP-712 signer microservice + us-east-1 deploy`

---

## Session 7 — `poly-runner` skeleton + `maker_quote` (simplest strategy first)

**Goal:** end-to-end paper trade: bus → runner → signer → PM testnet. Validate plumbing with the boring strategy.

**Deliverables:**
- `poly_runner/strategies/_base.py` — `Signal`, `Quote`, `StrategyBase`
- `poly_runner/runner.py` — registry, dispatch, position monitor at 1s cadence
- `poly_runner/signer_client.py` — Unix socket client
- `poly_runner/strategies/maker_quote.py` — per POLY_SPEC §3.3
- `poly_runner/server.py` — `/health`, `/state`, `/closures`, `/halt/<name>`
- Deploy to us-east-1 alongside bus + signer (single VM, systemd units)
- Paper mode: orders are signed but only logged, not POSTed (env flag `POLY_LIVE=0`)

**Acceptance:**
- Quotes posting at 4 Hz per market (250ms requote interval), inventory-aware skew working
- Position monitor cancels + reposts on 0.1% asset move
- 24h paper run produces 100+ quote events logged, zero crashes, signer p99 < 250ms

**Commit:** `feat(runner): scaffold + maker_quote paper`

---

## Session 8 — `cl_predictor` + `endgame`

**Goal:** ship the two latency-sensitive strategies and honest-backtest them before live.

**Deliverables:**
- `poly_runner/strategies/cl_predictor.py` per POLY_SPEC §3.1
- `poly_runner/strategies/endgame.py` per POLY_SPEC §3.2
- `scripts/honest_backtest.py`:
  - Replay 60d using bus's historical mode (CEX SQLite + CL ticks + PM CLOB depth from PM data exports if available, otherwise the strategy is paper-only)
  - Walk-forward 30d train / 30d test
  - Output WR, PF, expectancy, OOS PF per strategy
- Run backtest for both strategies. Commit `backtests/cl_predictor_<date>.md` and `backtests/endgame_<date>.md`.

**Gate rule (per POLY_SPEC §4):**
- OOS PF ≥ 2.0: ship to canary at capital_fraction 0.025
- 1.4 ≤ OOS PF < 2.0: ship to paper, canary only after n=50 paper fires confirm
- < 1.4: do not deploy

**Acceptance:**
- Both strategies running in paper at minimum
- Honest backtest reports committed
- Strategies at GREEN gate are promoted to canary in poly-pm registry (Session 10)

**Commit:** `feat(runner): cl_predictor + endgame + honest backtests`

---

## Session 9 — `cross_asset` + `reflexivity_emitter`

**Goal:** ship the two non-latency-sensitive strategies. reflexivity_emitter publishes a feed that SPM consumes.

**Deliverables:**
- `poly_runner/strategies/cross_asset.py` per POLY_SPEC §3.4
- `poly_runner/strategies/reflexivity_emitter.py` per POLY_SPEC §3.5 (publishes `/reflex_signal/{asset}`)
- Honest backtest for `cross_asset` (`reflexivity_emitter` is paper-only at this stage; SPM-side consumer is a separate effort)

**Acceptance:**
- cross_asset honest backtest committed
- reflexivity_emitter publishing well-formed signal events
- SPM is updated separately (out of scope for this repo) to consume `/reflex_signal/*`

**Commit:** `feat(runner): cross_asset + reflexivity_emitter`

---

## Session 10 — `poly-pm` + `poly-monitor` + canary promotion

**Goal:** governance layer. PM gates trades, monitor watches the rail.

**Deliverables:**
- `poly_pm/server.py` per POLY_SPEC §2.1 (mirror SPM PM patterns)
- `poly_pm/registry.py` — 5 strategy entries: cl_predictor, endgame, maker_quote, cross_asset, reflexivity_emitter (the last is informational, doesn't trade PM)
- `poly_pm/lifecycle.py` — paper / canary / full with capital_fraction
- `poly_monitor/server.py`, `poly_monitor/routines/*` — routines: `cl_drift_check` (alert if cl_aggregator divergence drifts >10bps for >1h), `fee_drag`, `inventory_skew`, `promotion_candidates`, `drawdown_watch`
- Deploy poly-pm + poly-monitor to Render free tier
- Wire poly-runner to talk to poly-pm via `POLY_PM_URL` env

**Acceptance:**
- PM denies a trade exceeding capital_fraction
- drawdown_watch end-to-end drill: synthetic 8% DD → halt-all fires → all 5 strategies show `halted: true`
- First 24h of monitor routines produces ≥5 JSON reports persisted

**Commit:** `feat(pm+monitor): governance + canary lifecycle + drawdown halt`

---

## Session 11 — Cutover to live + scale gates

**Goal:** lift `POLY_LIVE=0` flag for GREEN-gated strategies, watch closely.

**Deliverables:**
- For each GREEN-gated strategy (from Sessions 8-9 honest backtests):
  - `POLY_LIVE=1` and `capital_fraction=0.025` (canary tier)
  - Monitor for n=20 fires. If rolling-20 PF ≥ 2.0 → promote to `capital_fraction=0.05`
  - At n=50 if PF ≥ 3.0 → promote to full allocation
  - At any point, if rolling-20 PF < 1.5 → demote or halt
- Document promotion decisions in `LIFECYCLE_LOG.md`

**Acceptance:**
- At least one strategy reaches canary live trading without halt
- Daily P&L tracked and reconciled against PM dashboard
- 7-day live run completes without operational incident

**Commit:** `live(canary): GREEN strategies live with capital_fraction 0.025`

---

## Verification rituals (every session)

```bash
# Before commit:
python3 -m py_compile $(git ls-files '*.py')
pytest tests/
cargo build --release --manifest-path poly_signer/Cargo.toml
python3 -c "import yaml; yaml.safe_load(open('render.yaml'))"

# After deploying:
ssh us-east-1 'systemctl status poly-bus poly-runner poly-signer'
curl -sS https://poly-pm.onrender.com/health
curl -sS http://<us-east-1-ip>:10000/health    # poly-signal-bus
```

## Open questions tracked in `OPEN_QUESTIONS.md`

Anything ambiguous: append with timestamp, context, recommended resolution. Operator reviews between sessions.

## Done

When Session 11 acceptance passes and 30 days live trading complete:
- Tag repo `v1.0.0`
- Operator updates Anthropic memory: sentinel-poly = parallel rail to SPM
- Re-evaluate scaling: if PF sustained ≥ 2.5 at n=200, increase capital to $5k; if not, accept the rail as marginal and reallocate effort

**END OF POLY_WORKFLOW**
