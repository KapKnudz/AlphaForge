"""Build deterministic ranking inputs from the SQLite system of record."""

from __future__ import annotations

import json
from dataclasses import asdict, replace
from datetime import date, timedelta
from hashlib import sha256
from math import isfinite
from types import SimpleNamespace
from typing import Any

from alphaforge.core.financial.calculator import FinancialCalculator
from alphaforge.core.financial.mapper import FinancialMapper
from alphaforge.core.financial.per_share import adjust_historical_shares
from alphaforge.core.kpi_taxonomy import (
    KPI_DATE_ALIASES,
    KPI_REPORT_PERIOD_ALIASES,
    KPI_YEAR_ALIASES,
    PRICE_DATE_ALIASES,
    aliased_iso_date,
    integer_aliases,
    parse_iso_date,
    report_date_aliases,
    report_integer_aliases,
)
from alphaforge.core.ranking.sector_rules import ranking_model_for_branch
from alphaforge.core.types import Report, StockPrice
from alphaforge.core.valuation.calculator import ValuationCalculator
from alphaforge.core.valuation.dcf_contract import (
    DcfResultMetadata,
    DcfResultStatus,
    EvidenceReference,
    serialize_dcf_result,
)
from alphaforge.core.valuation.dcf_policy import (
    DcfAssumptionPolicy,
    DcfInputQualityView,
    DcfNormalizedFinancialView,
    DcfQualityIssue,
    DcfQualityPeriod,
)
from alphaforge.core.valuation.dcf_router import decide_dcf_route
from alphaforge.core.valuation.dividend_yield import (
    calculate_dividend_yield,
    trailing_dividend_window,
)
from alphaforge.core.valuation.raw_valuation import RawValuation, compute_raw_valuation
from alphaforge.core.valuation.reinvestment import qualify_calibration
from alphaforge.core.valuation.reverse_dcf import (
    UnsupportedEconomicPolicy,
    UnsupportedValuationModel,
)
from alphaforge.core.valuation.solve_eligibility import (
    SOLVE_AXIS_REGISTRY,
    fixed_assumption_provenance_complete,
    solve_axis_metadata,
)
from alphaforge.core.valuation.types import CurrentValuation, HistoricalValuation
from alphaforge.evidence.manifest_store import load_evidence_view

SELECTION_VERSION = "verified-dates-consecutive-annual-denomination-v1"
MAX_PRICE_AGE_DAYS = 7


