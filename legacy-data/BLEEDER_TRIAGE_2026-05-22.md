# Bleeder Triage — 2026-05-22

**Trigger:** operator instruction "3 kills - move to paper and fix; if unfixable archive + remove"
**Council:** 9 LLMs, 7 providers, 4 audit waves (engine + fix-attempt + closure-forensics)
**Wallet at decision time:** $481.53

---

## funding_triangulation — REMOVED

**Reason:** structural 8h funding-settlement-lag flaw, identical to killed `fd1`.

**Evidence:**
- Council CRITICAL 66% (Qwen3 235B + Mistral Large + Codestral + GPT-4o + Qwen3 Coder all independently cited the 8h lag)
- 14 live trades: WR 29%, net -$0.69
- Gross P&L -$0.39 (vs fees $0.30 = 43% of net loss is fees, but gross is also negative)
- Threshold-doubling + 6-coin denylist this session FAILED to fix

**Actions taken:**
- Closures archived → `legacy-data/funding_triangulation_closures.json`
- Strategy file deleted: `strategy_runner/strategies/funding_triangulation.py`
- Registry entry removed: `pm/pretrade.py`
- Loader block removed: `strategy_runner/runner.py` (tombstone comment retained)
- Tests removed: `tests/test_funding_triangulation.py`, references in `test_engine_positive_fires.py`
- Test fixture in `test_coin_denylist.py` migrated to `hl_settle_5m`
- Env vars removed from core (Render API): `STRATEGY_FUNDING_TRIANGULATION_ENABLED`, `FUNDING_TRIANGULATION_COIN_DENYLIST`, `FT_DIVERGENCE_BPS`, `STRATEGY_FUNDING_TRIANGULATION_LIVE`
- Backtest references purged from `scripts/honest_backtest_stage1.py` + `scripts/backtest_harness.py`

**Status:** dead. Do not re-introduce without solving the 8h-lag problem at the venue level (impossible — funding settles every 8h on Binance/OKX, can't be sped up).

---

## hl_settle_5m — FIXABLE, paper + trail re-enabled

**Reason:** closures forensics revealed real edge on a subset + prior session disabled the EV-flipping trail-stop.

**Evidence:**
- Council CRITICAL 57% (treating net P&L as edge proof) — but council didn't have the closures breakdown
- 94 closures analysis:
  - Net -$2.15 dominated by fees (62% of loss) — gross only -$0.81
  - Close reasons: tp(46 wins +$3.77) | sl(42 losses -$5.06) | trail(1 fire +$0.04)
  - Per-coin WR: WIF 76% (n=17, +$0.59), ETH 100% (n=2), SEI 60%, NEAR 60%, INJ 100%
  - Losers (already denylisted): LINK/LTC/JUP/OP/BTC/APT/ARB 0-40% WR
- TP gross avg +$0.096 vs SL gross avg -$0.106 → R:R is **inverted** from configured 1.33 (~0.90 actual)
- Session memo: trail-stop was disabled this session in deploy 1 — backtest had shown trail flips per-trade EV from -$0.018 to +$0.0004

**Actions taken:**
- `HL_SETTLE_TRAIL_ENABLED=1` restored
- Stays paper (`STRATEGY_HL_SETTLE_5M_LIVE=0`)
- Closures archived → `legacy-data/hl_settle_5m_closures.json`
- Denylist retained (15 coins blocked; universe is effectively WIF, SEI, INJ + ETH + observation list)

**Promotion gate:** re-audit at n=30 new trades with trail-on.
- Promote to live (5% capital) if: WR ≥ 55% AND gross PF ≥ 1.2 AND TP-fill avg ≥ SL-fill avg
- Archive + remove if: WR < 45% OR gross PF < 0.8 at n=30

**Open issue:** SL slippage > configured (R:R inverted). Needs execution-layer audit — not a strategy fix. Track separately.

---

## hl_depth_shock — FIX ATTEMPT, paper

**Reason:** n=9 too small to conclude no-edge; council split (4 CRITICAL, 3 MODERATE, 1 CLEAN). GPT-4.1 alone proposed a concrete fix path.

**Evidence:**
- Council CRITICAL 47% but with explicit CLEAN dissent (GPT-4.1: "cautious, empirically grounded, propose concrete fixes")
- 9 live trades: WR 22%, net -$0.50, gross -$0.31
- L2 snapshot latency (~3s) vs 5s window = noise-dominated signal
- Structural concern remains: low-latency L2 strategy may be fundamentally incompatible with signal-bus poll cadence

**Actions taken:**
- `DS_WINDOW_S=30` (was 5) — catch real depth-pull events spanning multiple snapshots
- `DS_SHOCK_PCT_MIN=60.0` (was 40) — raise threshold to filter noise
- Stays paper (`STRATEGY_HL_DEPTH_SHOCK_LIVE=0`)
- Closures archived → `legacy-data/hl_depth_shock_closures.json`
- 3-coin denylist retained (INJ, TIA, WIF — illiquid book noise)

**Promotion gate:** re-audit at n=30 new trades with widened window.
- Promote to live (5% capital) if: WR ≥ 50% AND gross PF ≥ 1.3
- Archive + remove if: WR < 35% OR gross PF < 0.9 at n=30
- Hard stop: if window-widening produces zero new fires in 14 days, the 60% threshold is too tight → revisit OR archive

---

## Capital impact

```
Pre-treatment   Net 7d bleed: -$5.07 across 3 engines
                Live capital exposure on bleeders: ~$30 (estimated from cap_fraction)

Post-treatment  Live exposure on bleeders: $0
                hlp_fade (only live winner) unaffected, +$0.28/7d retained
                
Annualized bleed avoided: ~$265/year on the $481 wallet (55%/yr defensive)
```

## Open follow-ups (next session)

1. Resume `spm-strategy-runner`, `spm-pm`, `spm-monitor` (still suspended)
2. Deploy UZT_REV v3 at canary 0.025
3. Read-path lock-free patch on signal-bus (`/candles`, `/liq` still 7-8s due to RLock contention) — required before strategy-runner can scan UZT-16 efficiently
4. Honest backtest harness for `fsp_v2`, `range_fade_v2`, `liq_cascade_fade`
5. Audit `fmom` (n=19, MODERATE 65% — defer ruling until n=30)
6. SL-slippage execution audit on hl_settle_5m (separate from strategy fix)
7. Dashboard $0.00 frontend bug patch (core/landing.html)
