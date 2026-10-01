"""Build deterministic ranking inputs from the SQLite system of record."""

from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any

from alphaforge.core.financial.calculator import FinancialCalculator
from alphaforge.core.financial.mapper import FinancialMapper
from alphaforge.core.financial.per_share import adjust_historical_shares
from alphaforge.core.kpi_taxonomy import (
    KPI_DATE_ALIASES,
    PRICE_DATE_ALIASES,
    aliased_iso_date,
    parse_iso_date,
    report_date_aliases,
    report_integer_aliases,
)
from alphaforge.core.ranking.sector_rules import ranking_model_for_branch
from alphaforge.core.types import Report, StockPrice
from alphaforge.core.valuation.calculator import ValuationCalculator
from alphaforge.core.valuation.dividend_yield import (
    calculate_dividend_yield,
    trailing_dividend_window,
)
from alphaforge.core.valuation.raw_valuation import RawValuation, compute_raw_valuation
from alphaforge.core.valuation.types import CurrentValuation, HistoricalValuation
from alphaforge.evidence.manifest_store import load_evidence_view

SELECTION_VERSION = "verified-dates-consecutive-annual-v2"
MAX_PRICE_AGE_DAYS = 7


def _date(value: Any) -> date | None:
    return parse_iso_date(value)


def _payload(row) -> dict:
    value = row["raw_payload"]
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value) if value else None
    except (ValueError, TypeError):
        parsed = None
    return parsed if isinstance(parsed, dict) else {}


def _verified_fiscal_end(row) -> date | None:
    ends, malformed = report_date_aliases(_payload(row), "period_end")
    stored = _date(row["period_end"])
    return stored if not malformed and stored is not None and ends == {stored} else None


