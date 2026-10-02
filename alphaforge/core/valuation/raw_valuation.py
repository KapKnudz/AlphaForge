"""Derive valuation multiples from raw stock price and report data.

The metrics computed here are an independent cross-check against Börsdata's
pre-computed KPI values. They reflect the *latest* stock price rather than
whatever date the KPI engine last recalculated.
"""

from dataclasses import dataclass

from alphaforge.core.statistics import safe_div
from alphaforge.core.types import Report, StockPrice


@dataclass
class RawValuation:
    """Valuation multiples derived from latest close × report fundamentals."""

    market_cap: float | None = None
    enterprise_value: float | None = None

    # Yields (income / market_cap — higher = cheaper)
    earnings_yield: float | None = None
    fcf_yield: float | None = None
    sales_yield: float | None = None
    book_yield: float | None = None

    # Multiples (price / fundamental — lower = cheaper)
    pe: float | None = None
    pfcf: float | None = None
    ps: float | None = None
    pb: float | None = None

    # Enterprise-value multiples
    ev_ebit: float | None = None
    ev_ebitda: float | None = None


def compute_raw_valuation(
    stock_price: StockPrice | None,
    report: Report | None,
) -> RawValuation:
    """Derive valuation multiples from a stock price and a single report.

    If either input is missing, returns an empty ``RawValuation`` (all Nones).
    Callers should fall back to the KPI-based valuation path when this happens.
    """
    if stock_price is None or report is None:
        return RawValuation()

    if not stock_price.currency or not report.currency:
        return RawValuation()
    if stock_price.currency.upper() != report.currency.upper():
        return RawValuation()

    price = stock_price.close
    shares = report.shares_outstanding

    if not price or not shares:
        return RawValuation()

    market_cap = price * shares
    if report.net_debt is not None:
        net_debt = report.net_debt
    elif report.total_debt is not None and report.cash is not None:
        net_debt = report.total_debt - report.cash
    else:
        net_debt = None
    enterprise_value = market_cap + net_debt if net_debt is not None else None

    return RawValuation(
        market_cap=market_cap,
        enterprise_value=enterprise_value,
        earnings_yield=_yield(report.net_income, market_cap),
        fcf_yield=_yield(report.free_cash_flow, market_cap),
        sales_yield=_yield(report.revenue, market_cap),
        book_yield=_yield(report.equity, market_cap),
        pe=_multiple(market_cap, report.net_income),
        pfcf=_multiple(market_cap, report.free_cash_flow),
        ps=_multiple(market_cap, report.revenue),
        pb=_multiple(market_cap, report.equity),
        ev_ebit=_multiple(enterprise_value, report.ebit),
        ev_ebitda=_multiple(enterprise_value, report.ebitda),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _yield(numerator: float | None, denominator: float | None) -> float | None:
    """Yield = numerator / denominator. Returns None when inputs are invalid."""
    return safe_div(numerator, denominator)


def _multiple(numerator: float | None, denominator: float | None) -> float | None:
    """Multiple = numerator / denominator. Returns None when inputs are invalid."""
    return safe_div(numerator, denominator)
