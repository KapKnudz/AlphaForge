from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pytest

from alphaforge.core.frozen_packet import canonical_packet_hash
from alphaforge.db.connection import get_connection, init_db
from alphaforge.llm import (
    MISSING_INFORMATION_CLASSES,
    JevShadowConfig,
    JevShadowSidecar,
)

LABELS = (
    "supports",
    "contradicts",
    "says_nothing",
    "insufficient_context",
)


@dataclass
class Response:
    model: str
    answers: dict
    usage: object


@dataclass
class Usage:
    input_tokens: int = 100
    output_tokens: int = 20


class FakeClient:
    def __init__(self, *, label: str = "supports", confidence: float = 0.92):
        self.calls: list[dict] = []
        self.label = label
        self.confidence = confidence

    def system_one(self, **kwargs):
        self.calls.append(kwargs)
        question_id = next(iter(kwargs["questions"]))
        labels = LABELS if question_id == "citation_relation" else MISSING_INFORMATION_CLASSES
        probabilities = dict.fromkeys(labels, 0.02)
        probabilities[self.label] = 0.94
        return Response(
            model="jev-1.13.0",
            answers={
                "citation_relation": {
                    "type": "choice",
                    "choice": self.label,
                    "probabilities": probabilities,
                    "confidence": self.confidence,
                },
                "missing_information_impact": {
                    "type": "choice",
                    "choice": self.label,
                    "probabilities": probabilities,
                    "confidence": self.confidence,
                },
            },
            usage=Usage(),
        )


def _packet(*, publication_date: str = "2026-05-01", coverage: dict | None = None) -> dict:
    text = "Revenue increased in Sweden."
    page = {
        "page_number": 1,
        "anchor": "document:1#page:1",
        "text": text,
        "text_checksum": hashlib.sha256(text.encode()).hexdigest(),
    }
    source = {
        "source_id": "document:1",
        "source_url": "https://mfn.test/report/1",
        "publication_date": publication_date,
        "publication_timestamp_authoritative": True,
        "ingestion_date": "2026-05-02T00:00:00Z",
        "attachment": {
            "source_url": "https://storage.test/report.pdf",
            "sha256": "a" * 64,
        },
        "extraction": {
            "extractor": "pypdf",
            "text_checksum": hashlib.sha256(f"[page 1]\n{text}".encode()).hexdigest(),
            "page_count": 1,
        },
        "pages": [page],
        "body": {
            "paragraphs": [
                {"anchor": "document:1#paragraph:1", "text": text},
            ]
        },
    }
    packet = {
        "schema_version": "evidence-packet-v1",
        "frozen": True,
        "company_id": 1,
        "as_of": "2026-05-31",
        "sources": [source],
        "evidence_catalog": {"canonical_source_ids": ["document:1"]},
        "limitations": [],
        "coverage_facts": coverage or {"core": {"revenue": "covered"}},
    }
    packet["packet_hash"] = canonical_packet_hash(packet)
    return packet


def _config(**overrides) -> JevShadowConfig:
    values = {
        "enabled": True,
        "citation_relations": True,
        "missing_information": True,
        "max_calls": 2,
        "max_input_tokens": 100_000,
        "max_cost_usd": 10.0,
    }
    values.update(overrides)
    return JevShadowConfig(**values)


def _citation(*, anchor: str = "document:1#page:1", excerpt: str = "Revenue increased") -> dict:
    return {
        "source_id": "document:1",
        "anchor": anchor,
        "excerpt": excerpt,
        "span": {"start": 0, "end": len(excerpt)},
    }


def test_citation_relation_is_packet_backed_and_audited():
    client = FakeClient(label="supports")
    sidecar = JevShadowSidecar(config=_config(), client=client)

    result = sidecar.classify_citation_relation(
        _packet(), _citation(), "Revenue increased in Sweden."
    )

    assert result.status == "shadow_result"
    assert result.selected_class == "supports"
    assert set(result.probabilities or {}) == set(LABELS)
    assert result.returned_model == "jev-1.13.0"
    assert result.audit["packet_hash"]
    assert result.audit["source_id"] == "document:1"
    assert result.audit["anchor"] == "document:1#page:1"
    assert result.audit["span_checksum"]
    assert result.audit["claim_hash"]
    assert result.audit["question_hash"]
    assert result.audit["action"] == "shadow_only"
    assert client.calls[0]["state"]["packet_hash"] == result.packet_hash
    assert client.calls[0]["state"]["citation"]["excerpt"] == "Revenue increased"


@pytest.mark.parametrize(
    ("label", "claim", "excerpt"),
    [
        ("supports", "Bolagets intäkter ökade i Sverige.", "Revenue increased"),
        ("contradicts", "Revenue decreased in Sweden.", "Revenue increased"),
        ("says_nothing", "The company changed its dividend policy.", "Revenue increased"),
        (
            "insufficient_context",
            "Revenue increased because of pricing power.",
            "Revenue increased",
        ),
    ],
)
def test_citation_relation_supports_independently_labelled_swedish_and_english_cases(
    label: str, claim: str, excerpt: str
):
    client = FakeClient(label=label)
    result = JevShadowSidecar(config=_config(), client=client).classify_citation_relation(
        _packet(), _citation(excerpt=excerpt), claim
    )
    assert result.selected_class == label
    assert result.error_code is None


