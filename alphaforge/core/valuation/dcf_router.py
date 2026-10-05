"""Explicit manual/evidenced routing for the supported DCF profile."""

import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from hashlib import sha256

from alphaforge.core.valuation.dcf_contract import EvidenceReference

DCF_ROUTING_POLICY_VERSION = "dcf-routing-v1-explicit-mature-operating-company"
FCFF_METHOD_IDENTITY = "fcff-forward-reinvestment-v1"


class DcfArchetype(StrEnum):
    OPERATING_COMPANY = "operating_company"
    BANK = "bank"
    FINANCIAL = "financial"
    PROPERTY = "property"
    RESOURCE = "resource"
    HOLDING = "holding"
    UNUSUAL = "unusual"
    UNKNOWN = "unknown"
    MIXED = "mixed"


class DcfForecastProfile(StrEnum):
    MATURE = "mature"
    HIGH_GROWTH = "high_growth"
    CYCLICAL = "cyclical"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"
    MIXED = "mixed"


@dataclass(frozen=True)
class DcfRoutingInput:
    """A caller-supplied archetype/profile and its catalogued evidence references."""

    archetype: DcfArchetype = DcfArchetype.UNKNOWN
    forecast_profile: DcfForecastProfile = DcfForecastProfile.UNKNOWN
    evidence_references: tuple[EvidenceReference, ...] = ()


def routing_input_from_value(value) -> DcfRoutingInput:
    """Normalize the frozen JSON or typed caller input; absent input stays unknown."""
    if value is None:
        return DcfRoutingInput()
    if isinstance(value, DcfRoutingInput):
        value = {
            "archetype": value.archetype,
            "forecast_profile": value.forecast_profile,
            "evidence_references": value.evidence_references,
        }
    if not isinstance(value, dict):
        raise TypeError("DCF routing input must be a mapping")
    references = value.get("evidence_references", ())
    if not isinstance(references, (list, tuple)):
        raise TypeError("DCF routing evidence_references must be a sequence")
    return DcfRoutingInput(
        archetype=DcfArchetype(value.get("archetype", DcfArchetype.UNKNOWN)),
        forecast_profile=DcfForecastProfile(
            value.get("forecast_profile", DcfForecastProfile.UNKNOWN)
        ),
        evidence_references=tuple(
            reference
            if isinstance(reference, EvidenceReference)
            else EvidenceReference(**reference)
            for reference in references
        ),
    )


def routing_input_payload(value) -> dict:
    """Return the canonical, immutable input facts used by capture and replay."""
    routing = routing_input_from_value(value)
    if any(not reference.source_id.strip() for reference in routing.evidence_references):
        raise ValueError("DCF routing evidence source_id must not be empty")
    return {
        "archetype": routing.archetype.value,
        "forecast_profile": routing.forecast_profile.value,
        "evidence_references": [asdict(reference) for reference in routing.evidence_references],
    }


@dataclass(frozen=True)
class DcfRoutingDecision:
    policy_version: str
    status: str
    reason: str | None
    method: str | None
    method_identity: str | None
    archetype: str
    forecast_profile: str
    profile_identity: str
    evidence_references: tuple[EvidenceReference, ...]
    evidence_identity: str
    limitations: tuple[str, ...]
    input_identity: str
    decision_identity: str


def decide_dcf_route(
    value=None, *, catalogued_source_ids: set[str] | frozenset[str] | None = None
) -> DcfRoutingDecision:
    routing = routing_input_from_value(value)
    payload = routing_input_payload(routing)
    encoded_input = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    input_identity = sha256(encoded_input.encode("utf-8")).hexdigest()
    evidence_identity = sha256(
        json.dumps(
            payload["evidence_references"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()

    reason = None
    status = "available"
    method = (
        FCFF_METHOD_IDENTITY
        if (
            routing.archetype == DcfArchetype.OPERATING_COMPANY
            and routing.forecast_profile == DcfForecastProfile.MATURE
        )
        else None
    )
    if routing.archetype in {DcfArchetype.BANK, DcfArchetype.FINANCIAL}:
        status, reason = "unsupported", "financial_company_requires_non_fcff_method"
    elif routing.archetype == DcfArchetype.PROPERTY:
        status, reason = "unsupported", "property_company_requires_nav_or_ffo_method"
    elif routing.archetype == DcfArchetype.RESOURCE:
        status, reason = "unsupported", "resource_company_requires_separate_economic_policy"
    elif routing.archetype in {DcfArchetype.HOLDING, DcfArchetype.UNUSUAL}:
        status, reason = "unsupported", "holding_or_unusual_structure_unsupported"
    elif routing.archetype in {DcfArchetype.UNKNOWN, DcfArchetype.MIXED}:
        status, reason = "insufficient_evidence", "archetype_unknown_or_mixed"
    elif routing.forecast_profile == DcfForecastProfile.HIGH_GROWTH:
        status, reason = "unavailable", "high_growth_transition_and_funding_evidence_unavailable"
    elif routing.forecast_profile == DcfForecastProfile.CYCLICAL:
        status, reason = "unavailable", "cyclical_normalized_base_unavailable"
    elif routing.forecast_profile in {
        DcfForecastProfile.UNKNOWN,
        DcfForecastProfile.MIXED,
    }:
        status, reason = "insufficient_evidence", "forecast_profile_unknown_or_mixed"
    elif routing.forecast_profile == DcfForecastProfile.UNSUPPORTED:
        status, reason = "unsupported", "unsupported_forecast_profile"
    elif not routing.evidence_references:
        status, reason, method = (
            "insufficient_evidence",
            "mature_operating_route_evidence_unavailable",
            None,
        )
    elif not {reference.source_id for reference in routing.evidence_references}.issubset(
        catalogued_source_ids or set()
    ):
        status, reason, method = (
            "insufficient_evidence",
            "mature_operating_route_evidence_not_catalogued",
            None,
        )

    decision_facts = {
        "policy_version": DCF_ROUTING_POLICY_VERSION,
        "status": status,
        "reason": reason,
        "method": method,
        "input_identity": input_identity,
    }
    decision_identity = sha256(
        json.dumps(decision_facts, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return DcfRoutingDecision(
        policy_version=DCF_ROUTING_POLICY_VERSION,
        status=status,
        reason=reason,
        method="fcff" if method else None,
        method_identity=method,
        archetype=routing.archetype.value,
        forecast_profile=routing.forecast_profile.value,
        profile_identity=f"{routing.archetype.value}+{routing.forecast_profile.value}-v1",
        evidence_references=routing.evidence_references,
        evidence_identity=evidence_identity,
        limitations=(
            "archetype and forecast profile are caller-supplied; cited source identity is not an automatic classifier or proof of eligibility",
        ),
        input_identity=input_identity,
        decision_identity=decision_identity,
    )
