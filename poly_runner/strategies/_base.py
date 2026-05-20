"""Base contract for poly-runner strategies.

Each strategy module exposes a `evaluate(market, bus) -> Optional[Signal]`
classmethod. Pure functions: no network calls except via `bus`.

The runner imports all strategies from this package's submodules and
dispatches them per active market.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal, Optional


SignalSide = Literal["BUY", "SELL"]
Token = Literal["YES", "NO"]


@dataclass
class Signal:
    """Single directional bet (`cl_predictor`, `endgame`, `cross_asset`)."""
    strategy: str
    market_id: str
    asset: str
    token: Token
    side: SignalSide
    price: float           # limit price; FOK if take-side, GTC if maker
    size_usdc: float
    order_type: Literal["GTC", "FOK"] = "FOK"
    edge_bps: float = 0.0
    cl_predicted: Optional[float] = None
    pm_implied: Optional[float] = None
    fire_reason: str = ""
    extras: dict = field(default_factory=dict)


@dataclass
class Quote:
    """Maker quote pair (`maker_quote`)."""
    strategy: str
    market_id: str
    token: Token
    bid_price: float
    bid_size_usdc: float
    ask_price: float
    ask_size_usdc: float
    fair_prob: float
    inventory: float
    extras: dict = field(default_factory=dict)


class StrategyBase:
    """Common interface. Subclasses set NAME + override evaluate()."""

    NAME: str = ""
    CAPITAL_FRACTION: float = 0.0
    REQUIRES_LIVE_CL: bool = True       # most strategies need CL prediction

    @classmethod
    def evaluate(cls, market: dict, bus) -> Optional[Signal]:
        raise NotImplementedError


# ────────────────────────── Helpers ──────────────────────────

def dynamic_fee(price: float) -> float:
    """Polymarket's dynamic taker fee curve (Jan 2026 schedule).

    Peak ~1.56% at price=0.5, declining to ~0% at the 0.05/0.95 tails.
    Triangular profile (verify against current PM docs before live trade):

        f(p) = 0.0156 * (1 - |2p - 1|)
    """
    if price <= 0 or price >= 1:
        return 0.0
    return 0.0156 * (1.0 - abs(2.0 * price - 1.0))


def kelly_fraction(edge: float, win_prob: float, loss_payoff: float = 1.0,
                   cap: float = 0.05) -> float:
    """Half-Kelly sizing on a binary outcome.

    edge: expected_value/notional (post-fee)
    win_prob: probability of full win (we get $1 per token)
    loss_payoff: we lose $price; for binary, loss = price paid

    Returns fraction of capital to bet, capped.
    """
    if edge <= 0 or win_prob <= 0 or win_prob >= 1:
        return 0.0
    # Standard Kelly for binary: f* = (p*b - q) / b, where b = payoff/cost ratio
    # For a YES contract bought at price p: cost=p, payoff_if_win=(1-p)/p
    # We pass edge directly because the caller has the better composite estimate
    f = win_prob - (1 - win_prob) / max(edge, 1e-6)
    f = max(0.0, min(f, 1.0)) * 0.5  # half-Kelly
    return min(f, cap)


def true_prob_from_cl(predicted_cl: float, start_price: float,
                       time_remaining_s: float, sigma_per_sec: float) -> float:
    """Compute true_prob(price>start at expiry) under Brownian motion drift
    from the current predicted level.

    Args:
        predicted_cl: our local Chainlink prediction
        start_price: market's start_price (set at window open)
        time_remaining_s: seconds until window close
        sigma_per_sec: realized vol per second (log return stdev)

    Returns:
        Probability the resolution will be `YES` (price > start_price).
    """
    if start_price <= 0:
        return 0.5
    drift = math.log(max(predicted_cl, 1e-9) / start_price)
    if time_remaining_s <= 0:
        return 1.0 if drift > 0 else 0.0
    sigma_total = sigma_per_sec * math.sqrt(max(time_remaining_s, 0))
    if sigma_total <= 1e-9:
        return 1.0 if drift > 0 else 0.0
    # Standard normal CDF approximation
    z = drift / sigma_total
    return _phi(z)


def _phi(z: float) -> float:
    """Standard normal CDF, no scipy dep."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
