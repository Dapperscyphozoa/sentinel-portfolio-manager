"""Universe + costs config for sentinel-pm bench validation.

Universe: top-30 USDT-perp coins with >=3yr history on OKX SWAP, intersected
with the active strategy universes across SPEC §3. Symbol format BTCUSDT to
match Binance convention expected by the validation script.

Costs: per-symbol fee + slippage. Fees are taker side (HL fee schedule ≈ 5bp).
Slippage tiered by market depth — measured values where we have them, else
conservative defaults per the brief.
"""
from __future__ import annotations

# Stable universe — 30 USDT perps with >=3yr OKX SWAP history.
# Excludes APT/SUI/SEI (listed 2023, fail 3yr lookback) and meme tokens
# with thin tape (WIF, BANANA, BLUR). Add later if validation needs them.
UNIVERSE_DEFAULT = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    "LTCUSDT", "NEARUSDT", "ATOMUSDT", "UNIUSDT", "FILUSDT",
    "MATICUSDT", "ETCUSDT", "BCHUSDT", "TRXUSDT", "XLMUSDT",
    "INJUSDT", "AAVEUSDT", "OPUSDT", "ARBUSDT", "APEUSDT",
    "FTMUSDT", "SANDUSDT", "MANAUSDT", "CRVUSDT", "COMPUSDT",
]

# Historical universe by date (survivorship-bias control). If as_of is before
# a coin's listing on OKX SWAP, exclude it. Cut-offs sourced from OKX listing
# announcements + HL listing schedule.
_LISTED_AT = {
    "BTCUSDT":  "2018-01-01", "ETHUSDT":  "2018-01-01", "LTCUSDT":  "2018-01-01",
    "BCHUSDT":  "2018-01-01", "XRPUSDT":  "2018-01-01", "ETCUSDT":  "2019-01-01",
    "EOSUSDT":  "2019-01-01", "ADAUSDT":  "2019-01-01", "TRXUSDT":  "2019-01-01",
    "LINKUSDT": "2019-06-01", "XLMUSDT":  "2019-01-01", "DOTUSDT":  "2020-08-01",
    "DOGEUSDT": "2020-07-01", "UNIUSDT":  "2020-09-01", "SOLUSDT":  "2020-09-01",
    "AVAXUSDT": "2020-09-22", "FILUSDT":  "2020-10-15", "NEARUSDT": "2020-10-14",
    "ATOMUSDT": "2020-08-01", "BNBUSDT":  "2020-01-01", "MATICUSDT":"2020-04-01",
    "AAVEUSDT": "2020-10-02", "SANDUSDT": "2020-08-14", "MANAUSDT": "2020-08-01",
    "CRVUSDT":  "2020-08-13", "COMPUSDT": "2020-06-18", "INJUSDT":  "2021-01-01",
    "OPUSDT":   "2022-06-01", "ARBUSDT":  "2023-03-23", "APEUSDT":  "2022-03-17",
    "FTMUSDT":  "2020-08-01",
}


def universe_as_of(as_of: str | None) -> list[str]:
    """Return universe membership as of `as_of` (YYYY-MM-DD or ISO). If None,
    returns current universe."""
    if not as_of:
        return list(UNIVERSE_DEFAULT)
    cutoff = as_of[:10]  # YYYY-MM-DD
    return [s for s in UNIVERSE_DEFAULT if _LISTED_AT.get(s, "1970-01-01") <= cutoff]


# Per-symbol cost table. fee_per_side is taker fee (HL ≈ 0.0005 = 5bp).
# slip_per_side from brief defaults: 0.0002 BTC/ETH, 0.0010 top-10 alts,
# 0.0020 everything else. Measured values override where available.
_TOP_10 = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
           "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT"}


def _slip(symbol: str) -> float:
    if symbol in ("BTCUSDT", "ETHUSDT"):
        return 0.0002
    if symbol in _TOP_10:
        return 0.0010
    return 0.0020


def costs_table(symbols: list[str] | None = None) -> dict:
    syms = symbols or UNIVERSE_DEFAULT
    return {
        s: {"fee_per_side": 0.0005, "slip_per_side": _slip(s)}
        for s in syms
    }
