"""Kronos confirmation gate for ICT entries.

Validated zero-shot on HL 4h (60 samples, 60% directional acc, p~0.07).
Magnitude is unreliable — DIRECTION ONLY for our use case.

Lazy-loads model on first call to keep startup fast. Caches predictions
per (coin, last_bar_ts) — same bar won't be predicted twice.

Optional via env: KRONOS_GATE_ENABLED=0 disables entirely (default 1).
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

log = logging.getLogger("kronos_gate")


# Global state — lazy init
_predictor = None
_cache: dict = {}
_cache_max = 1000
_disabled_reason: Optional[str] = None


def _load_predictor():
    """Lazy import + load Kronos. Sets _disabled_reason if unavailable."""
    global _predictor, _disabled_reason
    if _predictor is not None or _disabled_reason is not None:
        return
    try:
        # Try the bundled Kronos repo first
        import sys
        kronos_path = os.environ.get("KRONOS_REPO_PATH", "/opt/Kronos")
        if os.path.isdir(kronos_path) and kronos_path not in sys.path:
            sys.path.insert(0, kronos_path)
        from model import Kronos, KronosTokenizer, KronosPredictor
        tokenizer = KronosTokenizer.from_pretrained(
            os.environ.get("KRONOS_TOKENIZER", "NeoQuasar/Kronos-Tokenizer-base")
        )
        model = Kronos.from_pretrained(
            os.environ.get("KRONOS_MODEL", "NeoQuasar/Kronos-small")
        )
        device = os.environ.get("KRONOS_DEVICE", "cpu")
        _predictor = KronosPredictor(model, tokenizer, device=device, max_context=512)
        log.info("Kronos predictor loaded on %s", device)
    except Exception as e:
        _disabled_reason = f"load_error:{e}"
        log.warning("Kronos disabled: %s", e)


def is_enabled() -> bool:
    """Check env flag + load status."""
    if os.environ.get("KRONOS_GATE_ENABLED", "1") != "1":
        return False
    _load_predictor()
    return _predictor is not None


def _cache_key(coin: str, last_bar_ts: int, pred_len: int) -> str:
    return f"{coin}:{last_bar_ts}:{pred_len}"


def _maybe_evict_cache():
    global _cache
    if len(_cache) > _cache_max:
        # drop oldest 50%
        keys = list(_cache.keys())
        for k in keys[: len(keys) // 2]:
            _cache.pop(k, None)


def predict_direction(coin: str, bars: list[dict], pred_len: int = 6) -> Optional[dict]:
    """Get Kronos directional forecast for the next pred_len bars.

    Args:
        coin: e.g. 'BTC'
        bars: list of {open_ts, open, high, low, close, volume} (most recent last).
              Need at least 300 bars of history.
        pred_len: how many bars ahead to predict (default 6 = 24h on 4h TF)

    Returns: {"direction": "BULL"|"BEAR"|"FLAT", "pred_return": float,
              "cur_price": float, "pred_close": float, "cached": bool}
              or None if Kronos unavailable / not enough data.
    """
    if not is_enabled():
        return None
    if not bars or len(bars) < 300:
        return None

    last_bar_ts = int(bars[-1]["open_ts"])
    key = _cache_key(coin, last_bar_ts, pred_len)
    if key in _cache:
        out = dict(_cache[key])
        out["cached"] = True
        return out

    try:
        import pandas as pd
        # Build DataFrame in Kronos format
        df = pd.DataFrame(bars[-300:])
        df["timestamps"] = pd.to_datetime(df["open_ts"], unit="ms")
        df["amount"] = df["volume"] * df["close"]
        x_df = df[["open", "high", "low", "close", "volume", "amount"]].reset_index(drop=True)
        x_ts = df["timestamps"].reset_index(drop=True)
        # Future timestamps — extrapolate from last bar interval
        if len(df) >= 2:
            bar_delta = df["timestamps"].iloc[-1] - df["timestamps"].iloc[-2]
        else:
            bar_delta = pd.Timedelta(hours=4)
        y_ts = pd.Series([df["timestamps"].iloc[-1] + (i + 1) * bar_delta for i in range(pred_len)])

        pred = _predictor.predict(
            df=x_df, x_timestamp=x_ts, y_timestamp=y_ts,
            pred_len=pred_len, T=1.0, top_p=0.9, sample_count=1, verbose=False,
        )
        cur_price = float(bars[-1]["close"])
        pred_close = float(pred["close"].iloc[-1])
        pred_ret = (pred_close - cur_price) / cur_price
        # Threshold: |ret| < 0.5% = FLAT (no conviction)
        flat_threshold = float(os.environ.get("KRONOS_FLAT_THRESHOLD", "0.005"))
        if abs(pred_ret) < flat_threshold:
            direction = "FLAT"
        elif pred_ret > 0:
            direction = "BULL"
        else:
            direction = "BEAR"
        out = {
            "direction": direction,
            "pred_return": pred_ret,
            "cur_price": cur_price,
            "pred_close": pred_close,
            "cached": False,
            "ts": int(time.time()),
        }
        _cache[key] = dict(out)
        _maybe_evict_cache()
        return out
    except Exception as e:
        log.exception("Kronos predict failed for %s: %s", coin, e)
        return None


def agrees(direction: str, ict_is_long: bool) -> bool:
    """Does Kronos agree with ICT's direction?

    FLAT = no opinion, defaults to ALLOW (don't filter on indecision).
    """
    if direction == "FLAT":
        return True
    if direction == "BULL":
        return ict_is_long
    if direction == "BEAR":
        return not ict_is_long
    return True   # unknown direction = allow
