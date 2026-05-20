# Sentinel-Poly — Project Specification v1.0

**Repo:** `Dapperscyphozoa/sentinel-portfolio-manager`, branch `sentinel-poly`
**Audience:** Claude Code (and operator review)
**Mission:** Parallel cashflow rail on Polymarket BTC/ETH 5m binary markets. Architecturally mirrors `sentinel-portfolio-manager` (SPM) so the operator runs one mental model across two venues. Lives in the **same repo as SPM** on the `sentinel-poly` branch so `common/` modules (persistence, halt, config patterns) and shared schemas are reused without duplication. Polymarket-specific code lives under `poly_*/` directories alongside SPM's `signal_bus/`, `strategy_runner/`, `pm/`, `monitor/` — no conflict, distinct service namespace.

This document is the **single source of truth** for the poly rail. SPM's `SPEC.md` is the source of truth for the HL stack. They live side-by-side on this branch.

---

## §0 Operator Context

- **Operator:** solo, building two parallel rails (SPM on HL perps, this on Polymarket binaries)
- **Capital intent:** small allocation ($100-1000 USDC.e) — this rail has a structural ceiling around $30-100k AUM and is not the path to the $50M target. It's a diversified-income stream uncorrelated with HL perp performance.
- **Wallets:**
  - Polymarket trader (new, to be funded): TBD — operator generates fresh EVM wallet
  - Polygon network, USDC.e for trade collateral, MATIC for gas
- **Render owner ID:** `tea-d6ufmnea2pns739be9gg` (same as SPM)
- **GitHub org:** `Dapperscyphozoa`

---

## §1 Why This Rail

Three converging facts make Polymarket BTC/ETH 5m markets exploitable in 2026 without competing in the sub-100ms race:

1. **Chainlink Data Stream resolution is publicly reproducible.** PM settles against a weighted aggregator of ~7 CEX feeds. If we replicate the aggregation locally, we predict resolution before Chainlink publishes the attestation. The bot pack prices against Binance only.

2. **Maker-side fees are 0% with a 25% rebate on taker fees** (Jan 2026 dynamic fee schedule). Continuous LP'ing at fair value bleeds positive expectancy from retail flow regardless of latency.

3. **Cross-asset correlation between BTC 5m and ETH 5m is undertraded** because retail bets each market independently. ~80% correlation → ~5-7% divergence threshold is a tradeable spread.

The architecture below targets these three. We do NOT compete on raw take-side latency arb against the sub-100ms pack.

---

## §2 Final Architecture

### 2.1 Services (4 total)

