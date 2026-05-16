# Sentinel Portfolio Manager — Project Specification v1.0

**Repo:** `Dapperscyphozoa/sentinel-portfolio-manager`
**Audience:** Claude Code (and any future builder)
**Mission:** A single, lean, bloat-free algorithmic trading stack on Hyperliquid perps, built from scratch with Binance as the signal venue and HL as the execution venue. Replaces 11+ legacy Render services with 4-5.

This document is the **single source of truth**. Anything not in this doc is not in scope. Anything from the legacy stack not listed in §3 (Engines to Port) is **dead and must not be rebuilt**.

---

**Audit status:** sentinel council audit completed, verdict MODERATE 7/7 unanimous. Plan is sound. Required additions:
- Session 1.5 inserted in WORKFLOW.md for honest-backtest gate on vsq/fd1/lh1/range_fade before porting
- Tiered side-by-side validation rules in WORKFLOW.md for low-frequency strategies
- Per-routine model selection (haiku for cheap routines) in §8.4
- HA mitigations in §8.5 — single-zone risk accepted in v1

---

## §0 Operator Context (read first)

- **Operator:** running solo. Risk tolerance high. Targeting $50M from current $491.35 HL wallet.
- **Ethos:** PROFIT / AGGRESSIVE / PRECISE. World-first. No theater.
- **Wallets:**
  - Main (live): `0x3eDaD0649Db466E6E7B9a0Caa3E5d6ddc71B5ffE`
  - Agent (HL extraAgent, approved): `0xaed87768d3b6a76c997b6c0048610ab1e718fdb2`
  - Paper: `0xa5B93ED71881D05Ca669BA00182B3984E041432C`
- **Render owner ID:** `tea-d6ufmnea2pns739be9gg`
- **GitHub org:** `Dapperscyphozoa`

---

## §1 Why Rebuild

The legacy stack accreted 11 Render services, 47 PM registry entries, and 12,000+ lines of duplicated boilerplate. Each engine independently polls HL REST and hits 429 rate limits, starving every other engine. The strongest backtest in the stack (vsq, PF 3.04) has fired **zero** times because of this throttling.

**The architectural insight that unblocks everything:** 90% of crypto liquidations and order book depth live on Binance. HL is a downstream reflection of Binance. Therefore: **Binance is the signal venue. HL is the execution venue.**

This rebuild collapses the stack to a single Binance WS feed, a single strategy runtime, a single PM, and an autonomous monitor that fires Claude Code routines for health checks and decision support.

---

## §2 Final Architecture

### 2.1 Services (4 total, ~$28-50/mo on Render starter plans)

```
┌─────────────────────────────────────────────────────────────────┐
│  signal-bus                                                     │
│  ─────────                                                       │
│  Inputs:  Binance WS (klines/liq/funding/markPrice)             │
│           HL WS    (fills/positions/account)                    │
│  Cache:   /var/data (rolling, persistent)                        │
│  Outputs: HTTP GET /candles, /liq, /funding, /markprice         │
│           HTTP GET /hl/account, /hl/positions, /hl/fills        │
│  Purpose: ONE network egress for the entire stack.              │
└────────────────────────────┬────────────────────────────────────┘
                             │ HTTP polling (cheap, local)
┌────────────────────────────▼────────────────────────────────────┐
│  strategy-runner                                                │
│  ───────────────                                                 │
│  Modules: strategies/fsp.py vsq.py range_fade.py range_bo.py    │
│           lh1.py fd1.py precog.py liq_cascade.py cex_dex_arb.py │
│  Contract: each module exposes evaluate(coin, bus) -> Signal?    │
│  Halt:    per-strategy env flag + per-strategy halt token       │
│  Orders:  single HL exchange wrapper, cloid attribution         │
│  State:   single SQLite, strategy-tagged                         │
└────────────────────────────┬────────────────────────────────────┘
                             │ POST /check, /register_cloid, /attribution
┌────────────────────────────▼────────────────────────────────────┐
│  pm  (Portfolio Manager — slim rewrite)                         │
│  ──                                                              │
│  • Pre-trade gate (Rule 5b: regime + trend_direction_aware)     │
│  • Lifecycle (paper / canary / full)                            │
│  • Capital fraction per strategy                                 │
│  • Auto-attribution via HL WS in signal-bus                     │
│  • Halt/promote endpoints                                        │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│  monitor                                                        │
│  ───────                                                         │
│  • Cron-style routines (apscheduler or simple loop)             │
│  • Fires Claude Code via Anthropic Messages API                  │
│  • Routine outputs → /var/data/reports/ + push notifications    │
│  • Auto-actions where safe (halt on drawdown trigger)            │
└─────────────────────────────────────────────────────────────────┘

         dashboard  (existing Vercel/Render UI, point at new PM)
```

### 2.2 Stack

- **Language:** Python 3.11+
- **Web:** stdlib `http.server` (per legacy convention; no FastAPI/Flask unless justified)
- **Async:** `httpx` for HTTP, `websockets` for WS
- **DB:** SQLite via stdlib; one file per service in `/var/data`
- **Deploy:** Render web services, `python3 server.py`, autoDeploy from `main`
- **HL SDK:** `hyperliquid-python-sdk` for orders/account; raw WS for fills/positions
- **Binance:** raw WS via `wss://fstream.binance.com/ws/...` and `wss://fstream.binance.com/stream?streams=...`

### 2.3 Repo layout

