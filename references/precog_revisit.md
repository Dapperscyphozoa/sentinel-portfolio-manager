# precog — if revisited

**Status: PARKED 2026-05-17.** Sentinel killed via 3-of-5 council vote, 87% confidence.
Code preserved in `strategy_runner/strategies/_archived/precog_pivot_rsi.py`. The
old 14,621-line `precog-hl/precog.py` is archived at the GitHub repo level — do
not redeploy.

---

## Why it was parked

OOS walk-forward, 30d / 6 majors / 15m bars / 5bp roundtrip fees:

```
              n    WR    PF    return
TRAIN (20d)  69   42%   0.90   -4.8%
TEST  (10d)  44   43%   1.34   +10.8%
```

Three reasons it failed the bar:

1. **TRAIN is unprofitable.** PF 0.90 over 69 trades is below breakeven. The "test
   PF 1.34" headline is on top of a strategy that already lost money in the
   adjacent window.
2. **n=44 test is below the noise floor for PF claims.** Bootstrap CI on the
   +10.8% test return spans roughly [-3%, +25%]. The result is not statistically
   distinguishable from zero edge.
3. **Per-coin "winners" (BNB/XRP/DOGE) are selection bias.** With 6 coins, 3
   showing positive PF in both windows is exactly what chance produces. There's
   no a-priori reason these coins should respond to pivot+RSI+wick differently
   than BTC/ETH/SOL.

The strategy concept (pivot + RSI extreme + rejection wick) is recognizable
mean-reversion — not a bad starting idea. But the implementation as-is doesn't
clear the deploy gate.

---

## Deploy gate — what to verify if you ever bring it back

Do NOT add `PrecogPivotRsi` to `runner.STRATEGY_REGISTRY` until the strategy
clears ALL of these gates:

1. **n ≥ 100 per coin per window.** Below this, PF and WR are too noisy to
   trust. With 15m bars firing 3 trades/coin/week, that's ~8 months per coin
   per window. Use a 4h or 1h timeframe variant instead, or accept the data
   collection time.
2. **Bonferroni-corrected statistical significance.** If sweeping params (LB,
   RH, RL, WICK_RATIO), divide α by the number of params × universe size.
   Single-config OOS pass is fine; sweep-and-pick-best is not.
3. **PF ≥ 1.2 in BOTH train and test windows.** PF in only one window =
   regime-favorable luck or overfit. Train AND test PF ≥ 1.2 is the floor.
4. **Bootstrap CI on PF excludes 1.0 at 95%.** Required. PF 1.34 means nothing
   if the CI is [0.7, 2.1].
5. **No look-ahead in the harness.** Strategy must see only `bars[:i+1]`,
   entries simulated at `bars[i+1]` open. The existing `backtest_harness.py`
   does this correctly via HistoricalBus.
6. **Realistic fee model.** 5bp roundtrip minimum. Add 2bp slippage on
   timeout exits (no SL/TP fill, market exit).

If all 6 gates pass → register in `STRATEGY_REGISTRY` at `stage=paper`,
cf=0.0 until 50 live paper closures land. Promote per the standard
lifecycle gates already in PM.

---

## Tuning levers worth trying (if revisited)

The OOS data hints these directions might unlock edge:

- **Drop the chase-gate entirely.** It was designed for the old precog-hl
  environment with different signal sources. On standalone pivot+RSI it just
  filters out valid mean-reversion entries.
- **Wider TP target.** Current TP=3.75% is rarely hit (TP fires < 14% of
  trades; most exits are timeout). Either tighten to 2.0% to hit more, or
  drop the TP entirely and use trailing.
- **Higher RSI threshold for the wick filter.** Currently rsi > 70 OR rsi < 30;
  could test rsi > 80 / rsi < 20 with relaxed wick ratio. Concentrate the
  signal at true extremes.
- **Test 4h or 1h timeframe, not 15m.** 15m noise dominates the wick signal.
  Pivot+RSI on 4h bars has been historically more reliable than on 15m.
- **Per-coin parameters.** The original precog had postmortem-overridable
  thresholds. If reviving, fit per-coin LB / RH / RL on the historical bar
  shape, not a one-size-fits-all default.

---

## Files

- `strategy_runner/strategies/_archived/precog_pivot_rsi.py` — clean Strategy
  class port (~170 lines). Drop-in compatible with the live stack's
  StrategyBase contract.
- `references/precog_revisit.md` — this file.

GitHub origin of full strategy: `Dapperscyphozoa/precog-hl` (archived).
