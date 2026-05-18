# vsq honest backtest — 2026-05-18

**Gate:** `YELLOW`
**Timeframe:** 1H  **Lookback:** 180d  **Universe:** 20 coins

## Aggregate (all coins pooled)
```
{
  "n": 377,
  "wr": 0.401,
  "pf": 1.138,
  "expectancy": 0.00152,
  "total_return": 0.5748,
  "avg_win": 0.0315,
  "avg_loss": -0.0185,
  "max_dd_pct_units": -0.3263,
  "sharpe_annualized": 3.87,
  "by_exit": {
    "sl": 192,
    "tp": 59,
    "timeout": 126
  }
}
```

## Walk-forward
```
{
  "train": {
    "n": 188,
    "wr": 0.335,
    "pf": 0.977,
    "expectancy": -0.0003,
    "total_return": -0.0573,
    "avg_win": 0.0383,
    "avg_loss": -0.0197,
    "max_dd_pct_units": -0.9261,
    "sharpe_annualized": -0.71,
    "by_exit": {
      "sl": 108,
      "tp": 33,
      "timeout": 47
    }
  },
  "oos": {
    "n": 189,
    "wr": 0.466,
    "pf": 1.37,
    "expectancy": 0.00334,
    "total_return": 0.6321,
    "avg_win": 0.0266,
    "avg_loss": -0.0169,
    "max_dd_pct_units": -0.5011,
    "sharpe_annualized": 9.37,
    "by_exit": {
      "sl": 84,
      "tp": 26,
      "timeout": 79
    }
  },
  "split_ts": 1771246800000
}
```

## Per-coin
| coin | trades | WR | PF | expectancy |
|---|---|---|---|---|
| BTC | 22 | 40.9% | 1.368 | +0.00231 |
| ETH | 30 | 33.3% | 0.862 | -0.00128 |
| SOL | 19 | 31.6% | 0.933 | -0.00075 |
| BNB | 13 | 46.2% | 0.855 | -0.00080 |
| XRP | 19 | 47.4% | 1.639 | +0.00434 |
| DOGE | 17 | 29.4% | 0.713 | -0.00433 |
| AVAX | 18 | 38.9% | 0.634 | -0.00362 |
| LINK | 18 | 33.3% | 0.896 | -0.00111 |
| DOT | 20 | 45.0% | 1.377 | +0.00449 |
| ADA | 24 | 29.2% | 0.727 | -0.00389 |
| ATOM | 15 | 46.7% | 1.551 | +0.00526 |
| NEAR | 19 | 31.6% | 1.091 | +0.00144 |
| APT | 20 | 40.0% | 0.962 | -0.00055 |
| ARB | 15 | 53.3% | 1.658 | +0.00764 |
| OP | 17 | 41.2% | 1.341 | +0.00497 |
| SUI | 20 | 35.0% | 0.715 | -0.00359 |
| TIA | 12 | 25.0% | 1.004 | +0.00009 |
| SEI | 14 | 42.9% | 1.282 | +0.00259 |
| INJ | 16 | 62.5% | 3.264 | +0.02159 |
| LTC | 29 | 51.7% | 1.294 | +0.00211 |