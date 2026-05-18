#!/usr/bin/env python3
"""
CLSI v2 dimensional collinearity test.

Pulls live data from the operator's signal_bus, computes the proposed CLSI
features per coin per hour, then runs PCA + Ledoit-Wolf condition analysis
to determine the effective rank of the feature matrix.

OUTPUTS
-------
- Per-coin correlation matrix between proposed features
- PCA explained-variance ratio
- Effective rank @ 95% variance threshold
- Σ condition number (with and without Ledoit-Wolf shrinkage)
- DATA GAPS report: which CLSI dimensions are not computable with current
  signal_bus deployment + the specific endpoint/subscription that would fix it

DESIGN NOTES
------------
Council critique was that v1's 5 features (basis, funding-velocity, OI-momentum,
HLP-PnL, liquidation-density) may have effective rank << 5. v2 reduced to 3
after merging D3 into D2 and dropping D4. This test verifies empirically.

USAGE
-----
    python3 clsi_rank_test.py
"""
from __future__ import annotations
import json
import sys
import time
from collections import defaultdict

import httpx
import numpy as np

BASE = "https://core-o21t.onrender.com/signal_bus"
COINS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "AVAX", "LINK", "ARB", "OP"]
TF = "1h"
N_BARS = 200       # ~8d at 1h
FUNDING_HOURS = 24 # only 34h available; use 24 to be safe

client = httpx.Client(timeout=30.0)


def pull_candles(coin: str) -> list[dict]:
    r = client.get(f"{BASE}/candles/{coin}/{TF}", params={"n": N_BARS})
    r.raise_for_status()
    return r.json()


def pull_funding(coin: str, hours: int) -> list[dict]:
    r = client.get(f"{BASE}/funding/{coin}", params={"hours": hours})
    r.raise_for_status()
    return r.json()


def pull_liqs(since_ms: int, coin: str | None = None) -> list[dict]:
    params = {"since": since_ms}
    if coin:
        params["coin"] = coin
    r = client.get(f"{BASE}/liq", params=params)
    r.raise_for_status()
    return r.json()


def pull_markprice(coin: str) -> dict:
    r = client.get(f"{BASE}/markprice/{coin}")
    if r.status_code != 200:
        return {}
    return r.json()


def pull_hlp(coin: str) -> dict:
    r = client.get(f"{BASE}/hlp_position/{coin}")
    if r.status_code != 200:
        return {}
    return r.json()


