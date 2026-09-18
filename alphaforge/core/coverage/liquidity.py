"""Liquidity (ADTV) — pure, frozen, versioned, no DB/model imports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

VERSION = "liquidity-v1-adtv-proxy"


@dataclass(frozen=True)
class PriceBar:
    date: date
    close: float
    volume: int | None = None
    currency: str | None = None


@dataclass(frozen=True)
class LiquidityEvidence:
    currency: str | None
    adtv_20: float | None
    adtv_60: float | None
    adtv_120: float | None
    observed_days_20: int
    observed_days_60: int
    observed_days_120: int
    zero_volume_days_120: int
    calculation_method: str = "close_times_volume_proxy"
    status: str = "available"


def _adtv(prices: list[PriceBar], window: int) -> tuple[float | None, int]:
    window_prices = prices[:window]
    if len(window_prices) < window:
        return None, len(window_prices)
    total = sum((p.close * (p.volume or 0)) for p in window_prices)
    return round(total / len(window_prices), 2), len(window_prices)


def build(prices: list[PriceBar], as_of: date | None = None) -> LiquidityEvidence:
    """Pure ADTV from PriceBar[120] — close*volume proxy."""
    currency = next((p.currency for p in prices if p.currency), None)
    # Ensure sorted descending by date (most recent first); caller should already sort, but ensure.
    # If as_of provided, filter to <= as_of (PIT)
    if as_of is not None:
        filtered = [p for p in prices if p.date <= as_of]
    else:
        filtered = prices
    # Sort descending
    filtered = sorted(filtered, key=lambda p: p.date, reverse=True)
    adtv_20, obs20 = _adtv(filtered, 20)
    adtv_60, obs60 = _adtv(filtered, 60)
    adtv_120, obs120 = _adtv(filtered, 120)
    zero120 = sum(1 for p in filtered[:120] if (p.volume or 0) == 0)
    # status
    vals = (adtv_20, adtv_60, adtv_120)
    if all(v is not None for v in vals):
        status = "available"
    elif any(v is not None for v in vals):
        status = "partial"
    else:
        status = "unavailable"
    return LiquidityEvidence(
        currency=currency,
        adtv_20=adtv_20,
        adtv_60=adtv_60,
        adtv_120=adtv_120,
        observed_days_20=obs20,
        observed_days_60=obs60,
        observed_days_120=obs120,
        zero_volume_days_120=zero120,
        status=status,
    )


def adtv(prices: list[PriceBar], window: int) -> float | None:
    val, _ = _adtv(sorted(prices, key=lambda p: p.date, reverse=True), window)
    return val


__all__ = ["PriceBar", "LiquidityEvidence", "build", "adtv", "VERSION"]
