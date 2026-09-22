"""Optional Jev shadow signals for frozen AlphaForge evidence packets.

This module is deliberately outside :mod:`alphaforge.core`.  It may classify
already validated packet material, but it never owns validation, readiness,
ranking, persistence of claims, or a repair lifecycle.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from time import perf_counter
from typing import Any, Literal

from alphaforge.core.frozen_packet import packet_hash_matches, validate_frozen_packet

CITATION_RELATION_CLASSES = (
    "supports",
    "contradicts",
    "says_nothing",
    "insufficient_context",
)
MISSING_INFORMATION_CLASSES = (
    "core_blocker",
    "supplemental_limitation",
    "covered_or_not_a_gap",
    "ambiguous_review",
)
CITATION_QUESTION_VERSION = "citation-relation-v1"
MISSING_INFORMATION_QUESTION_VERSION = "missing-information-impact-v1"
VARIANT_RELATION_QUESTION_VERSION = "variant-relation-v1"
CRITERIA_VERSION = "jev-shadow-criteria-v1"
PINNED_MODEL_VERSION = "jev-1.13.0"
JEV_INPUT_COST_USD_PER_TOKEN = 0.042 / 1_000_000

VARIANT_RELATION_CLASSES = (
    "translation",
    "revision",
    "different_report",
    "uncertain",
)

FeatureName = Literal["citation_relation", "missing_information", "variant_relation"]


def accept_variant_relation_decision(selected_class: str | None) -> str:
    """Deterministic acceptance rule for shadow variant-relation hints.

    This slice never auto-merges on a model answer: every outcome keeps the
    pair separate so ambiguity fails safe. High-confidence hints are surfaced
    in the evidence diagnostic only.
    """
    return "keep_separate"


def _question_version_for(feature: FeatureName) -> str:
    if feature == "citation_relation":
        return CITATION_QUESTION_VERSION
    if feature == "variant_relation":
        return VARIANT_RELATION_QUESTION_VERSION
    return MISSING_INFORMATION_QUESTION_VERSION


@dataclass(frozen=True)
class JevShadowConfig:
    """Explicit, bounded configuration for the optional shadow lane.

    ``api_key`` is intentionally absent.  Credentialed calls read only
    ``TYPESAFE_API_KEY`` from the server environment when a call is made.
    """

    enabled: bool = False
    citation_relations: bool = False
    missing_information: bool = False
    variant_relations: bool = False
    model_version: str = PINNED_MODEL_VERSION
    timeout_seconds: float = 5.0
    max_transport_retries: int = 1
    max_calls: int = 2
    max_input_tokens: int = 12_000
    max_cost_usd: float = 0.50
    citation_review_confidence: float = 0.80
    missing_information_review_confidence: float = 0.80
    variant_relation_review_confidence: float = 0.80

    @classmethod
    def from_env(cls) -> JevShadowConfig:
        """Read opt-in flags and bounds without ever returning the secret."""

        return cls(
            enabled=_env_bool("ALPHAFORGE_JEV_SHADOW_ENABLED", False),
            citation_relations=_env_bool("ALPHAFORGE_JEV_CITATION_RELATIONS", False),
            missing_information=_env_bool("ALPHAFORGE_JEV_MISSING_INFORMATION", False),
            variant_relations=_env_bool("ALPHAFORGE_JEV_VARIANT_RELATIONS", False),
            model_version=os.environ.get("ALPHAFORGE_JEV_MODEL", PINNED_MODEL_VERSION).strip()
            or PINNED_MODEL_VERSION,
            timeout_seconds=_env_float("ALPHAFORGE_JEV_TIMEOUT_SECONDS", 5.0, minimum=0.1),
            max_transport_retries=_env_int("ALPHAFORGE_JEV_MAX_TRANSPORT_RETRIES", 1, minimum=0),
            max_calls=_env_int("ALPHAFORGE_JEV_MAX_CALLS", 2, minimum=0),
            max_input_tokens=_env_int("ALPHAFORGE_JEV_MAX_INPUT_TOKENS", 12_000, minimum=1),
            max_cost_usd=_env_float("ALPHAFORGE_JEV_MAX_COST_USD", 0.50, minimum=0.0),
            citation_review_confidence=_env_float(
                "ALPHAFORGE_JEV_CITATION_REVIEW_CONFIDENCE", 0.80, minimum=0.0, maximum=1.0
            ),
            missing_information_review_confidence=_env_float(
                "ALPHAFORGE_JEV_MISSING_INFORMATION_REVIEW_CONFIDENCE",
                0.80,
                minimum=0.0,
                maximum=1.0,
            ),
            variant_relation_review_confidence=_env_float(
                "ALPHAFORGE_JEV_VARIANT_RELATION_REVIEW_CONFIDENCE",
                0.80,
                minimum=0.0,
                maximum=1.0,
            ),
        )


@dataclass
class JevShadowBudget:
    """Per-run call, token, and input-cost ceiling."""

    max_calls: int = 2
    max_input_tokens: int = 12_000
    max_cost_usd: float = 0.50
    calls: int = 0
    input_tokens: int = 0
    cost_usd: float = 0.0
    reserved_input_tokens: int = 0
    reserved_cost_usd: float = 0.0

    def reserve(self, estimated_input_tokens: int) -> bool:
        estimate = max(0, estimated_input_tokens)
        if self.calls >= self.max_calls:
            return False
        if self.input_tokens + self.reserved_input_tokens + estimate > self.max_input_tokens:
            return False
        if self.cost_usd + self.reserved_cost_usd + estimate * JEV_INPUT_COST_USD_PER_TOKEN > (
            self.max_cost_usd
        ):
            return False
        self.calls += 1
        self.reserved_input_tokens += estimate
        self.reserved_cost_usd += estimate * JEV_INPUT_COST_USD_PER_TOKEN
        return True

    def record_usage(
        self, input_tokens: int | None, estimated_input_tokens: int | None = None
    ) -> None:
        estimate = (
            self.reserved_input_tokens
            if estimated_input_tokens is None
            else min(max(0, estimated_input_tokens), self.reserved_input_tokens)
        )
        self.reserved_input_tokens -= estimate
        self.reserved_cost_usd -= estimate * JEV_INPUT_COST_USD_PER_TOKEN
        actual = estimate if input_tokens is None else max(0, input_tokens)
        self.input_tokens += actual
        self.cost_usd += actual * JEV_INPUT_COST_USD_PER_TOKEN


@dataclass(frozen=True)
class JevShadowResult:
    """A typed shadow outcome.  ``deterministic_output_unchanged`` is always true."""

    feature: FeatureName
    status: str
    packet_hash: str | None
    selected_class: str | None = None
    probabilities: dict[str, float] | None = None
    confidence: float | None = None
    returned_model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    review_required: bool = False
    error_code: str | None = None
    audit: dict[str, Any] = field(default_factory=dict)

    @property
    def deterministic_output_unchanged(self) -> bool:
        return True


class _JevResponseError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float | None = None,
) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    value = max(minimum, value)
    return min(maximum, value) if maximum is not None else value


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _value(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _json_copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _packet_error(packet: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(packet, dict):
        return None, "packet_not_object"
    packet_hash = packet.get("packet_hash")
    if isinstance(packet_hash, str) and packet_hash:
        if not packet_hash_matches(packet):
            return None, "packet_hash_mismatch"
    if not validate_frozen_packet(packet):
        return None, "packet_invalid"
    return _json_copy(packet), None


def _source_for(packet: dict[str, Any], source_id: str) -> tuple[dict[str, Any] | None, str | None]:
    catalog = packet.get("evidence_catalog")
    catalog_ids = catalog.get("canonical_source_ids") if isinstance(catalog, dict) else None
    if not isinstance(catalog_ids, list) or source_id not in catalog_ids:
        return None, "source_not_catalogued"
    sources = packet.get("sources")
    if not isinstance(sources, list):
        return None, "source_catalog_invalid"
    matches = [
        source
        for source in sources
        if isinstance(source, dict) and source.get("source_id") == source_id
    ]
    if len(matches) != 1:
        return None, "canonical_source_id_invalid"
    return matches[0], None


def _visible_source(source: dict[str, Any], as_of: str) -> str | None:
    publication = source.get("publication_date")
    try:
        publication_day = date.fromisoformat(str(publication)[:10])
        cutoff_day = date.fromisoformat(str(as_of)[:10])
    except (TypeError, ValueError):
        return "publication_date_invalid"
    return "source_not_visible_at_packet_cutoff" if publication_day > cutoff_day else None


def _anchor_text(source: dict[str, Any], anchor: str) -> tuple[str | None, str | None]:
    if not isinstance(anchor, str) or not anchor.strip():
        return None, "anchor_missing"
    for page in source.get("pages", []):
        if isinstance(page, dict) and page.get("anchor") == anchor:
            return page.get("text") if isinstance(page.get("text"), str) else None, None
    body = source.get("body")
    paragraphs = body.get("paragraphs") if isinstance(body, dict) else None
    for paragraph in paragraphs or []:
        if isinstance(paragraph, dict) and paragraph.get("anchor") == anchor:
            return (
                paragraph.get("text") if isinstance(paragraph.get("text"), str) else None,
                None,
            )
    return None, "anchor_not_found"


def _citation_excerpt(
    citation: Mapping[str, Any], anchor_text: str
) -> tuple[str | None, str | None]:
    excerpt = citation.get("excerpt")
    span = citation.get("span")
    if excerpt is None and isinstance(span, str):
        excerpt = span
    if not isinstance(excerpt, str) or not excerpt:
        return None, "excerpt_missing"
    if excerpt not in anchor_text:
        return None, "excerpt_not_found"
    if isinstance(span, dict):
        start = span.get("start")
        end = span.get("end")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
        ):
            return None, "span_invalid"
        if start < 0 or end < start or end > len(anchor_text):
            return None, "span_invalid"
        if anchor_text[start:end] != excerpt:
            return None, "span_excerpt_mismatch"
    elif isinstance(span, str) and span != excerpt:
        return None, "span_excerpt_mismatch"
    elif span is not None and not isinstance(span, str):
        return None, "span_invalid"
    return excerpt, None


def _citation_precheck(
    packet: dict[str, Any], citation: Mapping[str, Any], claim: str
) -> tuple[dict[str, Any] | None, str | None]:
    citation_packet_hash = citation.get("packet_hash")
    if citation_packet_hash is not None and citation_packet_hash != packet.get("packet_hash"):
        return None, "citation_packet_hash_mismatch"
    source_id = citation.get("source_id")
    if not isinstance(source_id, str) or not source_id or source_id != source_id.strip():
        return None, "canonical_source_id_invalid"
    source, error = _source_for(packet, source_id)
    if error or source is None:
        return None, error
    visible_error = _visible_source(source, str(packet.get("as_of")))
    if visible_error:
        return None, visible_error
    anchor = citation.get("anchor")
    anchor_text, error = _anchor_text(source, anchor)
    if error or anchor_text is None:
        return None, error
    excerpt, error = _citation_excerpt(citation, anchor_text)
    if error or excerpt is None:
        return None, error
    if not isinstance(claim, str) or not claim.strip():
        return None, "claim_missing"
    return {
        "source_id": source_id,
        "anchor": anchor,
        "excerpt": excerpt,
        "source_text": anchor_text,
        "claim": claim,
    }, None


def _coverage_facts(packet: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    value = packet.get("coverage_facts")
    if isinstance(value, dict):
        return _json_copy(value), _sha256_json(value), None
    return None, None, "coverage_facts_missing"


def _missing_item_id(missing_item: Any) -> tuple[str | None, str | None]:
    if isinstance(missing_item, str) and missing_item.strip():
        return missing_item, None
    if isinstance(missing_item, Mapping):
        value = (
            missing_item.get("id")
            or missing_item.get("item_id")
            or missing_item.get("missing_item_id")
        )
        if isinstance(value, str) and value.strip():
            return value, None
    return None, "missing_item_identity_invalid"


def _variant_candidate_summary(candidate: Any) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(candidate, Mapping):
        return None, "variant_candidate_not_object"
    summary = {
        "company": candidate.get("company") or candidate.get("mfn_slug"),
        "language": (
            candidate.get("language")
            or candidate.get("pdf_language")
            or candidate.get("ingested_lang")
            or candidate.get("lang")
        ),
        "type_guess": (
            candidate.get("type_guess")
            or candidate.get("document_type")
            or candidate.get("report_kind")
        ),
        "period_start": candidate.get("period_start") or candidate.get("report_period_start"),
        "period_end": candidate.get("period_end") or candidate.get("report_period_end"),
        "published_at": candidate.get("published_at"),
        "title": candidate.get("title"),
    }
    if not summary["language"] or not summary["title"]:
        return None, "variant_candidate_missing_language_or_title"
    return summary, None


def _estimate_input_tokens(state: Any, instructions: Any, criteria: Any) -> int:
    payload = json.dumps(
        {"state": state, "instructions": instructions, "criteria": criteria},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return max(1, math.ceil(len(payload) / 4))


def _choice_question(feature: FeatureName) -> tuple[str, Any, tuple[str, ...], str, str]:
    if feature == "citation_relation":
        question_id = "citation_relation"
        question_version = CITATION_QUESTION_VERSION
        labels = CITATION_RELATION_CLASSES
        instructions = {
            "question_version": question_version,
            "question": (
                "Classify the relation between the claim and the cited excerpt using only "
                "the supplied excerpt. Select insufficient_context when the excerpt does not "
                "contain enough context for a reliable relation."
            ),
            "claim_field": "`claim`",
            "excerpt_field": "`citation.excerpt`",
        }
        criteria = {
            "supports": "The cited source context supports the claim.",
            "contradicts": "The cited source context conflicts with or refutes the claim.",
            "says_nothing": "The cited source context is relevant text but neither supports nor contradicts the claim.",
            "insufficient_context": "The supplied context is too incomplete or ambiguous to determine the relation.",
        }
    elif feature == "variant_relation":
        question_id = "variant_relation"
        question_version = VARIANT_RELATION_QUESTION_VERSION
        labels = VARIANT_RELATION_CLASSES
        instructions = {
            "question_version": question_version,
            "question": (
                "Decide whether the two report candidates describe the same "
                "reporting event using only the supplied metadata. Select "
                "uncertain when the metadata does not distinguish the relation."
            ),
            "candidate_a_field": "`candidate_a`",
            "candidate_b_field": "`candidate_b`",
        }
        criteria = {
            "translation": "The candidates are Swedish and English editions of the same report for the same fiscal period.",
            "revision": "One candidate corrects or revises the other for the same fiscal period.",
            "different_report": "The candidates cover different reporting events or periods.",
            "uncertain": "The metadata does not distinguish whether the candidates share a reporting event.",
        }
    else:
        question_id = "missing_information_impact"
        question_version = MISSING_INFORMATION_QUESTION_VERSION
        labels = MISSING_INFORMATION_CLASSES
        instructions = {
            "question_version": question_version,
            "question": (
                "Classify the impact of the named missing information using only the frozen "
                "packet coverage facts and specialist requirement. Do not infer facts absent "
                "from those inputs."
            ),
            "missing_item_field": "`missing_item`",
            "coverage_field": "`coverage_facts`",
            "specialist_requirement_field": "`specialist_requirement`",
        }
        criteria = {
            "core_blocker": "Missing information prevents the core decision or required specialist conclusion.",
            "supplemental_limitation": "Missing information limits context or completeness but does not block the core decision.",
            "covered_or_not_a_gap": "The packet already covers the named item, or it is not an actual information gap.",
            "ambiguous_review": "The coverage facts and requirement do not distinguish the impact; route for review.",
        }
    try:
        from typesafe_sdk import Choice

        question = Choice(instructions=instructions, criteria=criteria)
    except (ImportError, ModuleNotFoundError):
        # This fallback is only for uncredentialed local tests.  A live call
        # still requires the official SDK dependency in the deployment image.
        question = {"type": "choice", "instructions": instructions, "criteria": criteria}
    return question_id, question, labels, question_version, _sha256_json(criteria)


def _response_choice(
    response: Any, question_id: str, labels: tuple[str, ...]
) -> tuple[str, dict[str, float], float, str, int | None, int | None]:
    model = _value(response, "model")
    answers = _value(response, "answers")
    if not isinstance(answers, Mapping):
        raise _JevResponseError("malformed_response")
    answer = answers.get(question_id)
    if answer is None:
        raise _JevResponseError("malformed_response")
    probabilities = _value(answer, "probabilities")
    confidence = _value(answer, "confidence")
    returned_choice = _value(answer, "choice")
    if not isinstance(model, str) or not model:
        raise _JevResponseError("malformed_response")
    if not isinstance(probabilities, Mapping) or set(probabilities) != set(labels):
        raise _JevResponseError("malformed_response")
    normalized: dict[str, float] = {}
    for label in labels:
        value = probabilities.get(label)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise _JevResponseError("malformed_response")
        if value < 0 or value > 1:
            raise _JevResponseError("malformed_response")
        normalized[label] = float(value)
    if not math.isclose(sum(normalized.values()), 1.0, abs_tol=1e-3):
        raise _JevResponseError("malformed_response")
    if (
        not isinstance(confidence, (int, float))
        or isinstance(confidence, bool)
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        raise _JevResponseError("malformed_response")
    if returned_choice not in labels:
        raise _JevResponseError("malformed_response")
    # Code owns tie behavior: contract order, not an opaque model field.
    selected = max(labels, key=lambda label: (normalized[label], -labels.index(label)))
    usage = _value(response, "usage")
    input_tokens = _value(usage, "input_tokens") if usage is not None else None
    output_tokens = _value(usage, "output_tokens") if usage is not None else None
    for token_count in (input_tokens, output_tokens):
        if token_count is not None and (
            not isinstance(token_count, int) or isinstance(token_count, bool) or token_count < 0
        ):
            raise _JevResponseError("malformed_response")
    return selected, normalized, float(confidence), model, input_tokens, output_tokens


class JevShadowSidecar:
    """Run bounded, auditable Jev classifications without changing core state."""

    def __init__(
        self,
        *,
        config: JevShadowConfig | None = None,
        client: Any | None = None,
        client_factory: Callable[[JevShadowConfig], Any] | None = None,
        audit_sink: Callable[[dict[str, Any]], None] | None = None,
        conn: Any | None = None,
    ) -> None:
        self.config = config or JevShadowConfig.from_env()
        self.client = client
        self._owns_client = False
        self.client_factory = client_factory
        self.audit_sink = audit_sink
        self.conn = conn

    def new_budget(self) -> JevShadowBudget:
        return JevShadowBudget(
            max_calls=self.config.max_calls,
            max_input_tokens=self.config.max_input_tokens,
            max_cost_usd=self.config.max_cost_usd,
        )

    def classify_citation_relation(
        self,
        packet: dict[str, Any],
        citation: Mapping[str, Any],
        claim: str,
        *,
        budget: JevShadowBudget | None = None,
    ) -> JevShadowResult:
        return self._classify(
            feature="citation_relation",
            packet=packet,
            identity={"source_id": citation.get("source_id"), "anchor": citation.get("anchor")},
            precheck=lambda frozen: _citation_precheck(frozen, citation, claim),
            state_builder=lambda frozen, checked: {
                "packet_hash": frozen["packet_hash"],
                "claim": claim,
                "citation": {
                    "source_id": checked["source_id"],
                    "anchor": checked["anchor"],
                    "excerpt": checked["excerpt"],
                },
            },
            budget=budget,
        )

    def classify_missing_information(
        self,
        packet: dict[str, Any],
        missing_item: str | Mapping[str, Any],
        specialist_requirement: str,
        *,
        budget: JevShadowBudget | None = None,
    ) -> JevShadowResult:
        item_id, item_error = _missing_item_id(missing_item)
        identity = {"missing_item_id": item_id}

        def precheck(frozen: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
            if item_error:
                return None, item_error
            if not isinstance(specialist_requirement, str) or not specialist_requirement.strip():
                return None, "specialist_requirement_missing"
            coverage, checksum, error = _coverage_facts(frozen)
            if error:
                return None, error
            return {
                "missing_item": _json_copy(missing_item),
                "missing_item_id": item_id,
                "specialist_requirement": specialist_requirement,
                "coverage_facts": coverage,
                "coverage_checksum": checksum,
            }, None

        return self._classify(
            feature="missing_information",
            packet=packet,
            identity=identity,
            precheck=precheck,
            state_builder=lambda frozen, checked: {
                "packet_hash": frozen["packet_hash"],
                "missing_item": checked["missing_item"],
                "specialist_requirement": checked["specialist_requirement"],
                "coverage_facts": checked["coverage_facts"],
            },
            budget=budget,
        )

    def classify_variant_relation(
        self,
        packet: dict[str, Any],
        candidate_a: Mapping[str, Any],
        candidate_b: Mapping[str, Any],
        *,
        budget: JevShadowBudget | None = None,
    ) -> JevShadowResult:
        """Shadow-only typed relation for a pair deterministic code left separate.

        Never auto-merges: apply :func:`accept_variant_relation_decision` to
        the outcome, which always keeps the pair separate in this slice.
        """

        def precheck(frozen: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
            summary_a, error_a = _variant_candidate_summary(candidate_a)
            if error_a or summary_a is None:
                return None, error_a
            summary_b, error_b = _variant_candidate_summary(candidate_b)
            if error_b or summary_b is None:
                return None, error_b
            return {"candidate_a": summary_a, "candidate_b": summary_b}, None

        return self._classify(
            feature="variant_relation",
            packet=packet,
            identity={},
            precheck=precheck,
            state_builder=lambda frozen, checked: {
                "packet_hash": frozen["packet_hash"],
                "candidate_a": checked["candidate_a"],
                "candidate_b": checked["candidate_b"],
            },
            budget=budget,
        )

    def _get_client(self) -> Any | None:
        if self.client is not None:
            return self.client
        if not os.environ.get("TYPESAFE_API_KEY", "").strip():
            return None
        if self.client_factory is not None:
            self.client = self.client_factory(self.config)
            self._owns_client = True
            return self.client
        try:
            from typesafe_sdk import RetryPolicy, TypeSafeClient
        except (ImportError, ModuleNotFoundError):
            return None
        retry = RetryPolicy(
            max_retries=self.config.max_transport_retries,
            timeout=self.config.timeout_seconds,
            http_statuses={429, 500, 502, 503, 504},
            api_connection_error=True,
            api_timeout_error=True,
            backoff_initial=0.0,
            backoff_max=0.0,
            backoff_jitter=0.0,
            respect_retry_after=True,
        )
        self.client = TypeSafeClient(
            api_key=os.environ["TYPESAFE_API_KEY"],
            model=self.config.model_version,
            retry=retry,
            timeout=self.config.timeout_seconds,
        )
        self._owns_client = True
        return self.client

    def close(self) -> None:
        if not self._owns_client or self.client is None:
            return
        client = self.client
        self.client = None
        self._owns_client = False
        closer = getattr(client, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:
                pass

    def _classify(
        self,
        *,
        feature: FeatureName,
        packet: dict[str, Any],
        identity: dict[str, Any],
        precheck: Callable[[dict[str, Any]], tuple[dict[str, Any] | None, str | None]],
        state_builder: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
        budget: JevShadowBudget | None,
    ) -> JevShadowResult:
        if (
            not self.config.enabled
            or (feature == "citation_relation" and not self.config.citation_relations)
            or (feature == "missing_information" and not self.config.missing_information)
            or (feature == "variant_relation" and not self.config.variant_relations)
        ):
            return self._finish(
                feature,
                packet_hash=packet.get("packet_hash") if isinstance(packet, dict) else None,
                identity=identity,
                question_version=_question_version_for(feature),
                error_code="shadow_disabled",
                status="disabled",
            )
        frozen, packet_error = _packet_error(packet)
        packet_hash = packet.get("packet_hash") if isinstance(packet, dict) else None
        if packet_error or frozen is None:
            return self._finish(
                feature,
                packet_hash=packet_hash if isinstance(packet_hash, str) else None,
                identity=identity,
                question_version=_question_version_for(feature),
                error_code=packet_error,
                status="invalid_input",
            )
        checked, precheck_error = precheck(frozen)
        if precheck_error or checked is None:
            return self._finish(
                feature,
                packet_hash=frozen["packet_hash"],
                identity={**identity, **({} if checked is None else checked)},
                question_version=_question_version_for(feature),
                error_code=precheck_error,
                status="invalid_input",
            )
        question_id, question, labels, question_version, _criteria_hash = _choice_question(feature)
        state = state_builder(frozen, checked)
        criteria = _value(question, "criteria")
        instructions = _value(question, "instructions")
        question_hash = _sha256_json(
            {
                "question_version": question_version,
                "instructions": instructions,
                "criteria": criteria,
            }
        )
        input_hash = _sha256_json(
            {
                "packet_hash": frozen["packet_hash"],
                "identity": identity,
                "state": state,
                "question_version": question_version,
                "question_hash": question_hash,
                "model_version": self.config.model_version,
            }
        )
        active_budget = budget or self.new_budget()
        estimate = _estimate_input_tokens(state, instructions, criteria)
        if not active_budget.reserve(estimate):
            return self._finish(
                feature,
                packet_hash=frozen["packet_hash"],
                identity=identity,
                question_version=question_version,
                criteria_hash=question_hash,
                input_hash=input_hash,
                error_code="budget_exceeded",
                status="skipped_budget",
            )
        started = perf_counter()
        try:
            client = self._get_client()
            if client is None:
                raise _JevResponseError("missing_api_key_or_sdk")
            response = client.system_one(
                state=state,
                questions={question_id: question},
                model=self.config.model_version,
                timeout=self.config.timeout_seconds,
            )
            selected, probabilities, confidence, returned_model, input_tokens, output_tokens = (
                _response_choice(response, question_id, labels)
            )
            active_budget.record_usage(input_tokens, estimate)
            if returned_model != self.config.model_version:
                raise _JevResponseError("model_version_mismatch")
            if (
                active_budget.input_tokens + active_budget.reserved_input_tokens
                > active_budget.max_input_tokens
            ):
                raise _JevResponseError("token_budget_exceeded")
            if input_tokens is not None and input_tokens > self.config.max_input_tokens:
                raise _JevResponseError("token_budget_exceeded")
            if (
                active_budget.cost_usd + active_budget.reserved_cost_usd
                > active_budget.max_cost_usd
            ):
                raise _JevResponseError("cost_budget_exceeded")
            latency_ms = round((perf_counter() - started) * 1000)
            if feature == "citation_relation":
                threshold = self.config.citation_review_confidence
            elif feature == "variant_relation":
                threshold = self.config.variant_relation_review_confidence
            else:
                threshold = self.config.missing_information_review_confidence
            audit = self._audit(
                feature=feature,
                packet_hash=frozen["packet_hash"],
                identity={**identity, **checked},
                question_version=question_version,
                criteria_hash=question_hash,
                input_hash=input_hash,
                returned_model=returned_model,
                selected_class=selected,
                probabilities=probabilities,
                confidence=confidence,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                retry_outcome="success",
                error_code=None,
            )
            return JevShadowResult(
                feature=feature,
                status="shadow_result",
                packet_hash=frozen["packet_hash"],
                selected_class=selected,
                probabilities=probabilities,
                confidence=confidence,
                returned_model=returned_model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                review_required=(
                    confidence < threshold
                    or selected in {"insufficient_context", "ambiguous_review", "uncertain"}
                ),
                audit=audit,
            )
        except _JevResponseError as exc:
            latency_ms = round((perf_counter() - started) * 1000)
            audit = self._audit(
                feature=feature,
                packet_hash=frozen["packet_hash"],
                identity={**identity, **checked},
                question_version=question_version,
                criteria_hash=question_hash,
                input_hash=input_hash,
                returned_model=None,
                selected_class=None,
                probabilities=None,
                confidence=None,
                input_tokens=None,
                output_tokens=None,
                latency_ms=latency_ms,
                retry_outcome="error",
                error_code=exc.code,
            )
            return JevShadowResult(
                feature=feature,
                status="error",
                packet_hash=frozen["packet_hash"],
                error_code=exc.code,
                audit=audit,
            )
        except Exception as exc:  # SDK errors are a side-step, never a fallback path.
            error_code = _sdk_error_code(exc)
            latency_ms = round((perf_counter() - started) * 1000)
            audit = self._audit(
                feature=feature,
                packet_hash=frozen["packet_hash"],
                identity={**identity, **checked},
                question_version=question_version,
                criteria_hash=question_hash,
                input_hash=input_hash,
                returned_model=None,
                selected_class=None,
                probabilities=None,
                confidence=None,
                input_tokens=None,
                output_tokens=None,
                latency_ms=latency_ms,
                retry_outcome="error",
                error_code=error_code,
            )
            return JevShadowResult(
                feature=feature,
                status="error",
                packet_hash=frozen["packet_hash"],
                error_code=error_code,
                audit=audit,
            )
        finally:
            self.close()

    def _finish(
        self,
        feature: FeatureName,
        *,
        packet_hash: str | None,
        identity: dict[str, Any],
        question_version: str,
        criteria_hash: str | None = None,
        input_hash: str | None = None,
        error_code: str | None,
        status: str,
    ) -> JevShadowResult:
        audit = self._audit(
            feature=feature,
            packet_hash=packet_hash,
            identity=identity,
            question_version=question_version,
            criteria_hash=criteria_hash,
            input_hash=input_hash,
            returned_model=None,
            selected_class=None,
            probabilities=None,
            confidence=None,
            input_tokens=None,
            output_tokens=None,
            latency_ms=0,
            retry_outcome="not_attempted",
            error_code=error_code,
        )
        return JevShadowResult(
            feature=feature,
            status=status,
            packet_hash=packet_hash,
            error_code=error_code,
            audit=audit,
        )

    def _audit(
        self,
        *,
        feature: FeatureName,
        packet_hash: str | None,
        identity: dict[str, Any],
        question_version: str,
        criteria_hash: str | None,
        input_hash: str | None,
        returned_model: str | None,
        selected_class: str | None,
        probabilities: dict[str, float] | None,
        confidence: float | None,
        input_tokens: int | None,
        output_tokens: int | None,
        latency_ms: int,
        retry_outcome: str,
        error_code: str | None,
    ) -> dict[str, Any]:
        excerpt = identity.get("excerpt")
        coverage_checksum = identity.get("coverage_checksum")
        audit = {
            "observed_at": _now(),
            "feature": feature,
            "packet_hash": packet_hash,
            "source_id": identity.get("source_id"),
            "missing_item_id": identity.get("missing_item_id"),
            "anchor": identity.get("anchor"),
            "span_checksum": hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
            if isinstance(excerpt, str)
            else None,
            "coverage_checksum": coverage_checksum,
            "claim_hash": _sha256_json(identity.get("claim"))
            if identity.get("claim") is not None
            else None,
            "question_hash": criteria_hash,
            "question_version": question_version,
            "criteria_version": CRITERIA_VERSION,
            "pinned_model_version": self.config.model_version,
            "returned_model": returned_model,
            "selected_class": selected_class,
            "probabilities": probabilities,
            "confidence": confidence,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            "latency_ms": latency_ms,
            "retry_outcome": retry_outcome,
            "error_code": error_code,
            "input_hash": input_hash,
            "action": "shadow_only",
        }
        safe_audit = _json_copy(audit)
        if self.conn is not None:
            try:
                from alphaforge.db.repositories import append_jev_shadow_audit

                append_jev_shadow_audit(self.conn, safe_audit)
            except Exception:
                # The signal must never affect deterministic application output.
                safe_audit["audit_persist_error"] = "audit_persist_failed"
        if self.audit_sink is not None:
            try:
                self.audit_sink(_json_copy(safe_audit))
            except Exception:
                safe_audit["audit_sink_error"] = "audit_sink_failed"
        return safe_audit


def _sdk_error_code(error: Exception) -> str:
    name = error.__class__.__name__.lower()
    if "timeout" in name:
        return "timeout"
    if "ratelimit" in name or "rate_limit" in name:
        return "rate_limit"
    if "validation" in name or "unprocessable" in name or "badrequest" in name:
        return "api_validation_error"
    if "connection" in name:
        return "transport_error"
    if isinstance(error, ImportError):
        return "missing_api_key_or_sdk"
    return "api_error"


def run_shadow_signals(
    packet: dict[str, Any],
    *,
    citation: Mapping[str, Any] | None = None,
    claim: str | None = None,
    missing_item: str | Mapping[str, Any] | None = None,
    specialist_requirement: str | None = None,
    variant_pairs: Sequence[tuple[Mapping[str, Any], Mapping[str, Any]]] | None = None,
    config: JevShadowConfig | None = None,
    client: Any | None = None,
    conn: Any | None = None,
) -> dict[str, JevShadowResult]:
    """Run selected sidecars under one shared per-run budget."""

    sidecar = JevShadowSidecar(config=config, client=client, conn=conn)
    budget = sidecar.new_budget()
    results: dict[str, JevShadowResult] = {}
    if citation is not None and claim is not None:
        results["citation_relation"] = sidecar.classify_citation_relation(
            packet, citation, claim, budget=budget
        )
    if missing_item is not None and specialist_requirement is not None:
        results["missing_information"] = sidecar.classify_missing_information(
            packet, missing_item, specialist_requirement, budget=budget
        )
    for index, pair in enumerate(variant_pairs or ()):
        if len(pair) != 2:
            continue
        key = "variant_relation" if index == 0 else f"variant_relation:{index}"
        results[key] = sidecar.classify_variant_relation(packet, pair[0], pair[1], budget=budget)
    return results


__all__ = [
    "CITATION_RELATION_CLASSES",
    "CITATION_QUESTION_VERSION",
    "CRITERIA_VERSION",
    "JevShadowBudget",
    "JevShadowConfig",
    "JevShadowResult",
    "JevShadowSidecar",
    "MISSING_INFORMATION_CLASSES",
    "MISSING_INFORMATION_QUESTION_VERSION",
    "PINNED_MODEL_VERSION",
    "VARIANT_RELATION_CLASSES",
    "VARIANT_RELATION_QUESTION_VERSION",
    "accept_variant_relation_decision",
    "run_shadow_signals",
]
