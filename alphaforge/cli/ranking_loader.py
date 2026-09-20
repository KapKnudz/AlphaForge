"""Build deterministic ranking inputs from the SQLite system of record."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

from alphaforge.core.financial.calculator import FinancialCalculator
from alphaforge.core.financial.mapper import FinancialMapper
from alphaforge.core.financial.per_share import adjust_historical_shares
from alphaforge.core.types import Report, StockPrice
from alphaforge.core.valuation.calculator import ValuationCalculator
from alphaforge.core.valuation.raw_valuation import RawValuation, compute_raw_valuation
from alphaforge.core.valuation.types import CurrentValuation, HistoricalValuation
from alphaforge.db.repositories import load_evidence_packet


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _report(row, *, shares_override: float | None = None) -> Report:
    return Report(
        revenue=_number(row["revenue"]),
        operating_profit=_number(row["operating_profit"]),
        ebit=_number(row["ebit"]),
        ebitda=_number(row["ebitda"]),
        net_income=_number(row["net_income"]),
        free_cash_flow=_number(row["free_cash_flow"]),
        equity=_number(row["equity"]),
        total_assets=_number(row["total_assets"]),
        total_debt=_number(row["total_debt"]),
        shares_outstanding=(
            shares_override if shares_override is not None else _number(row["shares_outstanding"])
        ),
        gross_income=_number(row["gross_income"]),
        operating_cash_flow=_number(row["operating_cash_flow"]),
        cash=_number(row["cash"]),
        eps=_number(row["eps"]),
        dividend_per_share=_number(row["dividend_per_share"]),
        year=row["report_year"],
        period=row["report_period"],
        currency=row["currency"],
    )


def _price(row, fallback_currency: str | None) -> StockPrice:
    return StockPrice(
        date=date.fromisoformat(str(row["price_date"])[:10]),
        close=float(row["close"]),
        volume=int(row["volume"]) if row["volume"] is not None else None,
        currency=row["currency"] or fallback_currency,
    )


def load_results_for_company(conn, company_id: int, as_of: str) -> dict[str, Any]:
    """Load all ranking inputs visible at *as_of* (never current live rows)."""
    cutoff = date.fromisoformat(as_of[:10])
    evidence_packet = load_evidence_packet(conn, company_id, as_of[:10])
    company = conn.execute(
        "SELECT stock_price_currency, report_currency FROM companies WHERE id=?", (company_id,)
    ).fetchone()
    stock_currency = company[0] if company else None

    period_rows = conn.execute(
        """
        SELECT * FROM financial_periods
        WHERE company_id=? AND is_placeholder=0
          AND substr(period_end, 1, 10) <= ?
          AND report_date IS NOT NULL AND substr(report_date, 1, 10) <= ?
        ORDER BY period_end ASC, report_date ASC
        """,
        (company_id, cutoff.isoformat(), cutoff.isoformat()),
    ).fetchall()
    price_rows = conn.execute(
        """
        SELECT * FROM prices
        WHERE company_id=? AND substr(price_date, 1, 10) <= ?
        ORDER BY price_date ASC
        """,
        (company_id, cutoff.isoformat()),
    ).fetchall()
    if not period_rows or not price_rows:
        return {
            "financial": None,
            "valuation": None,
            "fundamental_kpis": {},
            "research_evidence": {
                "documents": [],
                "evidence_packet": evidence_packet,
                "evidence_lane": bool(evidence_packet),
            },
        }

    # Börsdata prices are split-adjusted but report share counts are not. Keep
    # the raw row in the database and adjust only historical calculation input
    # into the latest report's share basis.
    split_rows = conn.execute(
        "SELECT split_type, ratio, split_date FROM stock_splits WHERE company_id=? ORDER BY split_date",
        (company_id,),
    ).fetchall()
    split_events = [(row[0], row[1], row[2]) for row in split_rows]
    comparison_date = str(period_rows[-1]["period_end"])[:10]
    reports = []
    for index, row in enumerate(period_rows):
        raw_shares = _number(row["shares_outstanding"])
        adjusted = raw_shares
        if index < len(period_rows) - 1 and raw_shares is not None and split_events:
            adjusted = adjust_historical_shares(
                raw_shares,
                str(row["period_end"])[:10],
                comparison_date,
                split_events,
            )
        reports.append(_report(row, shares_override=adjusted))
    current_report = reports[-1]
    historical_reports = reports[:-1]
    financial_mapper = FinancialMapper()
    financial = FinancialCalculator().calculate(
        financial_mapper.to_current(current_report),
        financial_mapper.to_historical(historical_reports),
    )
    latest_price = _price(price_rows[-1], stock_currency)
    current_raw = compute_raw_valuation(latest_price, current_report)

    historical_raw: list[RawValuation] = []
    for row, report in zip(period_rows[:-1], historical_reports, strict=False):
        candidates = [
            p for p in price_rows if str(p["price_date"])[:10] <= str(row["period_end"])[:10]
        ]
        if candidates:
            historical_raw.append(
                compute_raw_valuation(_price(candidates[-1], stock_currency), report)
            )
    pe_history = [item.pe for item in historical_raw if item.pe is not None and item.pe > 0]
    ev_ebit_history = [
        item.ev_ebit for item in historical_raw if item.ev_ebit is not None and item.ev_ebit > 0
    ]
    pb_history = [item.pb for item in historical_raw if item.pb is not None and item.pb > 0]
    historical = HistoricalValuation(
        pe_history=pe_history,
        ev_ebit_history=ev_ebit_history,
        pb_history=pb_history,
        avg_pe=sum(pe_history) / len(pe_history) if pe_history else None,
        avg_ev_ebit=sum(ev_ebit_history) / len(ev_ebit_history) if ev_ebit_history else None,
        avg_pb=sum(pb_history) / len(pb_history) if pb_history else None,
        median_pe=sorted(pe_history)[len(pe_history) // 2] if pe_history else None,
        median_ev_ebit=sorted(ev_ebit_history)[len(ev_ebit_history) // 2]
        if ev_ebit_history
        else None,
        median_pb=sorted(pb_history)[len(pb_history) // 2] if pb_history else None,
    )
    current = CurrentValuation(
        market_cap=current_raw.market_cap,
        enterprise_value=current_raw.enterprise_value,
        pe=current_raw.pe,
        ev_ebit=current_raw.ev_ebit,
        ev_ebitda=current_raw.ev_ebitda,
        pb=current_raw.pb,
        ps=current_raw.ps,
        pfcf=current_raw.pfcf,
        peg=None,
        dividend_yield=None,
    )
    dividends = conn.execute(
        "SELECT amount FROM dividends WHERE company_id=? AND substr(ex_date, 1, 10) <= ? AND substr(ex_date, 1, 10) > ?",
        (
            company_id,
            cutoff.isoformat(),
            date(cutoff.year - 1, cutoff.month, cutoff.day).isoformat(),
        ),
    ).fetchall()
    if dividends and latest_price.close > 0:
        current.dividend_yield = sum(float(row[0]) for row in dividends) / latest_price.close * 100
    valuation = ValuationCalculator().calculate(current, historical, current_raw)

    kpis: dict[int, float] = {}
    for row in conn.execute(
        """
        SELECT kpi_id, value FROM kpi_observations
        WHERE company_id=? AND value IS NOT NULL
          AND (
              (observation_date IS NOT NULL AND substr(observation_date, 1, 10) <= ?)
              OR (observation_date IS NULL AND year < ?)
          )
        ORDER BY COALESCE(observation_date, printf('%04d-12-31', year)) ASC
        """,
        (company_id, cutoff.isoformat(), cutoff.year),
    ).fetchall():
        kpis[int(row[0])] = float(row[1])

    docs = [
        dict(row)
        for row in conn.execute(
            "SELECT id, source_url, title, published_at FROM research_documents WHERE company_id=? AND published_at IS NOT NULL AND substr(published_at, 1, 10) <= ?",
            (company_id, cutoff.isoformat()),
        ).fetchall()
    ]
    reverse_dcf = {
        "status": "available" if current_raw.market_cap is not None else "unavailable",
        "current_price": latest_price.close,
        "current_revenue": current_report.revenue,
        "current_shares": current_report.shares_outstanding,
        "current_net_debt": (
            current_report.total_debt - current_report.cash
            if current_report.total_debt is not None and current_report.cash is not None
            else None
        ),
        "price_currency": latest_price.currency,
        "financial_currency": current_report.currency or stock_currency,
    }
    candidate = SimpleNamespace(
        company_id=company_id,
        ticker="",
        ranking_model="general",
        research_evidence={
            "documents": docs,
            "evidence_packet": evidence_packet,
            "evidence_lane": bool(evidence_packet),
        },
        full_results={"valuation": valuation, "reverse_dcf": reverse_dcf},
    )
    return {
        "financial": financial,
        "valuation": valuation,
        "fundamental_kpis": kpis,
        "sector_kpis": {"current": kpis, "histories": {}},
        "candidate": candidate,
    }
