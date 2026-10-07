"""Price-independent funding certificate for the existing five-year mature path.

This proves real-arithmetic funding inequalities, not machine evaluation safety,
root completeness, or the truth of an analyst's installed-capacity interpretation.
"""

import json
from datetime import date
from fractions import Fraction
from hashlib import sha256
from math import inf, isfinite, nextafter

from alphaforge.core.valuation.reinvestment import (
    ECONOMIC_CONVENTION,
    calibration_identity,
    qualify_calibration,
)

GROWTH_DOMAIN_POLICY_VERSION = "reverse-growth-domain-v1"
GROWTH_SCOPE = (0.0, 0.30)
SAMPLE_INTERVALS = 200
PRICE_TOLERANCE = 1e-6
ASSUMPTION_TOLERANCE = 1e-10
MAX_ITERATIONS = 200


def domain_digest(value) -> str:
    def encode(item):
        if isinstance(item, date):
            return item.isoformat()
        raise TypeError("unsupported domain identity operand")

    return sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=encode
        ).encode()
    ).hexdigest()


def growth_history_identity(quality: dict, annual_operands: list) -> str:
    # Rejection quality evidence ids may be source DB surrogate ids, which capture
    # renumbers. Economic operand/source facts and all quality reasons remain bound.
    quality = dict(quality)
    for key in ("selected_periods", "excluded_periods", "evidenced_anomalies"):
        quality[key] = [
            {k: v for k, v in item.items() if k != "evidence_id"} for item in quality[key]
        ]
    if quality.get("valuation_period"):
        quality["valuation_period"] = {
            k: v for k, v in quality["valuation_period"].items() if k != "evidence_id"
        }
    return domain_digest({"quality": quality, "annual_operands": annual_operands})


def growth_fixed_basis(inputs) -> dict:
    """Bind analyst coverage without a circular calibration hash or target price."""
    context = inputs.growth_domain_context if isinstance(inputs.growth_domain_context, dict) else {}
    record = context.get("calibration_record")
    record = record if isinstance(record, dict) else {}
    a = inputs.assumptions
    return {
        "company_id": context.get("company_id"),
        "currency": context.get("currency"),
        "units": "millions",
        "current_revenue": inputs.current_revenue,
        "ebit_margin": a.ebit_margin,
        "tax_rate": a.tax_rate,
        "projection_years": a.projection_years,
        "terminal_growth": a.terminal_growth,
        "discount_rate": a.discount_rate,
        "reinvestment_return": a.reinvestment_return,
        "economic_convention": a.economic_convention,
        "packet_hash": context.get("packet_hash"),
        "route_decision_identity": context.get("route_decision_identity"),
        "selected_history_identity": context.get("selected_history_identity"),
        "calibration_operands_identity": calibration_identity(
            {key: value for key, value in record.items() if key != "reverse_growth_coverage"}
        ),
    }


def growth_coverage_error(inputs) -> str | None:
    """Check frozen review structure and basis, never manufacture an assertion."""
    context = inputs.growth_domain_context
    if not isinstance(context, dict):
        return "full_interval_starting_capital_coverage_unavailable"
    record = context.get("calibration_record")
    if not isinstance(record, dict):
        return "full_interval_starting_capital_coverage_unavailable"
    coverage = record.get("reverse_growth_coverage")
    if not isinstance(coverage, dict):
        return "full_interval_starting_capital_coverage_unavailable"
    try:
        cutoff = date.fromisoformat(context["as_of"])
        qualified = qualify_calibration(
            record, as_of=cutoff, currency=context["currency"], tax_rate=inputs.assumptions.tax_rate
        )
        if (
            qualified.identity != inputs.assumptions.calibration_identity
            or qualified.future_incremental_return != inputs.assumptions.reinvestment_return
            or record["company_id"] != context["company_id"]
        ):
            return "reverse_growth_coverage_basis_mismatch"
        expected = growth_fixed_basis(inputs)
        if domain_digest(coverage.get("fixed_basis")) != domain_digest(expected):
            return "reverse_growth_coverage_basis_mismatch"
        if any(
            not isinstance(expected[key], str) or not expected[key].strip()
            for key in ("packet_hash", "route_decision_identity", "selected_history_identity")
        ):
            return "reverse_growth_coverage_basis_unavailable"
        declarations = {
            "version": GROWTH_DOMAIN_POLICY_VERSION,
            "scope": "full_derived_domain",
            "capital_timing": "year_one_installed",
            "constant_margin_and_return_path": "conditional_for_full_derived_domain",
        }
        if any(coverage.get(key) != value for key, value in declarations.items()):
            return "reverse_growth_coverage_declaration_invalid"
        if any(
            not isinstance(coverage.get(key), str) or not coverage[key].strip()
            for key in ("starting_capacity_rationale", "approval_id")
        ):
            return "reverse_growth_coverage_review_unavailable"
        if date.fromisoformat(coverage["approved_on"]) > cutoff:
            return "reverse_growth_coverage_unavailable_at_cutoff"
    except (KeyError, ValueError, TypeError, OverflowError):
        return "reverse_growth_coverage_invalid"
    return None


