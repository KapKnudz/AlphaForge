"""Single-file Börsdata raw-key → canonical mapping (anti-leakage seam).

This is the ONLY file that may contain Börsdata raw keys. Nothing above it
imports raw keys.
"""

from __future__ import annotations

from datetime import date
from typing import Any

# Börsdata report fields (GET /v1/instruments/reports) — raw → canonical
REPORT_FIELD_MAP: dict[str, str] = {
    "revenues": "revenue",
    "gross_Income": "gross_income",
    "operating_Income": "operating_profit",
    "operatingIncome": "operating_profit",
    "ebit": "ebit",
    "ebitda": "ebitda",
    "profit_To_Equity_Holders": "net_income",
    "profitToEquityHolders": "net_income",
    "netIncome": "net_income",
    "free_Cash_Flow": "free_cash_flow",
    "operating_Cash_Flow": "operating_cash_flow",
    "investing_Cash_Flow": "investing_cash_flow",
    "financing_Cash_Flow": "financing_cash_flow",
    # Live Börsdata also returns cash-flow statement keys with full names
    # (e.g. cash_Flow_From_Operating_Activities) — map them to the same
    # canonical fields so cash conversion is not silently dropped.
    "cash_Flow_From_Operating_Activities": "operating_cash_flow",
    "cash_Flow_From_Investing_Activities": "investing_cash_flow",
    "cash_Flow_From_Financing_Activities": "financing_cash_flow",
    "book_Value": "equity",
    "equity": "equity",
    # Live uses total_Equity (not book_Value) for equity; keep book_Value for
    # backward compatibility with older fixtures.
    "total_Equity": "equity",
    "total_Assets": "total_assets",
    "total_Debt": "total_debt",
    # Live reports expose net_Debt (already net of cash) rather than gross
    # total_Debt — preserve a dedicated net_debt column so the provider value
    # is not mislabelled as gross debt.
    "net_Debt": "net_debt",
    "cash_And_Equivalents": "cash",
    "earnings_Per_Share": "eps",
    "dividend": "dividend_per_share",
    "number_Of_Shares": "shares_outstanding",
    "currency": "currency",
    "currency_Ratio": "currency_ratio",
    "currencyRatio": "currency_ratio",
    "report_Date": "report_date",
    "reportDate": "report_date",
    "ReportDate": "report_date",
    "periodEnd": "period_end",
    "period_End": "period_end",
    "reportEndDate": "period_end",
    "report_End_Date": "period_end",
    "report_end_date": "period_end",
    "currency_ratio": "currency_ratio",
    "year": "report_year",
    "period": "report_period",
    "period_Start": "period_start",
    "broken_Fiscal_Year": "broken_fiscal_year",
}

# KPI ids that are known to be used (hard-coded ids validated against kpi_metadata)
# These are documented per branch; format checked against metadata.
KNOWN_KPI_IDS: dict[int, str] = {
    1: "pe",
    2: "ps",
    3: "pb",
    7: "ev_ebit",
    11: "ev_sales",
    14: "dividend_yield",
    15: "return_on_equity",
    18: "solidity",
    21: "gross_margin",
    22: "operating_margin",
    28: "gross_margin_q",
    29: "operating_margin_q",
    30: "ebit_margin",
    31: "ebit_margin_q",
    32: "net_margin",
}

# Report property metadata canonical names
REPORT_PROPERTY_MAP: dict[str, str] = {
    "revenues": "revenue",
    "gross_Income": "gross_income",
    "operating_Income": "operating_profit",
    "profit_To_Equity_Holders": "net_income",
    "book_Value": "equity",
    "number_Of_Shares": "shares_outstanding",
}


def canonical_report_field(raw_key: str) -> str | None:
    return REPORT_FIELD_MAP.get(raw_key)


def report_alias_values(payload: dict[str, Any], canonical_field: str) -> tuple[Any, ...]:
    return tuple(
        value
        for key, value in payload.items()
        if value is not None
        and (key == canonical_field or REPORT_FIELD_MAP.get(key) == canonical_field)
    )


def report_date_aliases(
    payload: dict[str, Any], canonical_field: str
) -> tuple[frozenset[date], bool]:
    parsed = set()
    malformed = False
    for value in report_alias_values(payload, canonical_field):
        try:
            parsed.add(date.fromisoformat(str(value)[:10]))
        except ValueError:
            malformed = True
    return frozenset(parsed), malformed


def report_integer_aliases(
    payload: dict[str, Any], canonical_field: str
) -> tuple[frozenset[int], bool]:
    parsed = set()
    malformed = False
    for value in report_alias_values(payload, canonical_field):
        if isinstance(value, bool):
            malformed = True
        elif isinstance(value, int):
            parsed.add(value)
        elif isinstance(value, str) and value.strip().isdigit():
            parsed.add(int(value.strip()))
        else:
            malformed = True
    return frozenset(parsed), malformed


def is_known_kpi(kpi_id: int) -> bool:
    return kpi_id in KNOWN_KPI_IDS


class KpiIds:
    """Borsdata KPI identifiers — mirrored from reference, pure."""

    DIVIDEND_YIELD = 1
    PE = 2
    PS = 3
    PB = 4
    EV_EBIT = 10
    EV_EBITDA = 11
    PEG = 19
    ENTERPRISE_VALUE = 49
    MARKET_CAP = 50
    PFCF = 76
    ROIC = 37
    NET_DEBT_EBITDA = 42
    GENERAL_FUNDAMENTAL_KPIS = (ROIC, NET_DEBT_EBITDA)
    PROPERTY_NAV = 277
    PROPERTY_INTEREST_COVERAGE = 278
    PROPERTY_LTV = 279
    PROPERTY_OCCUPANCY = 280
    PROPERTY_NOI = 281
    PROPERTY_NOI_PER_SHARE = 282
    PROPERTY_NOI_MARGIN = 283
    PROPERTY_INCOME = 284
    PROPERTY_INCOME_PER_SHARE = 285
    PROPERTY_INCOME_MARGIN = 286
    PROPERTY_NAV_DISCOUNT = 287
    PROPERTY_PRICE_TO_NOI = 288
    PROPERTY_PRICE_TO_INCOME = 289
    BANK_COST_INCOME = 290
    BANK_CREDIT_LOSSES = 291
    BANK_CET1 = 292
    BANK_TIER1 = 293
    BANK_CAPITAL_ADEQUACY = 294
    BANK_DEPOSITS_LENDING = 295
    BANK_LCR = 296
    PROPERTY_KPIS = tuple(range(PROPERTY_NAV, PROPERTY_PRICE_TO_INCOME + 1))
    BANK_KPIS = tuple(range(BANK_COST_INCOME, BANK_LCR + 1))
