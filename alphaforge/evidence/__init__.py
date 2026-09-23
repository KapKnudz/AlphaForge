"""Evidence ingestion."""

from alphaforge.core.frozen_packet import (
    EVIDENCE_RULES_VERSION,
    is_stale_evidence_packet,
    packet_rules_version,
)
from alphaforge.evidence.flow import (
    EvidenceResourceLimits,
    OneCompanyEvidenceFlow,
    build_frozen_evidence_packet,
    validate_frozen_packet,
)
from alphaforge.evidence.report_rules import (
    REPORT_RULES_VERSION,
    current_report_rules_fingerprint,
    report_rules_fingerprint,
    report_rules_metadata,
)

__all__ = [
    "EVIDENCE_RULES_VERSION",
    "EvidenceResourceLimits",
    "OneCompanyEvidenceFlow",
    "REPORT_RULES_VERSION",
    "current_report_rules_fingerprint",
    "report_rules_fingerprint",
    "report_rules_metadata",
    "build_frozen_evidence_packet",
    "is_stale_evidence_packet",
    "packet_rules_version",
    "validate_frozen_packet",
]
