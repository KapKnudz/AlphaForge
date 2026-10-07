"""Single registry for reverse-DCF solve eligibility and interpretation."""

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass, replace
from hashlib import sha256
from types import MappingProxyType
from typing import Any

from alphaforge.core.valuation.dcf_contract import (
    FIXED_DEFAULT_ASSUMPTION_POLICY,
    AssumptionOrigin,
)
from alphaforge.core.valuation.growth_domain import (
    GROWTH_SCOPE,
    derive_growth_domain,
    growth_coverage_error,
)

SOLVE_REGISTRY_VERSION = "reverse-dcf-solve-registry-v3-admissible-growth"

# Validate the existing DcfAssumptionPolicy provenance contract; this does not acquire evidence.
_ASSUMPTION_ORIGIN_CONTRACT = {
    "projection_years": {AssumptionOrigin.FIXED_DEFAULT.value},
    "revenue_growth": {
        AssumptionOrigin.COMPANY_HISTORY.value,
        AssumptionOrigin.FIXED_DEFAULT.value,
    },
    "ebit_margin": {AssumptionOrigin.REPORT_EVIDENCE.value},
    "tax_rate": {AssumptionOrigin.FIXED_DEFAULT.value},
    "discount_rate": {AssumptionOrigin.MARKET_EVIDENCE.value},
    "terminal_growth": {AssumptionOrigin.FIXED_DEFAULT.value},
    "net_reinvestment_rate": {AssumptionOrigin.FIXED_DEFAULT.value},
    "reinvestment_return": {AssumptionOrigin.QUALIFIED_CALIBRATION.value},
    "revenue_growth_fade_to": {AssumptionOrigin.FIXED_DEFAULT.value},
    "ebit_margin_start": {AssumptionOrigin.REPORT_EVIDENCE.value},
    "economic_convention": {AssumptionOrigin.FIXED_DEFAULT.value},
    "calibration_identity": {AssumptionOrigin.QUALIFIED_CALIBRATION.value},
}


@dataclass(frozen=True)
class SolveAxisDefinition:
    axis: str
    lower_bound: float
    upper_bound: float
    status: str
    reason: str | None
    evidence_prerequisites: tuple[str, ...]
    root_interpretation: str


@dataclass(frozen=True)
class SolveAxisMetadata:
    registry_version: str
    axis: str
    domain: dict[str, Any]
    status: str
    reason: str | None
    evidence_prerequisites: tuple[dict[str, Any], ...]
    fixed_assumptions: dict[str, Any]
    root_interpretation: str
    input_identity: str | None = None


SOLVE_AXIS_REGISTRY = MappingProxyType(
    {
        "revenue_growth": SolveAxisDefinition(
            axis="revenue_growth",
            lower_bound=GROWTH_SCOPE[0],
            upper_bound=GROWTH_SCOPE[1],
            status="supported",
            reason=None,
            evidence_prerequisites=(
                "explicit_mature_operating_route",
                "qualified_consecutive_annual_history",
                "qualified_reinvestment_calibration",
                "fixed_assumptions_with_provenance",
                "full_interval_starting_capital_coverage",
            ),
            root_interpretation=(
                "initial revenue growth fades linearly to the fixed mature endpoint; funded bounds "
                "are derived before candidate prices, conditional on reviewed installed year-one "
                "capacity and fixed margin/return coverage for the entire interval; sampled "
                "conditional candidates do not establish uniqueness or completeness"
            ),
        ),
        "ebit_margin": SolveAxisDefinition(
            axis="ebit_margin",
            lower_bound=0.0,
            upper_bound=0.50,
            status="unsupported",
            reason="unavailable_constant_margin_only",
            evidence_prerequisites=("constant_margin_capital_basis",),
            root_interpretation="axis refused before sampling; no root or value is available",
        ),
        "terminal_growth": SolveAxisDefinition(
            axis="terminal_growth",
            lower_bound=-0.01,
            upper_bound=0.04,
            status="not_identifiable",
            reason="not_identifiable",
            evidence_prerequisites=("terminal_transition_identifiability",),
            root_interpretation=(
                "mature returns equal the hurdle; terminal growth alone is not identifiable. A linked "
                "pre-convergence transition can still change value, so the full forecast is not invariant"
            ),
        ),
    }
)