```
┌─────────────────────────────────────────────────────────────────┐
│  poly-signal-bus                                                │
│  ────────────────                                                │
│  Inputs:  Binance WS    (btcusdt/ethusdt 1s ticks)              │
│           Coinbase WS   (BTC-USD/ETH-USD)                       │
│           Kraken WS     (XBT/USD, ETH/USD)                      │
│           Bitstamp WS   (btcusd, ethusd)                        │
│           Bitfinex WS   (tBTCUSD, tETHUSD)                      │
│           OKX WS        (BTC-USDT, ETH-USDT)                    │
│           Huobi WS      (btcusdt, ethusdt)                      │
│           Chainlink Data Stream WS  (Polygon BTC/USD, ETH/USD)  │
│           Polymarket CLOB WS  (all active btc/eth-updown-5m)    │
│           Polymarket REST  (market discovery every 30s)         │
│  Compute: rolling CL aggregator prediction (median-with-trim)   │
│  Outputs: HTTP GET /cl_predicted/{asset}, /pm_book/{market},    │
│                    /implied_prob/{market}, /market_list,        │
│                    /cex_consensus/{asset}                       │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│  poly-runner                                                    │
│  ────────────                                                    │
│  Modules: cl_predictor, endgame, maker_quote, cross_asset,      │
│           reflexivity_emitter (publishes to SPM signal-bus)     │
│  Contract: each module exposes evaluate(market, bus) -> Signal? │
│  Cadence: 1s position monitor (not 60s — events sub-second)     │
│  Orders: forwards to poly-signer over Unix socket               │
│  State: SQLite, strategy-tagged                                  │
└────────────────────────────┬────────────────────────────────────┘
                             │ Unix domain socket
┌────────────────────────────▼────────────────────────────────────┐
│  poly-signer  (Rust microservice, ethers-rs)                    │
│  ──────────────                                                  │
│  • Listens on /tmp/poly-signer.sock                             │
│  • Signs EIP-712 orders for Polymarket CTF Exchange             │
│  • Submits to PM CLOB REST + watches for match                  │
│  • Returns {order_id, status, fill_amount, fill_price}          │
│  • Target: <5ms sign latency, <100ms wall-clock to ack          │
└─────────────────────────────────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│  poly-pm                                                        │
│  ────────                                                        │
│  • Pre-trade gate (capital_fraction per strategy)               │
│  • USDC.e balance tracking on Polygon                           │
│  • Lifecycle (paper / canary / full)                            │
│  • Halt/promote endpoints                                        │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│  poly-monitor                                                   │
│  ───────────                                                     │
│  • Reuse SPM monitor pattern                                     │
│  • Routines: cl_drift_check, fee_drag, inventory_skew,          │
│              promotion_candidates, drawdown_watch                │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 Deployment topology — latency tiering

Two of the five strategies require AWS us-east-1 deployment (where PM CLOB API is hosted). The rest run fine on Render Oregon, where the rest of the stack lives.

| Service | Host | Reason |
|---|---|---|
| `poly-signal-bus` | AWS us-east-1 (t3.medium, ~$30/mo) | CEX WS feeds geo-distributed; needs to be close to PM API for order submission round-trip; serves AWS-local poly-signer |
| `poly-runner` | AWS us-east-1 (same VM as bus + signer) | Latency-sensitive strategies (cl_predictor, endgame) need same-VM call to signer |
| `poly-signer` | AWS us-east-1 (same VM) | Rust binary, Unix socket IPC with runner |
| `poly-pm` | Render Oregon (free tier OK) | Not latency-sensitive |
| `poly-monitor` | Render Oregon | Same as SPM monitor |

Single us-east-1 VM hosts bus + runner + signer. Saves on RPC round-trips, EIP-712 signing happens on the same host as the order POST.

### 2.3 Stack

- **Python 3.11+** for bus, runner, pm, monitor
- **Rust (stable)** for signer — `ethers-rs`, `tokio`, `serde`, `reqwest`
- **HTTP:** stdlib `http.server` (SPM convention)
- **Async:** `httpx` + `websockets` (Python); `tokio` (Rust)
- **DB:** SQLite per service
- **Deploy:** AWS EC2 + Render mix (see §2.2)

### 2.4 Repo layout

On branch `sentinel-poly` of `sentinel-portfolio-manager`. SPM's existing files (`signal_bus/`, `strategy_runner/`, `pm/`, `monitor/`, `SPEC.md`, `WORKFLOW.md`, `common/`, etc.) stay untouched on this branch — they're the HL stack. Poly-specific code goes in new top-level dirs prefixed `poly_*` plus the three spec files at root:

```
sentinel-portfolio-manager/    (on branch: sentinel-poly)
├── SPEC.md                     ← SPM (untouched)
├── WORKFLOW.md                 ← SPM (untouched)
├── signal_bus/                 ← SPM HL stack
├── strategy_runner/            ← SPM HL stack
├── pm/                         ← SPM HL stack
├── monitor/                    ← SPM HL stack
├── common/                     ← SHARED. Poly imports persistence, halt, config patterns.
│
├── POLY_SPEC.md                ← (this doc) added on branch
├── POLY_WORKFLOW.md            ← Claude Code session plan, added on branch
├── poly_signer.rs              ← Rust signer skeleton, moves to poly_signer/src/main.rs in Session 6
│
├── poly_signal_bus/            ← NEW on branch
│   ├── server.py
│   ├── cex_ws.py               (7-venue WS subscriber)
│   ├── chainlink_stream.py     (CL Data Stream WS)
│   ├── polymarket_clob.py      (PM CLOB WS + REST)
│   ├── cl_aggregator.py        (median-with-trim replication)
│   ├── cache.py
│   └── README.md
│
├── poly_runner/                ← NEW
│   ├── server.py
│   ├── runner.py
│   ├── strategies/
│   │   ├── _base.py
│   │   ├── cl_predictor.py
│   │   ├── endgame.py
│   │   ├── maker_quote.py
│   │   ├── cross_asset.py
│   │   └── reflexivity_emitter.py
│   ├── signer_client.py        (Unix socket client to poly-signer)
│   └── README.md
│
├── poly_signer/                ← NEW (Rust crate)
│   ├── Cargo.toml
│   ├── src/
│   │   ├── main.rs             (seeded from /poly_signer.rs at branch root)
│   │   ├── eip712.rs
│   │   ├── submit.rs
│   │   └── socket.rs
│   └── README.md
│
├── poly_pm/                    ← NEW
├── poly_monitor/               ← NEW
│
├── scripts/                    ← SHARED dir. Poly scripts added alongside SPM's:
│   ├── honest_backtest.py      (SPM existing — extend for poly strategies in Session 8)
│   ├── cl_aggregator_validate.py  ← NEW (Session 4 gate)
│   └── verify_deploy.py        (SPM existing — extend)
│
├── render.yaml                 ← SHARED. Append poly-pm + poly-monitor services. Do NOT remove SPM services.
├── requirements.txt            ← SHARED. Add scipy if not present.
└── aws-userdata.sh             ← NEW (us-east-1 bootstrap: poly bus + runner + signer)
```

**Key rule for Claude Code:** never touch existing SPM directories (`signal_bus/`, `strategy_runner/`, `pm/`, `monitor/`) or SPM specs (`SPEC.md`, `WORKFLOW.md`, `STRATEGY_GATES.md`, `VALIDATION_UZT_REV.md`) on this branch. Poly is purely additive. The two stacks share `common/` (read-only from poly's perspective — extend it only with backward-compatible additions) and `scripts/` (add new files, don't modify existing).

---

## §3 Strategy Modules

### 3.1 `cl_predictor` — Chainlink Data Stream Prediction

**Class:** oracle_arbitrage
**Thesis:** Polymarket settles to Chainlink, not Binance. Bot pack prices to Binance. In dispersing-vol regimes the basket diverges from Binance, creating mispricing on PM book.

**Config:**
```python
CL_VENUES               = ["binance", "coinbase", "kraken", "bitstamp", "bitfinex", "okx", "huobi"]
CL_AGGREGATION          = "median_trim"        # drop top/bottom outlier, median rest
CL_TRIM_OUTLIER_BPS     = 50                   # drop venue ticks >50bps from median
CL_MIN_VENUES_REQUIRED  = 5                    # need ≥5 venues live to fire
CL_DIVERGENCE_THRESH    = 0.05                 # 5% implied-prob divergence from PM book
CL_FEE_ADJ_EDGE_MIN     = 0.02                 # 2% edge after fees
CL_WINDOW_TIME_REMAIN   = 60                   # only fire in last 60s of 5m window
CL_MAX_POSITION_USD     = 50                   # per-market position cap
CL_CAPITAL_FRACTION     = 0.30
```

**Signal logic (pseudocode):**
```python
def evaluate(market, bus):
    if market.time_remaining > CL_WINDOW_TIME_REMAIN:
        return None
    cl_pred = bus.cl_predicted(market.asset)   # our locally computed aggregator
    pm_book = bus.pm_book(market.id)            # YES/NO best bid/ask
    
    # Compute true implied probability from our predicted CL price + Brownian remainder
    sigma = bus.realized_vol(market.asset, lookback_s=60)
    dt = market.time_remaining / 3600
    drift = (cl_pred - market.start_price) / market.start_price
    sigma_remain = sigma * sqrt(dt)
    true_prob_up = norm.cdf(drift / sigma_remain) if sigma_remain > 0 else (0.99 if drift > 0 else 0.01)
    
    # PM book implied
    pm_yes_ask = pm_book.yes_ask
    pm_no_ask = pm_book.no_ask
    
    # Fee-adjusted threshold (current dynamic fee at this price)
    fee = dynamic_fee(min(pm_yes_ask, pm_no_ask))   # 1.56% peak at 0.5
    
    edge_yes = true_prob_up - pm_yes_ask - fee
    edge_no  = (1 - true_prob_up) - pm_no_ask - fee
    
    if edge_yes > CL_FEE_ADJ_EDGE_MIN:
        return Signal(side="BUY", token="YES", price=pm_yes_ask, size_usd=size_kelly(edge_yes))
    if edge_no > CL_FEE_ADJ_EDGE_MIN:
        return Signal(side="BUY", token="NO", price=pm_no_ask, size_usd=size_kelly(edge_no))
    return None