def _select_reinvestment_calibration(conn, company_id: int, cutoff: date, currency: str | None):
    """Retain rejection reasons; deterministic choice by fiscal end/content identity."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reinvestment_calibrations'"
    ).fetchone():
        return None, {"selected_identity": None, "candidates": []}
    rows = conn.execute(
        "SELECT identity,record_json FROM reinvestment_calibrations WHERE company_id=? ORDER BY identity",
        (company_id,),
    ).fetchall()
    candidates = []
    admitted = []
    for row in rows:
        try:
            record = json.loads(row["record_json"])
            if not isinstance(record, dict) or record.get("company_id") != company_id:
                raise ValueError("calibration company identity mismatch")
            qualified = qualify_calibration(record, as_of=cutoff, currency=currency, tax_rate=0.21)
            if qualified.identity != row["identity"]:
                raise ValueError("calibration content identity mismatch")
            admitted.append((record["period_end"], qualified.identity, record))
            reason = None
        except (ValueError, TypeError, KeyError, OverflowError, AttributeError) as exc:
            reason = str(exc)
        candidates.append({"identity": row["identity"], "rejection_reason": reason})
    selected = max(admitted, key=lambda x: (x[0], x[1])) if admitted else None
    # Conflicting reviews for the same latest fiscal period require a new review,
    # not a hash-order business choice.
    if selected and sum(x[0] == selected[0] for x in admitted) > 1:
        return None, {
            "selected_identity": None,
            "candidates": candidates,
            "refusal": "ambiguous_calibration",
        }
    return (selected[2] if selected else None), {
        "selected_identity": selected[1] if selected else None,
        "candidates": candidates,
    }


def _dcf_unavailable_reason(missing_information: tuple[str, ...]) -> str | None:
    if "admissible_reinvestment_calibration" in missing_information:
        return "Qualified operating-capital/earnings calibration unavailable; a dated scalar ROIC alone is insufficient."
    if "dated_positive_roic" in missing_information:
        return (
            "Growth-based FCFF requires qualified dated operating-return/capital evidence; "
            "no ordinary value or implied roots were produced. A provider ROIC alone is insufficient."
        )
    if "negative_nopat_unsupported_reinvestment" in missing_information:
        return (
            "ROIC-based reinvestment is unsupported for negative NOPAT; the model does not "
            "treat negative investment as cash released."
        )
    return None


def _normalization_payload(normalization) -> dict[str, Any] | None:
    if normalization is None:
        return None
    return {
        "confidence": normalization.confidence,
        "selected_window_years": normalization.selected_window_years,
        "reasons": list(normalization.reasons),
    }


def _dcf_exception_status(exc: Exception) -> DcfResultStatus:
    if isinstance(exc, UnsupportedEconomicPolicy):
        return DcfResultStatus.DOMAIN_UNAVAILABLE
    if isinstance(exc, UnsupportedValuationModel):
        return DcfResultStatus.UNSUPPORTED
    if isinstance(exc, RuntimeError):
        return DcfResultStatus.NONCONVERGENCE
    if isinstance(exc, (ValueError, OverflowError)):
        return DcfResultStatus.INVALID_INPUT
    return DcfResultStatus.UNAVAILABLE


def _dcf_failure_status(reason: str | None) -> DcfResultStatus:
    if reason is None:
        return DcfResultStatus.INSUFFICIENT_EVIDENCE
    if "did not converge" in reason:
        return DcfResultStatus.NONCONVERGENCE
    if "not bracketed" in reason or "no solve bracket" in reason:
        return DcfResultStatus.NO_CROSSING
    if "finite" in reason or "must be positive" in reason or "must exceed" in reason:
        return DcfResultStatus.INVALID_INPUT
    if reason == "unavailable_constant_margin_only" or reason.startswith(
        "market-cap hurdle policy is defined for SEK; received "
    ):
        return DcfResultStatus.UNSUPPORTED
    if reason == "not_identifiable":
        return DcfResultStatus.NOT_IDENTIFIABLE
    if reason in {
        "dated_positive_roic",
        "admissible_reinvestment_calibration",
        "normalized EBIT history unavailable",
        "net_debt",
        "current_revenue_or_shares",
        "price_unavailable",
        "archetype_unknown_or_mixed",
        "forecast_profile_unknown_or_mixed",
        "mature_operating_route_evidence_unavailable",
        "mature_operating_route_evidence_not_catalogued",
        "mature_operating_route_evidence_mismatch",
        "high_growth_transition_and_funding_evidence_unavailable",
        "cyclical_normalized_base_unavailable",
    }:
        return DcfResultStatus.INSUFFICIENT_EVIDENCE
    if reason in {
        "negative_nopat_unsupported_reinvestment",
        "nonpositive_nopat_unsupported_reinvestment",
        "varying_margin_capital_evidence_unavailable",
        "unsupported_capital_release",
        "unsupported_financing",
        "invalid_candidate_economics",
    }:
        return DcfResultStatus.DOMAIN_UNAVAILABLE
    if (
        "banks require" in reason
        or "property companies require" in reason
        or "not FCFF" in reason
        or reason
        in {
            "financial_company_requires_non_fcff_method",
            "property_company_requires_nav_or_ffo_method",
            "resource_company_requires_separate_economic_policy",
            "holding_or_unusual_structure_unsupported",
            "unsupported_forecast_profile",
        }
    ):
        return DcfResultStatus.UNSUPPORTED
    if "not both verified" in reason:
        return DcfResultStatus.INSUFFICIENT_EVIDENCE
    if "denomination mismatch" in reason or "conflicts" in reason:
        return DcfResultStatus.INVALID_INPUT
    return DcfResultStatus.INSUFFICIENT_EVIDENCE


def _dcf_solve_status(
    solution_status: str | None, reason: str | None, error: str | None
) -> DcfResultStatus:
    if solution_status == "not_identifiable":
        return DcfResultStatus.NOT_IDENTIFIABLE
    if solution_status == "sampled_match_region":
        return DcfResultStatus.SAMPLED_MATCH_REGION
    if solution_status == "sampled_match":
        return DcfResultStatus.SAMPLED_MATCH
    if solution_status == "no_candidate_solution":
        return DcfResultStatus.NO_CROSSING
    if solution_status == "candidate_solutions":
        return DcfResultStatus.CANDIDATE_SOLUTIONS
    if solution_status == "unavailable":
        return _dcf_failure_status(
            error if reason == "invalid_candidate_economics" else reason or error
        )
    return DcfResultStatus.UNAVAILABLE


def _solve_axis_result(engine, inputs, axis: str, eligibility) -> dict[str, Any]:
    """Return conditional solve evidence without promoting sampled candidates to a unique answer."""
    definition = SOLVE_AXIS_REGISTRY[axis]
    metadata = asdict(eligibility)
    metadata["root_interpretation"] = definition.root_interpretation
    if definition.status != "supported":
        metadata["status"] = definition.status
        metadata["reason"] = definition.reason
        return {
            "available": False,
            "solution_status": (
                "not_identifiable" if definition.status == "not_identifiable" else "unavailable"
            ),
            "reason": definition.reason,
            "eligibility": metadata,
            "candidate_roots": [],
            "candidate_solution_count": 0,
            "qualification": definition.root_interpretation,
        }
    if eligibility.status == "supported":
        metadata["reason"] = definition.reason
    if eligibility.status != "supported":
        return {
            "available": False,
            "solution_status": "unavailable",
            "reason": eligibility.reason,
            "eligibility": metadata,
            "candidate_roots": [],
            "candidate_solution_count": 0,
            "qualification": definition.root_interpretation,
        }

    lower = eligibility.domain["lower_bound"]
    upper = eligibility.domain["upper_bound"]
    try:
        diagnostics, brackets, sampled_matches = engine.diagnose_solve_range(
            inputs, axis, lower, upper, eligibility=eligibility
        )
        roots = []
        for bracket_lower, bracket_upper in brackets:
            result = engine.solve(
                inputs,
                axis,
                bracket_lower,
                bracket_upper,
                eligibility=eligibility,
            )
            value = result.valuation
            roots.append(
                {
                    "implied_assumption": result.implied_assumption,
                    "modeled_price": result.modeled_price,
                    "price_difference": result.price_difference,
                    "iterations": result.iterations,
                    "root_bracket": [bracket_lower, bracket_upper],
                    "solution_evidence": "sign_change_bracket",
                    "solution_evidence_qualification": (
                        "opposite-signed sampled residuals bracket a conditional numerical solution"
                    ),
                    "enterprise_value": value.enterprise_value,
                    "equity_value": value.equity_value,
                    "value_per_share": value.value_per_share,
                    "terminal_value_share_of_enterprise_value": (
                        value.discounted_terminal_value / value.enterprise_value
                        if value.enterprise_value != 0
                        else None
                    ),
                    **_equity_qualification(value.equity_value),
                }
            )
        roots.sort(key=lambda root: root["implied_assumption"])
        sampled_only = [
            match for match in sampled_matches if not match["associated_sign_change_bracket_count"]
        ]
        base = {
            **diagnostics,
            "sampled_match_point_count": len(sampled_only),
            "sampled_match_points": sampled_only,
            "eligibility": metadata,
        }
        if diagnostics["sampled_match_regions"] and roots:
            return {
                **base,
                "available": False,
                "solution_status": "candidate_solutions",
                "candidate_roots": roots,
                "candidate_solution_count": len(roots),
                "crossing_count_on_grid": len(roots),
                "solve_scope": "one-variable conditional solve; all other assumptions held fixed",
                "solution_evidence": (
                    f"{len(roots)} sampled sign-change bracket(s) and "
                    f"{len(diagnostics['sampled_match_regions'])} sampled match region(s)"
                ),
                "solution_qualification": (
                    "conditional candidates and sampled match regions are distinct observations; "
                    "finite sampling does not establish uniqueness or completeness, nor a "
                    "continuous equivalence interval, and no candidate is selected as the answer"
                ),
            }
        if diagnostics["sampled_match_regions"]:
            return {
                **base,
                "available": False,
                "solution_status": "sampled_match_region",
                "candidate_roots": [],
                "candidate_solution_count": 0,
                "solve_scope": "one-variable conditional solve; all other assumptions held fixed",
                "solution_evidence": (
                    "contiguous sampled assumptions match within price tolerance; a continuous "
                    "equivalence interval and complete root set are not established"
                ),
            }
        if not roots:
            if sampled_only:
                return {
                    **base,
                    "available": False,
                    "solution_status": "sampled_match",
                    "candidate_roots": [],
                    "candidate_solution_count": 0,
                    "solve_scope": "one-variable conditional solve; all other assumptions held fixed",
                    "solution_qualification": (
                        "sampled tolerance match only; a tangency or exact root is not established"
                    ),
                }
            direction = diagnostics["no_solution_direction"]
            error = (
                f"target price is {direction} the sampled attainable range"
                if direction in {"above", "below"}
                else "target lies within the sampled range but no sampled sign-change bracket was found"
            )
            return {
                **base,
                "available": False,
                "error": error,
                "solution_status": "no_candidate_solution",
                "candidate_roots": [],
                "candidate_solution_count": 0,
                "solve_scope": "one-variable conditional solve; all other assumptions held fixed",
                "solution_qualification": (
                    "no crossing was observed in the finite sampled range; unsampled crossings "
                    "or extrema are not excluded"
                ),
            }
        return {
            **base,
            "available": False,
            "solution_status": "candidate_solutions",
            "candidate_roots": roots,
            "candidate_solution_count": len(roots),
            "crossing_count_on_grid": len(roots),
            "solution_evidence": (
                f"{len(roots)} sampled sign-change bracket(s); "
                f"{len(sampled_only)} unbracketed tolerance-match sample(s)"
            ),
            "solution_qualification": (
                "conditional candidate solutions only; finite sampling does not establish "
                "uniqueness or completeness, and no candidate is selected as the answer"
            ),
            "solve_scope": "one-variable conditional solve; all other assumptions held fixed",
        }
    except RuntimeError as exc:
        return {
            "available": False,
            "solution_status": "unavailable",
            "reason": "reverse DCF solver did not converge",
            "error": str(exc),
            "eligibility": metadata,
            "candidate_roots": [],
            "candidate_solution_count": 0,
        }
    except Exception as exc:
        return {
            "available": False,
            "solution_status": "unavailable",
            "reason": "invalid_candidate_economics",
            "error": str(exc),
            "eligibility": metadata,
            "candidate_roots": [],
            "candidate_solution_count": 0,
        }


def _equity_qualification(equity_value: float) -> dict[str, Any]:
    negative = equity_value < 0
    return {
        "negative_modeled_equity": negative,
        "equity_value_qualification": (
            "negative modeled equity is not a tradable negative share price; "
            "limited-liability and turnaround option value are outside this FCFF model"
            if negative
            else "conditional FCFF result, not an investment conclusion"
        ),
    }


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


def _currency_code(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    code = value.strip().upper()
    return code if len(code) == 3 and code.isascii() and code.isalpha() else None


def _report_denomination(row) -> tuple[dict[str, Any], str | None]:
    original = _currency_code(row["currency"])
    values = _currency_code(row["values_currency"])
    target = _currency_code(row["conversion_target_currency"])
    mode = row["conversion_mode"]
    result = {
        "original_currency": original,
        "values_currency": values,
        "conversion_mode": mode,
        "conversion_target_currency": target,
        "currency_ratio": row["currency_ratio"],
        "fx_rate_to_sek": None,
    }
    if mode not in {"converted", "original"}:
        return result, "report conversion mode unavailable or unsupported"
    if target is None:
        return result, "report conversion target currency unavailable or invalid"
    if original is None:
        return result, "original report currency unavailable or invalid"
    if values is None:
        return result, "report values currency unavailable or invalid"
    expected = target if mode == "converted" else original
    if values != expected:
        return result, "report values currency conflicts with acquisition mode and target"
    raw = _payload(row)
    raw_values = _currency_code(raw.get("values_currency"))
    raw_target = _currency_code(raw.get("conversion_target_currency"))
    if raw.get("values_currency") is not None and raw_values != values:
        return result, "report values currency conflicts with acquired payload"
    if raw.get("conversion_mode") is not None and raw.get("conversion_mode") != mode:
        return result, "report conversion mode conflicts with acquired payload"
    if raw.get("conversion_target_currency") is not None and raw_target != target:
        return result, "report conversion target conflicts with acquired payload"
    ratio = row["currency_ratio"]
    if ratio is not None:
        try:
            valid_ratio = isfinite(float(ratio)) and float(ratio) > 0
        except (TypeError, ValueError):
            valid_ratio = False
        if not valid_ratio:
            return result, "report currency ratio is invalid"
        if original == target and float(ratio) != 1.0:
            return result, "report currency ratio conflicts with same-currency acquisition"
    raw_ratios = [
        raw[key]
        for key in ("currency_Ratio", "currency_ratio", "currencyRatio")
        if key in raw and raw[key] is not None
    ]
    if raw_ratios:
        if any(isinstance(value, bool) for value in raw_ratios):
            return result, "report currency ratio is invalid"
        try:
            parsed_ratios = [float(value) for value in raw_ratios]
            if any(not isfinite(value) or value <= 0 for value in parsed_ratios):
                return result, "report currency ratio is invalid"
            if len(set(parsed_ratios)) != 1:
                return result, "report currency ratio aliases conflict"
            raw_ratio = parsed_ratios[0]
            if ratio is None or float(ratio) != raw_ratio:
                return result, "report currency ratio conflicts with stored provenance"
            if target == "SEK" and row["fx_rate_to_sek"] == ratio:
                result["fx_rate_to_sek"] = ratio
        except (TypeError, ValueError):
            return result, "report currency ratio is invalid"
    return result, None


def report_selection_reason(row, cutoff: date) -> tuple[str | None, dict]:
    """Shared report admission for retention and calculation selection."""
    raw = _payload(row)
    end = _verified_fiscal_end(row)
    publication = _verified_publication(row)
    years, malformed_year = report_integer_aliases(raw, "report_year")
    periods, malformed_period = report_integer_aliases(raw, "report_period")
    starts, malformed_start = report_date_aliases(raw, "period_start")
    denomination, denomination_reason = _report_denomination(row)
    if row["is_placeholder"]:
        reason = "placeholder"
    elif end is None or publication is None:
        reason = "fiscal end or publication date unverified"
    elif malformed_year or len(years) > 1 or (years and _verified_fiscal_year(row) is None):
        reason = "fiscal-year metadata unverified"
    elif malformed_period or len(periods) > 1 or (periods and _verified_report_period(row) is None):
        reason = "report period metadata unverified"
    elif malformed_start or len(starts) > 1:
        reason = "fiscal start metadata unverified"
    elif end > cutoff or publication > cutoff:
        reason = "after cutoff"
    elif publication < end:
        reason = "publication precedes fiscal end"
    else:
        reason = denomination_reason
    return reason, denomination


def verified_price_date(row) -> date | None:
    raw = _payload(row)
    observed, issue = aliased_iso_date(raw, PRICE_DATE_ALIASES)
    return observed if raw and not issue and observed == _date(row["price_date"]) else None


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
        currency = _currency_code(row["values_currency"])
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


def _dcf_quality_period(row, disposition: str, reason: str | None = None) -> DcfQualityPeriod:
    starts, malformed_start = report_date_aliases(_payload(row), "period_start")
    start = next(iter(starts)) if not malformed_start and len(starts) == 1 else None
    end = _verified_fiscal_end(row)
    publication = _verified_publication(row)
    fiscal_year = _verified_fiscal_year(row)
    period_type = str(row["period_type"])
    revenue = _number(row["revenue"])
    ebit = _number(row["ebit"])
    if ebit is None:
        ebit = _number(row["operating_profit"])
    unknown_fields = ("period_start_and_duration",) if not starts and not malformed_start else ()
    return DcfQualityPeriod(
        fiscal_year=fiscal_year,
        period_type=period_type,
        period_start=start.isoformat() if start else None,
        period_end=end.isoformat() if end else None,
        published_on=publication.isoformat() if publication else None,
        duration_days=(end - start).days + 1 if start is not None and end is not None else None,
        currency=_currency_code(row["values_currency"]),
        disposition=disposition,
        revenue_operand_qualified=revenue is not None and revenue > 0,
        ebit_operand_qualified=ebit is not None,
        reported_period=_verified_report_period(row),
        unknown_fields=unknown_fields,
        evidence_id=f"{period_type}:{fiscal_year}:{end.isoformat() if end else None}",
        reason=reason,
    )


def _dcf_rejected_quality_period(item: dict) -> DcfQualityPeriod:
    raw = item.get("raw_payload") or {}
    ends, malformed_end = report_date_aliases(raw, "period_end")
    starts, malformed_start = report_date_aliases(raw, "period_start")
    publications, malformed_publication = report_date_aliases(raw, "report_date")
    years, malformed_year = report_integer_aliases(raw, "report_year")
    periods, malformed_period = report_integer_aliases(raw, "report_period")
    fiscal_year = _fiscal_year(item.get("report_year"))
    stored_period_end = _date(item.get("period_end"))
    stored_publication = _date(item.get("report_date"))
    stored_periods, malformed_stored_period = report_integer_aliases(
        {"report_period": item.get("report_period")}, "report_period"
    )
    valid_period_end = (
        not malformed_end
        and len(ends) == 1
        and ("period_end" not in item or ends == {stored_period_end})
    )
    valid_period_start = not malformed_start and len(starts) == 1
    valid_publication = (
        not malformed_publication
        and len(publications) == 1
        and ("report_date" not in item or publications == {stored_publication})
    )
    valid_fiscal_year = fiscal_year is not None and not malformed_year and years == {fiscal_year}
    valid_reported_period = (
        not malformed_period
        and not malformed_stored_period
        and len(periods) == 1
        and periods == stored_periods
    )
    period_end = next(iter(ends)) if valid_period_end else None
    period_start = next(iter(starts)) if valid_period_start else None
    published_on = next(iter(publications)) if valid_publication else None
    denomination = item.get("denomination") or {}
    currency = _currency_code(denomination.get("values_currency"))
    if currency is None:
        currency = _currency_code(raw.get("values_currency"))
    unknown_fields = []
    if not valid_period_start:
        unknown_fields.append("period_start_and_duration")
    if not valid_period_end:
        unknown_fields.append("period_end")
    if not valid_publication:
        unknown_fields.append("published_on")
    if not valid_fiscal_year:
        unknown_fields.append("fiscal_year")
    if currency is None:
        unknown_fields.append("values_currency")
    return DcfQualityPeriod(
        fiscal_year=fiscal_year if valid_fiscal_year else None,
        period_type=str(item.get("period_type", "year")),
        period_start=period_start.isoformat() if period_start else None,
        period_end=period_end.isoformat() if period_end else None,
        published_on=published_on.isoformat() if published_on else None,
        duration_days=(
            (period_end - period_start).days + 1
            if period_start is not None and period_end is not None
            else None
        ),
        currency=currency,
        disposition="excluded",
        reported_period=next(iter(periods)) if valid_reported_period else None,
        unknown_fields=tuple(unknown_fields),
        evidence_id=str(item.get("id")) if item.get("id") is not None else None,
        reason=str(item.get("reason", "annual report rejected")),
    )


def _dcf_input_quality_view(
    annual_rows, selected_rows, selection, valuation_row
) -> DcfInputQualityView:
    history = selection.get("annual_history", {})
    selected_ids = {row["id"] for row in selected_rows}
    excluded_reason = {
        (str(item.get("period_end")), str(item.get("report_year"))): item.get("reason")
        for item in history.get("excluded", [])
    }
    valuation_period = (
        _dcf_quality_period(valuation_row, "valuation_input") if valuation_row is not None else None
    )
    selected = tuple(_dcf_quality_period(row, "selected") for row in selected_rows)
    excluded_items = []
    for row in annual_rows:
        if row["id"] in selected_ids:
            continue
        key = (str(row["period_end"]), str(row["report_year"]))
        reason = excluded_reason.get(key)
        if reason is None:
            reason = next(
                iter(history.get("reasons", ())), "outside selected consecutive annual suffix"
            )
        excluded_items.append(_dcf_quality_period(row, "excluded", reason))
    rejected_items = tuple(
        item
        for item in selection.get("rejected_reports", [])
        if item.get("period_type") in {"year", "r12"} and item.get("current_refusal")
    )
    rejected_periods = tuple(_dcf_rejected_quality_period(item) for item in rejected_items)
    excluded = (*excluded_items, *rejected_periods)

    annual_periods = (
        *selected,
        *(period for period in excluded if period.period_type == "year"),
    )
    years = {period.fiscal_year for period in annual_periods if period.fiscal_year is not None}
    expected = tuple(range(min(years), max(years) + 1)) if years else ()
    missing = tuple(year for year in expected if year not in years)

    issues = []
    for reason in history.get("reasons", []):
        issues.append(DcfQualityIssue("annual_history", None, None, reason))
    for item in excluded_items:
        if item.reason:
            issues.append(
                DcfQualityIssue(
                    "annual_history",
                    item.fiscal_year,
                    item.period_end,
                    item.reason,
                    item.evidence_id,
                )
            )
    for item, period in zip(rejected_items, rejected_periods, strict=True):
        issues.append(
            DcfQualityIssue(
                str(item.get("source", "rejected_report")),
                period.fiscal_year,
                period.period_end,
                str(item.get("reason", "annual report rejected")),
                period.evidence_id,
            )
        )
    unique_issues = {
        (issue.source, issue.fiscal_year, issue.period_end, issue.reason, issue.evidence_id): issue
        for issue in issues
    }
    unknowns = tuple(
        sorted(
            {
                f"{period.period_type} period {period.fiscal_year or 'unknown-year'} "
                f"ending {period.period_end or 'unknown-end'}: {field} unknown"
                for period in (
                    *selected,
                    *excluded,
                    *((valuation_period,) if valuation_period else ()),
                )
                for field in period.unknown_fields
            }
        )
    )
    return DcfInputQualityView(
        expected_periods=expected,
        valuation_period=valuation_period,
        selected_periods=selected,
        excluded_periods=excluded,
        missing_periods=missing,
        evidenced_anomalies=tuple(
            unique_issues[key]
            for key in sorted(unique_issues, key=lambda value: tuple(str(x) for x in value))
        ),
        unknowns=unknowns,
        selection_reasons=tuple(history.get("reasons", ())),
    )


def _date_facts(payload: dict[str, Any], aliases: tuple[str, ...]) -> dict[str, Any]:
    return {key: payload[key] for key in aliases if key in payload and payload[key] is not None}


def _raw_value(payload: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    return next(
        (payload[key] for key in aliases if key in payload and payload[key] is not None),
        None,
    )


def _select_kpis(
    conn, company_id: int, cutoff: date, *, selected_input_ids: set | None = None
) -> tuple[dict[int, float], list[dict]]:
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
        invalid_slot = False
        slot_reason = None
        if row["period_type"] != "last" and raw:
            raw_years, malformed_year = integer_aliases(raw, KPI_YEAR_ALIASES)
            stored_years, malformed_stored_year = integer_aliases(
                {"year": row["year"]}, KPI_YEAR_ALIASES
            )
            raw_periods, malformed_period = integer_aliases(raw, KPI_REPORT_PERIOD_ALIASES)
            stored_periods, malformed_stored_period = integer_aliases(
                {"report_period": row["report_period"]}, KPI_REPORT_PERIOD_ALIASES
            )
            if (
                malformed_year
                or malformed_stored_year
                or len(raw_years) != 1
                or raw_years != stored_years
            ):
                invalid_slot = True
                slot_reason = "KPI fiscal-year metadata unverified"
            elif (
                malformed_period
                or malformed_stored_period
                or len(raw_periods) > 1
                or raw_periods != stored_periods
            ):
                invalid_slot = True
                slot_reason = "KPI report-period metadata unverified"
        reason = None
        if observed is not None and observed > cutoff:
            reason = "KPI after cutoff"
        elif observed is None:
            reason = (
                "KPI observation date unverified"
                if raw
                else "legacy KPI observation date provenance unverified"
            )
        elif slot_reason is not None:
            reason = slot_reason
        elif fiscal_year is None:
            reason = "KPI fiscal-year metadata unavailable"
        elif fiscal_year > cutoff.year:
            reason = "KPI after cutoff"
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
                    "invalid_slot": invalid_slot,
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
        years, malformed_year = integer_aliases(raw, KPI_YEAR_ALIASES)
        report_periods, malformed_period = integer_aliases(raw, KPI_REPORT_PERIOD_ALIASES)
        year = next(iter(years)) if not malformed_year and len(years) == 1 else None
        report_period = (
            next(iter(report_periods))
            if not malformed_period and len(report_periods) == 1
            else None
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
                "invalid_slot": (
                    malformed_year or malformed_period or len(years) > 1 or len(report_periods) > 1
                ),
                "reason": row["reason"],
            }
        )

    selected_r12 = set(kpi_r12)
    for item in rejected:
        rejected_year = _fiscal_year(item["year"])
        rejected_period = item["report_period"]
        rejected_date = _date(item["observation_date"])
        invalid_slot = bool(item.get("invalid_slot"))
        current = not (
            (rejected_date is not None and rejected_date > cutoff)
            or (not invalid_slot and rejected_year is not None and rejected_year > cutoff.year)
            or item["reason"] == "KPI after cutoff"
        )
        if (
            current
            and not invalid_slot
            and item["period_type"] != "r12"
            and item["kpi_id"] in selected_r12
        ):
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
    if selected_input_ids is not None:
        selected_input_ids.update(row["id"] for row in selected_rows.values())
        selected_input_ids.update(item["id"] for item in rejected if isinstance(item["id"], int))
    return ({**kpi_annual, **kpi_r12}, rejected)


def _selection_refusal_reasons(selection: dict[str, Any]) -> list[str]:
    annual_history = selection.get("annual_history", {})
    reasons = list(annual_history.get("reasons", []))
    reasons.extend(selection.get("price", {}).get("reasons", []))
    reasons.extend(selection.get("valuation_refusals", []))
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
    if ends:
        future_checks.append(all(value > cutoff for value in ends))
    if publications:
        future_checks.append(all(value > cutoff for value in publications))
    if invalid_identity:
        return True
    if any(future_checks):
        return False

    is_annual = item.get("period_type") == "year"
    has_slot_identity = bool(ends or years) if is_annual else bool(ends or (years and periods))
    same_type_rows = [row for row in admitted_rows if row["period_type"] == item.get("period_type")]
    if ends:
        matching_rows = [row for row in same_type_rows if ends == {_verified_fiscal_end(row)}]
    elif is_annual and years:
        matching_rows = [row for row in same_type_rows if years == {_verified_fiscal_year(row)}]
    elif years and periods:
        matching_rows = [
            row
            for row in same_type_rows
            if years == {_verified_fiscal_year(row)} and periods == {_verified_report_period(row)}
        ]
    else:
        matching_rows = []
    superseded = has_slot_identity and len(matching_rows) == 1
    if superseded:
        return False
    if not is_annual:
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
            older_checks.append(all(value < latest_end for value in ends))
        if years and latest_year is not None:
            if all(value < latest_year for value in years):
                older_checks.append(True)
            elif years == {latest_year} and periods and latest_period is not None:
                older_checks.append(all(value < latest_period for value in periods))
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
        values = [raw[key] for key in PRICE_DATE_ALIASES if key in raw and raw[key] is not None]
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


def _adjusted_shares_with_splits(
    shares: float | None,
    period_end: str,
    comparison_date: str,
    split_rows,
):
    if shares is None:
        return None, ()
    start = date.fromisoformat(period_end[:10])
    end = date.fromisoformat(comparison_date[:10])
    if start >= end:
        return shares, ()
    applicable = tuple(
        row for row in split_rows if start < date.fromisoformat(str(row["split_date"])[:10]) <= end
    )
    if not applicable:
        return shares, applicable
    adjusted = adjust_historical_shares(
        shares,
        period_end,
        comparison_date,
        [(row["split_type"], row["ratio"], row["split_date"]) for row in applicable],
    )
    return adjusted, applicable


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
        currency=_currency_code(row["values_currency"]),
        original_currency=_currency_code(row["currency"]),
        conversion_mode=row["conversion_mode"],
        conversion_target_currency=_currency_code(row["conversion_target_currency"]),
        currency_ratio=_number(row["currency_ratio"]),
        company_id=int(row["company_id"]),
        period_type=str(row["period_type"]),
    )


def _price(row) -> StockPrice:
    price_date = _date(row["price_date"])
    if price_date is None:
        raise ValueError("stock price date unverified")
    return StockPrice(
        date=price_date,
        close=float(row["close"]),
        volume=int(row["volume"]) if row["volume"] is not None else None,
        currency=_currency_code(row["currency"]),
        company_id=int(row["company_id"]),
    )


def _valuation_currency_refusal(
    stock_price: StockPrice | None,
    report: Report | None,
    label: str,
) -> str | None:
    if report is None:
        return None
    report_currency = _currency_code(report.currency)
    price_currency = _currency_code(stock_price.currency) if stock_price else None
    if report_currency is None or price_currency is None:
        return f"{label} and stock price currencies are not both verified"
    if report_currency != price_currency:
        return (
            f"{label} values currency {report_currency} conflicts with "
            f"stock price currency {price_currency}"
        )
    return None


def load_results_for_company(
    conn,
    company_id: int,
    as_of: str,
    *,
    retained_research_evidence: dict | None = None,
    dcf_routing=None,
    artifact_store: Any | None = None,
) -> dict[str, Any]:
    """Select cutoff-filtered stored observations, not historical vintages."""
    cutoff = date.fromisoformat(as_of[:10])
    if retained_research_evidence is None:
        evidence_packet, selection_manifest = load_evidence_view(
            conn, company_id=company_id, as_of=as_of[:10], artifact_store=artifact_store
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
        research_evidence = {
            "documents": docs,
            "evidence_packet": evidence_packet,
            "evidence_manifest": selection_manifest.to_dict(),
            "evidence_lane": bool(evidence_packet),
        }
    else:
        # Historical audit context only; never used to authorize a new live analysis.
        research_evidence = retained_research_evidence
    packet = (
        research_evidence.get("evidence_packet") if isinstance(research_evidence, dict) else None
    )
    dcf_route_decision = decide_dcf_route(dcf_routing, evidence_packet=packet)
    dcf_route_payload = asdict(dcf_route_decision)
    company = conn.execute(
        "SELECT branch_id FROM companies WHERE id=?",
        (company_id,),
    ).fetchone()
    branch_id = int(company[0]) if company and company[0] is not None else None
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
        "report_denominations": [],
        "valuation_refusals": [],
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
        reason, denomination = report_selection_reason(row, cutoff)
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
                    "denomination": denomination,
                    "reason": reason,
                }
            )
        else:
            period_rows.append(row)
            selection["report_denominations"].append(
                {
                    "period_type": row["period_type"],
                    "period_end": row["period_end"],
                    **denomination,
                }
            )
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
    quality_valuation_rows = [row for row in period_rows if row["period_type"] == "r12"]
    quality_valuation_row = (
        quality_valuation_rows[-1]
        if quality_valuation_rows
        else (admitted_annuals[-1] if admitted_annuals else None)
    )
    input_quality_view = _dcf_input_quality_view(
        admitted_annuals,
        annual_period_rows,
        selection,
        quality_valuation_row,
    )
    input_quality_decision = DcfAssumptionPolicy.assess_input_quality(input_quality_view)
    normalized_financial_view = DcfNormalizedFinancialView(input_quality_view, None)
    input_quality_payload = {
        "view": asdict(input_quality_view),
        "decision": asdict(input_quality_decision),
    }
    selection["dcf_input_quality"] = input_quality_payload
    stored_price_rows = conn.execute(
        "SELECT * FROM prices WHERE company_id=? ORDER BY price_date ASC",
        (company_id,),
    ).fetchall()
    price_rows = []
    for row in stored_price_rows:
        raw = _payload(row)
        verified_date = verified_price_date(row)
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

    candidate_price = _price(price_rows[-1]) if price_rows else None
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
        _missing = ["financial_period", *price_missing, *selection["refusal_reasons"]]
        if selection["rejected_reports"]:
            _missing.append(
                "financial fiscal end/publication unavailable under verified-date selection"
            )
        _unavailable_dcf = serialize_dcf_result(
            {
                "available": False,
                "policy_version": DcfAssumptionPolicy.VERSION,
                "missing_information": _missing,
                "input_quality": input_quality_payload,
                "normalized_financial_view": asdict(normalized_financial_view),
                "routing": dcf_route_payload,
            },
            DcfResultMetadata(
                DcfResultStatus.INSUFFICIENT_EVIDENCE,
                _missing[0] if _missing else None,
                (),
                DcfAssumptionPolicy.VERSION,
            ),
        )
        _unavailable = {
            "status": "unavailable",
            "missing_information": _missing,
            "selection": selection,
            "dcf": _unavailable_dcf,
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
                "routing": dcf_route_decision,
                "policy": None,
                "value": None,
                "implied": {},
                "reverse_dcf": _unavailable,
            },
            "reverse_dcf": {**_unavailable, "routing": dcf_route_payload},
        }

    # Börsdata prices are split-adjusted but report share counts are not. Keep
    # the raw row in the database and adjust only historical calculation input
    # into the latest report's share basis.
    split_rows = conn.execute(
        "SELECT borsdata_id, split_type, ratio, split_date "
        "FROM stock_splits WHERE company_id=? ORDER BY split_date",
        (company_id,),
    ).fetchall()
    comparison_date = str(period_rows[-1]["period_end"])[:10]
    reports = []
    applied_splits_by_report = {}
    for row in period_rows:
        adjusted, applied_splits = _adjusted_shares_with_splits(
            _number(row["shares_outstanding"]),
            str(row["period_end"]),
            comparison_date,
            split_rows,
        )
        reports.append(_report(row, shares_override=adjusted))
        applied_splits_by_report[(row["period_type"], row["period_end"])] = applied_splits
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
    dcf_split_rows = (
        applied_splits_by_report.get(
            (dcf_current_report.period_type, dcf_current_report.period_end.isoformat()),
            (),
        )
        if dcf_current_report is not None and dcf_current_report.period_end is not None
        else ()
    )
    dcf_split_refs = tuple(
        EvidenceReference(
            source_id=(f"stock-split:borsdata-{row['borsdata_id']}:date-{row['split_date']}"),
            observed_on=str(row["split_date"]),
            anchor="shares outstanding adjustment event",
        )
        for row in dcf_split_rows
    )
    financial_mapper = FinancialMapper()
    annual_reports = []
    for row in annual_period_rows:
        shares, _ = _adjusted_shares_with_splits(
            _number(row["shares_outstanding"]),
            str(row["period_end"]),
            comparison_date,
            split_rows,
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
    current_currency_refusal = _valuation_currency_refusal(
        latest_price, current_report, "current report"
    )
    dcf_currency_refusal = _valuation_currency_refusal(
        latest_price, dcf_current_report, "DCF report"
    )
    for refusal in (current_currency_refusal, dcf_currency_refusal):
        if refusal is not None:
            selection["valuation_refusals"].append(refusal)
    current_raw = compute_raw_valuation(
        latest_price if current_currency_refusal is None else None,
        current_report,
    )
    dcf_raw = compute_raw_valuation(
        latest_price if dcf_currency_refusal is None else None,
        dcf_current_report,
    )

    historical_raw: list[RawValuation] = []
    for row, report in zip(period_rows[:-1], historical_reports, strict=False):
        candidates = [
            p for p in price_rows if str(p["price_date"])[:10] <= str(row["period_end"])[:10]
        ]
        paired = _price(candidates[-1]) if candidates else None
        age = (report.period_end - paired.date).days if paired else None
        if age is None or age > MAX_PRICE_AGE_DAYS:
            pairing_reason = "historical price missing or older than seven calendar days"
        else:
            pairing_reason = _valuation_currency_refusal(paired, report, "historical report")
        selection["historical_price_pairings"].append(
            {
                "period_end": row["period_end"],
                "price_date": paired.date.isoformat() if paired else None,
                "age_calendar_days": age,
                **_price_evidence(candidates[-1] if candidates else None),
                "reason": pairing_reason,
            }
        )
        if pairing_reason is None:
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
        latest_price.currency if latest_price else None,
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
        "price_currency": latest_price.currency if latest_price else None,
        "selection": selection,
        "financial_currency": dcf_current_report.currency
        if dcf_current_report is not None
        else None,
        "report_denomination": (
            {
                "original_currency": dcf_current_report.original_currency,
                "values_currency": dcf_current_report.currency,
                "conversion_mode": dcf_current_report.conversion_mode,
                "conversion_target_currency": dcf_current_report.conversion_target_currency,
                "currency_ratio": dcf_current_report.currency_ratio,
            }
            if dcf_current_report is not None
            else None
        ),
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
    dcf_failure_status = None
    reverse_dcf_results: dict[str, Any] = {}
    # Build annual report history for DCF policy (needs year property)
    try:
        # Use the same validated annual chronology for policy; R12/current
        # valuation remains distinct from the annual growth anchor.
        # Provider ROIC remains a percentage-point diagnostic; it no longer
        # supplies a naked future return. Qualified capital records own that input.
        roic_for_dcf = kpis.get(37)
        from alphaforge.core.valuation.dcf_policy import DcfPolicyDecision
        from alphaforge.core.valuation.reverse_dcf import ReverseDcfEngine, ReverseDcfInputs

        policy = DcfAssumptionPolicy()
        calibration_record, calibration_selection = _select_reinvestment_calibration(
            conn,
            company_id,
            cutoff,
            dcf_current_report.currency if dcf_current_report is not None else None,
        )
        selection["reinvestment_calibration"] = calibration_selection
        # market_cap from raw valuation is in report-currency millions (SEK MSEK)
        # because Börsdata reports and shares are in millions; required-return
        # buckets are in absolute SEK, so scale to SEK for the hurdle.
        market_cap_for_hurdle = None
        if dcf_raw.market_cap is not None:
            # Heuristic: shares are in millions (63.45 = 63M), so market cap in MSEK.
            # Convert to SEK for bucket selection.
            market_cap_for_hurdle = float(dcf_raw.market_cap) * 1_000_000
        if dcf_currency_refusal is not None:
            dcf_policy_decision = DcfPolicyDecision(
                available=False,
                policy_version=policy.VERSION,
                assumptions=None,
                solve_bounds=dict(policy.SOLVE_BOUNDS),
                assumption_sources={},
                missing_information=(dcf_currency_refusal,),
            )
        elif (
            dcf_current_report is not None
            and latest_annual is not None
            and dcf_current_report.currency != latest_annual.currency
        ):
            mismatch = "report denomination mismatch across DCF inputs"
            selection["valuation_refusals"].append(mismatch)
            dcf_policy_decision = DcfPolicyDecision(
                available=False,
                policy_version=policy.VERSION,
                assumptions=None,
                solve_bounds=dict(policy.SOLVE_BOUNDS),
                assumption_sources={},
                missing_information=(mismatch,),
            )
        else:
            dcf_policy_decision = policy.build(
                dcf_current_report,
                latest_annual,
                historical_annuals,
                as_of=cutoff,
                currency=(dcf_current_report.currency if dcf_current_report is not None else None),
                market_cap=market_cap_for_hurdle,
                market_price=latest_price,
                market_split_references=dcf_split_refs,
                roic=roic_for_dcf,
                calibration_record=calibration_record,
                input_quality=input_quality_view,
            )
            if (
                calibration_record is None
                and calibration_selection["candidates"]
                and dcf_policy_decision.missing_information == ("dated_positive_roic",)
            ):
                dcf_policy_decision = replace(
                    dcf_policy_decision,
                    missing_information=("admissible_reinvestment_calibration",),
                )
        route_engine_inputs_available = (
            not price_missing
            and current_net_debt is not None
            and dcf_current_report is not None
            and dcf_current_report.revenue is not None
            and dcf_current_report.revenue > 0
            and dcf_current_report.shares_outstanding is not None
            and dcf_current_report.shares_outstanding > 0
        )
        policy_available_before_routing = (
            dcf_policy_decision.available and dcf_policy_decision.assumptions is not None
        )
        if policy_available_before_routing and dcf_route_decision.status != "available":
            dcf_policy_decision = replace(
                dcf_policy_decision,
                available=False,
                assumptions=None,
                missing_information=(dcf_route_decision.reason,),
                warnings=dcf_policy_decision.warnings
                + ("DCF route is not supported by the supplied archetype/profile evidence",),
            )
        if policy_available_before_routing and (
            dcf_policy_decision.available or not route_engine_inputs_available
        ):
            if price_missing:
                reverse_dcf["status"] = "unavailable"
            elif current_net_debt is None:
                reverse_dcf["dcf_error"] = (
                    "net debt unavailable; DCF enterprise-to-equity bridge not valued"
                )
                reverse_dcf["dcf"] = {
                    "available": False,
                    "policy_version": dcf_policy_decision.policy_version,
                    "normalization": _normalization_payload(dcf_policy_decision.normalization),
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
                    eligibility_context_facts = {
                        "company_id": company_id,
                        "packet_hash": packet.get("packet_hash")
                        if isinstance(packet, dict)
                        else None,
                        "route_decision_identity": dcf_route_decision.decision_identity,
                        "route_input_identity": dcf_route_decision.input_identity,
                        "route_evidence_identity": dcf_route_decision.evidence_identity,
                        "assumption_provenance": {
                            name: asdict(record)
                            for name, record in sorted(
                                dcf_policy_decision.assumption_provenance.items()
                            )
                        },
                    }
                    eligibility_context_identity = sha256(
                        json.dumps(
                            eligibility_context_facts,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                        ).encode("utf-8")
                    ).hexdigest()
                    dcf_inputs = ReverseDcfInputs(
                        current_price=latest_price.close,
                        shares_outstanding=dcf_current_report.shares_outstanding,
                        current_revenue=dcf_current_report.revenue,
                        net_debt=float(current_net_debt),
                        assumptions=dcf_policy_decision.assumptions,
                        branch_id=branch_id,
                        eligibility_context_identity=eligibility_context_identity,
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
                            "revenue_growth_fade_to": dcf_policy_decision.assumptions.revenue_growth_fade_to,
                            "net_reinvestment_rate": dcf_policy_decision.assumptions.net_reinvestment_rate,
                            "reinvestment_return": dcf_policy_decision.assumptions.reinvestment_return,
                            "ebit_margin_start": dcf_policy_decision.assumptions.ebit_margin_start,
                            "economic_convention": dcf_policy_decision.assumptions.economic_convention,
                            "calibration_identity": dcf_policy_decision.assumptions.calibration_identity,
                        },
                        "assumption_sources": dcf_policy_decision.assumption_sources,
                        "calibration": {
                            "identity": dcf_policy_decision.calibration.identity,
                            "historical_average_roic": dcf_policy_decision.calibration.historical_average_roic,
                            "future_incremental_return": dcf_policy_decision.calibration.future_incremental_return,
                            "record": json.loads(dcf_policy_decision.calibration.record_json),
                        },
                        "required_return": {
                            "policy_version": dcf_policy_decision.required_return.policy_version,
                            "market_cap": dcf_policy_decision.required_return.market_cap,
                            "size_bucket": dcf_policy_decision.required_return.size_bucket,
                            "required_return": dcf_policy_decision.required_return.required_return,
                            "source_date": dcf_policy_decision.required_return.source_date,
                            "basis": "discount_rate_proxy_for_cost_of_capital",
                        }
                        if dcf_policy_decision.required_return
                        else None,
                        "enterprise_value": dcf_value.enterprise_value,
                        "equity_value": dcf_value.equity_value,
                        "value_per_share": dcf_value.value_per_share,
                        "terminal_cash_flow": asdict(dcf_value.terminal_cash_flow),
                        "terminal_pole_distance": dcf_value.terminal_pole_distance,
                        "terminal_value": dcf_value.terminal_value,
                        "discounted_terminal_value": dcf_value.discounted_terminal_value,
                        "terminal_value_share_of_enterprise_value": (
                            dcf_value.discounted_terminal_value / dcf_value.enterprise_value
                            if dcf_value.enterprise_value != 0
                            else None
                        ),
                        **_equity_qualification(dcf_value.equity_value),
                        "projected_cash_flows": [asdict(p) for p in dcf_value.projected_cash_flows],
                        "normalization": _normalization_payload(dcf_policy_decision.normalization),
                        "warnings": list(dcf_policy_decision.warnings)
                        if dcf_policy_decision.warnings
                        else [],
                        "missing_information": list(dcf_policy_decision.missing_information),
                    }
                    # All admission evidence is frozen and checked before any solve sampling.
                    quality_decision = policy.assess_input_quality(input_quality_view)
                    provenance = dcf_policy_decision.assumption_provenance
                    report_references = tuple(
                        reference
                        for name in ("revenue_growth", "ebit_margin", "ebit_margin_start")
                        for reference in getattr(provenance.get(name), "evidence_references", ())
                    )
                    calibration_references = tuple(
                        getattr(provenance.get("reinvestment_return"), "evidence_references", ())
                    )
                    fixed_provenance_complete = fixed_assumption_provenance_complete(
                        dcf_policy_decision.assumptions, "revenue_growth", provenance
                    )
                    prerequisite_evidence = {
                        "explicit_mature_operating_route": {
                            "status": "met"
                            if dcf_route_decision.status == "available"
                            else "unmet",
                            "reason": dcf_route_decision.reason,
                            "evidence_references": dcf_route_decision.evidence_references,
                        },
                        "qualified_consecutive_annual_history": {
                            "status": "met" if quality_decision.available else "unmet",
                            "reason": next(iter(quality_decision.reasons), None),
                            "evidence_references": report_references,
                        },
                        "qualified_reinvestment_calibration": {
                            "status": "met"
                            if dcf_policy_decision.calibration is not None
                            and calibration_references
                            else "unmet",
                            "reason": None
                            if dcf_policy_decision.calibration is not None
                            and calibration_references
                            else "admissible_reinvestment_calibration",
                            "evidence_references": calibration_references,
                        },
                        "fixed_assumptions_with_provenance": {
                            "status": "met" if fixed_provenance_complete else "unmet",
                            "reason": None
                            if fixed_provenance_complete
                            else "fixed assumption provenance incomplete",
                            "evidence_references": (),
                        },
                    }
                    # The registry supplies the declared full domains and every axis decision.
                    for _assump in SOLVE_AXIS_REGISTRY:
                        _eligibility = solve_axis_metadata(
                            _assump,
                            assumptions=dcf_policy_decision.assumptions,
                            assumption_provenance=provenance,
                            prerequisite_evidence=prerequisite_evidence,
                            inputs=dcf_inputs,
                        )
                        reverse_dcf_results[_assump] = _solve_axis_result(
                            engine, dcf_inputs, _assump, _eligibility
                        )
                    reverse_dcf["implied"] = reverse_dcf_results
                    reverse_dcf["status"] = "available"
                except Exception as exc:
                    dcf_failure_status = _dcf_exception_status(exc)
                    reverse_dcf["dcf_error"] = str(exc)
                    reverse_dcf["dcf"] = {
                        "available": False,
                        "policy_version": dcf_policy_decision.policy_version,
                        "normalization": _normalization_payload(dcf_policy_decision.normalization),
                        "missing_information": [getattr(exc, "reason", "dcf_engine_failed")],
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
                    "normalization": _normalization_payload(dcf_policy_decision.normalization),
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
                    "normalization": _normalization_payload(dcf_policy_decision.normalization),
                    "missing_information": list(dcf_policy_decision.missing_information),
                    "unavailable_reason": _dcf_unavailable_reason(
                        dcf_policy_decision.missing_information
                    ),
                    "warnings": list(dcf_policy_decision.warnings)
                    if dcf_policy_decision.warnings
                    else [],
                }
                reverse_dcf["status"] = "unavailable"
    except Exception as exc:
        dcf_failure_status = _dcf_exception_status(exc)
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
                "normalization": _normalization_payload(
                    decision.normalization if decision is not None else None
                ),
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
            "normalization": _normalization_payload(
                dcf_policy_decision.normalization if dcf_policy_decision else None
            ),
            "missing_information": price_missing,
            "warnings": [],
        }
    normalized_financial_view = DcfNormalizedFinancialView(
        input_quality_view,
        dcf_policy_decision.normalization if dcf_policy_decision is not None else None,
    )
    input_quality_payload = {
        "view": asdict(input_quality_view),
        "decision": asdict(input_quality_decision),
    }
    selection["dcf_input_quality"] = input_quality_payload
    reverse_dcf["routing"] = dcf_route_payload
    dcf_result = reverse_dcf.get("dcf")
    if isinstance(dcf_result, dict):
        dcf_result["routing"] = dcf_route_payload
        dcf_result["input_quality"] = input_quality_payload
        dcf_result["normalized_financial_view"] = asdict(normalized_financial_view)
        missing = dcf_result.get("missing_information") or ()
        failure_reason = next(iter(missing), None)
        if dcf_result.get("available"):
            result_status = DcfResultStatus.AVAILABLE
            result_reason = None
        else:
            result_reason = (
                reverse_dcf.get("dcf_error") or failure_reason
                if dcf_failure_status is not None
                else failure_reason or reverse_dcf.get("dcf_error")
            )
            result_status = dcf_failure_status or _dcf_failure_status(result_reason)
        decision_warnings = tuple(
            dcf_result.get("warnings")
            or (dcf_policy_decision.warnings if dcf_policy_decision is not None else ())
        )
        result_version = dcf_result.get("policy_version") or (
            dcf_policy_decision.policy_version if dcf_policy_decision is not None else None
        )
        reverse_dcf["dcf"] = serialize_dcf_result(
            dcf_result,
            DcfResultMetadata(result_status, result_reason, decision_warnings, result_version),
            dcf_policy_decision.assumption_provenance
            if dcf_result.get("available") and dcf_policy_decision is not None
            else {},
        )
    for solve_name, solve_result in reverse_dcf_results.items():
        result_status = _dcf_solve_status(
            solve_result.get("solution_status"),
            solve_result.get("reason"),
            solve_result.get("error"),
        )
        solve_result_version = (
            dcf_policy_decision.policy_version if dcf_policy_decision is not None else None
        )
        solve_reason = solve_result.get("reason") or solve_result.get("error")
        if result_status in {
            DcfResultStatus.INVALID_INPUT,
            DcfResultStatus.NO_CROSSING,
            DcfResultStatus.NONCONVERGENCE,
        }:
            solve_reason = solve_result.get("error") or solve_reason
        reverse_dcf_results[solve_name] = serialize_dcf_result(
            solve_result,
            DcfResultMetadata(
                result_status,
                solve_reason,
                tuple(dcf_policy_decision.warnings) if dcf_policy_decision is not None else (),
                solve_result_version,
            ),
        )
    # Provide DCF artefacts at top level so callers can export them without
    # reaching into candidate.full_results, and keep provenance separate from
    # the heuristic valuation_score.
    dcf_payload = {
        "routing": dcf_route_decision,
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
