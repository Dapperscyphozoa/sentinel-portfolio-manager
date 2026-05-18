# range_fade honest backtest — 2026-05-18

**Gate:** `RED`
**Timeframe:** 15m  **Lookback:** 90d  **Universe:** 11 coins

## Caveats
- CAVEAT: no PM regime filter applied (regime classifier history not available). Production version gates on regime != trend at conf > 0.7. This is the PERMISSIVE version — expect production PF to be slightly higher due to fewer trend-fade losses.

## Aggregate (all coins pooled)
```
{
  "n": 794,
  "wr": 0.435,
  "pf": 0.654,
  "expectancy": -0.00188,
  "total_return": -1.4912,
  "avg_win": 0.0082,
  "avg_loss": -0.0096,
  "max_dd_pct_units": -1.5282,
  "sharpe_annualized": -13.97,
  "by_exit": {
    "sl": 266,
    "tp": 70,
    "timeout": 458
  }
}
```

## Walk-forward
```
{
  "train": {
    "n": 392,
    "wr": 0.406,
    "pf": 0.631,
    "expectancy": -0.00224,
    "total_return": -0.8771,
    "avg_win": 0.0094,
    "avg_loss": -0.0102,
    "max_dd_pct_units": -1.0488,
    "sharpe_annualized": -15.6,
    "by_exit": {
      "sl": 152,
      "tp": 43,
      "timeout": 197
    }
  },
  "oos": {
    "n": 402,
    "wr": 0.463,
    "pf": 0.682,
    "expectancy": -0.00153,
    "total_return": -0.6141,
    "avg_win": 0.0071,
    "avg_loss": -0.009,
    "max_dd_pct_units": -0.7094,
    "sharpe_annualized": -12.23,
    "by_exit": {
      "sl": 114,
      "tp": 27,
      "timeout": 261
    }
  },
  "split_ts": 1775369700000
}
```

## Per-coin
| coin | trades | WR | PF | expectancy |
|---|---|---|---|---|
| BTC | 69 | 42.0% | 0.489 | -0.00228 |
| ETH | 77 | 48.1% | 0.749 | -0.00130 |
| SOL | 78 | 47.4% | 0.698 | -0.00165 |
| BNB | 65 | 43.1% | 0.758 | -0.00099 |
| XRP | 71 | 45.1% | 0.721 | -0.00137 |
| DOGE | 75 | 45.3% | 0.859 | -0.00072 |
| AVAX | 81 | 39.5% | 0.608 | -0.00255 |
| LINK | 67 | 35.8% | 0.501 | -0.00314 |
| DOT | 70 | 48.6% | 0.662 | -0.00192 |
| ADA | 75 | 42.7% | 0.6 | -0.00253 |
| ATOM | 66 | 39.4% | 0.588 | -0.00225 |