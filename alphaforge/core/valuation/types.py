"""Valuation dataclasses consolidated — pure."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CurrentValuation:
    market_cap: float | None
    enterprise_value: float | None
    pe: float | None
    ev_ebit: float | None
    ev_ebitda: float | None
    pb: float | None
    ps: float | None
    pfcf: float | None
    peg: float | None
    dividend_yield: float | None


@dataclass
class HistoricalValuation:
    pe_history: list[float]
    ev_ebit_history: list[float]
    pb_history: list[float]
    avg_pe: float | None
    avg_ev_ebit: float | None
    avg_pb: float | None
    median_pe: float | None
    median_ev_ebit: float | None
    median_pb: float | None


@dataclass
class ValuationResult:
    pe: float | None
    ev_ebit: float | None
    ev_ebitda: float | None
    pb: float | None
    ps: float | None
    pfcf: float | None
    peg: float | None
    earnings_yield: float | None
    free_cash_flow_yield: float | None
    pe_vs_5y_avg: float | None
    ev_ebit_vs_5y_avg: float | None
    pb_vs_5y_avg: float | None
    pe_percentile: float | None
    ev_ebit_percentile: float | None
    ev_ebit_guardrail_low: float | None = None
    ev_ebit_guardrail_high: float | None = None
    ev_ebit_base_ceiling: float | None = None
    ev_ebit_bull_ceiling: float | None = None
    ev_ebit_history_count: int = 0
    raw_market_cap: float | None = None
    raw_enterprise_value: float | None = None
    raw_earnings_yield: float | None = None
    raw_fcf_yield: float | None = None
    raw_pe: float | None = None
    raw_pfcf: float | None = None
    raw_ev_ebit: float | None = None
    raw_ev_ebitda: float | None = None
    dividend_yield: float | None = None
