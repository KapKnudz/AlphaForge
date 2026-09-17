"""FX utility — sums-only conversion via Börsdata currency_ratio.

Verified semantics: converted = original * ratio
  where ratio = currency_ratio from Börsdata reports (ReportV1.currency_Ratio)
  converts original report-currency → stockPriceCurrency.

Fetch with original=0 so monetary fields arrive already in stockPriceCurrency,
but persist currency (original) + currency_ratio for provenance and for
converting sums that need SEK denomination.

This utility MUST only be used for level sums (net debt, market cap, EV).
Never for ratios (margins, yields, multiples, growth).
"""

from __future__ import annotations


def convert_sum(value_in_report_ccy: float, rate_sek_per_ccy: float) -> float:
    """Convert a level sum using verified ratio: converted = original * ratio.

    Args:
        value_in_report_ccy: monetary sum in original report currency.
        rate_sek_per_ccy: currency_ratio (SEK per foreign unit) where
            rate > 0. For SEK-denominated reports ratio is 1.0.

    Returns:
        Value in SEK (stockPriceCurrency when report is foreign).

    Raises:
        ValueError: if rate is not finite or <= 0.
    """
    import math

    if not math.isfinite(value_in_report_ccy):
        raise ValueError(f"value must be finite, got {value_in_report_ccy}")
    if not math.isfinite(rate_sek_per_ccy) or rate_sek_per_ccy <= 0:
        raise ValueError(f"rate must be finite >0, got {rate_sek_per_ccy}")
    return value_in_report_ccy * rate_sek_per_ccy


def is_ratio_field(field_name: str) -> bool:
    """Return True if field is a ratio that must NOT be FX-converted."""
    ratios = {
        "gross_margin",
        "operating_margin",
        "ebit_margin",
        "net_margin",
        "pe",
        "pe_percentile",
        "ev_ebit",
        "ev_sales",
        "ps",
        "pb",
        "dividend_yield",
        "earnings_yield",
        "fcf_yield",
        "roic",
        "revenue_growth",
        "earnings_growth",
        "gross_to_ebit_spread",
    }
    return field_name.lower() in ratios