```
sentinel-portfolio-manager/
├── README.md
├── SPEC.md                  (this doc)
├── WORKFLOW.md              (Claude Code build sessions)
├── render.yaml              (multi-service blueprint)
├── requirements.txt
│
├── common/
│   ├── __init__.py
│   ├── persistence.py       (shared SQLite schema)
│   ├── hl_exchange.py       (single HL order wrapper, cloid hashing)
│   ├── pm_client.py         (HTTP client for pm service)
│   ├── bus_client.py        (HTTP client for signal-bus)
│   ├── config.py            (env loader)
│   └── halt.py              (halt token + state)
│
├── signal_bus/
│   ├── server.py            (HTTP server)
│   ├── binance_ws.py        (klines/liq/funding subscribers)
│   ├── hl_ws.py             (fills/positions subscriber)
│   ├── cache.py             (in-memory + /var/data persistence)
│   └── README.md
│
├── strategy_runner/
│   ├── server.py            (HTTP server + scan loop + position loop)
│   ├── runner.py            (strategy registry, scan dispatch)
│   ├── strategies/
│   │   ├── __init__.py
│   │   ├── _base.py         (Signal dataclass, evaluate contract)
│   │   ├── fsp.py
│   │   ├── vsq.py
│   │   ├── range_fade.py
│   │   ├── range_breakout.py
│   │   ├── lh1.py
│   │   ├── fd1.py
│   │   ├── precog.py
│   │   ├── liq_cascade.py
│   │   └── cex_dex_arb.py
│   ├── trader.py            (open/close/SL/TP/time-stop)
│   └── README.md
│
├── pm/
│   ├── server.py
│   ├── registry.py          (STRATEGY_REGISTRY)
│   ├── pretrade_gate.py     (Rule 5b + concentration + halt checks)
│   ├── lifecycle.py
│   ├── attribution.py       (via signal-bus HL WS)
│   └── README.md
│
├── monitor/
│   ├── server.py
│   ├── scheduler.py
│   ├── claude_client.py     (Anthropic Messages API wrapper)
│   ├── routines/
│   │   ├── silent_engines.py
│   │   ├── fee_drag.py
│   │   ├── regime_shift.py
│   │   ├── pnl_attribution.py
│   │   ├── promotion_candidates.py
│   │   ├── drawdown_watch.py
│   │   └── sentinel_audit.py
│   └── README.md
│
├── scripts/
│   ├── backtest_harness.py  (uses signal-bus historical mode)
│   ├── replay_strategy.py
│   └── verify_deploy.py
│
└── legacy-data/             (snapshots from old services, archive only)
```

---

## §3 Engines to Port (9 — every other engine is dead)

Each block below is the **complete specification** for one strategy. Claude Code implements `strategies/<name>.py` with the listed parameters, contract, and tests.

### 3.1 `fsp` — Funding Spike Predator

**Class:** funding_mean_reversion
**Affinity:** range, chop, trend_up, trend_down (trend_direction_aware)
**Thesis:** When HL funding rate stays extreme ≥3 consecutive hours, leveraged positions on the crowded side pay punitive carry. Capitulation forced. Enter opposite.
**Direction:** LONG when funding ≤ -F_NEG sustained (shorts paying); SHORT when ≥ +F_POS sustained.

**Config (verified rr3, walk-forward OOS PF 2.65):**
```python
FSP_F_NEG     = 0.0003   # raw HL units, hourly
FSP_F_POS     = 0.0003
FSP_F_RESET   = 0.0001
FSP_CONSEC    = 3
FSP_TP_PCT    = 0.030
FSP_SL_PCT    = 0.010
MAX_HOLD_H    = 48
CLOID_PREFIX  = "fspv1_"
```

**Universe:** alts with historical funding extremes — `INJ,SNX,YGG,FTT,FET,ATOM,SEI,OP,APE,POLYX,GAS,BSV,COMP,DOT,ARK,SOL,LINK,DOGE,LTC,NEAR,SUI,AVAX,XRP,BLUR,BANANA,W,STG,JUP,WIF,TIA`

**Signal logic:**
```
last_N = bus.funding(coin, hours=FSP_CONSEC + 1)   # N+1 readings, oldest→newest
window = last_N[-FSP_CONSEC:]
prior  = last_N[-(FSP_CONSEC+1):-1]
fire_long  = all(r <= -F_NEG for r in window) and not all(r <= -F_NEG for r in prior)
fire_short = all(r >=  F_POS for r in window) and not all(r >=  F_POS for r in prior)
# Fire only on regime ENTRY (not while sustained)
```

**Backtest:** 90d × 49 coins → 53 trades, WR 37.7%, PF 1.64, OOS PF **2.65** (edge strengthens out-of-sample).

**Data sources:** HL funding history (HL-specific signal; cannot substitute).

---

### 3.2 `vsq` — Volatility Squeeze Breakout

**Class:** breakout_after_squeeze
**Affinity:** trend_up, trend_down (rides expansion of vol after compression)
**Thesis:** Bollinger inside Keltner = squeeze; volume expansion + close outside bands = breakout. Trade direction of breakout.

**Config (backtest PF 3.04, WR 51% per memory — REQUIRES HONEST RE-BACKTEST after rebuild):**
```python
VSQ_BB_PERIOD     = 20
VSQ_BB_STD        = 2.0
VSQ_KC_PERIOD     = 14
VSQ_KC_ATR_MULT   = 1.5
VSQ_SQUEEZE_BARS  = 6        # BB inside KC for N bars
VSQ_VOL_MULT      = 1.8      # breakout vol must exceed 1.8× rolling avg
VSQ_SL_ATR_MULT   = 2.0
VSQ_TP_ATR_MULT   = 6.0
VSQ_MAX_HOLD_BARS = 24
VSQ_TF            = "1h"
CLOID_PREFIX      = "vsqzr_"
```

**Universe:** majors + top-30 alts.