```

**Critical validation:** before this strategy is allowed to trade live capital, `scripts/cl_aggregator_validate.py` must show local prediction matches historical Chainlink BTC/USD ticks within 5bps for ≥95% of 100k+ samples. This is the gate. If we can't predict Chainlink within 5bps, the whole thesis collapses.

**Expected fire rate:** 30-80/day across BTC + ETH 5m markets. Expected WR (honest target): 65-75% post-fee. Sharpe target: >3.

---

### 3.2 `endgame` — Last-30s Pure Pricing

**Class:** microstructure_pricing
**Thesis:** In the last 30 seconds, direction is no longer the question — only "where is BTC right now vs threshold." Quote tight makers, take with edge if book is dislocated.

**Config:**
```python
EG_WINDOW_TIME_REMAIN_MAX = 30                 # fire only when ≤30s left
EG_WINDOW_TIME_REMAIN_MIN = 5                  # too late, settlement uncertainty
EG_DIVERGENCE_THRESH      = 0.03               # tighter than cl_predictor
EG_VOL_GATE_REALIZED_MIN  = 0.0005             # need some vol to have edge
EG_MAX_POSITION_USD       = 30
EG_CAPITAL_FRACTION       = 0.20
```

**Signal logic:** uses `cl_predictor`'s `true_prob_up` calculation but with much smaller `sigma_remain` (5-30s left). At 5s remaining, this collapses to "is current price > start_price" — almost deterministic. Trade aggressively when PM book hasn't caught up.

**Expected fire rate:** 50-150/day. Tighter edges, higher fire rate, smaller positions.

---

### 3.3 `maker_quote` — Continuous Liquidity Provision

**Class:** market_making
**Thesis:** 25% rebate on taker fees + 0% maker fees + retail flow = positive expectancy without latency dependence.

**Config:**
```python
MM_QUOTE_SPREAD_BPS       = 80                 # ±0.4% around fair value
MM_INVENTORY_SKEW_RISK    = 2.0                # gamma coefficient on inventory
MM_MAX_INVENTORY_USD      = 100                # per market
MM_REQUOTE_INTERVAL_MS    = 250                # refresh quotes 4x/sec
MM_CANCEL_ON_TICK_PCT     = 0.001              # 0.1% asset move = cancel + requote
MM_CAPITAL_FRACTION       = 0.30
```

**Signal logic:**
```python
def quote(market, bus, inventory):
    fair_prob = compute_fair_prob_from_cl_predictor(market, bus)
    skew = MM_INVENTORY_SKEW_RISK * (inventory / MM_MAX_INVENTORY_USD)
    
    bid_yes = max(0.01, fair_prob - MM_QUOTE_SPREAD_BPS/10000 - skew)
    ask_yes = min(0.99, fair_prob + MM_QUOTE_SPREAD_BPS/10000 - skew)
    
    return [
        Quote(side="BUY",  token="YES", price=bid_yes, size_usd=10),
        Quote(side="SELL", token="YES", price=ask_yes, size_usd=10),
    ]
