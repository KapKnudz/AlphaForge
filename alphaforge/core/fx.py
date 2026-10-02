"""Standalone sums-only arithmetic for a Börsdata currency ratio.

Verified semantics: ``converted = original * ratio``, where ``currency_ratio``
maps original report currency to stock-price currency. It is not generically an
FX rate to SEK. Callers must independently verify both denominations and the
acquisition mode before using it.

Reports fetched with ``original=0`` already contain converted monetary values;
the ranking loader therefore retains the ratio as provenance and does not apply
this helper to those values. This utility must never be used for ratios such as
margins, yields, multiples, or growth.
"""

from __future__ import annotations


def convert_sum(value_in_report_ccy: float, rate_sek_per_ccy: float) -> float:
    """Convert a level sum using verified ratio: converted = original * ratio.

    Args:
        value_in_report_ccy: monetary sum in original report currency.
        rate_sek_per_ccy: Legacy parameter name for the positive
            original-report-currency to stock-price-currency ratio. It denotes
            SEK only when the independently verified target is SEK.

    Returns:
        Value in stock-price currency.

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