**Signal logic:**
```
bb_upper, bb_lower = bollinger(closes, VSQ_BB_PERIOD, VSQ_BB_STD)
kc_upper, kc_lower = keltner(closes, highs, lows, VSQ_KC_PERIOD, VSQ_KC_ATR_MULT)
squeeze = (bb_upper < kc_upper) and (bb_lower > kc_lower)
sustained_squeeze = squeeze for last VSQ_SQUEEZE_BARS
breakout_up   = close > bb_upper and vol > VSQ_VOL_MULT * avg_vol_20
breakout_down = close < bb_lower and vol > VSQ_VOL_MULT * avg_vol_20
fire = sustained_squeeze and (breakout_up or breakout_down)
```

**WARNING:** memory's PF 3.04 number predates the look-ahead-bias purge that found cex-dex-arb's PF 14.92 was fictional. The first task after porting vsq is to **rerun the backtest with honest historical Binance data** (the new signal-bus serves this) before promoting beyond paper.

**Data sources:** Binance WS klines (1h). No HL data needed for signal.

---

### 3.3 `range_fade` — Mean-reversion in Range

**Class:** mean_reversion_range
**Affinity:** range, chop
**Thesis:** In sideways markets, RSI extremes + Bollinger touches mean-revert. Fade them.

**Config (backtest PF 1.64 per memory):**
```python
RF_RSI_PERIOD   = 14
RF_RSI_LOW      = 25
RF_RSI_HIGH     = 75
RF_BB_PERIOD    = 20
RF_BB_STD       = 2.0
RF_REGIME_FILTER = True    # require PM regime != trend
RF_SL_PCT       = 0.012
RF_TP_PCT       = 0.020
RF_MAX_HOLD_BARS = 12
RF_TF           = "15m"
CLOID_PREFIX    = "rngfd_"
```

**Universe:** mid-caps with clean range structure.

**Signal logic:**
```
rsi = RSI(closes, RF_RSI_PERIOD)
bb_upper, bb_lower = bollinger(closes, RF_BB_PERIOD, RF_BB_STD)
fire_long  = rsi < RF_RSI_LOW  and close <= bb_lower * 1.001
fire_short = rsi > RF_RSI_HIGH and close >= bb_upper * 0.999
# block if PM regime is trend_up/trend_down at conf > 0.7
```

**Data sources:** Binance WS klines (15m). PM regime check.

---

### 3.4 `range_breakout` — Trend continuation on range break

**Class:** trend_breakout
**Affinity:** trend_up, trend_down (rides initial momentum after range exits)
**Thesis:** Range compression breaks → continuation. Buy break of high (range_high * 1.001) with volume confirmation; mirror for shorts.

**Config (backtest PF ~1.7 majors per memory):**
```python
RB_RANGE_LOOKBACK = 48     # bars to define range
RB_RANGE_MAX_PCT  = 0.04   # range must be <4% of mid (compressed)
RB_BREAK_BUFFER   = 0.001
RB_VOL_MULT       = 2.0
RB_SL_PCT         = 0.015
RB_TP_PCT         = 0.045
RB_MAX_HOLD_BARS  = 24
RB_TF             = "15m"
CLOID_PREFIX      = "rngbo_"
```

**Universe:** majors (BTC, ETH, SOL, XRP, BNB).

**Signal logic:**
```
range_high = max(highs[-RB_RANGE_LOOKBACK:])
range_low  = min(lows[-RB_RANGE_LOOKBACK:])
range_pct  = (range_high - range_low) / range_low
if range_pct > RB_RANGE_MAX_PCT: skip
vol_ok = vol > RB_VOL_MULT * avg_vol_20
fire_long  = close > range_high * (1 + RB_BREAK_BUFFER) and vol_ok
fire_short = close < range_low  * (1 - RB_BREAK_BUFFER) and vol_ok
```

**Data sources:** Binance WS klines (15m).

---

### 3.5 `lh1` — Liquidation Heatmap (inverted)

**Class:** liquidation_cascade_fader
**Affinity:** range, chop, trend_up, trend_down (trend_direction_aware)
**Thesis (v2, inverted):** Sweep wicks INTO equal-high/low clusters mark **continuation**, not exhaustion. Trade WITH the sweep direction.

**Config:**
```python
LH_CLUSTER_LOOKBACK   = 120
LH_PIVOT_LOOKBACK     = 5
LH_CLUSTER_BAND_PCT   = 0.003
LH_MIN_PIVOTS         = 3
LH_SWEEP_PCT          = 0.002
LH_VOL_SPIKE_MULT     = 1.5
LH_MAX_PROXIMITY_PCT  = 0.020
LH_SL_BUFFER_PCT      = 0.003
LH_RR                 = 3.0
LH_MAX_HOLD_BARS      = 8
LH_TF                 = "1h"
LH_INVERTED           = True
CLOID_PREFIX          = "liqhmp_"
```

**Universe:** top-50 by HL volume.

**Signal logic:** see legacy `liq-heatmap-v1/engine/signal_detector.py` — but invert direction (long sweeps of SSL pools, short sweeps of BSL pools).

**Data sources:** Binance WS klines (1h). Binance liq stream as confluence (optional v1.1).

---

### 3.6 `fd1` — Funding Divergence

**Class:** funding_divergence
**Affinity:** range, chop, trend_up, trend_down
**Thesis:** When funding rate diverges from short-term price action (e.g., price up but funding falling = longs unloading despite rally), fade the price move.

**Config (recalibrated 13 May session — verify against memory):**
```python
FD_FUNDING_THRESHOLD_HI = 1.5e-5
FD_FUNDING_THRESHOLD_LO = -5e-5
FD_DIVERGENCE_BARS      = 4
FD_SL_PCT               = 0.015
FD_TP_PCT               = 0.030
FD_MAX_HOLD_BARS        = 24
FD_TF                   = "1h"
CLOID_PREFIX            = "fdivg_"
```

