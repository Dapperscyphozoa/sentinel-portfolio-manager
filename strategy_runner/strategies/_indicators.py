"""TA primitives used by multiple strategies. Pure-Python, no numpy/pandas dependency
required at call-site (tests run in minimal envs).
"""
from __future__ import annotations

from typing import Sequence


def sma(xs: Sequence[float], n: int) -> list[float | None]:
    out: list[float | None] = []
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out.append(s / n)
        else:
            out.append(None)
    return out


def stdev(xs: Sequence[float], n: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(xs)):
        if i < n - 1:
            out.append(None)
            continue
        window = xs[i - n + 1: i + 1]
        m = sum(window) / n
        var = sum((w - m) ** 2 for w in window) / n
        out.append(var ** 0.5)
    return out


def ema(xs: Sequence[float], n: int) -> list[float | None]:
    out: list[float | None] = []
    if not xs:
        return out
    k = 2 / (n + 1)
    prev: float | None = None
    for i, x in enumerate(xs):
        if i < n - 1:
            out.append(None)
            continue
        if prev is None:
            prev = sum(xs[: n]) / n
            out.append(prev)
            continue
        prev = x * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(closes: Sequence[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, n + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    rs = avg_gain / avg_loss if avg_loss > 0 else float("inf")
    out[n] = 100 - 100 / (1 + rs) if avg_loss > 0 else 100.0
    for i in range(n + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        g = max(diff, 0)
        l = max(-diff, 0)
        avg_gain = (avg_gain * (n - 1) + g) / n
        avg_loss = (avg_loss * (n - 1) + l) / n
        rs = avg_gain / avg_loss if avg_loss > 0 else float("inf")
        out[i] = 100 - 100 / (1 + rs) if avg_loss > 0 else 100.0
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    trs: list[float] = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    # Wilder smoothing
    if len(trs) < n:
        return out
    a = sum(trs[:n]) / n
    out[n] = a
    for i in range(n, len(trs)):
        a = (a * (n - 1) + trs[i]) / n
        out[i + 1] = a
    return out


def bollinger(closes: Sequence[float], n: int = 20, k: float = 2.0):
    m = sma(closes, n)
    s = stdev(closes, n)
    upper = [(mi + k * si) if (mi is not None and si is not None) else None for mi, si in zip(m, s)]
    lower = [(mi - k * si) if (mi is not None and si is not None) else None for mi, si in zip(m, s)]
    return upper, m, lower


def keltner(highs, lows, closes, n: int = 14, atr_mult: float = 1.5):
    midline = ema(closes, n)
    atrs = atr(highs, lows, closes, n)
    upper = [(m + atr_mult * a) if (m is not None and a is not None) else None for m, a in zip(midline, atrs)]
    lower = [(m - atr_mult * a) if (m is not None and a is not None) else None for m, a in zip(midline, atrs)]
    return upper, midline, lower


def pivot_lows(lows: Sequence[float], lb: int = 5, rb: int = 5) -> list[int]:
    out: list[int] = []
    for i in range(lb, len(lows) - rb):
        v = lows[i]
        if all(lows[i] <= lows[i - j] for j in range(1, lb + 1)) and \
           all(lows[i] <= lows[i + j] for j in range(1, rb + 1)):
            out.append(i)
    return out


def pivot_highs(highs: Sequence[float], lb: int = 5, rb: int = 5) -> list[int]:
    out: list[int] = []
    for i in range(lb, len(highs) - rb):
        if all(highs[i] >= highs[i - j] for j in range(1, lb + 1)) and \
           all(highs[i] >= highs[i + j] for j in range(1, rb + 1)):
            out.append(i)
    return out


def donchian(highs: Sequence[float], lows: Sequence[float], n: int):
    """Donchian channel: upper = N-period highest high, lower = N-period lowest low.

    The classical Turtle System uses the channel value as of the PREVIOUS bar's
    close (so a breakout is "close > prev N-bar high"), to avoid using the
    current bar's own data to define the channel. Callers should use
    `upper[i-1]` and `lower[i-1]` when evaluating bar i for a breakout.
    """
    upper: list[float | None] = [None] * len(highs)
    lower: list[float | None] = [None] * len(lows)
    for i in range(n - 1, len(highs)):
        upper[i] = max(highs[i - n + 1: i + 1])
        lower[i] = min(lows[i - n + 1: i + 1])
    return upper, lower
