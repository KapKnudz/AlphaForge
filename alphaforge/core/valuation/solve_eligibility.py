"""Single registry for reverse-DCF solve eligibility and interpretation."""

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import Any

from alphaforge.core.valuation.dcf_contract import AssumptionOrigin

SOLVE_REGISTRY_VERSION = "reverse-dcf-solve-registry-v2"

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
            lower_bound=-0.10,
            upper_bound=0.30,
            status="supported",
            reason=None,
            evidence_prerequisites=(
                "explicit_mature_operating_route",
                "qualified_consecutive_annual_history",
                "qualified_reinvestment_calibration",
                "fixed_assumptions_with_provenance",
            ),
            root_interpretation=(
                "initial revenue growth fades linearly to the fixed mature endpoint; sampled "
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
            if references or not any(
                isinstance(limitation, str) and limitation.strip() for limitation in limitations
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


def solve_input_identity(
    inputs: Any,
    fixed_assumptions: Mapping[str, Any] | None = None,
    evidence_prerequisites: tuple[dict[str, Any], ...] | None = None,
) -> str | None:
    """Bind eligibility to frozen evidence, numerical inputs, and its exported evidence record."""
    context_identity = getattr(inputs, "eligibility_context_identity", None)
    assumptions = getattr(inputs, "assumptions", None)
    if not isinstance(context_identity, str) or not context_identity.strip() or assumptions is None:
        return None
    payload = {
        "eligibility_context_identity": context_identity,
        "current_price": inputs.current_price,
        "shares_outstanding": inputs.shares_outstanding,
        "current_revenue": inputs.current_revenue,
        "net_debt": inputs.net_debt,
        "branch_id": inputs.branch_id,
        "assumptions": asdict(assumptions) if is_dataclass(assumptions) else dict(assumptions),
        "fixed_assumptions": fixed_assumptions,
        "evidence_prerequisites": evidence_prerequisites,
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
    if status == "supported" and prerequisite_evidence is not None:
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
        if unmet is not None:
            status = "insufficient_evidence"
            reason = unmet["reason"] or f"solve_evidence_unavailable:{unmet['name']}"

    return SolveAxisMetadata(
        registry_version=SOLVE_REGISTRY_VERSION,
        axis=axis,
        domain={
            "lower_bound": definition.lower_bound,
            "upper_bound": definition.upper_bound,
            "bounds_inclusive": True,
            "candidate_policy": (
                "sample the full declared range; any invalid sampled candidate refuses the axis"
            ),
        },
        status=status,
        reason=reason,
        evidence_prerequisites=prerequisites,
        fixed_assumptions=fixed,
        root_interpretation=definition.root_interpretation,
        input_identity=(
            solve_input_identity(inputs, fixed, prerequisites) if inputs is not None else None
        ),
    )