```

**Position management:** every 250ms, check fair_prob shift. If shift > MM_CANCEL_ON_TICK_PCT, cancel + repost. End-of-window: close inventory at market or let it settle (if within +/- expected resolution).

**Expected daily fills:** 100-300 per market × 5 active markets. Per-fill profit: 0.5-2 cents on $10 position = $0.05-0.20. Daily target: $5-30 from MM alone, scaling with capital.

---

### 3.4 `cross_asset` — BTC↔ETH 5m Correlation Spread

**Class:** statistical_arbitrage
**Thesis:** BTC 5m and ETH 5m windows opening at the same timestamp should resolve with ~80% correlation. When implied probs diverge by >7%, fade the spread.

**Config:**
```python
XA_CORRELATION_LOOKBACK_D = 30
XA_DIVERGENCE_THRESH      = 0.07
XA_CORRELATION_MIN        = 0.65               # don't trade when correlation breaks
XA_MAX_POSITION_USD       = 40                 # per leg
XA_CAPITAL_FRACTION       = 0.10
```

**Signal logic:**
```python
def evaluate(bus):
    btc_market = bus.current_5m_market("BTC")
    eth_market = bus.current_5m_market("ETH")
    if btc_market.start_ts != eth_market.start_ts:
        return None    # markets must be synchronized
    
    btc_prob_up = bus.implied_prob(btc_market.id)
    eth_prob_up = bus.implied_prob(eth_market.id)
    
    historical_corr = bus.correlation("BTC", "ETH", lookback_d=XA_CORRELATION_LOOKBACK_D)
    if historical_corr < XA_CORRELATION_MIN: return None
    
    spread = btc_prob_up - eth_prob_up
    expected_spread = 0  # markets should be similar when correlation high
    
    if abs(spread - expected_spread) > XA_DIVERGENCE_THRESH:
        if spread > 0:
            return [
                Signal(market=btc_market, side="SELL", token="YES"),   # BTC overpriced
                Signal(market=eth_market, side="BUY",  token="YES"),   # ETH underpriced
            ]
        else:
            return [
                Signal(market=btc_market, side="BUY",  token="YES"),
                Signal(market=eth_market, side="SELL", token="YES"),
            ]
    return None