def _exact(value: Fraction) -> dict:
    return {"numerator": str(value.numerator), "denominator": str(value.denominator)}


def derive_growth_domain(inputs, fixed_assumptions: dict | None = None) -> dict:
    """Intersect every affine funding constraint before any price evaluation."""
    a = inputs.assumptions
    operands = (
        a.reinvestment_return,
        a.discount_rate,
        a.terminal_growth,
        a.ebit_margin,
        a.tax_rate,
    )
    if (
        type(a.projection_years) is not int
        or a.projection_years != 5
        or a.economic_convention != ECONOMIC_CONVENTION
        or any(
            isinstance(x, bool) or not isinstance(x, (float, int)) or not isfinite(x)
            for x in operands
        )
        or a.reinvestment_return <= 0
        or a.discount_rate <= 0
        or a.ebit_margin <= 0
        or not 0 <= a.tax_rate < 1
    ):
        raise ValueError("invalid_growth_domain_inputs")
    if a.terminal_growth < 0:
        raise ValueError("unsupported_capital_release")
    if a.discount_rate <= a.terminal_growth:
        raise ValueError("discount_rate must exceed terminal_growth")
    if a.ebit_margin_start not in (None, a.ebit_margin):
        raise ValueError("varying_margin_capital_evidence_unavailable")
    if a.revenue_growth_fade_to != a.terminal_growth:
        raise ValueError("explicit growth endpoint must equal terminal growth")
    q, r, t = map(Fraction, operands[:3])
    funding_returns = [q + (r - q) * Fraction(index, 4) for index in range(4)] + [r, r]
    limits = [
        (funding_returns[index] - t * Fraction(index + 1, 4)) / (1 - Fraction(index + 1, 4))
        for index in range(3)
    ]
    upper = min(Fraction(GROWTH_SCOPE[1]), *limits)
    fixed_pass = t <= funding_returns[3]
    reason = (
        "unsupported_financing"
        if not fixed_pass
        else "empty_admissible_domain"
        if upper < 0
        else "degenerate_admissible_domain"
        if upper == 0
        else None
    )
    executable = float(upper)
    if Fraction(executable) > upper:
        executable = nextafter(executable, -inf)
    if reason is None and executable <= 0:
        reason = "numerically_unresolvable_domain"
    constraints = [
        {
            "funding_year": index + 1,
            "next_growth_x_coefficient": _exact(1 - Fraction(index + 1, 4)),
            "next_growth_constant": _exact(t * Fraction(index + 1, 4)),
            "incremental_return": _exact(funding_returns[index]),
            "upper_limit": _exact(limit),
        }
        for index, limit in enumerate(limits)
    ]
    constraints += [
        {
            "funding_year": year,
            "next_growth_constant": _exact(t),
            "incremental_return": _exact(funding_returns[year - 1]),
            "satisfied": t <= funding_returns[year - 1],
        }
        for year in (4, 5, 6)
    ]
    context = inputs.growth_domain_context if isinstance(inputs.growth_domain_context, dict) else {}
    record = context.get("calibration_record")
    record = record if isinstance(record, dict) else {}
    certificate = {
        "policy_version": GROWTH_DOMAIN_POLICY_VERSION,
        "scope_bounds": list(GROWTH_SCOPE),
        "fixed_basis": growth_fixed_basis(inputs),
        "fixed_assumptions_identity": domain_digest(fixed_assumptions or {}),
        "economic_bounds": [_exact(Fraction(0)), _exact(upper)]
        if fixed_pass and upper >= 0
        else None,
        "constraints": constraints,
        "binding_constraints": (["scope_ceiling"] if upper == Fraction(GROWTH_SCOPE[1]) else [])
        + [f"funding_year_{index + 1}" for index, limit in enumerate(limits) if limit == upper],
        "endpoint_conversion": "exact rational binary operands; directed inward float conversion",
        "interval_validation": "analytic_real_economics",
        "coverage_approval": record.get("reverse_growth_coverage"),
    }
    return {
        **certificate,
        "certificate_identity": domain_digest(certificate),
        "lower_bound": 0.0 if fixed_pass and upper >= 0 else None,
        "upper_bound": executable if fixed_pass and upper >= 0 else None,
        "reason": reason,
        "bounds_inclusive": True,
        "candidate_policy": "sample the full requested interval; any invalid evaluated candidate refuses the axis",
        "numerical_qualification": "real funding certificate only; machine safety and root completeness are not established",
    }
