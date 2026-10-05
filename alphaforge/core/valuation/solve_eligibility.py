"""Single registry for reverse-DCF solve eligibility and interpretation."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any

SOLVE_REGISTRY_VERSION = "reverse-dcf-solve-registry-v1"


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


def solve_axis_metadata(
    axis: str,
    *,
    assumptions: Any = None,
    assumption_provenance: Mapping[str, Any] | None = None,
    prerequisite_evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> SolveAxisMetadata:
    """Build the exported preflight record from the same axis declaration the solver uses."""
    definition = solve_axis_definition(axis)
    supplied_prerequisites = prerequisite_evidence or {}
    prerequisites = tuple(
        {
            "name": name,
            "status": supplied_prerequisites.get(name, {}).get("status", "not_verified"),
            "reason": supplied_prerequisites.get(name, {}).get("reason"),
            "evidence_references": supplied_prerequisites.get(name, {}).get(
                "evidence_references", ()
            ),
        }
        for name in definition.evidence_prerequisites
    )
    status, reason = definition.status, definition.reason
    if status == "supported" and prerequisite_evidence is not None:
        unmet = next(
            (
                item
                for item in prerequisites
                if item["status"] != "met"
                or (
                    item["name"] != "fixed_assumptions_with_provenance"
                    and not item["evidence_references"]
                )
            ),
            None,
        )
        if unmet is not None:
            status = "insufficient_evidence"
            reason = unmet["reason"] or f"solve_evidence_unavailable:{unmet['name']}"

    fixed = {}
    if assumptions is not None:
        values = (
            asdict(assumptions)
            if hasattr(assumptions, "__dataclass_fields__")
            else dict(assumptions)
        )
        provenance = assumption_provenance or {}
        for name, value in values.items():
            if name == axis:
                continue
            source = provenance.get(name)
            if source is None:
                fixed[name] = {"value": value, "evidence_references": (), "limitations": ()}
                continue
            source_value = (
                asdict(source) if hasattr(source, "__dataclass_fields__") else dict(source)
            )
            fixed[name] = {
                "value": value,
                "origin": source_value.get("origin"),
                "source": source_value.get("source"),
                "evidence_references": source_value.get("evidence_references", ()),
                "limitations": source_value.get("limitations", ()),
            }

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
    )
