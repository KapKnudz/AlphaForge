"""Build deterministic ranking inputs from the SQLite system of record."""

from __future__ import annotations

import json
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
from alphaforge.evidence.manifest import load_evidence_selection_manifest
from alphaforge.evidence.report_rules import current_report_rules_fingerprint, report_rules_metadata


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _report(row, *, shares_override: float | None = None) -> Report:
    # Prefer dedicated net_debt column when present; keep total_debt for compat.
    try:
        net_debt_value = _number(row["net_debt"])
    except (KeyError, IndexError, TypeError):
        net_debt_value = None
    try:
        investing_value = _number(row["investing_cash_flow"])
    except (KeyError, IndexError, TypeError):
        investing_value = None
    try:
        raw_value = row["raw_payload"]
    except (KeyError, IndexError, TypeError):
        raw_value = None
    raw_payload: dict | None = None
    if isinstance(raw_value, dict):
        raw_payload = raw_value
    elif isinstance(raw_value, str) and raw_value:
        try:
            parsed = json.loads(raw_value)
        except (ValueError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            raw_payload = parsed
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
        net_debt=net_debt_value,
        shares_outstanding=(
            shares_override if shares_override is not None else _number(row["shares_outstanding"])
        ),
        gross_income=_number(row["gross_income"]),
        operating_cash_flow=_number(row["operating_cash_flow"]),
        investing_cash_flow=investing_value,
        raw_payload=raw_payload,
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
    evidence_packet = load_evidence_packet(
        conn,
        company_id,
        as_of[:10],
        current_rules_fingerprint=current_report_rules_fingerprint(),
    )
    selection_manifest = load_evidence_selection_manifest(
        conn,
        company_id=company_id,
        as_of=as_of[:10],
        report_rules=report_rules_metadata(),
    )
    docs = [
        {
            "id": row.get("document_id"),
            "source_url": row.get("source_url"),
            "title": row.get("title"),
            "published_at": row.get("published_at"),
        }
        for row in selection_manifest.audit_history
        if row.get("published_at") and str(row["published_at"])[:10] <= as_of[:10]
    ]
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
        _missing = []
        if not period_rows:
            _missing.append("financial_period")
        if not price_rows:
            _missing.append("price")
        _unavailable = {
            "status": "unavailable",
            "dcf": {
                "available": False,
                "policy_version": None,
                "missing_information": _missing,
                "warnings": [],
            },
        }
        return {
            "financial": None,
            "valuation": None,
            "fundamental_kpis": {},
            "research_evidence": {
                "documents": docs,
                "evidence_packet": evidence_packet,
                "evidence_manifest": selection_manifest.to_dict(),
                "evidence_lane": bool(evidence_packet),
            },
            "dcf": {
                "policy": None,
                "value": None,
                "implied": {},
                "reverse_dcf": _unavailable,
            },
            "reverse_dcf": _unavailable,
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
    dcf_r12_reports = [
        report
        for prow, report in zip(period_rows, reports, strict=False)
        if prow["period_type"] == "r12"
    ]
    dcf_annual_reports = [
        report
        for prow, report in zip(period_rows, reports, strict=False)
        if prow["period_type"] == "year"
    ]
    if dcf_r12_reports:
        dcf_current_report = dcf_r12_reports[-1]
    elif dcf_annual_reports:
        dcf_current_report = dcf_annual_reports[-1]
    else:
        dcf_current_report = None
    financial_mapper = FinancialMapper()
    financial = FinancialCalculator().calculate(
        financial_mapper.to_current(current_report),
        financial_mapper.to_historical(historical_reports),
    )
    latest_price = _price(price_rows[-1], stock_currency)
    current_raw = compute_raw_valuation(latest_price, current_report)
    dcf_raw = compute_raw_valuation(latest_price, dcf_current_report)

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

    kpi_r12: dict[int, float] = {}
    kpi_annual: dict[int, float] = {}
    for row in conn.execute(
        """
        SELECT kpi_id, value, period_type FROM kpi_observations
        WHERE company_id=? AND value IS NOT NULL
          AND year <= ?
          AND (observation_date IS NULL OR substr(observation_date, 1, 10) <= ?)
        ORDER BY COALESCE(observation_date, printf('%04d-12-31', year)) ASC
        """,
        (company_id, cutoff.year, cutoff.isoformat()),
    ).fetchall():
        if row[2] == "r12":
            kpi_r12[int(row[0])] = float(row[1])
        else:
            kpi_annual[int(row[0])] = float(row[1])
    kpis: dict[int, float] = {**kpi_annual, **kpi_r12}

    if dcf_current_report is not None and dcf_current_report.net_debt is not None:
        current_net_debt = dcf_current_report.net_debt
        net_debt_source = "net_debt"
    elif (
        dcf_current_report is not None
        and dcf_current_report.total_debt is not None
        and dcf_current_report.cash is not None
    ):
        current_net_debt = dcf_current_report.total_debt - dcf_current_report.cash
        net_debt_source = "total_debt_minus_cash"
    else:
        current_net_debt = None
        net_debt_source = None
    reverse_dcf = {
        "status": "available" if dcf_raw.market_cap is not None else "unavailable",
        "current_price": latest_price.close,
        "current_revenue": dcf_current_report.revenue if dcf_current_report is not None else None,
        "current_shares": dcf_current_report.shares_outstanding
        if dcf_current_report is not None
        else None,
        "current_net_debt": current_net_debt,
        "net_debt_source": net_debt_source,
        "price_currency": latest_price.currency,
        "financial_currency": (
            dcf_current_report.currency if dcf_current_report is not None else None
        )
        or stock_currency,
        "market_cap": dcf_raw.market_cap,
        "enterprise_value": dcf_raw.enterprise_value,
    }
    # ------------------------------------------------------------------
    # Auditable DCF: wire existing pure policy + engine so the ranking
    # path produces projected FCFF, discount rate, terminal assumptions,
    # enterprise/equity value, value per share, and reverse-DCF implied
    # assumptions — clearly distinguished from the heuristic valuation_score.
    # ------------------------------------------------------------------
    dcf_policy_decision = None
    dcf_value = None
    reverse_dcf_results: dict[str, Any] = {}
    # Build annual report history for DCF policy (needs year property)
    try:
        annual_period_rows = [r for r in period_rows if r["period_type"] == "year"]
        # Map annual rows to Reports in the same PIT-filtered, share-adjusted way
        # as the full ranking input, but only for annuals.
        annual_reports: list[Report] = []
        for row in annual_period_rows:
            raw_shares = _number(row["shares_outstanding"])
            adjusted = raw_shares
            if raw_shares is not None and split_events:
                adjusted = adjust_historical_shares(
                    raw_shares,
                    str(row["period_end"])[:10],
                    comparison_date,
                    split_events,
                )
            annual_reports.append(_report(row, shares_override=adjusted))
        if annual_reports:
            latest_annual = annual_reports[-1]
            historical_annuals = annual_reports[:-1]
        else:
            latest_annual = None
            historical_annuals = []
        # Branch for sector guard
        branch_row = conn.execute(
            "SELECT branch_id FROM companies WHERE id=?", (company_id,)
        ).fetchone()
        branch_id = int(branch_row[0]) if branch_row and branch_row[0] is not None else None
        roic_for_dcf = kpis.get(37)  # KPI 37 = ROIC (now reliably persisted)
        # Börsdata ROIC is percent (e.g. 22.9 means 22.9%); DcfAssumptionPolicy
        # expects percent and divides by 100 internally, so pass raw percent.
        from alphaforge.core.valuation.dcf_policy import DcfAssumptionPolicy
        from alphaforge.core.valuation.reverse_dcf import ReverseDcfEngine, ReverseDcfInputs

        policy = DcfAssumptionPolicy()
        # market_cap from raw valuation is in report-currency millions (SEK MSEK)
        # because Börsdata reports and shares are in millions; required-return
        # buckets are in absolute SEK, so scale to SEK for the hurdle.
        market_cap_for_hurdle = None
        if dcf_raw.market_cap is not None:
            # Heuristic: shares are in millions (63.45 = 63M), so market cap in MSEK.
            # Convert to SEK for bucket selection.
            market_cap_for_hurdle = float(dcf_raw.market_cap) * 1_000_000
        dcf_policy_decision = policy.build(
            dcf_current_report,
            latest_annual,
            historical_annuals,
            as_of=cutoff,
            currency=(dcf_current_report.currency if dcf_current_report is not None else None)
            or stock_currency
            or "SEK",
            market_cap=market_cap_for_hurdle,
            roic=roic_for_dcf,
        )
        if dcf_policy_decision.available and dcf_policy_decision.assumptions is not None:
            if current_net_debt is None:
                reverse_dcf["dcf_error"] = (
                    "net debt unavailable; DCF enterprise-to-equity bridge not valued"
                )
                reverse_dcf["dcf"] = {
                    "available": False,
                    "policy_version": dcf_policy_decision.policy_version,
                    "missing_information": ["net_debt"],
                    "warnings": list(dcf_policy_decision.warnings)
                    if dcf_policy_decision.warnings
                    else [],
                }
                reverse_dcf["status"] = "unavailable"
            elif (
                dcf_current_report is not None
                and dcf_current_report.revenue
                and dcf_current_report.shares_outstanding
                and dcf_current_report.revenue > 0
                and dcf_current_report.shares_outstanding > 0
            ):
                try:
                    dcf_inputs = ReverseDcfInputs(
                        current_price=latest_price.close,
                        shares_outstanding=dcf_current_report.shares_outstanding,
                        current_revenue=dcf_current_report.revenue,
                        net_debt=float(current_net_debt),
                        assumptions=dcf_policy_decision.assumptions,
                        branch_id=branch_id,
                    )
                    engine = ReverseDcfEngine()
                    dcf_value = engine.value(dcf_inputs)
                    reverse_dcf["dcf"] = {
                        "available": True,
                        "policy_version": dcf_policy_decision.policy_version,
                        "assumptions": {
                            "projection_years": dcf_policy_decision.assumptions.projection_years,
                            "revenue_growth": dcf_policy_decision.assumptions.revenue_growth,
                            "ebit_margin": dcf_policy_decision.assumptions.ebit_margin,
                            "tax_rate": dcf_policy_decision.assumptions.tax_rate,
                            "discount_rate": dcf_policy_decision.assumptions.discount_rate,
                            "terminal_growth": dcf_policy_decision.assumptions.terminal_growth,
                            "net_reinvestment_rate": dcf_policy_decision.assumptions.net_reinvestment_rate,
                            "reinvestment_return": dcf_policy_decision.assumptions.reinvestment_return,
                            "ebit_margin_start": dcf_policy_decision.assumptions.ebit_margin_start,
                        },
                        "assumption_sources": dcf_policy_decision.assumption_sources,
                        "required_return": {
                            "size_bucket": dcf_policy_decision.required_return.size_bucket
                            if dcf_policy_decision.required_return
                            else None,
                            "required_return": dcf_policy_decision.required_return.required_return
                            if dcf_policy_decision.required_return
                            else None,
                        }
                        if dcf_policy_decision.required_return
                        else None,
                        "enterprise_value": dcf_value.enterprise_value,
                        "equity_value": dcf_value.equity_value,
                        "value_per_share": dcf_value.value_per_share,
                        "terminal_value": dcf_value.terminal_value,
                        "discounted_terminal_value": dcf_value.discounted_terminal_value,
                        "projected_cash_flows": [
                            {
                                "year": p.year,
                                "revenue": p.revenue,
                                "revenue_growth": p.revenue_growth,
                                "ebit_margin": p.ebit_margin,
                                "ebit": p.ebit,
                                "nopat": p.nopat,
                                "fcff": p.fcff,
                                "discounted_fcff": p.discounted_fcff,
                            }
                            for p in dcf_value.projected_cash_flows
                        ],
                        "normalization": (
                            {
                                "confidence": dcf_policy_decision.normalization.confidence
                                if dcf_policy_decision.normalization
                                else None,
                                "selected_window_years": dcf_policy_decision.normalization.selected_window_years
                                if dcf_policy_decision.normalization
                                else None,
                                "reasons": list(dcf_policy_decision.normalization.reasons)
                                if dcf_policy_decision.normalization
                                else None,
                            }
                            if dcf_policy_decision.normalization
                            else None
                        ),
                        "warnings": list(dcf_policy_decision.warnings)
                        if dcf_policy_decision.warnings
                        else [],
                        "missing_information": list(dcf_policy_decision.missing_information),
                    }
                    # Reverse DCF: solve implied assumption that equates model to market price
                    for _assump in ("revenue_growth", "ebit_margin", "terminal_growth"):
                        _bounds = dcf_policy_decision.solve_bounds.get(_assump)
                        if _bounds is None:
                            continue
                        try:
                            _res = engine.solve(dcf_inputs, _assump, _bounds[0], _bounds[1])
                            reverse_dcf_results[_assump] = {
                                "implied_assumption": _res.implied_assumption,
                                "lower_bound": _res.lower_bound,
                                "upper_bound": _res.upper_bound,
                                "target_price": _res.target_price,
                                "modeled_price": _res.modeled_price,
                                "price_difference": _res.price_difference,
                                "iterations": _res.iterations,
                                "value_per_share": _res.valuation.value_per_share,
                                "enterprise_value": _res.valuation.enterprise_value,
                                "equity_value": _res.valuation.equity_value,
                            }
                        except Exception as exc:
                            reverse_dcf_results[_assump] = {"error": str(exc)}
                    reverse_dcf["implied"] = reverse_dcf_results
                    reverse_dcf["status"] = "available"
                except Exception as exc:
                    reverse_dcf["dcf_error"] = str(exc)
                    reverse_dcf["dcf"] = {
                        "available": False,
                        "policy_version": dcf_policy_decision.policy_version,
                        "missing_information": ["dcf_engine_failed"],
                        "warnings": list(dcf_policy_decision.warnings)
                        if dcf_policy_decision.warnings
                        else [],
                    }
                    reverse_dcf["status"] = "unavailable"
            else:
                reverse_dcf["dcf_error"] = "current revenue or shares unavailable/zero"
                reverse_dcf["dcf"] = {
                    "available": False,
                    "policy_version": dcf_policy_decision.policy_version,
                    "missing_information": ["current_revenue_or_shares"],
                    "warnings": list(dcf_policy_decision.warnings)
                    if dcf_policy_decision.warnings
                    else [],
                }
                reverse_dcf["status"] = "unavailable"
        else:
            # Policy unavailable — surface why so callers can distinguish from heuristic score
            if dcf_policy_decision is not None:
                reverse_dcf["dcf"] = {
                    "available": False,
                    "policy_version": dcf_policy_decision.policy_version,
                    "missing_information": list(dcf_policy_decision.missing_information),
                    "warnings": list(dcf_policy_decision.warnings)
                    if dcf_policy_decision.warnings
                    else [],
                }
                reverse_dcf["status"] = "unavailable"
    except Exception as exc:
        # Never break ranking on DCF failure — keep heuristic score available
        try:
            reverse_dcf["dcf_error"] = f"dcf wiring failed: {exc}"
        except Exception:
            pass
        if "dcf" not in reverse_dcf:
            decision = dcf_policy_decision
            reverse_dcf["dcf"] = {
                "available": False,
                "policy_version": decision.policy_version if decision is not None else None,
                "missing_information": list(decision.missing_information)
                if decision is not None and decision.missing_information
                else ["dcf_wiring_failed"],
                "warnings": list(decision.warnings)
                if decision is not None and decision.warnings
                else [],
            }
        reverse_dcf["status"] = "unavailable"
    # Provide DCF artefacts at top level so callers can export them without
    # reaching into candidate.full_results, and keep provenance separate from
    # the heuristic valuation_score.
    dcf_payload = {
        "policy": dcf_policy_decision,
        "value": dcf_value,
        "implied": reverse_dcf_results,
        "reverse_dcf": reverse_dcf,
    }
    candidate = SimpleNamespace(
        company_id=company_id,
        ticker="",
        ranking_model="general",
        research_evidence={
            "documents": docs,
            "evidence_packet": evidence_packet,
            "evidence_manifest": selection_manifest.to_dict(),
            "evidence_lane": bool(evidence_packet),
        },
        full_results={
            "valuation": valuation,
            "reverse_dcf": reverse_dcf,
            "dcf": dcf_payload,
        },
    )
    return {
        "financial": financial,
        "valuation": valuation,
        "fundamental_kpis": kpis,
        "sector_kpis": {"current": kpis, "histories": {}},
        # Top-level research_evidence mirrors the missing-data early return above:
        # RankingEngine.rank reads results["research_evidence"], not candidate.
        "research_evidence": candidate.research_evidence,
        "candidate": candidate,
        "dcf": dcf_payload,
        "reverse_dcf": reverse_dcf,
    }