**Signal logic:** detect funding/price divergence over `FD_DIVERGENCE_BARS`, fire when divergence is fresh (not stale).

**Data sources:** HL funding (HL-specific) + Binance klines for price.

---

### 3.7 `precog` — SMC Confluence with Structural Gate

**Class:** smc_trend
**Affinity:** trend_up, trend_down
**Thesis:** Multi-system confluence (BTC wall, OI, CVD, OBI, sniper signal) gated by structural pivots — only fire if last 3 pivot lows are descending (for shorts) or ascending (for longs).

**Config (FINAL config — 60d backtest 80.2% WR per memory):**
```python
PRECOG_EXT_LOOKBACK     = 70
PRECOG_RSI_HIGH         = 75
PRECOG_RSI_LOW          = 25
PRECOG_WICK_RATIO       = 0.2
PRECOG_STRUCT_N         = 3   # last N pivots must be monotonic
PRECOG_PIVOT_LB         = 5
PRECOG_PIVOT_RB         = 5
PRECOG_MAX_LEGS         = 5
PRECOG_CONF_TP_PCT      = 0.030
PRECOG_CONF_SL_PCT      = 0.015
PRECOG_CONF_MIN_SYS     = 2   # require ≥2 confluence systems
PRECOG_MAX_HOLD_BARS    = 48
CLOID_PREFIX            = "prec_"
```

**Universe:** 25-coin curated list (port from legacy precog-hl config).

**Signal logic:** port from `Dapperscyphozoa/precog-hl/confluence_engine.py` and `confluence_worker.py`. Preserve struct_n monotonic-pivot gate; this is the one feature that actually generates the 80% WR.

**Data sources:** Binance klines (1h) primary; HL wall + OI + CVD (HL-specific) for confluence layer.

---

### 3.8 `liq_cascade` — Liquidation Cascade Rider/Fader

**Class:** event_driven
**Affinity:** trend_up, trend_down (regime-aware direction)
**Thesis (revised):** Binance liq stream is 10-30× denser than HL's. Subscribe Binance liqs. In trend regime, RIDE cascades (continuation). In range, FADE cascades (mean-rev).

**Config:**
```python
LC_LIQ_USD_MIN          = 500_000      # min consolidated liq size in 30s window
LC_LIQ_WINDOW_S         = 30
LC_REGIME_AWARE         = True         # trend → ride, range → fade
LC_RIDE_TP_PCT          = 0.008
LC_RIDE_SL_PCT          = 0.004
LC_FADE_TP_PCT          = 0.012
LC_FADE_SL_PCT          = 0.006
LC_MAX_HOLD_S           = 180
CLOID_PREFIX            = "liqcs_"
```

**Universe:** all coins on both Binance and HL.

**Signal logic:**
```
liqs_30s = bus.liq(coin, since=now-30s)
total_long_liqs  = sum(l['qty'] * l['price'] for l in liqs_30s if l['side']=='SELL')  # forced sells = long liqs
total_short_liqs = sum(l['qty'] * l['price'] for l in liqs_30s if l['side']=='BUY')   # forced buys = short liqs
dominant_side = 'long' if total_long_liqs > total_short_liqs else 'short'
dominant_size = max(total_long_liqs, total_short_liqs)
if dominant_size < LC_LIQ_USD_MIN: skip
regime = pm.regime()
if regime in ('trend_up','trend_down') and LC_REGIME_AWARE:
    # ride
    direction = 'SELL' if dominant_side == 'long' else 'BUY'
    tp, sl = LC_RIDE_TP_PCT, LC_RIDE_SL_PCT
else:
    # fade
    direction = 'BUY' if dominant_side == 'long' else 'SELL'
    tp, sl = LC_FADE_TP_PCT, LC_FADE_SL_PCT
```

**Data sources:** Binance liq WS stream (`!forceOrder@arr`) — the killer feature this rebuild enables.

---

### 3.9 `cex_dex_arb` — Cross-Venue Basis Arb

**Class:** cross_venue_arb
**Affinity:** range, chop (basis convergence requires price action to bring HL back to CEX consensus)
**Thesis:** When HL price drifts >15bp from Binance/OKX/Bybit consensus, fade the gap. Critical: this engine has a documented look-ahead bias history; the new backtest harness must use signal-bus historical Binance data only.

**Config (post-bootstrap honest config):**
```python
CDA_MIN_BASIS_BPS       = 15        # strict tier only — weak tier killed
CDA_CEX_VENUES          = ["binance", "okx", "bybit"]
CDA_REQUIRE_LEADER      = True      # CEX must have moved in trade direction over last 5min
CDA_SL_ATR_MULT         = 0.3
CDA_TP_ATR_MULT         = 1.0       # 1:3 effective (avg winner ~1×ATR per live data)
CDA_MAX_HOLD_BARS       = 2
CDA_UNIVERSE            = ["BTC","ETH","SOL","AVAX","LINK","BNB","XRP","DOGE","ADA"]
CLOID_PREFIX            = "cexdx_"
```

**Signal logic:**
```
cex_mid = bus.markprice_cex(coin)   # avg across venues
hl_mid  = bus.markprice_hl(coin)
basis_bps = (cex_mid - hl_mid) / hl_mid * 10000
if abs(basis_bps) < CDA_MIN_BASIS_BPS: skip
# Direction: trade HL toward CEX
direction = 'BUY' if basis_bps > 0 else 'SELL'
if CDA_REQUIRE_LEADER:
    cex_5m_ret = bus.cex_return(coin, '5m')
    if sign(cex_5m_ret) != sign(basis_bps): skip
```

**Data sources:** Binance + OKX + Bybit markPrice (Binance is new). HL markPrice from HL WS via signal-bus.

**CRITICAL:** Honest backtest harness must use `bus.candles_historical(coin, '5m', since=T)` for ALL venues including HL — no live-leakage repeat.

