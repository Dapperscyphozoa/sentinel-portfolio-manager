# lh1 honest backtest — 2026-05-18

**Gate:** `YELLOW`
**Timeframe:** 1H  **Lookback:** 180d  **Universe:** 20 coins

## Caveats
- CAVEAT: liquidation confluence replaced with kline volume spike (historical liq data unavailable). This is the STRUCTURAL-ONLY version of the inverted SMC sweep strategy. Production version requires liquidation event history (e.g., Bybit forceOrder backfill).

## Aggregate (all coins pooled)
```
{
  "n": 95,
  "wr": 0.505,
  "pf": 1.723,
  "expectancy": 0.00336,
  "total_return": 0.3193,
  "avg_win": 0.0159,
  "avg_loss": -0.0094,
  "max_dd_pct_units": -0.1159,
  "sharpe_annualized": 11.73,
  "by_exit": {
    "sl": 61,
    "tp": 17,
    "timeout": 17
  }
}
```

## Walk-forward
```
{
  "train": {
    "n": 47,
    "wr": 0.553,
    "pf": 3.101,
    "expectancy": 0.00784,
    "total_return": 0.3684,
    "avg_win": 0.0209,
    "avg_loss": -0.0083,
    "max_dd_pct_units": -0.0374,
    "sharpe_annualized": 22.71,
    "by_exit": {
      "sl": 31,
      "tp": 10,
      "timeout": 6
    }
  },
  "oos": {
    "n": 48,
    "wr": 0.458,
    "pf": 0.816,
    "expectancy": -0.00102,
    "total_return": -0.0491,
    "avg_win": 0.0099,
    "avg_loss": -0.0102,
    "max_dd_pct_units": -0.1581,
    "sharpe_annualized": -5.15,
    "by_exit": {
      "sl": 30,
      "tp": 7,
      "timeout": 11
    }
  },
  "split_ts": 1770825600000
}
```

## Per-coin
| coin | trades | WR | PF | expectancy |
|---|---|---|---|---|
| BTC | 7 | 14.3% | 0.089 | -0.00721 |
| ETH | 3 | 0.0% | 0.0 | -0.01064 |
| SOL | 3 | 66.7% | 0.689 | -0.00217 |
| BNB | 4 | 75.0% | 1.862 | +0.00290 |
| XRP | 9 | 22.2% | 0.459 | -0.00247 |
| DOGE | 1 | 0.0% | 0.0 | -0.02527 |
| AVAX | 2 | 50.0% | 7.87 | +0.04117 |
| LINK | 5 | 80.0% | 1.982 | +0.00076 |
| DOT | 8 | 50.0% | 1.214 | +0.00133 |
| ADA | 6 | 83.3% | 7.551 | +0.00480 |
| ATOM | 12 | 50.0% | 2.483 | +0.00384 |
| NEAR | 5 | 0.0% | 0.0 | -0.01241 |
| APT | 6 | 66.7% | 3.708 | +0.01404 |
| ARB | 4 | 50.0% | 0.677 | -0.00185 |
| OP | 2 | 100.0% | inf | +0.00343 |
| SUI | 2 | 50.0% | 0.118 | -0.00380 |
| TIA | 0 | 0.0% | 0 | +0.00000 |
| SEI | 4 | 75.0% | 36.038 | +0.00475 |
| INJ | 8 | 75.0% | 16.755 | +0.02652 |
| LTC | 4 | 50.0% | 2.978 | +0.00680 |