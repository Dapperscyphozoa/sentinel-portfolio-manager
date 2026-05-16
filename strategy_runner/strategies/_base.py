"""Strategy contract per SPEC §6.1."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


@dataclass
class Signal:
    coin: str
    side: str              # "B" or "A"
    is_long: bool
    ref_price: float
    sl_px: float
    tp_px: float
    max_hold_bars: int
    fire_ts: float         # ms epoch
    fire_reason: str
    extras: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class StrategyBase:
    """Subclasses set class attributes and implement evaluate()."""

    NAME: str = ""
    CLOID_PREFIX: str = ""
    AFFINITY: list[str] = []
    TF: str = "1h"
    UNIVERSE: list[str] = []

    @classmethod
    def evaluate(cls, coin: str, bus) -> Optional[Signal]:
        raise NotImplementedError

    @classmethod
    def should_close(cls, trade_row, bus) -> tuple[bool, str]:
        """Optional override for strategies with strategy-defined exits beyond
        the fixed SL/TP/timeout that trader.position_loop handles. Return
        (True, reason) to close the position. Default: no strategy-driven close.

        Used by Donchian (10-bar opposite-channel exit) and any future strategy
        that wants trailing/dynamic exits.
        """
        return (False, "")

    @classmethod
    def info(cls) -> dict:
        return {
            "name": cls.NAME,
            "cloid_prefix": cls.CLOID_PREFIX,
            "affinity": list(cls.AFFINITY),
            "tf": cls.TF,
            "universe_size": len(cls.UNIVERSE),
        }