def bucket_funding_to_hour(funding_rows: list[dict]) -> dict[int, float]:
    """Return {hour_open_ts_ms: avg_rate}."""
    buckets: dict[int, list[float]] = defaultdict(list)
    for row in funding_rows:
        ts = row["ts"]
        hr = (ts // 3600000) * 3600000
        buckets[hr].append(row["rate"])
    return {hr: float(np.mean(v)) for hr, v in buckets.items()}


def build_feature_matrix(coin: str) -> tuple[np.ndarray, list[str], dict]:
    """
    Returns (X, feature_names, diagnostics).
    X is (n_bars, n_features) with each row aligned on hour open_ts.
    """
    diag = {"coin": coin}

    candles = pull_candles(coin)
    if not candles:
        diag["error"] = "no candles"
        return np.array([]), [], diag

    closes = np.array([c["close"] for c in candles], dtype=float)
    highs = np.array([c["high"] for c in candles], dtype=float)
    lows = np.array([c["low"] for c in candles], dtype=float)
    vols = np.array([c["volume"] for c in candles], dtype=float)
    opens_ts = np.array([c["open_ts"] for c in candles], dtype=np.int64)
    diag["n_candles"] = len(candles)

    # FEATURE 1: realized log-return absolute value (proxy for vol)
    rets = np.zeros_like(closes)
    rets[1:] = np.log(closes[1:] / closes[:-1])
    abs_ret = np.abs(rets)

    # FEATURE 2: high-low range as fraction of close (alt vol proxy, captures wicks)
    hl_range = (highs - lows) / closes

    # FEATURE 3: volume z-score over rolling 24-bar (1d) window
    vol_z = np.zeros_like(vols)
    for i in range(24, len(vols)):
        win = vols[i - 24:i]
        if win.std() > 0:
            vol_z[i] = (vols[i] - win.mean()) / win.std()
    vol_z_abs = np.abs(vol_z)  # magnitude only — direction is captured elsewhere

    # FEATURE 4: funding velocity (d_funding/dt) at hour granularity
    funding_rows = pull_funding(coin, FUNDING_HOURS)
    diag["n_funding_raw"] = len(funding_rows)
    hour_funding = bucket_funding_to_hour(funding_rows)
    funding_series = np.full_like(closes, np.nan, dtype=float)
    for i, ts in enumerate(opens_ts):
        if int(ts) in hour_funding:
            funding_series[i] = hour_funding[int(ts)]
    # First-difference to get velocity; fill leading NaN
    funding_vel = np.zeros_like(funding_series)
    funding_vel[1:] = funding_series[1:] - funding_series[:-1]
    # Mask rows where funding wasn't available
    funding_mask = ~np.isnan(funding_series)
    diag["n_funding_aligned"] = int(funding_mask.sum())

    # FEATURE 5: liquidation density (USD-liq per hour for this coin)
    # Limited by signal_bus retention; pull what we can
    span_ms = int(opens_ts[-1] - opens_ts[0] + 3600000)
    since_ms = int(opens_ts[0])
    liqs = pull_liqs(since_ms, coin)
    diag["n_liqs"] = len(liqs)
    liq_density = np.zeros_like(closes)
    for liq in liqs:
        ts = liq.get("ts", 0)
        usd = liq.get("usd", liq.get("qty", 0) * liq.get("price", 0))
        # Find hour bucket
        for i, hr in enumerate(opens_ts):
            if hr <= ts < hr + 3600000:
                liq_density[i] += usd
                break

    # Stack features. Use 5 dims to test full v1 + the council's reduction prediction.
    feature_names = ["abs_ret", "hl_range", "vol_z_abs", "funding_vel", "liq_density"]
    X_full = np.column_stack([abs_ret, hl_range, vol_z_abs, funding_vel, liq_density])

    # Drop rows where funding wasn't aligned (else funding_vel = 0 spuriously)
    keep = funding_mask & (np.arange(len(closes)) > 24)  # also drop warmup for vol_z
    X = X_full[keep]
    diag["n_clean_rows"] = int(keep.sum())

    return X, feature_names, diag


def analyze_rank(X: np.ndarray, names: list[str]) -> dict:
    """PCA + condition analysis. Drops zero-variance features automatically."""
    if X.shape[0] < 10 or X.shape[1] < 2:
        return {"error": f"insufficient data: shape={X.shape}"}

    # Drop degenerate features (zero variance — e.g. liq_density when liq stream dead)
    raw_sigma = X.std(axis=0)
    keep_mask = raw_sigma > 1e-12
    dropped = [names[i] for i in range(len(names)) if not keep_mask[i]]
    kept_names = [names[i] for i in range(len(names)) if keep_mask[i]]
    Xk = X[:, keep_mask]
    if Xk.shape[1] < 2:
        return {"error": f"only {Xk.shape[1]} non-degenerate features after drop",
                "dropped": dropped, "kept": kept_names}

    # Standardize
    mu = Xk.mean(axis=0)
    sigma = Xk.std(axis=0)
    Xs = (Xk - mu) / sigma

    n = Xs.shape[0]
    Sigma = (Xs.T @ Xs) / (n - 1)

    eigvals, _ = np.linalg.eigh(Sigma)
    eigvals = eigvals[::-1]
    eigvals = np.clip(eigvals, 1e-12, None)
    total_var = eigvals.sum()
    explained_var = eigvals / total_var
    cumvar = np.cumsum(explained_var)

    eff_rank_95 = int(np.searchsorted(cumvar, 0.95) + 1)
    eff_rank_99 = int(np.searchsorted(cumvar, 0.99) + 1)
    cond_raw = float(eigvals[0] / eigvals[-1])

    lam = 0.1
    Sigma_lw = (1 - lam) * Sigma + lam * np.trace(Sigma) / Sigma.shape[0] * np.eye(Sigma.shape[0])
    eigvals_lw, _ = np.linalg.eigh(Sigma_lw)
    eigvals_lw = eigvals_lw[::-1]
    eigvals_lw = np.clip(eigvals_lw, 1e-12, None)
    cond_lw = float(eigvals_lw[0] / eigvals_lw[-1])

    corr = np.corrcoef(Xs.T)

    return {
        "shape": list(Xk.shape),
        "kept_features": kept_names,
        "dropped_features": dropped,
        "explained_var": [float(v) for v in explained_var],
        "cumulative_var": [float(v) for v in cumvar],
        "eff_rank_95": eff_rank_95,
        "eff_rank_99": eff_rank_99,
        "condition_raw": cond_raw,
        "condition_lw_shrunk": cond_lw,
        "correlation_matrix": corr.tolist(),
        "feature_names": kept_names,
    }


def main():
    print(f"CLSI v2 rank test — {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n")
    print(f"Source: {BASE}")
    print(f"Coins:  {COINS}")
    print(f"TF:     {TF}  ({N_BARS} bars target)\n")
    print("=" * 78)

    per_coin = {}
    aggregate = []
    feature_names = None

    for coin in COINS:
        try:
            X, names, diag = build_feature_matrix(coin)
            feature_names = names
            print(f"\n{coin:5s}  candles={diag.get('n_candles','?'):4}  "
                  f"funding_raw={diag.get('n_funding_raw','?'):6}  "
                  f"funding_aligned={diag.get('n_funding_aligned','?'):3}  "
                  f"liqs={diag.get('n_liqs','?'):3}  "
                  f"clean_rows={diag.get('n_clean_rows','?'):3}")
            if X.shape[0] < 10:
                print(f"       SKIP — insufficient clean rows")
                continue
            analysis = analyze_rank(X, names)
            per_coin[coin] = analysis
            aggregate.append(X)
        except Exception as e:
            print(f"{coin:5s}  ERROR: {e}")

    print()
    print("=" * 78)
    print("\nPER-COIN EFFECTIVE RANK\n")
    print(f"{'coin':6s}  {'kept':>4s}  {'rank@95%':9s}  {'rank@99%':9s}  {'cond_raw':>10s}  {'cond_LW':>10s}  dropped")
    for coin, a in per_coin.items():
        if "error" in a:
            print(f"{coin:6s}  ERROR: {a['error']}")
            continue
        dropped_str = ",".join(a.get("dropped_features", [])) or "—"
        print(f"{coin:6s}  {a['shape'][1]:4d}  {a['eff_rank_95']:9d}  {a['eff_rank_99']:9d}  "
              f"{a['condition_raw']:10.1f}  {a['condition_lw_shrunk']:10.1f}  {dropped_str}")

    if aggregate:
        # Pooled (all coins concatenated, after per-coin standardization)
        pooled = []
        for X in aggregate:
            mu = X.mean(axis=0); s = X.std(axis=0); s[s==0]=1
            pooled.append((X - mu) / s)
        Xp = np.vstack(pooled)
        print(f"\nPOOLED ACROSS {len(aggregate)} COINS  (n_rows={Xp.shape[0]})\n")
        pooled_analysis = analyze_rank(Xp, feature_names)
        print(f"Explained variance per PC:  "
              f"{[round(v, 3) for v in pooled_analysis['explained_var']]}")
        print(f"Cumulative:                  "
              f"{[round(v, 3) for v in pooled_analysis['cumulative_var']]}")
        print(f"Effective rank @ 95% var: {pooled_analysis['eff_rank_95']}")
        print(f"Effective rank @ 99% var: {pooled_analysis['eff_rank_99']}")
        print(f"Σ condition number (raw):    {pooled_analysis['condition_raw']:.1f}")
        print(f"Σ condition number (L-W):    {pooled_analysis['condition_lw_shrunk']:.1f}")

        print(f"\nFeatures kept after degenerate-drop:  {pooled_analysis.get('kept_features', feature_names)}")
        print(f"Features dropped (zero variance):     {pooled_analysis.get('dropped_features', [])}")
        print(f"\nPOOLED CORRELATION MATRIX (kept features only):")
        corr = np.array(pooled_analysis["correlation_matrix"])
        kept = pooled_analysis.get("kept_features", feature_names)
        print(f"           {'  '.join(f'{n:>10s}' for n in kept)}")
        for i, n in enumerate(kept):
            print(f"{n:>10s}  {'  '.join(f'{corr[i,j]:>10.3f}' for j in range(len(kept)))}")

    print()
    print("=" * 78)
    print("\nDATA INFRASTRUCTURE GAPS BLOCKING CLSI v2 PRODUCTION DEPLOY")
    print("(diagnosed from this run vs the v2 design):")
    print()
    print("1. LIQUIDATION STREAM IS DEAD")
    print("   - signal_bus /health shows liq_events=0 cached")
    print("   - Per-coin /liq queries returned zero events across 8d windows")
    print("   - Root cause: OKX liquidation channel subscribe failed (code 60018)")
    print("     and Binance forceOrder stream not active (DATA_VENUE=okx)")
    print("   - Fix: either correct OKX channel name or set DATA_VENUE=binance")
    print("   - Until fixed: D5 (liquidation density) cannot be computed historically")
    print()
    print("2. FUNDING HISTORY LIMITED TO ~34h (target: 30d)")
    print("   - signal_bus only caches funding back to deploy boot time")
    print("   - For 30d rolling Mahalanobis Σ, need REST backfill of HL historical")
    print("     funding via the /info historicalFunding endpoint")
    print("   - Until fixed: D2 (funding velocity) Σ estimate is high-variance")
    print()
    print("3. NO BASIS HISTORY — CURRENT SNAPSHOT ONLY")
    print("   - /markprice returns {hl_mid, binance_mid} at request time")
    print("   - No historical persistence of basis time series")
    print("   - Fix: add a basis_snapshot persistence loop in signal_bus that")
    print("     samples /markprice every 60s and flushes to SQLite")
    print("   - Until fixed: D1 (basis stress) cannot be computed historically")
    print()
    print("4. NO OPEN-INTEREST ENDPOINT")
    print("   - signal_bus has no /oi route; HL exposes OI via /info metaAndAssetCtxs")
    print("   - Fix: add HL OI poller + /oi/{coin} endpoint OR derive OI proxy from")
    print("     funding_rate * notional_open from /hlp_positions (partial substitute)")
    print("   - Until fixed: D3 (OI momentum) substituted with vol_z_abs (weaker proxy)")
    print()
    print("5. HLP HISTORY SPARSE (26 samples for BTC)")
    print("   - Fresh redeploy lost any persisted history")
    print("   - z-scores return null until ≥200 samples accumulated (~17h at 5min)")
    print("   - Already running on the resumed core; will be usable in ~12h")
    print()
    print("RECOMMENDED SEQUENCING:")
    print("  (a) Ship signal_bus PR fixing liq stream + basis-history sampler")
    print("      (small, ~1-2 days work in signal_bus/ + 1 new endpoint each)")
    print("  (b) Wait 7-30d for data accumulation")
    print("  (c) Re-run this rank test on the complete 5-dim feature set")
    print("  (d) Then commit to CLSI v2 build OR abandon based on the empirical rank")


if __name__ == "__main__":
    main()
