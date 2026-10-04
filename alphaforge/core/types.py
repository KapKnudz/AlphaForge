"""Core types — Report, StockPrice, enums — pure, no DB/model imports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

# --- Börsdata report / price ---


@dataclass
class Report:
    revenue: float | None
    operating_profit: float | None
    ebit: float | None
    ebitda: float | None
    net_income: float | None
    free_cash_flow: float | None
    equity: float | None
    total_assets: float | None
    total_debt: float | None
    shares_outstanding: float | None
    gross_income: float | None = None
    operating_cash_flow: float | None = None
    investing_cash_flow: float | None = None
    financing_cash_flow: float | None = None
    # Dedicated net_debt (live Börsdata returns net_Debt, not gross total_Debt);
    # kept separate so gross debt is not mislabelled — see kpi_taxonomy map.
    net_debt: float | None = None
    cash: float | None = None
    eps: float | None = None
    dividend_per_share: float | None = None
    year: int | None = None
    period: int | None = None
    period_end: date | None = None
    report_date: date | None = None
    broken_fiscal_year: bool | None = None
    # currency is the verified denomination of the numeric values; original
    # report currency and acquisition conversion provenance remain auditable.
    currency: str | None = None
    original_currency: str | None = None
    conversion_mode: str | None = None
    conversion_target_currency: str | None = None
    currency_ratio: float | None = None
    raw_payload: dict | None = None
    company_id: int | None = None
    period_type: str | None = None

    def __post_init__(self) -> None:
        if self.ebit is None:
            self.ebit = self.operating_profit
        elif self.operating_profit is None:
            self.operating_profit = self.ebit
        elif self.operating_profit != self.ebit:
            self.operating_profit = self.ebit


@dataclass(frozen=True)
class InstrumentReportBundle:
    instrument_id: int
    annual: tuple[Report, ...]
    r12: tuple[Report, ...]
    quarterly: tuple[Report, ...]
    error: str | None = None


@dataclass
class StockPrice:
    date: date
    close: float
    currency: str | None = None
    volume: int | None = None
    company_id: int | None = None


# --- Enums ---


class DataQuality(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RankingModel(StrEnum):
    GENERAL = "general"
    BANK = "bank"
    PROPERTY = "property"
