# CLSI rank-test findings — 2026-05-18

## TL;DR
The proposed Crypto Leverage-Stress Index (CLSI v2) cannot be empirically validated against current `signal_bus` data because 3 of its 5 proposed dimensions have no historical data available, and a 4th has insufficient history. The 3 dimensions that DO have data (price-vol proxies) collapse to effective rank 2 — confirming the council's collinearity warning, but not testing CLSI's actual hypothesis.

## What was tested
Pulled live data for 10 majors (BTC, ETH, SOL, XRP, BNB, DOGE, AVAX, LINK, ARB, OP) from `signal_bus`. Computed features per coin per hour. Ran PCA + Ledoit-Wolf condition analysis. Pooled n=250 observations.

## Result
- Effective rank @ 95% variance: **2** (out of 3 usable features)
- Σ condition number (Ledoit-Wolf shrunk): **12.8**
- hl_range ↔ vol_z_abs correlation: **0.885** (essentially the same signal)

## Data infrastructure gaps blocking real CLSI validation
1. **Liquidation stream is dead** — `liq_events: 0` cached. OKX `liquidation-orders` subscribe failed at boot (code 60018). Fix: switch `DATA_VENUE=binance`.
2. **No basis history** — `/markprice` is snapshot-only. Fix: add 60s sampler + `/basis/{coin}?hours=N` endpoint.
3. **No OI endpoint** — signal_bus has no `/oi` route. Fix: add HL OI poller via `/info metaAndAssetCtxs`.
4. **Funding history < 34h** — cache only retains since boot. Fix: REST-backfill 30d at startup via HL `/info historicalFunding`.
5. **HLP history sparse** (26 samples, needs 200+) — accumulates naturally; ~12h wait.

## Reproducibility
```bash
python3 scripts/clsi_rank_test.py
```
Requires signal_bus to be reachable at the URL hardcoded in the script.

## Next step
Ship signal_bus substrate fixes (1-4), wait 7d, re-run this test.
