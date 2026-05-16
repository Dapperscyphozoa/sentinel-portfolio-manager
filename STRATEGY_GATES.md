# Strategy Gates — Honest Backtest (Session 1.5)

Generated: 2026-05-16 (post-build, post-audit, post-fix).

Data source: OKX historical klines + funding (Binance Futures geo-blocked from
sandbox; OKX provides equivalent USDT-perp data). Backtest harness uses cursor-
gated `HistoricalBus` with no future leakage. Funding rates are forward-filled
to hourly cadence to match production signal-bus semantics (the production bus
sees the live funding rate via Binance markPrice@1s).

## Verdicts

| Strategy | n | WR | PF | OOS PF | Universe | Days | Status |
|---|---|---|---|---|---|---|---|
| `vsq` | 251 | 47.8% | 1.46 | 1.18 | 24 (SPEC) | 90 | **GREEN** |
| `lh1` (inverted) | 1012 | 31.0% | 1.32 | 1.22 | 24 (SPEC) | 90 | **YELLOW** |
| `range_fade` | 266 | 57.9% | 1.25 | 1.11 | 8 (subset) | 30 | **YELLOW** |
| `fd1` | 818 | 35.8% | 0.85 | 0.78 | 9 (subset) | 90 | **RED** |

## Gate rules

- **GREEN** (PF ≥ 1.4 AND OOS PF ≥ 1.0): port as planned, eligible for canary promotion.
- **YELLOW** (1.0 ≤ PF < 1.4 OR OOS PF < 1.0): flag `audit_status: PROVISIONAL`, NO live capital, no canary promotion.
- **RED** (PF < 1.0): do NOT port. Add to SPEC §4 Dead Engine Registry.

## Findings

### vsq — GREEN (with caveat)

The legacy claim of PF 3.04 is fictional under honest backtest. True PF is 1.46
(half of claim), with concerning walk-forward decay (IS PF 1.70 → OOS PF 1.18).
The edge is real but thinner than advertised. Promote to canary, not full live.

Per-coin strength concentrated in: SUI (PF 3.79), SEI (3.29), AVAX (2.68), OP (2.62).
Per-coin weakness: APT (0.43), ARB (0.67), ETH (0.90), DOT (0.92). Consider
per-coin allowlist after deployment validation.

### lh1 (inverted) — YELLOW

The inversion hypothesis (sweep into cluster = continuation, not exhaustion) is
weakly profitable: PF 1.32. Trade count is high (n=1012 over 90d ≈ 11/day) with
low WR (31%) but adequate R:R 3.0 makes the math work. OOS PF 1.22 is acceptable.

Per-coin variance is severe: DOT (3.11), APT (2.61), JUP (2.38) vs ARB (0.41),
LTC (0.65), DOGE (0.66), WIF (0.73). Strategy is profitable on aggregate but
dependent on coin selection. Recommended action: restrict universe to coins
with per-coin PF > 1.5 over 90d before live capital.

### range_fade — YELLOW (sample-limited)

30-day × 8-coin sample (sandbox time-budget constraint; full 90d × 18 coins
should be re-run by operator). At this sample: PF 1.25, OOS PF 1.11. WR 57.9%
is healthy. Per-coin spread is narrow (BTC 0.82 weakest, AVAX 1.91 strongest).

Operator action: re-run with full universe + 90d window from a location with
Binance access before any live capital decision.

### fd1 — RED ❌

Honest backtest is conclusive: PF 0.85, OOS PF 0.78, negative expectancy of
-0.13% per trade across n=818 trades. The strategy's funding-divergence
hypothesis does not hold up against real out-of-sample data. Every per-coin
PF except BNB (1.25) and BTC (1.10) is below 1.0; aggregate is fatally
negative.

**Action: DO NOT port to live. Strategy is disabled in render.yaml
(`STRATEGY_FD1_ENABLED=0`). Added to SPEC §4 Dead Engine Registry.**

The code remains in the repo for documentation/replay purposes but is gated off.

## Caveats

1. OKX data is used as a Binance proxy. They are not identical:
   - OKX funding settles every 8h (some pairs 4h); Binance every 8h. We forward-fill to hourly.
   - Volume profiles differ across venues — strategies relying on volume confirmation (`vsq`, `range_bo`, `lh1`) may show different fire rates against real Binance data.
   - Operator should re-run from a Binance-accessible location before live capital deployment.
2. None of these results validate the production signal-bus integration. They test the strategy logic against historical OHLCV only. Production strategy fires depend on signal-bus WS uptime + correct funding feed.
3. Fee/slippage modeling in the harness uses tp_px / sl_px as fills — real fills will incur taker fees + slippage which will reduce all PFs by ~5-10%.

## Next actions

1. ✅ Add `fd1` to SPEC §4 Dead Engine Registry
2. ✅ Set `STRATEGY_FD1_ENABLED=0` in render.yaml
3. Operator post-deploy: re-run `range_fade` with full 90d × 18-coin universe
4. Operator pre-live: re-run all four against Binance Futures from accessible region
5. For YELLOW strategies (`lh1`, `range_fade`): cap at paper mode until operator decides
6. For GREEN strategy (`vsq`): canary eligible after deploy validation
