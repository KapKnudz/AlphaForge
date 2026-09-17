"""Single-file Börsdata raw-key → canonical mapping (anti-leakage seam).

This is the ONLY file that may contain Börsdata raw keys. Nothing above it
imports raw keys.
"""

from __future__ import annotations

# Börsdata report fields (GET /v1/instruments/reports) — raw → canonical
REPORT_FIELD_MAP: dict[str, str] = {
    "revenues": "revenue",
    "gross_Income": "gross_income",
    "operating_Income": "operating_profit",
    "operatingIncome": "operating_profit",
    "ebit": "ebit",
    "ebitda": "ebitda",
    "profit_To_Equity_Holders": "net_income",
    "netIncome": "net_income",
    "free_Cash_Flow": "free_cash_flow",
    "operating_Cash_Flow": "operating_cash_flow",
    "investing_Cash_Flow": "investing_cash_flow",
    "financing_Cash_Flow": "financing_cash_flow",
    "book_Value": "equity",
    "equity": "equity",
    "total_Assets": "total_assets",
    "total_Debt": "total_debt",
    "cash_And_Equivalents": "cash",
    "earnings_Per_Share": "eps",
    "dividend": "dividend_per_share",
    "number_Of_Shares": "shares_outstanding",
    "currency": "currency",
    "currency_Ratio": "currency_ratio",
    "report_Date": "report_date",
    "year": "report_year",
    "period": "report_period",
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


def is_known_kpi(kpi_id: int) -> bool:
    return kpi_id in KNOWN_KPI_IDS
