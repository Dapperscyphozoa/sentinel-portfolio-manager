# Stage 1 Gates — honest backtest + live paper accumulation

Generated: 2026-05-18T22:00:00Z

Per sentinel council 2026-05-18: 3 categories of Stage 1 engines, 3 gating paths.

## Gate rules

**Category A (historical data available):** 90d walk-forward, n ≥ 150 trades, 
bt_PF ≥ 1.4 AND OOS PF ≥ 1.0 → GREEN, eligible for canary 0.025 cap_frac

**Category B (HL data + Binance proxy):** proxy bt_PF ≥ 1.2 AND live paper n ≥ 30 
with rolling-PF ≥ 1.5 → GREEN

**Category C (HL-unique, live-paper only):** n=30-50 live closures with 
rolling-PF ≥ 1.5-2.0 → GREEN

**Promotion ladder (post-GREEN):** n=30 @ rolling-PF≥1.5 → canary 0.025 cap_frac · 
n=75 @ rolling-PF≥2.0 → 0.05 · n=150 @ rolling-PF≥1.8 sustained → full registry cap.

## Current status

| Engine | Cat | bt_n | bt_PF | OOS_PF | paper_n | rolling_PF | Status |
|---|---|---|---|---|---|---|---|
| hl_cvd_aggressor | B | — | — | — | 0 | — | PENDING — needs live paper accumulation |
| hl_depth_shock | B | — | — | — | 0 | — | PENDING — needs live paper accumulation |
| hl_whale_frontrun | C | — | — | — | 0 | — | NEEDS_DATA (n=0/50) |
| hl_vault_predict | C | — | — | — | 0 | — | NEEDS_DATA (n=0/30) |
| liq_cluster_hunt | C | — | — | — | 0 | — | NEEDS_DATA (n=0/40) — reclassified, no liq archive |
| funding_triangulation | C | — | — | — | 0 | — | NEEDS_DATA (n=0/30) — reclassified, no funding archive |

## Archived (killed, see SPEC §4)

- **cross_coin_zscore** — KILLED 2026-05-19. Honest backtest n=223 PF 0.99. Sentinel CRITICAL unanimous (thesis broken — pair ratios not cointegrated at 5m). Removed from runtime registry, scripts, and audit routines. Do NOT re-introduce.

## First gate findings (2026-05-18)

### funding_triangulation + liq_cluster_hunt — RECLASSIFIED to Category C
- HistoricalBus cannot replay HL hourly funding (no archive)
- HistoricalBus cannot replay Binance forceOrder events (no public archive)
- Only path to gate: live paper accumulation

### HL-specific engines (cvd_aggressor, depth_shock, whale_frontrun, vault_predict)
- All require live HL data not present in any historical archive
- Currently n=0 paper closures because they just deployed and signals haven't converted
- **Need 7-14 days** of live paper accumulation before gate decision

## Action items

1. **Wait 14 days** for HL-only engines to accumulate paper closures
2. **Daily re-run** of `scripts/honest_backtest_stage1.py` updates this gate
3. **Auto-promote** any engine that hits the gate criteria via monitor routine

## Critical findings

The infrastructure assumption that 3 engines could be gated via 90d historical data was wrong:
- Only `cross_coin_zscore` had historical data (klines)
- That engine FAILED the 90d gate
- 6 of 7 Stage 1 engines have no path to historical validation — must wait for live paper

**Net Stage 1 gate result: 0/7 engines passed today. 1/7 failed. 6/7 need 14-30 days of accumulation.**

## Recommended next infrastructure work

To make Category A/B gating possible for the remaining engines:
1. Build historical liq feed (Binance forceOrder archive via 3rd party or backfill from cache)
2. Build HL funding rate historical via signal-bus persistence (already pulling, just need to archive)
3. Build Binance L2 depth proxy historical (Bybit has free 30d L2 archive)
4. Build CVD historical via Binance aggTrade replay (free, has 30d history)

Estimated build: 3-5 days of harness work. Worth it before scaling.