---

## §4 Dead Engine Registry (DO NOT REBUILD)

These engines were tested and proven unprofitable. Do not port. Archive in git tag `legacy-pre-rebuild` only.

| Engine | Failure mode |
|---|---|
| cex-dex-arb-v1 (PF 14.92 claim) | Look-ahead bias — _cex_cache leaked live prices into historical replay |
| vol-squeeze-fade | Fee-model artifact (PF 1.22 → realistic OOS 0.85) |
| liq-heatmap-v1 (original direction) | Train PF 0.84 = Test PF 0.83 = no edge (kept as `lh1` inverted) |
| avwap-mesh-v1 | NOISE — PF 1.02 over 450 trades |
| tod-reversion-v1 | Fee-adjusted negative-EV (52% WR, but losses dominate) |
| wyckoff-v1 | FAIL_OOS PF 0.79 |
| trend-rider-v1 | Live PF 0.24 over n=12, council unanimous halt |
| vpin-v1 | Demoted, no edge |
| alt-rotation-v1 | Demoted |
| tod-momentum-v1 | Demoted |
| venue-lag-v1 | Parked; "do NOT redeploy without re-audit" per operator memory |
| precog-sa, smc-loose, smc-v2, V9, V10 | Pre-rebuild precog variants — superseded by `precog` (FINAL config) |
| dirmom-v1, pairs-engine-v1 | Never validated; abandoned in paper |
| vol-squeeze-breakout-v1, spot-perp-basis-v1, pair-stat-arb-v1 | In strategies-bundle but never fired profitably |
| whale-mirror, liq-rider, stat-arb (deprecated v1s) | Replaced |
| oi-divergence-v1, orderflow-imbalance-v1 | Bundled, zero fires, never validated |
| gamma-flow-v1, hyperevm-insider-v1, jump-diffusion-v1, vrp-harvester-v1 | Speculative entries, no backtest |
| liquidity-desert-v1, stablecoin-depeg-v1, mm-inventory-v1 | Speculative, no backtest |
| correlation-decoupling-v1, cross-asset-lag-v1, atr-spike-fader-v1 | Speculative, no backtest |
| vol-mom-inverted-v1 | Canary stage, no audit metrics |
| whale-tracker-v1, funding-harvester-v1, cross-venue-funding-v1 | Replaced or speculative |
| cyber-psycho (entire workspace) | DEAD-DEAD per operator memory |
| precog-local | Older variant of precog |
| **fd1** (this rebuild) | **Session 1.5 honest backtest: PF 0.85, OOS PF 0.78, n=818 — negative expectancy.** Hypothesis (funding/price divergence fade) does not hold on real out-of-sample data. Code retained for archival; gated off in `STRATEGY_FD1_ENABLED=0` and hard-blocked in `pm/pretrade.py::_RED_GATED`. |

---

## §5 Signal-Bus Specification

### 5.1 Binance WS subscriptions

```
Combined stream URL:
  wss://fstream.binance.com/stream?streams=
    btcusdt@kline_1m/btcusdt@kline_5m/btcusdt@kline_15m/btcusdt@kline_1h/
    btcusdt@markPrice@1s/!forceOrder@arr/
    ... (repeat for ~50 symbols)
```

- **klines:** subscribe 1m/5m/15m/1h for top-50 USDT-perp by volume
- **forceOrder:** single combined liquidation stream for all symbols
- **markPrice:** 1s update for live mark
- **funding rate:** derived from markPrice or polled REST every 5min (Binance settles funding every 8h)

### 5.2 HL WS subscriptions

```
WS URL: wss://api.hyperliquid.xyz/ws
Channels:
  userFills      (account: agent wallet)
  userPositions  (account: agent wallet)
  webData2       (account view)
```

**No HL candles via WS or REST — strategies use Binance candles for signal, HL only for execution price reference at fill time.**

### 5.3 HTTP API (consumed by strategy-runner, pm, monitor)

```
GET /candles/{coin}/{tf}?n=200
  → [{ts, open, high, low, close, volume}, ...]
  
GET /liq?since=<ts_ms>&coin=<optional>
  → [{ts, coin, side, qty, price, usd}, ...]

GET /funding/{coin}?hours=12
  → [{ts, rate}, ...]   # rate in raw HL or Binance units

GET /markprice/{coin}
  → {ts, hl_mid, binance_mid, okx_mid, bybit_mid}

GET /hl/account
  → {value, margin_used, positions: [...]}

GET /hl/fills?since=<ts_ms>
  → [...]

GET /hl/positions
  → [...]

GET /health
  → {ws_alive: {binance: bool, hl: bool}, cache_size, last_update}
```

### 5.4 Caching

- Klines: keep last 1000 bars per coin per TF in memory; flush to SQLite hourly
- Liq events: last 24h in memory; flush every 5min
- Funding: last 30d in memory + SQLite
- markPrice: last 5min in memory only

### 5.5 Failure modes

- **Binance WS disconnect:** auto-reconnect with exponential backoff. If down >30s, alert via monitor.
- **HL WS disconnect:** same; HL fills will queue and reconcile on reconnect.
- **Fallback:** if Binance unavailable >2min, fall back to Bybit WS (same kline format) and OKX WS. Use `cex_history.py` reference design from legacy `cex-dex-arb-v1`.

---

## §6 Strategy-Runner Specification

### 6.1 Strategy module contract