def _fiscal_year(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        year = value
    elif isinstance(value, str) and value.strip().isdigit():
        year = int(value.strip())
    else:
        return None
    return year if 1 <= year <= 9999 else None


def _verified_publication(row) -> date | None:
    publications, malformed = report_date_aliases(_payload(row), "report_date")
    stored = _date(row["report_date"])
    return stored if not malformed and stored is not None and publications == {stored} else None


def _verified_fiscal_year(row) -> int | None:
    years, malformed = report_integer_aliases(_payload(row), "report_year")
    stored = _fiscal_year(row["report_year"])
    return stored if not malformed and stored is not None and years == {stored} else None


def _verified_report_period(row) -> int | None:
    periods, malformed = report_integer_aliases(_payload(row), "report_period")
    stored_values, stored_malformed = report_integer_aliases(
        {"report_period": row["report_period"]}, "report_period"
    )
    if malformed or stored_malformed or len(stored_values) != 1 or periods != stored_values:
        return None
    return next(iter(stored_values))


def _annual_series(rows) -> tuple[list, list[str], list[dict]]:
    annuals = [row for row in rows if row["period_type"] == "year"]
    if not annuals:
        return [], ["annual history unavailable"], []

    metadata = []
    for row in annuals:
        year = _verified_fiscal_year(row)
        end = _verified_fiscal_end(row)
        currency = str(row["currency"]).upper() if row["currency"] else None
        issues = []
        if year is None:
            issues.append("annual fiscal-year metadata unverified")
        if end is None:
            issues.append("annual fiscal end unverified")
        starts, malformed_start = report_date_aliases(_payload(row), "period_start")
        if malformed_start or len(starts) > 1:
            issues.append("annual stub or duration unverified")
        elif starts:
            start = next(iter(starts))
            if end is None or not 365 <= (end - start).days + 1 <= 366:
                issues.append("annual stub or duration unverified")
        if currency is None:
            issues.append("annual currency comparability unverified")
        metadata.append((row, year, end, currency, issues))

    def excluded(items, boundary_reason: str) -> list[dict]:
        values = []
        for row, _, _, _, issues in items:
            values.append(
                {
                    "id": row["id"],
                    "period_end": row["period_end"],
                    "report_year": row["report_year"],
                    "report_period": row["report_period"],
                    "raw_payload": _payload(row),
                    "reason": "; ".join(issues) if issues else boundary_reason,
                }
            )
        return values

    latest = metadata[-1]
    latest_reasons = list(latest[4])
    if latest_reasons:
        reasons = sorted(set(latest_reasons))
        return [], reasons, excluded(metadata, "; ".join(reasons))

    selected = [latest]
    boundary_index = -1
    boundary_reason = ""
    for index in range(len(metadata) - 2, -1, -1):
        candidate = metadata[index]
        newer = selected[0]
        if candidate[4]:
            boundary_index = index
            boundary_reason = "; ".join(candidate[4])
            break
        previous, end = candidate[2], newer[2]
        if candidate[1] == newer[1]:
            boundary_index = index
            boundary_reason = "duplicate annual fiscal slot"
            break
        same_month_end = (
            previous.month == end.month
            and (previous + timedelta(days=1)).day == (end + timedelta(days=1)).day == 1
        )
        if (
            end.year != previous.year + 1
            or ((previous.month, previous.day) != (end.month, end.day) and not same_month_end)
            or newer[1] != candidate[1] + 1
        ):
            boundary_index = index
            boundary_reason = "annual periods are not consecutive fiscal anniversaries"
            break
        if candidate[3] != newer[3]:
            boundary_index = index
            boundary_reason = "annual currency comparability unverified"
            break
        selected.insert(0, candidate)

    omitted = (
        excluded(metadata[: boundary_index + 1], boundary_reason) if boundary_index >= 0 else []
    )
    reasons = []
    if len(selected) < 2:
        reasons = [boundary_reason or "fewer than two consecutive annual periods"]
    return [item[0] for item in selected], reasons, omitted


def _integer_alias(payload: dict[str, Any], aliases: tuple[str, ...]) -> tuple[int | None, bool]:
    values = [payload[key] for key in aliases if key in payload and payload[key] is not None]
    if not values:
        return None, False
    parsed = {_fiscal_year(value) for value in values}
    if None in parsed or len(parsed) != 1:
        return None, True
    return next(iter(parsed)), False


def _date_facts(payload: dict[str, Any], aliases: tuple[str, ...]) -> dict[str, Any]:
    return {key: payload[key] for key in aliases if key in payload and payload[key] is not None}


def _raw_value(payload: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    return next((payload[key] for key in aliases if key in payload), None)


def _select_kpis(conn, company_id: int, cutoff: date) -> tuple[dict[int, float], list[dict]]:
    kpi_r12: dict[int, float] = {}
    kpi_annual: dict[int, float] = {}
    selected_rows = {}
    rejected = []
    rows = conn.execute(
        """
        SELECT id, kpi_id, value, period_type, observation_date, year,
               price_type, report_period, raw_payload
        FROM kpi_observations WHERE company_id=? AND value IS NOT NULL
        ORDER BY observation_date ASC, year ASC, report_period ASC, price_type ASC
        """,
        (company_id,),
    ).fetchall()
    for row in rows:
        raw = _payload(row)
        stored_observed = _date(row["observation_date"])
        if raw:
            observed, date_issue = aliased_iso_date(raw, KPI_DATE_ALIASES)
            if date_issue or observed != stored_observed:
                observed = None
        else:
            observed = None
        fiscal_year = _fiscal_year(row["year"])
        reason = None
        if (observed is not None and observed > cutoff) or (
            fiscal_year is not None and fiscal_year > cutoff.year
        ):
            reason = "KPI after cutoff"
        elif observed is None:
            reason = (
                "KPI observation date unverified"
                if raw
                else "legacy KPI observation date provenance unverified"
            )
        elif fiscal_year is None:
            reason = "KPI fiscal-year metadata unavailable"
        if reason:
            rejected.append(
                {
                    "id": row["id"],
                    "source": "kpi_observations",
                    "kpi_id": row["kpi_id"],
                    "period_type": row["period_type"],
                    "price_type": row["price_type"],
                    "year": row["year"],
                    "report_period": row["report_period"],
                    "observation_date": row["observation_date"],
                    "value": row["value"],
                    "date_facts": _date_facts(raw, KPI_DATE_ALIASES),
                    "raw_payload": raw,
                    "provenance": "verified_raw_payload" if raw else "legacy_missing_raw_payload",
                    "reason": reason,
                }
            )
            continue
        target = kpi_r12 if row["period_type"] == "r12" else kpi_annual
        kpi_id = int(row["kpi_id"])
        target[kpi_id] = float(row["value"])
        selected_rows[(kpi_id, row["period_type"], row["price_type"])] = row

    for row in conn.execute(
        """
        SELECT id, reason, kpi_id, period_type, price_type, payload_hash,
               raw_payload, rejected_at
        FROM market_input_rejections
        WHERE company_id=? AND input_type='kpi' ORDER BY id ASC
        """,
        (company_id,),
    ).fetchall():
        raw = _payload(row)
        year, malformed_year = _integer_alias(raw, ("year", "y"))
        report_period, malformed_period = _integer_alias(
            raw, ("reportPeriod", "report_period", "p")
        )
        observed, _ = aliased_iso_date(raw, KPI_DATE_ALIASES)
        rejected.append(
            {
                "id": f"rejection:{row['id']}",
                "source": "ingestion_rejection",
                "kpi_id": row["kpi_id"],
                "period_type": row["period_type"],
                "price_type": row["price_type"],
                "year": year,
                "report_period": report_period,
                "observation_date": observed.isoformat() if observed is not None else None,
                "value": _raw_value(raw, ("v", "value")),
                "date_facts": _date_facts(raw, KPI_DATE_ALIASES),
                "raw_payload": raw,
                "payload_hash": row["payload_hash"],
                "rejected_at": row["rejected_at"],
                "invalid_slot": malformed_year or malformed_period,
                "reason": row["reason"],
            }
        )

    selected_r12 = set(kpi_r12)
    for item in rejected:
        rejected_year = _fiscal_year(item["year"])
        rejected_period = item["report_period"]
        rejected_date = _date(item["observation_date"])
        current = not (
            (rejected_date is not None and rejected_date > cutoff)
            or (rejected_year is not None and rejected_year > cutoff.year)
            or item["reason"] == "KPI after cutoff"
        )
        if current and item["period_type"] != "r12" and item["kpi_id"] in selected_r12:
            current = False
        selected = selected_rows.get((int(item["kpi_id"]), item["period_type"], item["price_type"]))
        selected_year = _fiscal_year(selected["year"]) if selected is not None else None
        selected_period = selected["report_period"] if selected is not None else None
        if current and item["period_type"] == "last":
            selected_date = _date(selected["observation_date"]) if selected is not None else None
            if rejected_date is not None and selected_date is not None:
                current = rejected_date > selected_date
        elif current and not item.get("invalid_slot") and rejected_year is not None:
            if selected_year is not None and selected_year > rejected_year:
                current = False
            elif selected_year == rejected_year:
                if selected_period == rejected_period:
                    current = False
                elif selected_period is not None and rejected_period is not None:
                    current = int(selected_period) < int(rejected_period)
        item.pop("invalid_slot", None)
        item["current_refusal"] = current
    return ({**kpi_annual, **kpi_r12}, rejected)


def _selection_refusal_reasons(selection: dict[str, Any]) -> list[str]:
    annual_history = selection.get("annual_history", {})
    reasons = list(annual_history.get("reasons", []))
    reasons.extend(selection.get("price", {}).get("reasons", []))
    reasons.extend(
        f"price {item.get('id', item.get('payload_hash', 'unknown'))}: {item['reason']}"
        for item in selection.get("rejected_prices", [])
        if item.get("reason") and item.get("current_refusal", True)
    )
    reasons.extend(
        f"report {item.get('id', item.get('payload_hash', 'unknown'))}: {item['reason']}"
        for item in selection.get("rejected_reports", [])
        if item.get("reason") and item.get("current_refusal", True)
    )
    reasons.extend(
        f"historical price for {item.get('period_end', 'unknown')}: {item['reason']}"
        for item in selection.get("historical_price_pairings", [])
        if item.get("reason")
    )
    reasons.extend(
        f"KPI {item.get('kpi_id', 'unknown')}: {item['reason']}"
        for item in selection.get("rejected_kpis", [])
        if item.get("reason") and item.get("current_refusal", True)
    )
    return list(dict.fromkeys(reasons))


def _rejection_is_current(
    item: dict[str, Any],
    cutoff: date,
    admitted_rows: list,
    annual_history_start,
) -> bool:
    if item.get("reason") == "after cutoff":
        return False

    raw = item.get("raw_payload") or {}
    years, malformed_year = report_integer_aliases(raw, "report_year")
    ends, malformed_end = report_date_aliases(raw, "period_end")
    periods, malformed_period = report_integer_aliases(raw, "report_period")
    publications, malformed_publication = report_date_aliases(raw, "report_date")
    starts, malformed_start = report_date_aliases(raw, "period_start")
    invalid_identity = (
        malformed_year
        or malformed_end
        or malformed_period
        or malformed_publication
        or malformed_start
        or len(years) > 1
        or len(ends) > 1
        or len(periods) > 1
        or len(publications) > 1
        or len(starts) > 1
        or any(_fiscal_year(value) is None for value in years)
    )

    future_checks = []
    if years:
        future_checks.append(all(value > cutoff.year for value in years))
    if ends:
        future_checks.append(all(value > cutoff for value in ends))
    if publications:
        future_checks.append(all(value > cutoff for value in publications))
    if not invalid_identity and any(future_checks):
        return False

    is_annual = item.get("period_type") == "year"
    has_slot_identity = bool(ends or years) if is_annual else bool(ends or (years and periods))
    matching_rows = [
        row
        for row in admitted_rows
        if row["period_type"] == item.get("period_type")
        and (not years or years == {_verified_fiscal_year(row)})
        and (not ends or ends == {_verified_fiscal_end(row)})
        and (not periods or periods == {_verified_report_period(row)})
    ]
    superseded = not invalid_identity and has_slot_identity and len(matching_rows) == 1
    if superseded:
        return False
    if not is_annual:
        same_type_rows = [
            row for row in admitted_rows if row["period_type"] == item.get("period_type")
        ]
        if not same_type_rows:
            return True
        latest_same_type = max(
            same_type_rows, key=lambda row: _verified_fiscal_end(row) or date.min
        )
        latest_end = _verified_fiscal_end(latest_same_type)
        latest_year = _verified_fiscal_year(latest_same_type)
        latest_period = _verified_report_period(latest_same_type)
        older_checks = []
        if ends and latest_end is not None:
            older_checks.append(next(iter(ends)) < latest_end)
        if years and latest_year is not None:
            rejected_year = next(iter(years))
            if rejected_year != latest_year:
                older_checks.append(rejected_year < latest_year)
            elif periods and latest_period is not None:
                older_checks.append(next(iter(periods)) < latest_period)
            else:
                older_checks.append(False)
        return not (older_checks and all(older_checks))

    if annual_history_start is None:
        return True
    # An unresolved slot inside the entire selected growth span is not historical audit only.
    anchor_year = _verified_fiscal_year(annual_history_start)
    anchor_end = _verified_fiscal_end(annual_history_start)
    comparisons = []
    if years and anchor_year is not None:
        comparisons.append(any(year >= anchor_year for year in years))
    if ends and anchor_end is not None:
        comparisons.append(any(end >= anchor_end for end in ends))
    return any(comparisons) if comparisons else True


def _price_rejection_is_current(item: dict[str, Any], cutoff: date, admitted_rows: list) -> bool:
    raw = item.get("raw_payload") or {}
    if raw:
        values = [
            raw[key]
            for key in PRICE_DATE_ALIASES
            if key in raw and raw[key] is not None
        ]
        rejected_dates = [_date(value) for value in values]
        if not values or any(value is None for value in rejected_dates):
            return True
    else:
        rejected_dates = [_date(item.get("price_date"))]
        if rejected_dates[0] is None:
            return True

    known_dates = [value for value in rejected_dates if value is not None]
    if all(value > cutoff for value in known_dates):
        return False
    if any(value > cutoff for value in known_dates):
        return True
    admitted_dates = {_date(row["price_date"]) for row in admitted_rows}
    latest_date = max((value for value in admitted_dates if value is not None), default=None)
    if latest_date is None:
        return True
    if all(value < latest_date for value in known_dates):
        return False
    return len(set(known_dates)) != 1 or known_dates[0] not in admitted_dates


def _price_evidence(row) -> dict[str, Any]:
    if row is None:
        return {
            "value": None,
            "date_facts": {},
            "raw_payload": None,
            "provenance": None,
        }
    raw = _payload(row)
    return {
        "value": row["close"],
        "date_facts": _date_facts(raw, PRICE_DATE_ALIASES),
        "raw_payload": raw,
        "provenance": "verified_raw_payload" if raw else "legacy_missing_raw_payload",
    }


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
        raw_payload=_payload(row) or None,
        cash=_number(row["cash"]),
        eps=_number(row["eps"]),
        dividend_per_share=_number(row["dividend_per_share"]),
        year=_verified_fiscal_year(row),
        period=_verified_report_period(row),
        period_end=_verified_fiscal_end(row),
        report_date=_verified_publication(row),
        broken_fiscal_year=row["broken_fiscal_year"],
        currency=row["currency"],
    )


def _price(row, fallback_currency: str | None) -> StockPrice:
    price_date = _date(row["price_date"])
    if price_date is None:
        raise ValueError("stock price date unverified")
    return StockPrice(
        date=price_date,
        close=float(row["close"]),
        volume=int(row["volume"]) if row["volume"] is not None else None,
        currency=row["currency"] or fallback_currency,
    )


def load_results_for_company(conn, company_id: int, as_of: str) -> dict[str, Any]:
    """Select cutoff-filtered stored observations, not historical vintages."""
    cutoff = date.fromisoformat(as_of[:10])
    evidence_packet, selection_manifest = load_evidence_view(
        conn, company_id=company_id, as_of=as_of[:10]
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
        "SELECT stock_price_currency, report_currency, branch_id FROM companies WHERE id=?",
        (company_id,),
    ).fetchone()
    stock_currency = company[0] if company else None
    branch_id = int(company[2]) if company and company[2] is not None else None
    research_evidence = {
        "documents": docs,
        "evidence_packet": evidence_packet,
        "evidence_manifest": selection_manifest.to_dict(),
        "evidence_lane": bool(evidence_packet),
    }
    rejected_reports = []
    for row in conn.execute(
        """
        SELECT id, reason, period_type, report_year, report_period,
               payload_hash, raw_payload, rejected_at
        FROM financial_period_rejections
        WHERE company_id=? ORDER BY id ASC
        """,
        (company_id,),
    ).fetchall():
        rejected_reports.append(
            {
                "id": f"rejection:{row['id']}",
                "source": "ingestion_rejection",
                "reason": row["reason"],
                "period_type": row["period_type"],
                "report_year": row["report_year"],
                "report_period": row["report_period"],
                "payload_hash": row["payload_hash"],
                "raw_payload": _payload(row),
                "rejected_at": row["rejected_at"],
            }
        )
    kpis, rejected_kpis = _select_kpis(conn, company_id, cutoff)
    selection: dict[str, Any] = {
        "version": SELECTION_VERSION,
        "max_price_age_calendar_days": MAX_PRICE_AGE_DAYS,
        "rejected_reports": rejected_reports,
        "historical_price_pairings": [],
        "rejected_kpis": rejected_kpis,
        "rejected_prices": [],
    }

    stored_period_rows = conn.execute(
        """
        SELECT * FROM financial_periods
        WHERE company_id=?
        ORDER BY period_end ASC, report_date ASC,
                 CASE period_type WHEN 'quarter' THEN 0 WHEN 'year' THEN 1 ELSE 2 END ASC
        """,
        (company_id,),
    ).fetchall()
    period_rows = []
    for row in stored_period_rows:
        raw = _payload(row)
        end = _verified_fiscal_end(row)
        publication = _verified_publication(row)
        years, malformed_year = report_integer_aliases(raw, "report_year")
        report_periods, malformed_period = report_integer_aliases(raw, "report_period")
        starts, malformed_start = report_date_aliases(raw, "period_start")
        fiscal_year = _verified_fiscal_year(row)
        report_period = _verified_report_period(row)
        reason = None
        if row["is_placeholder"]:
            reason = "placeholder"
        elif end is None or publication is None:
            reason = "fiscal end or publication date unverified"
        elif malformed_year or len(years) > 1 or (years and fiscal_year is None):
            reason = "fiscal-year metadata unverified"
        elif (
            malformed_period
            or len(report_periods) > 1
            or (report_periods and report_period is None)
        ):
            reason = "report period metadata unverified"
        elif malformed_start or len(starts) > 1:
            reason = "fiscal start metadata unverified"
        elif (
            end > cutoff
            or publication > cutoff
            or (fiscal_year is not None and fiscal_year > cutoff.year)
        ):
            reason = "after cutoff"
        elif publication < end:
            reason = "publication precedes fiscal end"
        if reason:
            selection["rejected_reports"].append(
                {
                    "id": row["id"],
                    "source": "financial_periods",
                    "period_type": row["period_type"],
                    "period_end": row["period_end"],
                    "report_year": row["report_year"],
                    "report_period": row["report_period"],
                    "report_date": row["report_date"],
                    "raw_payload": _payload(row),
                    "reason": reason,
                }
            )
        else:
            period_rows.append(row)
    annual_period_rows, annual_reasons, excluded_annuals = _annual_series(period_rows)
    admitted_annuals = [row for row in period_rows if row["period_type"] == "year"]
    annual_history_start = (
        annual_period_rows[0]
        if annual_period_rows
        else (admitted_annuals[-1] if admitted_annuals else None)
    )
    for item in selection["rejected_reports"]:
        item["current_refusal"] = _rejection_is_current(
            item, cutoff, period_rows, annual_history_start
        )
    blocking_rejections = [
        item
        for item in selection["rejected_reports"]
        if item.get("period_type") == "year" and item["current_refusal"]
    ]
    if annual_period_rows and blocking_rejections:
        annual_period_rows = []
        annual_reasons = [
            "unresolved applicable annual rejection: "
            + "; ".join(sorted({item["reason"] for item in blocking_rejections}))
        ]
    selection["annual_history"] = {
        "period_ends": [row["period_end"] for row in annual_period_rows],
        "reasons": annual_reasons,
        "excluded": excluded_annuals,
    }
    stored_price_rows = conn.execute(
        "SELECT * FROM prices WHERE company_id=? ORDER BY price_date ASC",
        (company_id,),
    ).fetchall()
    price_rows = []
    for row in stored_price_rows:
        raw = _payload(row)
        stored_date = _date(row["price_date"])
        if raw:
            verified_date, date_issue = aliased_iso_date(raw, PRICE_DATE_ALIASES)
            if date_issue or verified_date != stored_date:
                verified_date = None
        else:
            verified_date = None
        if verified_date is None or verified_date > cutoff:
            selection["rejected_prices"].append(
                {
                    "id": f"price:{row['price_date']}",
                    "source": "prices",
                    "price_date": row["price_date"],
                    "value": row["close"],
                    "date_facts": _date_facts(raw, PRICE_DATE_ALIASES),
                    "raw_payload": raw,
                    "provenance": "verified_raw_payload" if raw else "legacy_missing_raw_payload",
                    "reason": (
                        "stock price after cutoff"
                        if verified_date is not None and verified_date > cutoff
                        else (
                            "stock price date unverified"
                            if raw
                            else "legacy stock price date provenance unverified"
                        )
                    ),
                }
            )
        else:
            price_rows.append(row)
    for row in conn.execute(
        """
        SELECT id, reason, payload_hash, raw_payload, rejected_at
        FROM market_input_rejections
        WHERE company_id=? AND input_type='price' ORDER BY id ASC
        """,
        (company_id,),
    ).fetchall():
        raw = _payload(row)
        rejected_date, _ = aliased_iso_date(raw, PRICE_DATE_ALIASES)
        selection["rejected_prices"].append(
            {
                "id": f"rejection:{row['id']}",
                "source": "ingestion_rejection",
                "price_date": rejected_date.isoformat() if rejected_date is not None else None,
                "value": _raw_value(raw, ("close", "c", "price")),
                "date_facts": _date_facts(raw, PRICE_DATE_ALIASES),
                "raw_payload": raw,
                "payload_hash": row["payload_hash"],
                "rejected_at": row["rejected_at"],
                "reason": row["reason"],
            }
        )
    for item in selection["rejected_prices"]:
        item["current_refusal"] = _price_rejection_is_current(item, cutoff, price_rows)

    candidate_price = _price(price_rows[-1], stock_currency) if price_rows else None
    current_price_rejections = [
        item for item in selection["rejected_prices"] if item["current_refusal"]
    ]
    latest_price = candidate_price
    price_missing = []
    if latest_price is None:
        price_missing.append("latest stock price unavailable")
    elif current_price_rejections:
        price_missing.append(
            "unresolved applicable stock price rejection: "
            + "; ".join(sorted({item["reason"] for item in current_price_rejections}))
        )
        price_missing.append("latest stock price unavailable")
        latest_price = None
    elif (cutoff - latest_price.date).days > MAX_PRICE_AGE_DAYS:
        price_missing.append("stock price is older than seven calendar days")
        latest_price = None
    selection["price"] = {
        "selected_date": latest_price.date.isoformat() if latest_price else None,
        "candidate_date": candidate_price.date.isoformat() if candidate_price else None,
        "age_calendar_days": (cutoff - candidate_price.date).days if candidate_price else None,
        **_price_evidence(price_rows[-1] if price_rows else None),
        "reasons": price_missing,
    }
    if not period_rows:
        selection["refusal_reasons"] = _selection_refusal_reasons(selection)
        _missing = ["financial_period", *price_missing]
        if selection["rejected_reports"]:
            _missing.append(
                "financial fiscal end/publication unavailable under verified-date selection"
            )
        _unavailable = {
            "status": "unavailable",
            "missing_information": _missing,
            "selection": selection,
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
            "fundamental_kpis": kpis,
            "sector_kpis": {"current": kpis, "histories": {}},
            "research_evidence": research_evidence,
            "selection": selection,
            "candidate": SimpleNamespace(
                company_id=company_id,
                ticker="",
                ranking_model=ranking_model_for_branch(branch_id),
                research_evidence=research_evidence,
                full_results={
                    "valuation": None,
                    "reverse_dcf": _unavailable,
                    "selection": selection,
                },
            ),
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
    annual_reports = []
    for row in annual_period_rows:
        shares = _number(row["shares_outstanding"])
        if shares is not None and split_events:
            shares = adjust_historical_shares(
                shares, str(row["period_end"])[:10], comparison_date, split_events
            )
        annual_reports.append(_report(row, shares_override=shares))
    latest_annual = annual_reports[-1] if annual_reports else None
    historical_annuals = annual_reports[:-1]
    financial = FinancialCalculator().calculate(
        financial_mapper.to_current(current_report),
        financial_mapper.to_historical(historical_annuals),
        growth_current=financial_mapper.to_current(latest_annual) if latest_annual else None,
        growth_available=len(annual_reports) >= 2,
    )
    current_raw = compute_raw_valuation(latest_price, current_report)
    dcf_raw = compute_raw_valuation(latest_price, dcf_current_report)

    historical_raw: list[RawValuation] = []
    for row, report in zip(period_rows[:-1], historical_reports, strict=False):
        candidates = [
            p for p in price_rows if str(p["price_date"])[:10] <= str(row["period_end"])[:10]
        ]
        paired = _price(candidates[-1], stock_currency) if candidates else None
        age = (report.period_end - paired.date).days if paired else None
        usable = age is not None and age <= MAX_PRICE_AGE_DAYS
        selection["historical_price_pairings"].append(
            {
                "period_end": row["period_end"],
                "price_date": paired.date.isoformat() if paired else None,
                "age_calendar_days": age,
                **_price_evidence(candidates[-1] if candidates else None),
                "reason": None
                if usable
                else "historical price missing or older than seven calendar days",
            }
        )
        if usable:
            historical_raw.append(compute_raw_valuation(paired, report))
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
    window_start, window_end = trailing_dividend_window(cutoff)
    dividends = conn.execute(
        """SELECT substr(ex_date, 1, 10) AS ex_date, amount, currency, currency_verified,
                  dividend_type FROM dividends
           WHERE company_id=? AND substr(ex_date, 1, 10) > ?
             AND substr(ex_date, 1, 10) <= ?
           ORDER BY ex_date, dividend_type, amount, currency""",
        (company_id, window_start.isoformat(), window_end.isoformat()),
    ).fetchall()
    coverage = conn.execute(
        """SELECT window_start, window_end, status, source, assurance, verified_at
           FROM dividend_window_coverage
           WHERE company_id=? AND window_start=? AND window_end=?""",
        (company_id, window_start.isoformat(), window_end.isoformat()),
    ).fetchone()
    dividend_yield = calculate_dividend_yield(
        cutoff,
        latest_price.close if latest_price else None,
        price_rows[-1]["currency"] if price_rows else None,
        [dict(row) for row in dividends],
        dict(coverage) if coverage else None,
    )
    current.dividend_yield = dividend_yield.value
    valuation = ValuationCalculator().calculate(current, historical, current_raw)

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
        "current_price": latest_price.close if latest_price else None,
        "current_revenue": dcf_current_report.revenue if dcf_current_report is not None else None,
        "current_shares": dcf_current_report.shares_outstanding
        if dcf_current_report is not None
        else None,
        "current_net_debt": current_net_debt,
        "net_debt_source": net_debt_source,
        "price_currency": latest_price.currency if latest_price else stock_currency,
        "selection": selection,
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
        # Use the same validated annual chronology for policy; R12/current
        # valuation remains distinct from the annual growth anchor.
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
            if price_missing:
                reverse_dcf["status"] = "unavailable"
            elif current_net_debt is None:
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
    if price_missing:
        reverse_dcf["status"] = "unavailable"
        reverse_dcf["missing_information"] = price_missing
        reverse_dcf["dcf"] = {
            "available": False,
            "policy_version": dcf_policy_decision.policy_version if dcf_policy_decision else None,
            "missing_information": price_missing,
            "warnings": [],
        }
    # Provide DCF artefacts at top level so callers can export them without
    # reaching into candidate.full_results, and keep provenance separate from
    # the heuristic valuation_score.
    dcf_payload = {
        "policy": dcf_policy_decision,
        "value": dcf_value,
        "implied": reverse_dcf_results,
        "reverse_dcf": reverse_dcf,
    }
    selection["refusal_reasons"] = _selection_refusal_reasons(selection)
    candidate = SimpleNamespace(
        company_id=company_id,
        ticker="",
        ranking_model=ranking_model_for_branch(branch_id),
        research_evidence=research_evidence,
        full_results={
            "valuation": valuation,
            "reverse_dcf": reverse_dcf,
            "dcf": dcf_payload,
            "selection": selection,
        },
    )
    return {
        "financial": financial,
        "valuation": valuation,
        "dividend_yield": {
            **dividend_yield.to_dict(),
            "price_date": latest_price.date.isoformat() if latest_price else None,
            "close": latest_price.close if latest_price else None,
        },
        "fundamental_kpis": kpis,
        "sector_kpis": {"current": kpis, "histories": {}},
        "selection": selection,
        # Top-level research_evidence mirrors the missing-data early return above:
        # RankingEngine.rank reads results["research_evidence"], not candidate.
        "research_evidence": candidate.research_evidence,
        "candidate": candidate,
        "dcf": dcf_payload,
        "reverse_dcf": reverse_dcf,
    }
