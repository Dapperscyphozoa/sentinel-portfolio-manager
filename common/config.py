"""Env loader. No defaults that would silently mask misconfiguration in prod."""
from __future__ import annotations

import os
from typing import Optional


def get(key: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    v = os.environ.get(key, default)
    if required and (v is None or v == ""):
        raise RuntimeError(f"missing required env var: {key}")
    return v


def get_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return int(raw)


def get_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw == "":
        return default
    return float(raw)


def get_bool(key: str, default: bool = False) -> bool:
    raw = os.environ.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def state_dir() -> str:
    """Persistent dir. /var/data on Render; ./state locally."""
    d = os.environ.get("STATE_DIR", "./state")
    os.makedirs(d, exist_ok=True)
    return d


def strategy_enabled(name: str) -> bool:
    return get_bool(f"STRATEGY_{name.upper()}_ENABLED", default=False)


def strategy_live(name: str) -> bool:
    return get_bool(f"STRATEGY_{name.upper()}_LIVE", default=False)