```python
# strategy_runner/strategies/_base.py
from dataclasses import dataclass
from typing import Optional, Literal

@dataclass
class Signal:
    coin: str
    side: Literal["B", "A"]          # B=BUY, A=SELL (HL convention)
    is_long: bool
    ref_price: float
    sl_px: float                      # absolute price
    tp_px: float                      # absolute price
    max_hold_bars: int
    fire_ts: float                    # ms epoch
    fire_reason: str                  # short tag for telemetry
    extras: dict                      # debug fields → SQLite extras_json

class StrategyBase:
    NAME: str = ""
    CLOID_PREFIX: str = ""
    AFFINITY: list[str] = []
    TF: str = "1h"
    UNIVERSE: list[str] = []

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        """Return Signal or None. Must be pure: no side-effects, no network besides bus."""
        raise NotImplementedError
```

### 6.2 Scan loop

```python
# strategy_runner/runner.py
for strategy in REGISTRY:
    if not strategy_enabled(strategy.NAME): continue
    for coin in strategy.UNIVERSE:
        if pm_halt(strategy.NAME, coin): continue
        sig = strategy.evaluate(coin, bus)
        if sig is None: continue
        # PM gate
        decision = pm.check(strategy.NAME, sig)
        if not decision.allow: continue
        # Place order
        trader.open(sig, size=decision.size_usd, cloid_prefix=strategy.CLOID_PREFIX)
```

### 6.3 Position loop

Every 60s, for each open trade: check live mark price from signal-bus; if SL/TP/timeout, close via HL exchange.

### 6.4 Halt control

- `STRATEGY_<NAME>_ENABLED=0` env disables a strategy without redeploy (env hot-reload on signal)
- `POST /halt/<name>` with `X-Halt-Token` header — halts one strategy at runtime
- `POST /halt/all` — emergency halt

---

## §7 Portfolio Manager Specification

Port verbatim from legacy `Dapperscyphozoa/portfolio-manager` with these simplifications:

- **Drop:** every entry in §4 (Dead Engine Registry). Registry shrinks 47 → 9.
- **Keep:** pre-trade gate (Rule 5b incl. `trend_direction_aware`), lifecycle stages, capital_fraction logic, cloid attribution
- **Move:** HL WS subscription (was implicit in PM) → signal-bus. PM consumes signal-bus's `/hl/fills` instead of subscribing itself.
- **Auth:** `X-PM-Auth` header (NOT `Bearer`) for POST endpoints — preserve legacy convention.

### 7.1 STRATEGY_REGISTRY (new)

```python
STRATEGY_REGISTRY = {
    "fsp":          { "class": "funding_mean_reversion", "affinity": [...], "trend_direction_aware": True, "capital_fraction": 0.20, ...},
    "vsq":          { "class": "breakout_after_squeeze", "affinity": ["trend_up","trend_down"], "capital_fraction": 0.15, ...},
    "range_fade":   { "class": "mean_reversion_range",   "affinity": ["range","chop"], "capital_fraction": 0.10, ...},
    "range_bo":     { "class": "trend_breakout",         "affinity": ["trend_up","trend_down"], "capital_fraction": 0.10, ...},
    "lh1":          { "class": "liq_cascade_fader",      "affinity": [...], "trend_direction_aware": True, "capital_fraction": 0.10, ...},
    "fd1":          { "class": "funding_divergence",     "affinity": [...], "capital_fraction": 0.10, ...},
    "precog":       { "class": "smc_trend",              "affinity": ["trend_up","trend_down"], "capital_fraction": 0.15, ...},
    "liq_cascade":  { "class": "event_driven",           "affinity": ["trend_up","trend_down"], "capital_fraction": 0.05, ...},
    "cex_dex_arb":  { "class": "cross_venue_arb",        "affinity": ["range","chop"], "capital_fraction": 0.05, ...},
}
# Sum: 1.00 (allocate every dollar)
```

---

## §8 Monitor Specification

### 8.1 Claude Code API client

```python
# monitor/claude_client.py
import os, requests, json

API_KEY = os.environ["CLAUDE_CODE_API_KEY"]   # sk-ant-oat01-...

def ask_claude(system: str, user: str, max_tokens: int = 4096, model: str = "claude-opus-4-7") -> dict:
    """
    Fire a single message at the Anthropic Messages API and return parsed response.
    Reference: https://docs.claude.com/en/api/messages
    
    Note: sk-ant-oat01-* tokens are OAuth-style; the operator should verify the
    auth header format ('x-api-key' vs 'Authorization: Bearer') against current
    Claude Code docs at https://docs.claude.com/en/docs/claude-code/overview before deploy.
    """
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": API_KEY,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=120,
    )
    r.raise_for_status()
    return r.json()

def extract_text(response: dict) -> str:
    return "".join(c.get("text","") for c in response.get("content",[]) if c.get("type")=="text")
```

### 8.2 Routines (each ~50-200 LOC)

| Routine | Cadence | Input | Action |
|---|---|---|---|
| `silent_engines` | hourly | per-strategy fire-rate stats vs backtest expectation | Flag strategies firing <20% of expected rate; suggest threshold review |
| `fee_drag` | hourly | `fees / gross_pnl` per strategy | Flag >50%; recommend TP widening |
| `regime_shift` | every 30min | btc-hmm regime + per-strategy affinity | Recommend size_mult changes or halts |
| `pnl_attribution` | every 6h | last 6h fills per strategy per coin | Identify bleeders, suggest coin blocklist |
| `promotion_candidates` | daily | engines with n ≥ 50 paper closures, PF > 1.4 | Suggest promotion to canary |
| `demotion_candidates` | daily | live engines with PF < 0.7 × backtest PF after 50 closures | Suggest demotion |
| `drawdown_watch` | every 15min | account value vs trailing high | If DD > 5%, halt all and alert |
| `sentinel_audit` | on demand | any claim/code/decision | Fire full sentinel council via the installed sentinel skill |

### 8.3 Output