def solve_axis_definition(axis: str) -> SolveAxisDefinition:
    try:
        return SOLVE_AXIS_REGISTRY[axis]
    except KeyError as exc:
        raise ValueError(f"unsupported implied assumption: {axis}") from exc


def _mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return None


def fixed_assumption_provenance_complete(
    assumptions: Any, axis: str | None, assumption_provenance: Mapping[str, Any] | None
) -> bool:
    """Validate existing assumption origins without inventing references for policy defaults."""
    if assumptions is None or not isinstance(assumption_provenance, Mapping):
        return False
    values = asdict(assumptions) if is_dataclass(assumptions) else dict(assumptions)
    valid_origins = {origin.value for origin in AssumptionOrigin}
    for name in values:
        if name == axis:
            continue
        record = _mapping(assumption_provenance.get(name))
        if record is None:
            return False
        origin = getattr(record.get("origin"), "value", record.get("origin"))
        source = record.get("source")
        references = record.get("evidence_references", ())
        limitations = record.get("limitations", ())
        if (
            not isinstance(origin, str)
            or origin not in valid_origins
            or origin not in _ASSUMPTION_ORIGIN_CONTRACT.get(name, set())
            or not isinstance(source, str)
            or not source.strip()
            or not isinstance(references, (tuple, list))
            or not isinstance(limitations, (tuple, list))
        ):
            return False
        if origin == AssumptionOrigin.FIXED_DEFAULT.value:
            policy_default = FIXED_DEFAULT_ASSUMPTION_POLICY.get(name)
            assumption_value = values[name]
            assumption_record = _mapping(assumption_value)
            if assumption_record is not None and "value" in assumption_record:
                assumption_value = assumption_record["value"]
            if (
                policy_default is None
                or assumption_value != policy_default[0]
                or source != policy_default[1]
                or references
                or not any(
                    isinstance(limitation, str) and limitation.strip() for limitation in limitations
                )
            ):
                return False
            continue
        if not references:
            return False
        for reference in references:
            reference_value = _mapping(reference)
            if reference_value is None or any(
                not isinstance(reference_value.get(field), str)
                or not reference_value[field].strip()
                for field in ("source_id", "anchor")
            ):
                return False
    return True


def _identity_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _identity_value(asdict(value))
    if isinstance(value, Mapping):
        return {key: _identity_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_identity_value(item) for item in value]
    if isinstance(value, AssumptionOrigin):
        return value.value
    return value


