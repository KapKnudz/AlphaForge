from dataclasses import dataclass, replace
from math import isfinite
from typing import Literal

from alphaforge.core.frozen_packet import (
    EVIDENCE_RULES_VERSION,
    is_stale_evidence_packet,
    packet_rules_version,
    validate_frozen_packet,
)
from alphaforge.core.valuation.forward_scenario import (
    ForwardScenarioEngine,
    ForwardScenarioInputs,
    ForwardScenarioReadiness,
)
from alphaforge.evidence.report_rules import current_report_rules_fingerprint

ReadinessStatus = Literal[
    "ready",
    "evidence_blocked",
    "valuation_blocked",
    "method_unsupported",
]
EvidenceSubsectionStatus = Literal["available", "partial", "unavailable", "stale"]
BlockerCategory = Literal["evidence", "valuation", "method"]


@dataclass(frozen=True)
class ReadinessBlocker:
    code: str
    category: BlockerCategory
    message: str


@dataclass(frozen=True)
class AgentReadinessAssessment:
    company_id: int
    ticker: str
    status: ReadinessStatus
    blockers: tuple[ReadinessBlocker, ...] = ()
    limitations: tuple[ReadinessBlocker, ...] = ()
    forward_scenario_readiness: ForwardScenarioReadiness | None = None
    liquidity_status: EvidenceSubsectionStatus = "unavailable"
    ownership_status: EvidenceSubsectionStatus = "unavailable"

    @property
    def ready(self) -> bool:
        return self.status == "ready"


class AgentReadinessError(ValueError):
    def __init__(self, assessments: tuple[AgentReadinessAssessment, ...]):
        self.assessments = assessments
        details = []
        for assessment in assessments:
            blockers = "; ".join(
                f"{blocker.code}: {blocker.message}" for blocker in assessment.blockers
            )
            details.append(f"{assessment.ticker} [{assessment.status}]: {blockers}")
        super().__init__("Agent analysis readiness check failed: " + " | ".join(details))


class AgentReadinessGate:
    """Reject known-incomplete analysis packets before a paid model call."""

    SUPPORTED_RANKING_MODELS = {"general"}

    def assess(self, candidate) -> AgentReadinessAssessment:
        blockers: list[ReadinessBlocker] = []
        limitations: list[ReadinessBlocker] = []
        if candidate.ranking_model not in self.SUPPORTED_RANKING_MODELS:
            blockers.append(
                ReadinessBlocker(
                    code="forward_method_unsupported",
                    category="method",
                    message=(
                        f"{candidate.ranking_model} requires a dedicated forward valuation method"
                    ),
                )
            )

        evidence = candidate.research_evidence or {}
        if getattr(candidate, "evidence_lane", evidence.get("evidence_lane", False)):
            packet = evidence.get("evidence_packet")
            if not validate_frozen_packet(packet):
                blockers.append(
                    ReadinessBlocker(
                        code="frozen_evidence_packet_missing",
                        category="evidence",
                        message="a valid frozen point-in-time evidence packet is required",
                    )
                )
            elif is_stale_evidence_packet(
                packet, current_rules_fingerprint=current_report_rules_fingerprint()
            ):
                blockers.append(
                    ReadinessBlocker(
                        code="stale_evidence_packet",
                        category="evidence",
                        message=(
                            "frozen evidence packet was built under older evidence rules"
                            f" (v{packet_rules_version(packet)}); rerun the evidence lane"
                            f" for v{EVIDENCE_RULES_VERSION}"
                        ),
                    )
                )
        else:
            manifest = evidence.get("evidence_manifest") or {}
            fallback = manifest.get("readiness_fallback") or {}
            if fallback.get("documents_available"):
                blockers.append(
                    ReadinessBlocker(
                        code="frozen_evidence_packet_missing",
                        category="evidence",
                        message=(
                            "auditable evidence exists, but a usable current frozen "
                            "evidence packet is required"
                        ),
                    )
                )
            elif not evidence.get("documents"):
                blockers.append(
                    ReadinessBlocker(
                        code="primary_evidence_missing",
                        category="evidence",
                        message="no textual company reports or releases are stored",
                    )
                )

        reverse_dcf = candidate.full_results.get("reverse_dcf") or {}
        if _field(reverse_dcf, "status") != "available":
            # A model call cannot produce a trustworthy analysis without a
            # usable valuation input. Keep the reason typed and visible rather
            # than allowing a research document alone to make the candidate
            # ready.
            blockers.extend(_reverse_dcf_blockers(reverse_dcf))

        valuation = candidate.full_results.get("valuation") or {}
        guardrail_low = _field(valuation, "ev_ebit_guardrail_low")
        guardrail_high = _field(valuation, "ev_ebit_guardrail_high")
        if (
            not _positive(guardrail_low)
            or not _positive(guardrail_high)
            or guardrail_low > guardrail_high
        ):
            limitations.append(
                ReadinessBlocker(
                    code="historical_terminal_multiple_range_unavailable",
                    category="valuation",
                    message=(
                        "historical EV/EBIT range is unavailable; use current or "
                        "peer valuation anchors"
                    ),
                )
            )

        forward_scenario_readiness = assess_forward_scenario_readiness(candidate)
        ownership_liquidity = candidate.research_evidence.get("ownership_liquidity", {})
        liquidity_status = _subsection_status(ownership_liquidity.get("liquidity"))
        ownership_status = _subsection_status(ownership_liquidity.get("ownership"))
        return AgentReadinessAssessment(
            company_id=candidate.company_id,
            ticker=candidate.ticker,
            status=_status(blockers),
            blockers=tuple(blockers),
            limitations=tuple(limitations),
            forward_scenario_readiness=forward_scenario_readiness,
            liquidity_status=liquidity_status,
            ownership_status=ownership_status,
        )

    def require_ready(self, candidates: list) -> tuple[AgentReadinessAssessment, ...]:
        assessments = tuple(self.assess(candidate) for candidate in candidates)
        blocked = tuple(item for item in assessments if not item.ready)
        if blocked:
            raise AgentReadinessError(blocked)
        return assessments

    def require_forward_scenario_ready(
        self, candidates: list
    ) -> tuple[AgentReadinessAssessment, ...]:
        assessments = tuple(self.assess(candidate) for candidate in candidates)
        blocked = []
        for assessment in assessments:
            readiness = assessment.forward_scenario_readiness
            if readiness is None or readiness.status == "required":
                continue
            category = "method" if readiness.status == "method_not_supported" else "valuation"
            code = (
                "forward_method_unsupported"
                if category == "method"
                else "forward_scenario_inputs_missing"
            )
            blocker = ReadinessBlocker(
                code=code,
                category=category,
                message=(
                    "forward scenario readiness is "
                    f"{readiness.status}: "
                    + ", ".join(readiness.missing_inputs or readiness.warnings)
                ),
            )
            blocked.append(
                replace(
                    assessment,
                    status="method_unsupported" if category == "method" else "valuation_blocked",
                    blockers=(*assessment.blockers, blocker),
                )
            )
        if blocked:
            raise AgentReadinessError(tuple(blocked))
        return assessments