- Each routine writes a JSON report to `/var/data/reports/<routine>/<ts>.json`
- Critical findings push to dashboard via existing push channel (VAPID keys in operator memory)
- `drawdown_watch` is the only routine authorized to take action without operator approval (halt all)

### 8.4 Cost budget + per-routine model selection (sentinel-corrected)

Cheap routines use `claude-haiku-4-5`; audit-grade routines use `claude-opus-4-7`. Budget tracking enforced in `monitor/scheduler.py`.

| Routine | Model | Reason |
|---|---|---|
| `silent_engines` | haiku | mechanical pattern-match, no nuance |
| `fee_drag` | haiku | arithmetic + threshold |
| `regime_shift` | haiku | classification |
| `pnl_attribution` | haiku | aggregation + ranking |
| `promotion_candidates` | opus | strategic judgment |
| `demotion_candidates` | opus | strategic judgment |
| `drawdown_watch` | haiku (or no LLM; pure threshold) | speed critical |
| `sentinel_audit` | external council (no Claude Code spend) | uses installed sentinel skill, 9 providers |

**Daily cost ceiling: $5.** Hard cap with halt-firing on exceed. **Per-call cap: $0.25.** Tracked in `monitor/spend.sqlite`.

### 8.5 High-availability concerns (sentinel-corrected)

The 4-service architecture has 3 single points of failure (signal-bus, strategy-runner, pm). Mitigations in v1:

