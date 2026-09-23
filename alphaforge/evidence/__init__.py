"""Evidence ingestion."""

from __future__ import annotations

import importlib
from typing import Any

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

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "EVIDENCE_RULES_VERSION": ("alphaforge.core.frozen_packet", "EVIDENCE_RULES_VERSION"),
    "is_stale_evidence_packet": (
        "alphaforge.core.frozen_packet",
        "is_stale_evidence_packet",
    ),
    "packet_rules_version": ("alphaforge.core.frozen_packet", "packet_rules_version"),
    "EvidenceResourceLimits": ("alphaforge.evidence.flow", "EvidenceResourceLimits"),
    "OneCompanyEvidenceFlow": ("alphaforge.evidence.flow", "OneCompanyEvidenceFlow"),
    "build_frozen_evidence_packet": (
        "alphaforge.evidence.flow",
        "build_frozen_evidence_packet",
    ),
    "validate_frozen_packet": ("alphaforge.evidence.flow", "validate_frozen_packet"),
    "REPORT_RULES_VERSION": ("alphaforge.evidence.report_rules", "REPORT_RULES_VERSION"),
    "current_report_rules_fingerprint": (
        "alphaforge.evidence.report_rules",
        "current_report_rules_fingerprint",
    ),
    "report_rules_fingerprint": (
        "alphaforge.evidence.report_rules",
        "report_rules_fingerprint",
    ),
    "report_rules_metadata": ("alphaforge.evidence.report_rules", "report_rules_metadata"),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_EXPORTS:
        module_name, attr = _LAZY_EXPORTS[name]
        value = getattr(importlib.import_module(module_name), attr)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