@pytest.mark.parametrize(
    "citation,expected",
    [
        (
            {"source_id": "document:missing", "anchor": "document:1#page:1", "excerpt": "Revenue"},
            "source_not_catalogued",
        ),
        (
            {"source_id": "document:1", "anchor": "document:1#page:2", "excerpt": "Revenue"},
            "anchor_not_found",
        ),
        (
            {"source_id": "document:1", "anchor": "document:1#page:1", "excerpt": "Not stored"},
            "excerpt_not_found",
        ),
    ],
)
def test_invalid_citations_never_reach_jev(citation: dict, expected: str):
    client = FakeClient()
    result = JevShadowSidecar(config=_config(), client=client).classify_citation_relation(
        _packet(), citation, "Revenue increased."
    )
    assert result.status == "invalid_input"
    assert result.error_code == expected
    assert client.calls == []


@pytest.mark.parametrize(
    "label", ["core_blocker", "supplemental_limitation", "covered_or_not_a_gap", "ambiguous_review"]
)
def test_missing_information_impact_is_shadow_only_for_all_classes(label: str):
    client = FakeClient(label=label)
    result = JevShadowSidecar(config=_config(), client=client).classify_missing_information(
        _packet(coverage={"core": {"revenue": "covered"}, "specialist": "missing"}),
        {"id": "specialist_report", "kind": "specialist"},
        "A named specialist must assess the report.",
    )
    assert result.selected_class == label
    assert result.deterministic_output_unchanged is True
    assert result.audit["missing_item_id"] == "specialist_report"
    assert result.audit["coverage_checksum"]
    assert result.audit["action"] == "shadow_only"


def test_missing_key_steps_aside_without_changing_packet():
    packet = _packet()
    before = packet.copy()
    result = JevShadowSidecar(config=_config(), client=None).classify_missing_information(
        packet, "specialist_report", "A named specialist is required."
    )
    assert result.status == "error"
    assert result.error_code == "missing_api_key_or_sdk"
    assert packet == before


class TypeSafeAPITimeoutError(Exception):
    pass


class TypeSafeRateLimitError(Exception):
    pass


class ErrorClient:
    def __init__(self, error: Exception):
        self.error = error
        self.calls = 0

    def system_one(self, **kwargs):
        self.calls += 1
        raise self.error


@pytest.mark.parametrize(
    ("error", "expected"),
    [(TypeSafeAPITimeoutError(), "timeout"), (TypeSafeRateLimitError(), "rate_limit")],
)
def test_timeout_and_rate_limit_step_aside_without_repair(error: Exception, expected: str):
    client = ErrorClient(error)
    result = JevShadowSidecar(
        config=_config(max_transport_retries=1), client=client
    ).classify_citation_relation(_packet(), _citation(), "Revenue increased.")
    assert result.status == "error"
    assert result.error_code == expected
    assert client.calls == 1


def test_malformed_response_is_one_attempt_and_has_no_fallback():
    class MalformedClient:
        calls = 0

        def system_one(self, **kwargs):
            self.calls += 1
            return Response(
                model="jev-1.13.0",
                answers={"citation_relation": {"choice": "supports", "confidence": 0.9}},
                usage=Usage(),
            )

    client = MalformedClient()
    result = JevShadowSidecar(config=_config(), client=client).classify_citation_relation(
        _packet(), _citation(), "Revenue increased."
    )
    assert result.error_code == "malformed_response"
    assert client.calls == 1
    assert result.selected_class is None


def test_changed_packet_model_and_question_inputs_get_distinct_audit_identity():
    client = FakeClient()
    sidecar = JevShadowSidecar(config=_config(), client=client)
    first = sidecar.classify_citation_relation(_packet(), _citation(), "Revenue increased.")
    changed = _packet()
    changed["sources"][0]["pages"][0]["text"] = "Revenue fell."
    changed["sources"][0]["pages"][0]["text_checksum"] = hashlib.sha256(
        b"Revenue fell."
    ).hexdigest()
    changed["sources"][0]["extraction"]["text_checksum"] = hashlib.sha256(
        b"[page 1]\nRevenue fell."
    ).hexdigest()
    changed["packet_hash"] = canonical_packet_hash(
        {key: value for key, value in changed.items() if key != "packet_hash"}
    )
    second = sidecar.classify_citation_relation(
        changed, _citation(excerpt="Revenue fell."), "Revenue fell."
    )
    assert first.audit["input_hash"] != second.audit["input_hash"]
    assert first.packet_hash != second.packet_hash

    mismatch = JevShadowSidecar(
        config=_config(model_version="jev-1.14.0"), client=client
    ).classify_citation_relation(_packet(), _citation(), "Revenue increased.")
    assert mismatch.error_code == "model_version_mismatch"


def test_persisted_audit_is_append_only_and_contains_no_secret():
    conn = get_connection(path=":memory:")
    init_db(conn)
    client = FakeClient()
    result = JevShadowSidecar(
        config=_config(), client=client, conn=conn
    ).classify_citation_relation(_packet(), _citation(), "Revenue increased.")
    row = conn.execute("SELECT * FROM jev_shadow_audit").fetchone()
    assert row is not None
    assert row["selected_class"] == result.selected_class
    assert "TYPESAFE_API_KEY" not in str(dict(row))
    with pytest.raises(Exception, match="append-only"):
        conn.execute("DELETE FROM jev_shadow_audit")