- **Render auto-restart** on crash (enabled by default for starter plans)
- **Each service self-checks** every 30s; on detecting upstream failure (e.g., strategy-runner can't reach signal-bus), halts new trade openings but leaves position monitoring active so existing trades still get SL/TP
- **monitor/drawdown_watch fires regardless of other services** (consumes signal-bus directly; if signal-bus down, monitor halts all via Render API directly using `RENDER_API_TOKEN`)
- **State persistence:** every service flushes critical state to /var/data SQLite every 60s so a restart loses ≤1min of context
- **Deferred to v2:** active-active redundancy, hot-spare services. v1 accepts single-zone risk in exchange for simplicity.

---

## §9 Deployment

### 9.1 render.yaml (multi-service)

```yaml
services:
  - type: web
    name: spm-signal-bus
    runtime: python
    region: oregon
    plan: starter
    branch: main
    rootDir: signal_bus
    buildCommand: pip install -r ../requirements.txt
    startCommand: python3 server.py
    healthCheckPath: /health
    autoDeploy: true
    disk: { name: signal-bus-state, mountPath: /var/data, sizeGB: 1 }
    envVars:
      - { key: BINANCE_SYMBOLS, value: "BTCUSDT,ETHUSDT,SOLUSDT,..." }
      - { key: HL_AGENT_WALLET, value: "0xaed87768d3b6a76c997b6c0048610ab1e718fdb2" }
      # ... see §10

  - type: web
    name: spm-strategy-runner
    # ... similar pattern, rootDir: strategy_runner

  - type: web
    name: spm-pm
    # ... rootDir: pm

  - type: web
    name: spm-monitor
    # ... rootDir: monitor
```

### 9.2 Inter-service URLs

Use Render's internal service-to-service hostnames (`<name>.onrender.com` if exposed; private if not). Set as env vars:

- `SIGNAL_BUS_URL=https://spm-signal-bus.onrender.com`
- `PM_URL=https://spm-pm.onrender.com`

---

## §10 Environment Variables (canonical list)

Set per-service via Render dashboard or `render.yaml`. The `sync: false` ones are operator-managed.

### Signal-bus
```
BINANCE_SYMBOLS         (comma-separated; ~50 perp tickers)
HL_AGENT_WALLET         (read-only public address; for WS subscribe)
STATE_DIR=/var/data
HTTP_PORT=10000
```

### Strategy-runner
```
SIGNAL_BUS_URL          (internal Render URL)
PM_URL                  (internal Render URL)
PM_AUTH_TOKEN           (sync: false; X-PM-Auth header value)
HL_AGENT_WALLET
HL_PRIVATE_KEY          (sync: false; the agent key)
LIVE_TRADING            (0 or 1 per strategy via STRATEGY_<NAME>_LIVE)
RISK_PCT_PER_TRADE=0.02
LEVERAGE=5
MAX_OPEN_POSITIONS=6
SCAN_INTERVAL_SEC=300   (5min; bus is real-time so this is just dispatch cadence)
HALT_TOKEN              (sync: false)
# Per-strategy enable flags
STRATEGY_FSP_ENABLED=1
STRATEGY_VSQ_ENABLED=1
STRATEGY_RANGE_FADE_ENABLED=1
STRATEGY_RANGE_BO_ENABLED=1
STRATEGY_LH1_ENABLED=1
STRATEGY_FD1_ENABLED=1
STRATEGY_PRECOG_ENABLED=1
STRATEGY_LIQ_CASCADE_ENABLED=1
STRATEGY_CEX_DEX_ARB_ENABLED=1
# Per-strategy parameter overrides — see §3 for full list
FSP_F_NEG=0.0003
FSP_TP_PCT=0.030
# ... etc
```

### PM
```
SIGNAL_BUS_URL
PM_AUTH_TOKEN           (sync: false)
HL_AGENT_WALLET
STATE_DIR=/var/data
PRETRADE_COIN_CONC_MAX=2.0
```

### Monitor
```
CLAUDE_CODE_API_KEY     (sync: false; from operator memory)
SIGNAL_BUS_URL
PM_URL
PM_AUTH_TOKEN           (sync: false)
RENDER_API_TOKEN        (sync: false; for service-level halt actions)
DASH_PUSH_SECRET        (sync: false)
VAPID_PUBLIC_KEY
VAPID_PRIVATE_KEY       (sync: false)
DAILY_API_BUDGET_USD=5
STATE_DIR=/var/data
```

---

## §11 Migration Plan (operator-driven)

### Phase 0 — Pre-flight (this session, ~30 min execution)

1. Halt `trend-rider-v1`, `vol-squeeze-fade` (council-validated culls)
2. Backup `/closures` JSON from every legacy service → commit to `legacy-data/` later
3. Patch PM Rule 5b on the legacy PM to reflect new direction (already done in this session)

### Phase 1 — New repo (1 session)

1. Create `Dapperscyphozoa/sentinel-portfolio-manager` (public, MIT or proprietary as you prefer)
2. Commit this SPEC.md, WORKFLOW.md, and the directory skeleton
3. No code yet — just scaffolding and `render.yaml` placeholder

### Phase 2 — signal-bus (1-2 sessions)

1. Implement `signal_bus/binance_ws.py` (klines + liq + markPrice)
2. Implement `signal_bus/hl_ws.py` (fills + positions only)
3. Implement `signal_bus/cache.py` (in-memory + SQLite flush)
4. Implement `signal_bus/server.py` (HTTP endpoints per §5.3)
5. Deploy to Render. Verify 24h of uptime with zero 429s and >99% WS uptime.

### Phase 3 — strategy-runner skeleton + first strategy (1 session)

1. Implement `common/` modules (pm_client, bus_client, hl_exchange, persistence, halt)
2. Implement `strategy_runner/strategies/_base.py` (Signal, StrategyBase)
3. Implement `strategy_runner/runner.py` (scan loop, dispatch)
4. Implement `strategy_runner/trader.py` (open/close/SL/TP)
5. Implement `strategy_runner/strategies/fsp.py`
6. Deploy. Side-by-side test against the still-running legacy `fsp-v1` for 24h. Validate matching fires.

### Phase 4 — port remaining strategies (1-2 sessions)

In order: `range_fade`, `range_bo`, `vsq`, `fd1`, `lh1`, `precog`, `liq_cascade`, `cex_dex_arb`.

For each:
1. Implement `strategies/<name>.py` per §3
2. Side-by-side run with legacy version for 24h
3. Cut over: halt legacy service, enable in new runner
4. Confirm: 5 matching fires on new vs legacy in same window

### Phase 5 — PM rewrite (1 session)

1. Port PM with shrunk registry (9 entries only)
2. Move HL WS dependency to signal-bus
3. Deploy + side-by-side with legacy PM for 24h
4. Cut over: point strategy-runner at new PM, decommission legacy PM

### Phase 6 — monitor (1 session)

1. Implement Claude Code client
2. Implement routines per §8.2
3. Implement scheduler with daily-budget cap
4. Deploy. Verify first 24h of reports land in `/var/data/reports/`.

### Phase 7 — decommission (1 session)

1. For each legacy service: halt, archive `/closures` and `/state` to `legacy-data/`, delete service from Render
2. Update dashboard to point at new PM + signal-bus
3. Tag legacy repos with `archived-pre-rebuild`
4. Add `ARCHIVED` to legacy repo README

**Total: ~7-9 Claude Code sessions over 5-10 days, parallelizable.**

---

## §12 Acceptance Criteria (the rebuild is done when)

- [ ] `sentinel-portfolio-manager` repo exists, public
- [ ] 4 Render services deployed and live: signal-bus, strategy-runner, pm, monitor
- [ ] Total HL REST polls from new stack: < 50/hour (vs current ~5000+/hour)
- [ ] All 9 strategies implemented per §3, all firing at expected backtest cadence ±50%
- [ ] PM registry has exactly 9 entries
- [ ] Monitor fires ≥6 routines/day successfully, JSON reports persisted
- [ ] Daily Claude Code API spend < $5
- [ ] Drawdown-watch routine verified end-to-end on testnet halt drill
- [ ] All legacy services decommissioned from Render
- [ ] `legacy-data/` contains JSON snapshots of every legacy `/closures` and `/state`
- [ ] Dashboard shows all 9 strategies with live stats

---

## §13 Out of Scope (do not build in v1)

- Multi-account routing
- HL spot trading (perps only)
- HL HIP-2 staking integration
- Strategies from §4 (Dead Engine Registry)
- Custom dashboard (use existing)
- Discord/Telegram alerting (use existing PWA push)
- Backtester GUI (CLI-only via `scripts/`)
- Cloud-secrets vault (Render env vars are fine for v1)

---

## §14 References

- Legacy repo (do not fork from — port specific files only as cited):
  - https://github.com/Dapperscyphozoa/portfolio-manager
  - https://github.com/Dapperscyphozoa/precog-hl
  - https://github.com/Dapperscyphozoa/engine-template
  - https://github.com/Dapperscyphozoa/fsp-v1
  - https://github.com/Dapperscyphozoa/liq-heatmap-v1
  - https://github.com/Dapperscyphozoa/funding-div-v1
  - https://github.com/Dapperscyphozoa/range-fade-v1
  - https://github.com/Dapperscyphozoa/range-breakout-v1
- Hyperliquid:
  - Docs: https://hyperliquid.gitbook.io/hyperliquid-docs
  - WS API: wss://api.hyperliquid.xyz/ws
  - Python SDK: `hyperliquid-python-sdk`
- Binance Futures:
  - WS Streams: https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams
  - REST: https://developers.binance.com/docs/derivatives/usds-margined-futures/general-info
- Anthropic:
  - Messages API: https://docs.claude.com/en/api/messages
  - Claude Code docs: https://docs.claude.com/en/docs/claude-code/overview
- Sentinel skill (installed locally at `/mnt/skills/user/sentinel/`): used by `monitor/routines/sentinel_audit.py`

---

**END OF SPEC**

If anything in this document is ambiguous, ask the operator. Do not guess. Do not invent strategies not listed in §3. Do not reintroduce engines from §4.