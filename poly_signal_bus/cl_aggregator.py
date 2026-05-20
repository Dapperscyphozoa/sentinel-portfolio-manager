"""Chainlink Data Stream aggregator replication.

This module is THE EDGE. If the algorithm here predicts Chainlink BTC/USD
ticks within 5bps median / 15bps p95 over 100k samples, `cl_predictor` and
`endgame` strategies are GREEN. If not, both strategies are killed.

Algorithm (per Chainlink Crypto OCR2 docs as of 2026; verify against current
official aggregation method before live trade):

  1. Collect price observations from each oracle node (we approximate by
     using the spot mid from each CEX venue feeding the DON).
  2. Drop venues whose price diverges >TRIM_BPS from the rolling median.
  3. Drop the highest and lowest of the survivors (Tukey-style outlier trim).
  4. Take the median of what remains.
  5. Require min N venues alive to publish.

Notes:
  - The actual DON weighs by node reputation; we approximate with equal
    weights. If validation fails p95 < 15bps, try volume-weighting next.
  - Aggregator runs in pure-Python with no I/O. Bus injects current prices.
"""
from __future__ import annotations

import statistics
from typing import Mapping, Optional


# Default DON composition for BTC/USD on Polygon as of Q1 2026.
# Verify against Chainlink's published DON membership before mainnet trust.
DEFAULT_VENUES = ("binance", "coinbase", "kraken", "bitstamp", "bitfinex", "okx", "huobi")

TRIM_OUTLIER_BPS = 50.0
MIN_VENUES_REQUIRED = 5


def aggregate(
    venue_prices: Mapping[str, float],
    trim_outlier_bps: float = TRIM_OUTLIER_BPS,
    min_venues: int = MIN_VENUES_REQUIRED,
    drop_extremes: bool = True,
) -> Optional[float]:
    """Reproduce the Chainlink DON's aggregation step.

    Args:
        venue_prices: {"binance": 67830.4, "coinbase": 67829.8, ...}
            Only venues that have a recent (<3s) tick should be included.
        trim_outlier_bps: drop venues >this many bps from the rolling median.
        min_venues: require at least this many venues to compute.
        drop_extremes: drop highest+lowest of survivors before final median.

    Returns:
        Aggregated price, or None if insufficient consensus.
    """
    prices = [p for p in venue_prices.values() if p is not None and p > 0]
    if len(prices) < min_venues:
        return None

    # Step 1: outlier trim against rolling median
    rolling_median = statistics.median(prices)
    if rolling_median <= 0:
        return None
    threshold = rolling_median * (trim_outlier_bps / 10_000.0)
    survivors = [p for p in prices if abs(p - rolling_median) <= threshold]

    if len(survivors) < min_venues:
        return None

    # Step 2: drop highest + lowest (Tukey-style)
    if drop_extremes and len(survivors) >= 3:
        survivors = sorted(survivors)[1:-1]

    return statistics.median(survivors)


def aggregate_with_diagnostics(
    venue_prices: Mapping[str, float],
    trim_outlier_bps: float = TRIM_OUTLIER_BPS,
    min_venues: int = MIN_VENUES_REQUIRED,
) -> dict:
    """Same as aggregate() but returns rich diagnostics for the bus's
    /cl_predicted endpoint and the validation script.

    Returns:
        {
          "predicted": float | None,
          "n_input": int, "n_after_trim": int, "n_dropped_outlier": int,
          "rolling_median": float,
          "trimmed_venues": [venue_names_dropped],
          "venue_prices": {...},
        }
    """
    valid = {v: p for v, p in venue_prices.items() if p is not None and p > 0}
    if len(valid) < min_venues:
        return {
            "predicted": None,
            "n_input": len(valid),
            "n_after_trim": 0,
            "n_dropped_outlier": 0,
            "rolling_median": None,
            "trimmed_venues": [],
            "venue_prices": dict(valid),
            "reason": f"only {len(valid)} venues, need {min_venues}",
        }
    rolling_median = statistics.median(valid.values())
    threshold = rolling_median * (trim_outlier_bps / 10_000.0)
    dropped = [v for v, p in valid.items() if abs(p - rolling_median) > threshold]
    survivors = {v: p for v, p in valid.items() if v not in dropped}

    if len(survivors) < min_venues:
        return {
            "predicted": None,
            "n_input": len(valid),
            "n_after_trim": len(survivors),
            "n_dropped_outlier": len(dropped),
            "rolling_median": rolling_median,
            "trimmed_venues": dropped,
            "venue_prices": dict(valid),
            "reason": f"only {len(survivors)} after outlier trim",
        }
    surv_prices = sorted(survivors.values())
    if len(surv_prices) >= 3:
        surv_prices = surv_prices[1:-1]
    predicted = statistics.median(surv_prices)
    return {
        "predicted": predicted,
        "n_input": len(valid),
        "n_after_trim": len(survivors),
        "n_dropped_outlier": len(dropped),
        "rolling_median": rolling_median,
        "trimmed_venues": dropped,
        "venue_prices": dict(valid),
    }


def diff_bps(predicted: float, actual: float) -> float:
    """Bps difference, signed (predicted - actual)."""
    if actual <= 0:
        return float("inf")
    return (predicted - actual) / actual * 10_000.0


if __name__ == "__main__":
    # Smoke test
    sample = {
        "binance":   67830.2,
        "coinbase":  67831.5,
        "kraken":    67829.9,
        "bitstamp":  67830.8,
        "bitfinex":  67835.0,
        "okx":       67830.5,
        "huobi":     68500.0,  # outlier; should be trimmed
    }
    diag = aggregate_with_diagnostics(sample)
    print(diag)
    assert diag["predicted"] is not None
    assert "huobi" in diag["trimmed_venues"]
    print("OK")
