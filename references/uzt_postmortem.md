# UZT post-mortem — v1 implementation, May 2026

## TL;DR

**Status: RED per §1.5 honest-backtest gate. DO NOT deploy live capital.**

Lesson #2 (Unified Zone Trading) framework was implemented as `strategy_runner/strategies/uzt.py`,
sentinel-audited at MODERATE 7/7 (77% confidence) with the whipsaw-cooldown mitigation applied,
and tested honestly via the existing `scripts/backtest_harness.py` against 30 days of OKX 15m data
on 4 majors (BTC/ETH/SOL/SUI).

The result reproduced the audit's worst-case prediction: PF collapsed from the Lesson #2 reported
1.84 to **0.18**.

## Backtest results

| Window | n | WR | PF | Expectancy |
|---|---|---|---|---|
| All trades | 21 | 14.3% | 0.18 | -0.56%/trade |
| Walk-forward IS | 10 | 0.0% | 0.00 | -0.82% |
| Walk-forward OOS | 11 | 27.3% | 0.43 | -0.31% |

Per-coin:

- BTC: n=6 WR 0% PF 0
- ETH: n=6 WR 17% PF 0.09
- SOL: n=6 WR 17% PF 0.06
- SUI: n=3 WR 33% PF 0.77

Outcome distribution: 12 stop-outs, **0 take-profit hits**, 9 time-stops (10h max hold).
Hold time: median 8h.

## What this tells us

Either (a) my translation of Lesson #2 contains a structural bug I did not find in one session,
or (b) Lesson #2's reported PF 1.84 was overfit to the original 50d sample. The audit explicitly
flagged this risk: *"assume PF degrades to 1.2-1.4 once look-ahead is purged"*. The actual
degradation was far worse — closer to the cex-dex-arb PF 14.92 → 0.8 collapse that motivated
§1.5 in the first place.

The most telling signal: **zero take-profits in 30 days**. The strategy is finding entries that
look correct at fire time but get stopped out by normal 15m noise before TP. Possible structural
causes:

1. **SL too tight** — sweep-wick + 0.03% buffer is < 1 ATR on 15m for the major-coin universe.
2. **TP too ambitious** — 3R targets on 0.5%-tolerance retests means TP is ~1.5% away. In 15m noise
   environments without a strong trend, price walks back and forth across this range until SL hits.
3. **Zone formation is too rare on this regime/universe** — only 2 zones found across BTC in 60d
   (vs Lesson #2's implied dozens). The current 1.5×ATR displacement filter over 3 4h bars is
   stricter than what Lesson #2 likely used.
4. **The 15m state-machine fires REV signals into continuing trends** — the MSS check passes on
   noise sweeps that then fail to reverse.

## Disposition

- Strategy code retained at `strategy_runner/strategies/uzt.py` for future iteration.
- Registered in `runner.py` but **DISABLED by default** (opt-in via `STRATEGY_UZT_ENABLED=1`).
- `audit_status: PROVISIONAL` set in the Signal extras.
- Not added to `STRATEGY_GATES.md` GREEN/YELLOW list — sits below GREEN/YELLOW/RED as **rejected**.

## What it would take to revisit

1. **Independent re-derivation** of Lesson #2's exact zone-formation algorithm from the source
   document — particularly: how zones are defined, what counts as a sweep, what counts as MSS.
   The framework in Lesson #1 is more specific than Lesson #2's parameter table.
2. **A 90d backtest on the universe Lesson #2 actually used** (57 perps), not just majors.
   Edge may be concentrated in the alt tail.
3. **Strategy-grid search** over: SL buffer (0.03% / 0.1% / 0.3%), TP R-multiple (1.5R / 2R / 3R),
   retest tolerance (0.5% / 1% / 2%), HTF displacement (1.0× / 1.5× / 2.0× ATR), MSS displacement
   (1.0× / 1.2× / 1.5× ATR).
4. **A side-by-side comparison** against `ict_confluence_4h` and `ict_fvg_4h` — these are the
   already-live SMC strategies in this codebase; if they fire and UZT doesn't, the Lesson #2
   implementation has bugs. If they all fail, the regime is hostile to SMC and UZT cannot help.

Until at least (1) and (3) are done, treat UZT as a research artifact, not a strategy.

## Audit trail

- Original sentinel audit: MODERATE 77%, 5 valid / 1 silent / 3 failed voters.
- Whipsaw-cooldown mitigation (2-bar no-reclaim before CONTINUATION arm) was applied per audit.
- Honest backtest run: 2026-05-18, OKX 15m, 30d, 4 coins.
- Backtest artifacts: `backtests/uzt_20260518.md`, `backtests/uzt_20260518.jsonl`.
