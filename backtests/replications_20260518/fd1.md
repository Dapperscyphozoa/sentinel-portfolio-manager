# fd1 honest backtest — 2026-05-18

**Gate:** `YELLOW`
**Timeframe:** 1H  **Lookback:** 180d  **Universe:** 10 coins

## Caveats
- Funding rates pulled from HL /info fundingHistory REST (no caveat — this is the production data source).

## Aggregate (all coins pooled)
```
{
  "n": 150,
  "wr": 0.453,
  "pf": 1.246,
  "expectancy": 0.00219,
  "total_return": 0.3291,
  "avg_win": 0.0245,
  "avg_loss": -0.0163,
  "max_dd_pct_units": -0.156,
  "sharpe_annualized": 8.05,
  "by_exit": {
    "sl": 81,
    "tp": 56,
    "timeout": 13
  }
}
```

## Walk-forward
```
{
  "train": {
    "n": 74,
    "wr": 0.392,
    "pf": 1.03,
    "expectancy": 0.00029,
    "total_return": 0.0215,
    "avg_win": 0.0259,
    "avg_loss": -0.0162,
    "max_dd_pct_units": -0.2385,
    "sharpe_annualized": 1.08,
    "by_exit": {
      "sl": 44,
      "tp": 25,
      "timeout": 5
    }
  },
  "oos": {
    "n": 76,
    "wr": 0.513,
    "pf": 1.507,
    "expectancy": 0.00405,
    "total_return": 0.3076,
    "avg_win": 0.0234,
    "avg_loss": -0.0164,
    "max_dd_pct_units": -0.1311,
    "sharpe_annualized": 14.84,
    "by_exit": {
      "sl": 37,
      "tp": 31,
      "timeout": 8
    }
  },
  "split_ts": 1770451200000
}
```

## Per-coin
| coin | trades | WR | PF | expectancy |
|---|---|---|---|---|
| BTC | 6 | 66.7% | 1.971 | +0.00531 |
| ETH | 6 | 66.7% | 2.218 | +0.00666 |
| SOL | 15 | 46.7% | 1.368 | +0.00322 |
| BNB | 8 | 25.0% | 0.128 | -0.01072 |
| XRP | 17 | 29.4% | 0.727 | -0.00316 |
| DOGE | 3 | 66.7% | 3.488 | +0.01360 |
| AVAX | 15 | 46.7% | 1.526 | +0.00460 |
| LINK | 9 | 44.4% | 0.774 | -0.00206 |
| DOT | 53 | 50.9% | 1.641 | +0.00515 |
| ADA | 18 | 33.3% | 0.916 | -0.00088 |