def solve_input_identity(inputs: Any, metadata: SolveAxisMetadata) -> str | None:
    """Bind eligibility to frozen inputs and the complete exported eligibility record."""
    context_identity = getattr(inputs, "eligibility_context_identity", None)
    assumptions = getattr(inputs, "assumptions", None)
    if not isinstance(context_identity, str) or not context_identity.strip() or assumptions is None:
        return None
    eligibility_record = asdict(metadata)
    eligibility_record.pop("input_identity")
    payload = {
        "eligibility_context_identity": context_identity,
        "current_price": inputs.current_price,
        "shares_outstanding": inputs.shares_outstanding,
        "current_revenue": inputs.current_revenue,
        "net_debt": inputs.net_debt,
        "branch_id": inputs.branch_id,
        "assumptions": asdict(assumptions) if is_dataclass(assumptions) else dict(assumptions),
        "growth_domain_context": getattr(inputs, "growth_domain_context", None),
        "eligibility": eligibility_record,
    }
    encoded = json.dumps(
        _identity_value(payload), sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return sha256(encoded.encode("utf-8")).hexdigest()


def solve_axis_metadata(
    axis: str,
    *,
    assumptions: Any = None,
    assumption_provenance: Mapping[str, Any] | None = None,
    prerequisite_evidence: Mapping[str, Mapping[str, Any]] | None = None,
    inputs: Any = None,
    requested_bounds: tuple[float, float] | None = None,
) -> SolveAxisMetadata:
    """Build the exported preflight record from the same axis declaration the solver uses."""
    definition = solve_axis_definition(axis)
    if inputs is not None:
        if assumptions is not None and assumptions != inputs.assumptions:
            raise ValueError("solve eligibility assumptions do not match inputs")
        assumptions = inputs.assumptions
    values = (
        asdict(assumptions)
        if assumptions is not None and is_dataclass(assumptions)
        else dict(assumptions or {})
    )
    provenance = assumption_provenance or {}
    fixed = {}
    for name, value in values.items():
        if name == axis:
            continue
        source = provenance.get(name)
        if source is None:
            fixed[name] = {"value": value, "evidence_references": (), "limitations": ()}
            continue
        source_value = _mapping(source) or {}
        fixed[name] = {
            "value": value,
            "origin": source_value.get("origin"),
            "source": source_value.get("source"),
            "evidence_references": source_value.get("evidence_references", ()),
            "limitations": source_value.get("limitations", ()),
        }

    fixed_provenance_complete = fixed_assumption_provenance_complete(
        assumptions, axis, assumption_provenance
    )
    supplied_prerequisites = prerequisite_evidence or {}
    prerequisites_list = []
    for name in definition.evidence_prerequisites:
        supplied = supplied_prerequisites.get(name, {})
        status = supplied.get("status", "not_verified")
        reason = supplied.get("reason")
        if name == "fixed_assumptions_with_provenance" and not fixed_provenance_complete:
            status = "unmet"
            reason = reason or "fixed_assumption_provenance_incomplete"
        if name == "full_interval_starting_capital_coverage":
            reason = (
                growth_coverage_error(inputs)
                if inputs is not None
                else "full_interval_starting_capital_coverage_unavailable"
            )
            status = "unmet" if reason else "met"
            supplied = {
                "evidence_references": supplied_prerequisites.get(
                    "qualified_reinvestment_calibration", {}
                ).get("evidence_references", ()),
            }
        prerequisites_list.append(
            {
                "name": name,
                "status": status,
                "reason": reason,
                "evidence_references": supplied.get("evidence_references", ()),
            }
        )
    prerequisites = tuple(prerequisites_list)

    status, reason = definition.status, definition.reason
    unmet = next(
        (
            item
            for item in prerequisites
            if item["status"] != "met"
            or not item["evidence_references"]
            and item["name"] != "fixed_assumptions_with_provenance"
        ),
        None,
    )
    if status == "supported" and unmet is not None:
        status = "insufficient_evidence"
        reason = unmet["reason"] or f"solve_evidence_unavailable:{unmet['name']}"

    domain = {
        "lower_bound": definition.lower_bound,
        "upper_bound": definition.upper_bound,
        "bounds_inclusive": True,
        "candidate_policy": "axis refused before sampling",
    }
    if axis == "revenue_growth":
        domain = {"scope_bounds": list(GROWTH_SCOPE), "lower_bound": None, "upper_bound": None}
        coverage = next(
            item
            for item in prerequisites
            if item["name"] == "full_interval_starting_capital_coverage"
        )
        ordinary_unmet = next(
            (
                item
                for item in prerequisites
                if item["name"] != "full_interval_starting_capital_coverage"
                and (
                    item["status"] != "met"
                    or not item["evidence_references"]
                    and item["name"] != "fixed_assumptions_with_provenance"
                )
            ),
            None,
        )
        if definition.status == "supported" and ordinary_unmet is not None:
            status = "insufficient_evidence"
            reason = ordinary_unmet["reason"] or (
                f"solve_evidence_unavailable:{ordinary_unmet['name']}"
            )
        elif inputs is not None and fixed_provenance_complete:
            try:
                domain = derive_growth_domain(inputs, fixed)
                if domain["reason"]:
                    status, reason = "domain_unavailable", domain["reason"]
                elif coverage["status"] != "met" or not coverage["evidence_references"]:
                    status = "insufficient_evidence"
                    reason = coverage["reason"] or (
                        "solve_evidence_unavailable:full_interval_starting_capital_coverage"
                    )
                else:
                    status, reason = definition.status, definition.reason
            except ValueError as exc:
                status, reason = "invalid_input", str(exc)
        request = (
            list(requested_bounds)
            if requested_bounds is not None
            else [domain["lower_bound"], domain["upper_bound"]]
        )
        domain["requested_bounds"] = request
        domain["request_coverage"] = (
            "full_derived_domain"
            if request == [domain["lower_bound"], domain["upper_bound"]]
            else "restricted_interval"
        )

    metadata = SolveAxisMetadata(
        registry_version=SOLVE_REGISTRY_VERSION,
        axis=axis,
        domain=domain,
        status=status,
        reason=reason,
        evidence_prerequisites=prerequisites,
        fixed_assumptions=fixed,
        root_interpretation=definition.root_interpretation,
    )
    if inputs is None:
        return metadata
    return replace(metadata, input_identity=solve_input_identity(inputs, metadata))