def _status(blockers: list[ReadinessBlocker]) -> ReadinessStatus:
    categories = {blocker.category for blocker in blockers}
    if "method" in categories:
        return "method_unsupported"
    if "evidence" in categories:
        return "evidence_blocked"
    if "valuation" in categories:
        return "valuation_blocked"
    return "ready"


def _field(value, name):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def _subsection_status(value) -> EvidenceSubsectionStatus:
    status = _field(value, "status")
    if status in {"available", "partial", "unavailable", "stale"}:
        return status
    return "unavailable"


def _positive(value) -> bool:
    return isinstance(value, (int, float)) and isfinite(value) and value > 0


def _reverse_dcf_blockers(reverse_dcf) -> list[ReadinessBlocker]:
    missing = tuple(str(item) for item in (_field(reverse_dcf, "missing_information") or ()))
    matched = []
    checks = (
        (
            "stock price is older than",
            "stock_price_stale",
            "stored stock price is too old for the analysis date",
        ),
        (
            "latest stock price unavailable",
            "stock_price_missing",
            "latest stock price is unavailable",
        ),
        (
            "stock price and report currencies differ",
            "valuation_currency_mismatch",
            "stock-price and financial-report currencies differ",
        ),
    )
    for phrase, code, message in checks:
        if any(phrase in item for item in missing):
            matched.append(ReadinessBlocker(code, "valuation", message))
    if matched:
        return matched
    detail = "; ".join(missing) or "reverse DCF is unavailable"
    return [ReadinessBlocker("reverse_dcf_unavailable", "valuation", detail)]


def assess_forward_scenario_readiness(candidate) -> ForwardScenarioReadiness:
    reverse_dcf = candidate.full_results.get("reverse_dcf") or {}
    valuation = candidate.full_results.get("valuation") or {}
    current_multiple = _field(valuation, "raw_ev_ebit") or _field(valuation, "ev_ebit")
    return ForwardScenarioEngine.assess_readiness(
        ForwardScenarioInputs(
            current_price=_field(reverse_dcf, "current_price"),
            current_revenue=_field(reverse_dcf, "current_revenue"),
            current_shares=_field(reverse_dcf, "current_shares"),
            current_net_debt=_field(reverse_dcf, "current_net_debt"),
            historical_terminal_multiple_range=(
                _field(valuation, "ev_ebit_guardrail_low"),
                _field(valuation, "ev_ebit_guardrail_high"),
            ),
            bundles=(),
            ranking_model=candidate.ranking_model,
            price_currency=_field(reverse_dcf, "price_currency"),
            financial_currency=_field(reverse_dcf, "financial_currency"),
            current_terminal_multiple=current_multiple,
        )
    )