```

**Expected fire rate:** 5-20/day. Both legs hedge each other → low net delta. Pure spread capture.

---

### 3.5 `reflexivity_emitter` — PM → SPM Cross-Stack Signal

**Class:** cross_venue_indicator
**Thesis:** When PM 5m BTC hits >0.85 or <0.15 with ≥90s remaining, retail piles into BTC perp/spot on Binance to confirm. Measurable Binance drift. Trade *Binance/HL*, not PM.

**This module does not place PM trades.** It publishes an HTTP endpoint that SPM's signal-bus subscribes to as an additional data feed. SPM's `strategy_runner` then runs a new strategy module `poly_reflex.py` that consumes it.

**Config:**
```python
RE_PROB_EXTREME_HIGH      = 0.85
RE_PROB_EXTREME_LOW       = 0.15
RE_TIME_REMAINING_MIN     = 90
RE_SUSTAINED_S            = 5                  # extreme must persist 5s
```

**Output:** `GET /reflex_signal/{asset}` → `{state: "extreme_up"|"extreme_down"|"neutral", since_ts, pm_prob, time_remaining}`

The SPM-side `poly_reflex.py` strategy can then trade BTC long on Binance/HL when state=extreme_up for >5s (riding the retail-pile flow) with tight stops.

---

## §4 Honest Backtest Gate

Following SPM's pattern (sentinel audit lesson), no strategy moves past paper without:

1. **`cl_aggregator_validate.py`** — replicate Chainlink BTC/USD aggregator against 30 days of historical Chainlink ticks. PASS if median absolute error < 5bps and 95th percentile < 15bps. If FAIL, `cl_predictor` and `endgame` are killed; only `maker_quote` and `cross_asset` proceed.

2. **`honest_backtest.py`** per strategy:
   - Replay 60 days historical CEX WS + PM CLOB depth + Chainlink ticks
   - Strategy receives only the bus interface (historical mode)
   - No live HTTP calls inside strategies
   - Walk-forward train/test (30d train, 30d test)
   - Per-strategy WR, PF, expectancy, OOS PF

3. **Gate rules** (mirrors SPM's STRATEGY_GATES.md):

| Honest PF (OOS) | Action |
|---|---|
| ≥ 2.0 | GREEN — ship to canary at 0.025 capital_fraction |
| 1.4 - 2.0 | YELLOW — ship to paper only; canary only after n=50 paper fires confirm |
| 1.0 - 1.4 | RED-YELLOW — do not deploy live; reparameterize once |
| < 1.0 | RED — kill strategy. Do not deploy. |

---

## §5 Cost Budget

- **AWS us-east-1 t3.medium** (bus + runner + signer): $30/mo
- **Polygon RPC** (Alchemy paid tier for low-latency): $50/mo
- **Render free tier** (pm + monitor): $0
- **Polygon gas + USDC.e bridge fees:** ~$10/mo
- **Anthropic API for poly-monitor routines:** $5/mo (within SPM's $5 ceiling, shared)
- **Total infra:** ~$95/mo

Capital breakeven: at $500 starting capital and 1% monthly return target, monthly profit = $5. Infra eats all profit at this size. **Minimum viable capital: $5,000.** Below that, the rail is education only.

---

## §6 Risk Limits

- **Per-market position cap:** $50 (cl_predictor) / $30 (endgame) / $100 (maker_quote inventory)
- **Total open exposure:** ≤ 50% of USDC.e balance at any time
- **Daily drawdown halt:** -8% triggers halt-all (tighter than SPM's -5% because variance is higher on binaries)
- **Per-market max trades:** 3 (prevent over-trading single window)
- **Settlement risk:** at window close, all positions held to resolution. No mid-resolution exit. Position monitor must close ≥10s before window end if intending to exit pre-resolution.

---

## §7 Out of Scope (v1)

- Non-BTC/ETH 5m markets (SOL, DOGE 5m exist but are thinner — v2)
- 1-minute markets (announced by PM; deploy when live and depth >$50k/market)
- POLY token integration
- Cross-strategy hedging (cl_predictor + maker on same market simultaneously) — separate inventory bookkeeping, v2

---

## §8 References

- Polymarket CLOB API: https://docs.polymarket.com
- Polymarket CTF Exchange contract: verify current address on Polygonscan before deploy
- Chainlink Data Streams (Polygon): https://docs.chain.link/data-streams
- ethers-rs: https://docs.rs/ethers/
- SPM SPEC.md (sister project): `Dapperscyphozoa/sentinel-portfolio-manager`

---

**END OF POLY_SPEC**

If anything in this document is ambiguous, ask the operator. Do not place live trades without `cl_aggregator_validate.py` passing the 5bps gate.
