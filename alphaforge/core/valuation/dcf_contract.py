"""Typed provenance and canonical serialization for auditable DCF results."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from enum import StrEnum
from json import dumps, loads
from math import isfinite
from typing import Any

DCF_RESULT_CONTRACT_VERSION = "dcf-result-contract-v1"


class DcfResultStatus(StrEnum):
    AVAILABLE = "available"
    CANDIDATE_SOLUTIONS = "candidate_solutions"
    SAMPLED_MATCH_REGION = "sampled_match_region"
    NOT_IDENTIFIABLE = "not_identifiable"
    UNSUPPORTED = "unsupported"
    INVALID_INPUT = "invalid_input"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    DOMAIN_UNAVAILABLE = "domain_unavailable"
    NO_CROSSING = "no_crossing"
    NONCONVERGENCE = "nonconvergence"
    UNAVAILABLE = "unavailable"


class AssumptionOrigin(StrEnum):
    FIXED_DEFAULT = "fixed_default"
    COMPANY_HISTORY = "company_history"
    REPORT_EVIDENCE = "report_evidence"
    MARKET_EVIDENCE = "market_evidence"
    QUALIFIED_CALIBRATION = "qualified_calibration"


@dataclass(frozen=True)
class EvidenceReference:
    source_id: str
    source_url: str | None = None
    published_on: str | None = None
    observed_on: str | None = None
    anchor: str | None = None
    sha256: str | None = None


@dataclass(frozen=True)
class AssumptionProvenance:
    origin: AssumptionOrigin
    source: str
    evidence_references: tuple[EvidenceReference, ...] = ()
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class DcfResultMetadata:
    status: DcfResultStatus
    reason: str | None
    warnings: tuple[str, ...]
    version: str | None


def serialize_dcf_result(
    payload: Mapping[str, Any],
    metadata: DcfResultMetadata,
    assumption_provenance: Mapping[str, AssumptionProvenance] | None = None,
) -> dict[str, Any]:
    """Return the single finite, JSON-compatible representation of a DCF result."""
    if not isinstance(payload, Mapping):
        raise TypeError("DCF result payload must be a mapping")
    result = _json_value(dict(payload))
    result.update(
        {
            "status": metadata.status.value,
            "reason": metadata.reason,
            "warnings": list(metadata.warnings),
            "version": metadata.version,
            "contract_version": DCF_RESULT_CONTRACT_VERSION,
            "assumption_provenance": _json_value(assumption_provenance or {}),
        }
    )
    # Round-trip through strict JSON to enforce the public shape and reject NaN/inf.
    return loads(
        dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    )


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("DCF result contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("DCF result mapping keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported DCF result value: {type(value).__name__}")
