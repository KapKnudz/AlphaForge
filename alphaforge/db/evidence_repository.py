"""Append-only persistence for immutable evidence facts.

This module deliberately does not acquire objects or select manifest slots. Callers
supply canonical provider identities and verified object metadata.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from alphaforge.evidence.artifact_store import DEFAULT_MAX_PDF_BYTES


class ImmutableEvidenceConflict(ValueError):
    """A stable identity already exists with a different immutable payload."""


@dataclass(frozen=True)
class ObservationBatchInput:
    company_id: int
    as_of: str
    source_input_fingerprint: str
    report_rules_fingerprint: str
    effective_at: str
    first_recorded_at: str


@dataclass(frozen=True)
class CandidateInput:
    company_id: int
    release_source_url: str
    first_observed_at: str


@dataclass(frozen=True)
class ArtifactInput:
    sha256: str
    byte_size: int
    content_type: str
    first_observed_at: str


@dataclass(frozen=True)
class ArtifactObjectInput:
    artifact_id: str
    object_uri: str
    storage_kind: str
    verified_sha256: str
    verified_size: int
    stored_at: str
    acquisition_max_pdf_bytes: int | None = None


@dataclass(frozen=True)
class AttachmentObservationInput:
    candidate_key: str
    artifact_id: str
    batch_id: str
    attachment_source_url: str
    content_type: str
    http_status: int | None
    feed_attachment_attested: bool
    raw_metadata: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class ExtractionPage:
    page_number: int
    anchor: str
    text: str
    text_checksum: str


@dataclass(frozen=True)
class ExtractionInput:
    artifact_id: str
    extractor: str
    extractor_version: str
    config_fingerprint: str
    text_checksum: str
    page_count: int
    pages_included: str | None
    page_truncated: bool
    scanned: bool
    limitations: Sequence[Any]
    extracted_at: str
    pages: Sequence[ExtractionPage]


@dataclass(frozen=True)
class CandidateObservationInput:
    candidate_key: str
    batch_id: str
    authoritative_feed_title: str | None
    detail_title: str | None
    published_at: str | None
    language: str | None
    report_kind: str | None
    document_type: str | None
    fiscal_period: str | None
    period_start: str | None
    period_end: str | None
    feed_report_identity: str | None
    invitation_veto: bool
    eligibility: str
    eligibility_reason: str
    attachment_observation_id: str | None
    extraction_id: str | None
    report_rules_fingerprint: str
    raw_metadata: Mapping[str, Any]


@dataclass(frozen=True)
class RelationObservationInput:
    batch_id: str
    left_candidate_observation_id: str
    right_candidate_observation_id: str
    relation_type: str
    disposition: str
    corroboration: Mapping[str, Any]
    rules_fingerprint: str


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_canonical_date(value: str, *, field: str) -> None:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be a canonical date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be a canonical date")


def _require_canonical_batch(value: ObservationBatchInput) -> None:
    _require_canonical_date(value.as_of, field="observation batch as_of")
    for field, timestamp in (
        ("effective_at", value.effective_at),
        ("first_recorded_at", value.first_recorded_at),
    ):
        try:
            parsed = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        except ValueError as exc:
            raise ValueError(f"observation batch {field} must be canonical UTC") from exc
        canonical = parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")
        if canonical != timestamp:
            raise ValueError(f"observation batch {field} must be canonical UTC")


def _timestamp_postdates(timestamp: str, canonical_batch_timestamp: str) -> bool:
    try:
        observed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("referenced timestamp must be timezone-aware ISO 8601") from exc
    if observed_at.tzinfo is None:
        raise ValueError("referenced timestamp must be timezone-aware ISO 8601")
    batch_at = datetime.strptime(canonical_batch_timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=UTC
    )
    return observed_at.astimezone(UTC) > batch_at


def make_batch_id(value: ObservationBatchInput) -> str:
    _require_canonical_batch(value)
    return _stable_hash(
        {
            "provider": "mfn",
            "company_id": value.company_id,
            "as_of": value.as_of,
            "source_input_fingerprint": value.source_input_fingerprint,
            "report_rules_fingerprint": value.report_rules_fingerprint,
            "effective_at": value.effective_at,
        }
    )


def make_candidate_key(company_id: int, release_source_url: str) -> str:
    return _stable_hash(
        {
            "provider": "mfn",
            "company_id": company_id,
            "canonical_release_source_url": release_source_url.strip(),
        }
    )


def _row(conn: Any, table: str, identity_column: str, identity: str) -> dict[str, Any] | None:
    found = conn.execute(
        f"SELECT * FROM {table} WHERE {identity_column}=?",
        (identity,),  # noqa: S608
    ).fetchone()
    return dict(found) if found is not None else None


def _require_same(
    row: Mapping[str, Any], expected: Mapping[str, Any], *, identity: str
) -> dict[str, Any]:
    mismatches = [key for key, value in expected.items() if row.get(key) != value]
    if mismatches:
        raise ImmutableEvidenceConflict(
            f"immutable identity {identity} has conflicting fields: {', '.join(mismatches)}"
        )
    return dict(row)


def append_observation_batch(conn: Any, value: ObservationBatchInput) -> dict[str, Any]:
    identity = make_batch_id(value)
    existing = _row(conn, "evidence_observation_batches", "batch_id", identity)
    expected = {
        "company_id": value.company_id,
        "provider": "mfn",
        "as_of": value.as_of,
        "source_input_fingerprint": value.source_input_fingerprint,
        "report_rules_fingerprint": value.report_rules_fingerprint,
        "effective_at": value.effective_at,
        "first_recorded_at": value.first_recorded_at,
    }
    if existing is not None:
        return _require_same(existing, expected, identity=identity)
    previous = conn.execute(
        """SELECT effective_at FROM evidence_observation_batches
           WHERE company_id=? AND provider='mfn'
           ORDER BY effective_at DESC, batch_id DESC LIMIT 1""",
        (value.company_id,),
    ).fetchone()
    if previous is not None and value.effective_at <= str(previous[0]):
        raise ValueError("new observation batch effective_at must be strictly monotonic")
    conn.execute(
        """INSERT INTO evidence_observation_batches
           (batch_id, company_id, provider, as_of, source_input_fingerprint,
            report_rules_fingerprint, effective_at, first_recorded_at)
           VALUES (?, ?, 'mfn', ?, ?, ?, ?, ?)""",
        (
            identity,
            value.company_id,
            value.as_of,
            value.source_input_fingerprint,
            value.report_rules_fingerprint,
            value.effective_at,
            value.first_recorded_at,
        ),
    )
    return _row(conn, "evidence_observation_batches", "batch_id", identity) or {}


def _append_legacy_observation_batch(conn: Any, value: ObservationBatchInput) -> dict[str, Any]:
    """Import historical chronology without applying the live monotonic clock rule."""
    identity = make_batch_id(value)
    existing = _row(conn, "evidence_observation_batches", "batch_id", identity)
    expected = {
        "company_id": value.company_id,
        "provider": "mfn",
        "as_of": value.as_of,
        "source_input_fingerprint": value.source_input_fingerprint,
        "report_rules_fingerprint": value.report_rules_fingerprint,
        "effective_at": value.effective_at,
        "first_recorded_at": value.first_recorded_at,
    }
    if existing is not None:
        return _require_same(existing, expected, identity=identity)
    conn.execute(
        """INSERT INTO evidence_observation_batches
           (batch_id, company_id, provider, as_of, source_input_fingerprint,
            report_rules_fingerprint, effective_at, first_recorded_at)
           VALUES (?, ?, 'mfn', ?, ?, ?, ?, ?)""",
        (
            identity,
            value.company_id,
            value.as_of,
            value.source_input_fingerprint,
            value.report_rules_fingerprint,
            value.effective_at,
            value.first_recorded_at,
        ),
    )
    return _row(conn, "evidence_observation_batches", "batch_id", identity) or {}


def append_candidate(conn: Any, value: CandidateInput) -> dict[str, Any]:
    source_url = value.release_source_url.strip()
    identity = make_candidate_key(value.company_id, source_url)
    existing = _row(conn, "evidence_candidates", "candidate_key", identity)
    expected = {
        "company_id": value.company_id,
        "provider": "mfn",
        "release_source_url": source_url,
    }
    if existing is not None:
        return _require_same(existing, expected, identity=identity)
    conn.execute(
        """INSERT INTO evidence_candidates
           (candidate_key, company_id, provider, release_source_url, first_observed_at)
           VALUES (?, ?, 'mfn', ?, ?)""",
        (identity, value.company_id, source_url, value.first_observed_at),
    )
    return _row(conn, "evidence_candidates", "candidate_key", identity) or {}


def append_artifact(conn: Any, value: ArtifactInput) -> dict[str, Any]:
    digest = value.sha256.lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("artifact sha256 must be 64 lowercase hexadecimal characters")
    identity = f"sha256:{digest}"
    existing = _row(conn, "evidence_artifacts", "artifact_id", identity)
    expected = {
        "sha256": digest,
        "byte_size": value.byte_size,
        "content_type": value.content_type,
    }
    if existing is not None:
        return _require_same(existing, expected, identity=identity)
    conn.execute(
        """INSERT INTO evidence_artifacts
           (artifact_id, sha256, byte_size, content_type, first_observed_at)
           VALUES (?, ?, ?, ?, ?)""",
        (identity, digest, value.byte_size, value.content_type, value.first_observed_at),
    )
    return _row(conn, "evidence_artifacts", "artifact_id", identity) or {}


def append_artifact_object(conn: Any, value: ArtifactObjectInput) -> dict[str, Any]:
    artifact = _row(conn, "evidence_artifacts", "artifact_id", value.artifact_id)
    if artifact is None:
        raise ValueError("artifact object references an unknown artifact")
    if value.verified_sha256 != artifact["sha256"] or value.verified_size != artifact["byte_size"]:
        raise ValueError("verified object hash and size must match artifact metadata")
    if (
        value.acquisition_max_pdf_bytes is not None
        and value.acquisition_max_pdf_bytes < value.verified_size
    ):
        raise ValueError("artifact acquisition limit must accommodate verified size")
    identity = _stable_hash(
        {
            "artifact_id": value.artifact_id,
            "object_uri": value.object_uri,
            "verified_sha256": value.verified_sha256,
            "verified_size": value.verified_size,
        }
    )
    existing = _row(conn, "evidence_artifact_objects", "object_record_id", identity)
    expected = {
        "artifact_id": artifact["id"],
        "object_uri": value.object_uri,
        "storage_kind": value.storage_kind,
        "verified_sha256": value.verified_sha256,
        "verified_size": value.verified_size,
        "stored_at": value.stored_at,
        "acquisition_max_pdf_bytes": value.acquisition_max_pdf_bytes,
    }
    if existing is not None:
        stable_expected = {
            key: field_value
            for key, field_value in expected.items()
            if key not in {"stored_at", "acquisition_max_pdf_bytes"}
        }
        result = _require_same(existing, stable_expected, identity=identity)
        if (
            result["acquisition_max_pdf_bytes"] is None
            and value.acquisition_max_pdf_bytes is not None
        ):
            conn.execute(
                """UPDATE evidence_artifact_objects
                   SET acquisition_max_pdf_bytes=? WHERE id=?""",
                (value.acquisition_max_pdf_bytes, result["id"]),
            )
            return _row(conn, "evidence_artifact_objects", "id", result["id"]) or {}
        return result
    conn.execute(
        """INSERT INTO evidence_artifact_objects
           (object_record_id, artifact_id, object_uri, storage_kind,
            verified_sha256, verified_size, stored_at, acquisition_max_pdf_bytes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            identity,
            artifact["id"],
            value.object_uri,
            value.storage_kind,
            value.verified_sha256,
            value.verified_size,
            value.stored_at,
            value.acquisition_max_pdf_bytes,
        ),
    )
    return _row(conn, "evidence_artifact_objects", "object_record_id", identity) or {}


def append_attachment_observation(conn: Any, value: AttachmentObservationInput) -> dict[str, Any]:
    return _append_attachment_observation(conn, value, allow_later_identity=False)


def _append_attachment_observation(
    conn: Any,
    value: AttachmentObservationInput,
    *,
    allow_later_identity: bool,
) -> dict[str, Any]:
    candidate = _row(conn, "evidence_candidates", "candidate_key", value.candidate_key)
    artifact = _row(conn, "evidence_artifacts", "artifact_id", value.artifact_id)
    batch = _row(conn, "evidence_observation_batches", "batch_id", value.batch_id)
    if candidate is None or artifact is None or batch is None:
        raise ValueError("attachment observation references an unknown identity")
    if candidate["company_id"] != batch["company_id"]:
        raise ValueError("attachment candidate and batch belong to different companies")
    candidate_postdates = _timestamp_postdates(
        str(candidate["first_observed_at"]), str(batch["effective_at"])
    )
    artifact_postdates = _timestamp_postdates(
        str(artifact["first_observed_at"]), str(batch["effective_at"])
    )
    if not allow_later_identity and candidate_postdates:
        raise ValueError("attachment candidate postdates its batch")
    if not allow_later_identity and artifact_postdates:
        raise ValueError("attachment artifact postdates its batch")
    metadata = _canonical_json(value.raw_metadata) if value.raw_metadata is not None else None
    identity = _stable_hash(
        {
            "candidate_key": value.candidate_key,
            "attachment_source_url": value.attachment_source_url,
            "artifact_id": value.artifact_id,
            "batch_id": value.batch_id,
            "feed_attachment_attestation": bool(value.feed_attachment_attested),
            "http_provenance": {
                "status": value.http_status,
                "content_type": value.content_type,
                "raw_metadata": value.raw_metadata,
            },
        }
    )
    expected = {
        "candidate_id": candidate["id"],
        "artifact_id": artifact["id"],
        "batch_id": batch["id"],
        "attachment_source_url": value.attachment_source_url,
        "content_type": value.content_type,
        "http_status": value.http_status,
        "feed_attachment_attested": int(value.feed_attachment_attested),
        "raw_metadata": metadata,
    }
    existing = _row(conn, "evidence_attachment_observations", "attachment_observation_id", identity)
    if existing is not None:
        return _require_same(existing, expected, identity=identity)
    conn.execute(
        """INSERT INTO evidence_attachment_observations
           (attachment_observation_id, candidate_id, artifact_id, batch_id,
            attachment_source_url, content_type, http_status,
            feed_attachment_attested, raw_metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (identity, *expected.values()),
    )
    return (
        _row(conn, "evidence_attachment_observations", "attachment_observation_id", identity) or {}
    )


def append_extraction(conn: Any, value: ExtractionInput) -> dict[str, Any]:
    artifact = _row(conn, "evidence_artifacts", "artifact_id", value.artifact_id)
    if artifact is None:
        raise ValueError("extraction references an unknown artifact")
    ordered_pages = sorted(value.pages, key=lambda page: page.page_number)
    if len({page.page_number for page in ordered_pages}) != len(ordered_pages):
        raise ValueError("extraction page numbers must be unique")
    limitations = _canonical_json(list(value.limitations))
    identity = _stable_hash(
        {
            "artifact_id": value.artifact_id,
            "extractor": value.extractor,
            "extractor_version": value.extractor_version,
            "config_fingerprint": value.config_fingerprint,
            "text_checksum": value.text_checksum,
            "page_count": value.page_count,
            "pages_included": value.pages_included,
            "page_truncated": int(value.page_truncated),
            "scanned": int(value.scanned),
            "limitations": list(value.limitations),
            "pages": [asdict(page) for page in ordered_pages],
        }
    )
    expected = {
        "artifact_id": artifact["id"],
        "extractor": value.extractor,
        "extractor_version": value.extractor_version,
        "config_fingerprint": value.config_fingerprint,
        "text_checksum": value.text_checksum,
        "page_count": value.page_count,
        "pages_included": value.pages_included,
        "page_truncated": int(value.page_truncated),
        "scanned": int(value.scanned),
        "limitations": limitations,
        "extracted_at": value.extracted_at,
    }
    existing = _row(conn, "evidence_artifact_extractions", "extraction_id", identity)
    if existing is not None:
        stable_expected = {
            key: field_value for key, field_value in expected.items() if key != "extracted_at"
        }
        result = _require_same(existing, stable_expected, identity=identity)
        persisted_pages = [
            dict(row)
            for row in conn.execute(
                """SELECT page_number, anchor, text, text_checksum
                   FROM evidence_artifact_pages WHERE extraction_id=? ORDER BY page_number""",
                (existing["id"],),
            ).fetchall()
        ]
        if persisted_pages != [asdict(page) for page in ordered_pages]:
            raise ImmutableEvidenceConflict(
                f"immutable extraction {identity} has conflicting pages"
            )
        return result
    conn.execute("SAVEPOINT append_evidence_extraction")
    try:
        conn.execute(
            """INSERT INTO evidence_artifact_extractions
               (extraction_id, artifact_id, extractor, extractor_version, config_fingerprint,
                text_checksum, page_count, pages_included, page_truncated, scanned,
                limitations, extracted_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (identity, *expected.values()),
        )
        extraction = _row(conn, "evidence_artifact_extractions", "extraction_id", identity) or {}
        for page in ordered_pages:
            conn.execute(
                """INSERT INTO evidence_artifact_pages
                   (extraction_id, page_number, anchor, text, text_checksum)
                   VALUES (?, ?, ?, ?, ?)""",
                (extraction["id"], page.page_number, page.anchor, page.text, page.text_checksum),
            )
    except Exception:
        conn.execute("ROLLBACK TO append_evidence_extraction")
        conn.execute("RELEASE append_evidence_extraction")
        raise
    conn.execute("RELEASE append_evidence_extraction")
    return extraction


def append_candidate_observation(conn: Any, value: CandidateObservationInput) -> dict[str, Any]:
    return _append_candidate_observation(conn, value, allow_later_identity=False)


def _append_candidate_observation(
    conn: Any,
    value: CandidateObservationInput,
    *,
    allow_later_identity: bool,
) -> dict[str, Any]:
    candidate = _row(conn, "evidence_candidates", "candidate_key", value.candidate_key)
    batch = _row(conn, "evidence_observation_batches", "batch_id", value.batch_id)
    if candidate is None or batch is None:
        raise ValueError("candidate observation references an unknown candidate or batch")
    if candidate["company_id"] != batch["company_id"]:
        raise ValueError("candidate observation candidate and batch belong to different companies")
    candidate_postdates = _timestamp_postdates(
        str(candidate["first_observed_at"]), str(batch["effective_at"])
    )
    if not allow_later_identity and candidate_postdates:
        raise ValueError("candidate observation candidate postdates its batch")
    attachment = None
    if value.attachment_observation_id is not None:
        attachment = _row(
            conn,
            "evidence_attachment_observations",
            "attachment_observation_id",
            value.attachment_observation_id,
        )
        if attachment is None or attachment["candidate_id"] != candidate["id"]:
            raise ValueError("candidate observation attachment belongs to another candidate")
        attachment_batch = conn.execute(
            "SELECT as_of, effective_at FROM evidence_observation_batches WHERE id=?",
            (attachment["batch_id"],),
        ).fetchone()
        if attachment_batch is None or (
            str(attachment_batch[0]) > str(batch["as_of"])
            or str(attachment_batch[1]) > str(batch["effective_at"])
        ):
            raise ValueError("candidate observation attachment postdates its batch")
    extraction = None
    if value.extraction_id is not None:
        extraction = _row(
            conn, "evidence_artifact_extractions", "extraction_id", value.extraction_id
        )
        if extraction is None:
            raise ValueError("candidate observation references an unknown extraction")
        if attachment is None or extraction["artifact_id"] != attachment["artifact_id"]:
            raise ValueError("candidate observation extraction and attachment artifacts differ")
        if _timestamp_postdates(str(extraction["extracted_at"]), str(batch["effective_at"])):
            raise ValueError("candidate observation extraction postdates its batch")
    if value.report_rules_fingerprint != batch["report_rules_fingerprint"]:
        raise ValueError("candidate observation rules fingerprint differs from its batch")
    metadata = _canonical_json(value.raw_metadata)
    identity_payload = asdict(value)
    identity_payload["invitation_veto"] = bool(value.invitation_veto)
    identity = _stable_hash(identity_payload)
    expected = {
        "candidate_id": candidate["id"],
        "batch_id": batch["id"],
        "authoritative_feed_title": value.authoritative_feed_title,
        "detail_title": value.detail_title,
        "published_at": value.published_at,
        "language": value.language,
        "report_kind": value.report_kind,
        "document_type": value.document_type,
        "fiscal_period": value.fiscal_period,
        "period_start": value.period_start,
        "period_end": value.period_end,
        "feed_report_identity": value.feed_report_identity,
        "invitation_veto": int(value.invitation_veto),
        "eligibility": value.eligibility,
        "eligibility_reason": value.eligibility_reason,
        "attachment_observation_id": attachment["id"] if attachment else None,
        "extraction_id": extraction["id"] if extraction else None,
        "report_rules_fingerprint": value.report_rules_fingerprint,
        "raw_metadata": metadata,
    }
    existing = _row(conn, "evidence_candidate_observations", "candidate_observation_id", identity)
    if existing is not None:
        return _require_same(existing, expected, identity=identity)
    if (
        conn.execute(
            "SELECT 1 FROM evidence_candidate_observations WHERE candidate_id=? AND batch_id=?",
            (candidate["id"], batch["id"]),
        ).fetchone()
        is not None
    ):
        raise ImmutableEvidenceConflict(
            "observation batch already contains a different state for this candidate"
        )
    placeholders = ", ".join("?" for _ in range(19))
    conn.execute(
        f"""INSERT INTO evidence_candidate_observations
            (candidate_observation_id, candidate_id, batch_id, authoritative_feed_title,
             detail_title, published_at, language, report_kind, document_type,
             fiscal_period, period_start, period_end, feed_report_identity,
             invitation_veto, eligibility, eligibility_reason,
             attachment_observation_id, extraction_id, report_rules_fingerprint, raw_metadata)
            VALUES (?, {placeholders})""",  # noqa: S608
        (identity, *expected.values()),
    )
    return _row(conn, "evidence_candidate_observations", "candidate_observation_id", identity) or {}


def _relation_document(conn: Any, observation: Mapping[str, Any]) -> dict[str, Any]:
    attachment = conn.execute(
        "SELECT attachment_source_url FROM evidence_attachment_observations WHERE id=?",
        (observation["attachment_observation_id"],),
    ).fetchone()
    return {
        "title": " ".join(
            filter(
                None,
                (observation["authoritative_feed_title"], observation["detail_title"]),
            )
        ),
        "attachment_url": attachment[0] if attachment is not None else None,
        "raw_metadata": _json_object(observation["raw_metadata"]),
    }


def _extraction_text(conn: Any, extraction_id: int | None) -> str:
    if extraction_id is None:
        return ""
    return "\n".join(
        str(row[0])
        for row in conn.execute(
            "SELECT text FROM evidence_artifact_pages WHERE extraction_id=? ORDER BY page_number",
            (extraction_id,),
        ).fetchall()
    )


def _require_assertion_corroboration(
    conn: Any,
    relation_type: str,
    corroboration: Mapping[str, Any],
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    issuer: str,
) -> None:
    from alphaforge.evidence.ingest import (
        _has_revision_markers,
        _numeric_key_figure_fingerprint,
        _numeric_similarity,
        _translation_neutral_title,
    )

    if (
        not left["report_kind"]
        or left["report_kind"] != right["report_kind"]
        or (
            left["document_type"]
            and right["document_type"]
            and left["document_type"] != right["document_type"]
        )
    ):
        raise ValueError("asserted relation requires matching report kinds")
    languages = {str(left["language"] or "").lower(), str(right["language"] or "").lower()}
    if not languages <= {"en", "sv"} or "" in languages:
        raise ValueError("asserted relation requires resolved English or Swedish languages")
    has_revision = any(
        _has_revision_markers(_relation_document(conn, observation))
        for observation in (left, right)
    )
    if relation_type == "TRANSLATION" and (len(languages) != 2 or has_revision):
        raise ValueError(
            "translation relation requires opposite languages without revision markers"
        )
    if relation_type == "REVISION" and not has_revision:
        raise ValueError("revision relation requires persisted revision evidence")

    strong = corroboration.get("strong_corroborator")
    compatible = corroboration.get("compatible_signals")
    if (
        not isinstance(strong, Mapping)
        or not isinstance(compatible, Sequence)
        or isinstance(compatible, (str, bytes))
    ):
        raise ValueError("asserted relation requires structured corroboration")
    kind = strong.get("kind")
    value = strong.get("value")
    directly_legacy = any(
        bool(_json_object(observation["raw_metadata"]).get("legacy_import"))
        for observation in (left, right)
    )
    if kind == "shared_provider_event_id":
        inherited_legacy = (
            isinstance(value, str)
            and conn.execute(
                """SELECT 1 FROM evidence_candidate_observations
                   WHERE feed_report_identity=?
                     AND json_extract(raw_metadata, '$.legacy_import')=1
                   LIMIT 1""",
                (value,),
            ).fetchone()
            is not None
        )
        valid_strong = (
            isinstance(value, str)
            and bool(value)
            and value != "mfn-report-tag+archive-report-pdf"
            and not (directly_legacy or inherited_legacy)
            and all(observation["feed_report_identity"] == value for observation in (left, right))
        )
    elif kind == "shared_attachment_checksum":
        attachments = [
            conn.execute(
                """SELECT a.sha256, ao.artifact_id FROM evidence_attachment_observations ao
                   JOIN evidence_artifacts a ON a.id=ao.artifact_id WHERE ao.id=?""",
                (observation["attachment_observation_id"],),
            ).fetchone()
            for observation in (left, right)
        ]
        inherited_legacy = any(
            attachment is not None
            and conn.execute(
                """SELECT 1 FROM evidence_attachment_observations
                   WHERE artifact_id=?
                     AND json_extract(raw_metadata, '$.legacy_import')=1
                   LIMIT 1""",
                (attachment["artifact_id"],),
            ).fetchone()
            is not None
            for attachment in attachments
        )
        valid_strong = (
            not (directly_legacy or inherited_legacy)
            and bool(value)
            and all(
                attachment is not None and attachment["sha256"] == str(value).lower()
                for attachment in attachments
            )
        )
    else:
        numeric_fingerprints = [
            _numeric_key_figure_fingerprint(_extraction_text(conn, observation["extraction_id"]))
            for observation in (left, right)
        ]
        numeric_similarity = _numeric_similarity(*numeric_fingerprints)
        valid_strong = (
            kind == "numeric_key_figure_jaccard"
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and numeric_similarity >= 0.5
            and abs(float(value) - numeric_similarity) < 1e-12
        )
    normalized_titles = [
        _translation_neutral_title(_relation_document(conn, observation), issuer)
        for observation in (left, right)
    ]
    actual_signals = {
        "fiscal_period": bool(left["fiscal_period"])
        and left["fiscal_period"] == right["fiscal_period"],
        "resolved_observation_date": bool(left["period_end"])
        and left["period_end"] == right["period_end"],
        "publication_date": bool(left["published_at"])
        and str(left["published_at"])[:10] == str(right["published_at"])[:10],
        "translation_neutral_title": bool(normalized_titles[0])
        and normalized_titles[0] == normalized_titles[1],
    }
    distinct_signals = {
        signal
        for signal in compatible
        if isinstance(signal, str) and actual_signals.get(signal, False)
    }
    if not valid_strong or len(distinct_signals) < 2:
        raise ValueError(
            "asserted relation requires a strong corroborator and two compatible signals"
        )


def append_relation_observation(conn: Any, value: RelationObservationInput) -> dict[str, Any]:
    left = _row(
        conn,
        "evidence_candidate_observations",
        "candidate_observation_id",
        value.left_candidate_observation_id,
    )
    right = _row(
        conn,
        "evidence_candidate_observations",
        "candidate_observation_id",
        value.right_candidate_observation_id,
    )
    batch = _row(conn, "evidence_observation_batches", "batch_id", value.batch_id)
    if left is None or right is None or batch is None:
        raise ValueError("relation observation references an unknown identity")
    source_batches = conn.execute(
        """SELECT as_of, effective_at FROM evidence_observation_batches
           WHERE id IN (?, ?)""",
        (left["batch_id"], right["batch_id"]),
    ).fetchall()
    if len(source_batches) != len({left["batch_id"], right["batch_id"]}) or any(
        str(source_batch[0]) > str(batch["as_of"])
        or str(source_batch[1]) > str(batch["effective_at"])
        for source_batch in source_batches
    ):
        raise ValueError("relation candidate observation postdates its batch")
    left_candidate = conn.execute(
        "SELECT candidate_key, company_id FROM evidence_candidates WHERE id=?",
        (left["candidate_id"],),
    ).fetchone()
    right_candidate = conn.execute(
        "SELECT candidate_key, company_id FROM evidence_candidates WHERE id=?",
        (right["candidate_id"],),
    ).fetchone()
    if left_candidate is None or right_candidate is None:
        raise ValueError("relation candidate is missing")
    if left_candidate[1] != right_candidate[1] or left_candidate[1] != batch["company_id"]:
        raise ValueError("relation candidates and batch must belong to one company")
    company = conn.execute(
        "SELECT name, ticker FROM companies WHERE id=?", (left_candidate[1],)
    ).fetchone()
    if company is None:
        raise ValueError("relation company is missing")
    if value.disposition == "asserted":
        issuer = " ".join(str(part) for part in company if part)
        _require_assertion_corroboration(
            conn, value.relation_type, value.corroboration, left, right, issuer
        )
    if left_candidate[0] == right_candidate[0]:
        raise ValueError("relation requires two independent candidates")
    if left_candidate[0] > right_candidate[0]:
        left, right = right, left
        left_candidate, right_candidate = right_candidate, left_candidate
    relation_key = _stable_hash(
        {
            "canonical_left_candidate_key": left_candidate[0],
            "canonical_right_candidate_key": right_candidate[0],
            "relation_type": value.relation_type,
        }
    )
    identity = _stable_hash(
        {
            "relation_key": relation_key,
            "batch_id": value.batch_id,
            "left_candidate_observation_id": left["candidate_observation_id"],
            "right_candidate_observation_id": right["candidate_observation_id"],
            "disposition": value.disposition,
            "corroboration": value.corroboration,
            "rules_fingerprint": value.rules_fingerprint,
        }
    )
    expected = {
        "relation_key": relation_key,
        "batch_id": batch["id"],
        "left_candidate_observation_id": left["id"],
        "right_candidate_observation_id": right["id"],
        "relation_type": value.relation_type,
        "disposition": value.disposition,
        "corroboration": _canonical_json(value.corroboration),
        "rules_fingerprint": value.rules_fingerprint,
    }
    existing = _row(
        conn,
        "evidence_candidate_relation_observations",
        "relation_observation_id",
        identity,
    )
    if existing is not None:
        return _require_same(existing, expected, identity=identity)
    if (
        conn.execute(
            """SELECT 1 FROM evidence_candidate_relation_observations
           WHERE relation_key=? AND batch_id=?""",
            (relation_key, batch["id"]),
        ).fetchone()
        is not None
    ):
        raise ImmutableEvidenceConflict(
            "observation batch already contains a different state for this relation"
        )
    conn.execute(
        """INSERT INTO evidence_candidate_relation_observations
           (relation_observation_id, relation_key, batch_id,
            left_candidate_observation_id, right_candidate_observation_id,
            relation_type, disposition, corroboration, rules_fingerprint)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (identity, *expected.values()),
    )
    return (
        _row(
            conn,
            "evidence_candidate_relation_observations",
            "relation_observation_id",
            identity,
        )
        or {}
    )


def current_candidate_observations(
    conn: Any, *, company_id: int, as_of: str
) -> list[dict[str, Any]]:
    _require_canonical_date(as_of, field="candidate observation as_of")
    rows = conn.execute(
        """WITH ranked AS (
               SELECT o.*, c.candidate_key, c.release_source_url,
                      b.batch_id AS stable_batch_id, b.effective_at,
                      ROW_NUMBER() OVER (
                          PARTITION BY o.candidate_id
                          ORDER BY b.as_of DESC, b.effective_at DESC, b.batch_id DESC
                      ) AS precedence_rank
               FROM evidence_candidate_observations o
               JOIN evidence_candidates c ON c.id=o.candidate_id
               JOIN evidence_observation_batches b ON b.id=o.batch_id
               WHERE c.company_id=? AND b.as_of <= ?
           )
           SELECT * FROM ranked WHERE precedence_rank=1 ORDER BY candidate_key""",
        (company_id, as_of),
    ).fetchall()
    return [dict(row) for row in rows]


def current_relation_observations(
    conn: Any, *, company_id: int, as_of: str
) -> list[dict[str, Any]]:
    _require_canonical_date(as_of, field="relation observation as_of")
    rows = conn.execute(
        """WITH ranked AS (
               SELECT r.*, b.batch_id AS stable_batch_id, b.effective_at,
                      ROW_NUMBER() OVER (
                          PARTITION BY r.relation_key
                          ORDER BY b.as_of DESC, b.effective_at DESC, b.batch_id DESC
                      ) AS precedence_rank
               FROM evidence_candidate_relation_observations r
               JOIN evidence_observation_batches b ON b.id=r.batch_id
               WHERE b.company_id=? AND b.as_of <= ?
           )
           SELECT * FROM ranked WHERE precedence_rank=1 ORDER BY relation_key""",
        (company_id, as_of),
    ).fetchall()
    return [dict(row) for row in rows]


def manifest_v2_projection(
    conn: Any,
    *,
    company_id: int,
    as_of: str,
    artifact_store: Any | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Project current immutable observations into manifest candidates and packet rows.

    Once a company/window has immutable observations, this projection is the
    complete selection input. It never supplements missing V2 facts from the
    mutable legacy document tables.
    """
    observations = current_candidate_observations(conn, company_id=company_id, as_of=as_of)
    if artifact_store is None:
        from alphaforge.evidence.artifact_store import ArtifactUnavailableError

        raise ArtifactUnavailableError(
            "artifact_verifier_unavailable", "V2 projection requires an artifact store"
        )
    relations = current_relation_observations(conn, company_id=company_id, as_of=as_of)
    asserted = [row for row in relations if row["disposition"] == "asserted"]
    observation_by_id = {str(row["candidate_observation_id"]): row for row in observations}
    relation_membership: dict[str, list[dict[str, Any]]] = {}
    relation_bindings: dict[str, dict[str, Any]] = {}
    parent = {stable_id: stable_id for stable_id in observation_by_id}

    def component(stable_id: str) -> str:
        while parent[stable_id] != stable_id:
            parent[stable_id] = parent[parent[stable_id]]
            stable_id = parent[stable_id]
        return stable_id

    for relation in asserted:
        endpoints = [
            conn.execute(
                "SELECT candidate_observation_id FROM evidence_candidate_observations WHERE id=?",
                (relation[field],),
            ).fetchone()
            for field in (
                "left_candidate_observation_id",
                "right_candidate_observation_id",
            )
        ]
        if any(endpoint is None for endpoint in endpoints):
            continue
        left_id, right_id = (str(endpoint[0]) for endpoint in endpoints)
        if left_id not in observation_by_id or right_id not in observation_by_id:
            continue
        relation_membership.setdefault(left_id, []).append(relation)
        relation_membership.setdefault(right_id, []).append(relation)
        endpoint_urls = sorted(
            (
                str(observation_by_id[left_id]["release_source_url"]),
                str(observation_by_id[right_id]["release_source_url"]),
            )
        )
        relation_type = str(relation["relation_type"])
        relation_bindings[str(relation["relation_observation_id"])] = {
            "relation_key": _stable_hash(
                {
                    "relation_type": relation_type,
                    "endpoint_source_urls": endpoint_urls,
                }
            ),
            "relation_type": relation_type,
            "endpoint_source_urls": endpoint_urls,
        }
        left_root = component(left_id)
        right_root = component(right_id)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    component_group_ids: dict[str, str] = {}
    component_relation_ids: dict[str, set[str]] = {}
    for stable_id, memberships in relation_membership.items():
        root = component(stable_id)
        relation_keys = [str(row["relation_key"]) for row in memberships]
        current = component_group_ids.get(root)
        component_group_ids[root] = min([*relation_keys, *([current] if current else [])])
        component_relation_ids.setdefault(root, set()).update(
            str(row["relation_observation_id"]) for row in memberships
        )

    candidates: list[dict[str, Any]] = []
    usable: list[dict[str, Any]] = []
    for observation in observations:
        metadata = _json_object(observation.get("raw_metadata"))
        stable_observation_id = str(observation["candidate_observation_id"])
        memberships = relation_membership.get(stable_observation_id, [])
        root = component(stable_observation_id) if memberships else None
        relation_ids = sorted(component_relation_ids[root]) if root is not None else []
        group_id = (
            component_group_ids[root] if root is not None else str(observation["candidate_key"])
        )
        base = {
            "source_url": str(observation["release_source_url"]),
            "title": observation["authoritative_feed_title"] or observation["detail_title"] or "",
            "detail_title": observation["detail_title"],
            "published_at": observation["published_at"],
            "language": observation["language"],
            "ingested_lang": observation["language"],
            "report_kind": observation["report_kind"],
            "document_type": observation["document_type"],
            "fiscal_period": observation["fiscal_period"],
            "period_start": observation["period_start"],
            "period_end": observation["period_end"],
            "observation_date": (
                metadata.get("observation_date")
                if metadata.get("observation_date_authoritative")
                else None
            ),
            "observation_date_authoritative": bool(
                metadata.get("observation_date_authoritative")
            ),
            "candidate_key": observation["candidate_key"],
            "candidate_observation_id": stable_observation_id,
            "eligibility": observation["eligibility"],
            "rejection_reason": (
                None
                if observation["eligibility"] == "eligible"
                else observation["eligibility_reason"]
            ),
            "selection_group_id": group_id,
            "relation_observation_ids": relation_ids,
            "relation_bindings": [relation_bindings[relation_id] for relation_id in relation_ids],
            "raw_metadata": metadata,
        }
        candidates.append(base)
        if observation["eligibility"] != "eligible":
            continue
        if observation["attachment_observation_id"] is None or observation["extraction_id"] is None:
            base["rejection_reason"] = "immutable_binding_incomplete"
            continue
        bound = conn.execute(
            """SELECT ao.attachment_observation_id, ao.attachment_source_url,
                      ao.content_type AS attachment_content_type,
                      a.artifact_id, a.sha256, a.byte_size,
                      x.extraction_id, x.extractor, x.extractor_version,
                      x.config_fingerprint, x.text_checksum, x.page_count,
                      x.pages_included, x.page_truncated, x.scanned, x.limitations,
                      b.effective_at
               FROM evidence_attachment_observations ao
               JOIN evidence_artifacts a ON a.id=ao.artifact_id
               JOIN evidence_artifact_extractions x ON x.id=? AND x.artifact_id=a.id
               JOIN evidence_observation_batches b ON b.id=?
               WHERE ao.id=?""",
            (
                observation["extraction_id"],
                observation["batch_id"],
                observation["attachment_observation_id"],
            ),
        ).fetchone()
        if bound is None:
            base["rejection_reason"] = "immutable_binding_mismatch"
            continue
        bound = dict(bound)
        object_row = conn.execute(
            """SELECT object_uri, storage_kind, verified_sha256, verified_size,
                      acquisition_max_pdf_bytes
               FROM evidence_artifact_objects
               WHERE artifact_id=(SELECT id FROM evidence_artifacts WHERE artifact_id=?)
                 AND verified_sha256=? AND verified_size=?
               ORDER BY object_record_id LIMIT 1""",
            (bound["artifact_id"], bound["sha256"], bound["byte_size"]),
        ).fetchone()
        if object_row is None:
            base["rejection_reason"] = "artifact_unavailable"
            continue
        acquisition_limit = object_row["acquisition_max_pdf_bytes"]
        artifact_store.read_pdf(
            str(bound["sha256"]),
            expected_size=int(bound["byte_size"]),
            max_pdf_bytes=(
                int(acquisition_limit)
                if acquisition_limit is not None
                else DEFAULT_MAX_PDF_BYTES
            ),
        )
        pages = [
            dict(row)
            for row in conn.execute(
                """SELECT page_number, anchor, text, text_checksum
                   FROM evidence_artifact_pages WHERE extraction_id=? ORDER BY page_number""",
                (observation["extraction_id"],),
            ).fetchall()
        ]
        if not pages:
            base["rejection_reason"] = "extraction_pages_unavailable"
            continue
        usable.append(
            {
                **base,
                "attachment_observation_id": bound["attachment_observation_id"],
                "artifact_id": bound["artifact_id"],
                "immutable_extraction_id": bound["extraction_id"],
                "attachment_url": bound["attachment_source_url"],
                "content_type": bound["attachment_content_type"],
                "byte_size": bound["byte_size"],
                "attachment_sha256": bound["sha256"],
                "extractor": bound["extractor"],
                "extractor_version": bound["extractor_version"],
                "config_fingerprint": bound["config_fingerprint"],
                "text_checksum": bound["text_checksum"],
                "page_count": bound["page_count"],
                "pages_included": bound["pages_included"],
                "page_truncated": bound["page_truncated"],
                "scanned": bound["scanned"],
                "limitations": bound["limitations"],
                "fetched_at": bound["effective_at"],
                "release_body": str(metadata.get("content_text") or metadata.get("body") or ""),
                "pages": pages,
                "siblings": [],
                "object_uri": object_row["object_uri"],
                "acquisition_max_pdf_bytes": (
                    int(acquisition_limit)
                    if acquisition_limit is not None
                    else DEFAULT_MAX_PDF_BYTES
                ),
            }
        )

    # Relations are manifest inputs only. Select one independently retained
    # edition per asserted relation component; English wins translations and
    # revision-marked/latest observations win revisions.
    from alphaforge.evidence.ingest import _has_revision_markers

    selected: list[dict[str, Any]] = []
    by_group: dict[str, list[dict[str, Any]]] = {}
    for row in usable:
        by_group.setdefault(str(row["selection_group_id"]), []).append(row)
    for rows in by_group.values():
        relation_types = {
            str(relation["relation_type"])
            for row in rows
            for relation in relation_membership.get(str(row["candidate_observation_id"]), [])
        }
        if "REVISION" in relation_types:
            rows.sort(
                key=lambda row: (
                    _has_revision_markers(
                        {
                            "title": " ".join(
                                (str(row["title"]), str(row["detail_title"] or ""))
                            ),
                            "attachment_url": row.get("attachment_url"),
                            "raw_metadata": row.get("raw_metadata"),
                        }
                    ),
                    str(row["published_at"] or ""),
                    str(row.get("language") or "").lower() == "en",
                    str(row["source_url"]),
                ),
                reverse=True,
            )
        else:
            rows.sort(
                key=lambda row: (
                    0 if str(row.get("language") or "").lower() == "en" else 1,
                    str(row["source_url"]),
                )
            )
        selected.append(rows[0])
    candidates.sort(key=lambda row: str(row["source_url"]))
    selected.sort(key=lambda row: str(row["source_url"]))
    return candidates, selected


def has_manifest_v2_observations(conn: Any, *, company_id: int, as_of: str) -> bool:
    """Return whether strict V2 authority has begun for this company/window."""
    return bool(current_candidate_observations(conn, company_id=company_id, as_of=as_of))


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _legacy_datetime(raw: str | None) -> datetime:
    try:
        parsed = datetime.fromisoformat((raw or "1970-01-01T00:00:00Z").replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime(1970, 1, 1, tzinfo=UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _legacy_effective_at(raw: str | None, previous: str | None = None) -> str:
    effective = _legacy_datetime(raw)
    if previous is not None:
        prior = _legacy_datetime(previous)
        if effective <= prior:
            effective = prior + timedelta(microseconds=1)
    return effective.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _table_digest(conn: Any, table: str, columns: str) -> str:
    rows = [
        list(row) for row in conn.execute(f"SELECT {columns} FROM {table} ORDER BY id").fetchall()
    ]
    return _stable_hash(rows)


def _legacy_semantic_attachment(attachment: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in attachment.items()
        if key not in {"id", "document_id", "fetched_at"}
    }


def _legacy_semantic_state(
    document: Mapping[str, Any],
    attachments: Sequence[Mapping[str, Any]],
    extraction: Mapping[str, Any] | None,
    pages: Sequence[Mapping[str, Any]],
    duplicate_source_url: str | None,
) -> dict[str, Any]:
    semantic_document = {
        key: value for key, value in document.items() if key not in {"id", "duplicate_of"}
    }
    semantic_document["duplicate_of_source_url"] = duplicate_source_url
    semantic_attachments = [_legacy_semantic_attachment(attachment) for attachment in attachments]
    semantic_attachments.sort(key=_canonical_json)
    semantic_extraction = (
        {
            key: value
            for key, value in extraction.items()
            if key not in {"id", "document_id", "extracted_at"}
        }
        if extraction is not None
        else None
    )
    semantic_pages = []
    for page in pages:
        semantic_page = {
            key: value for key, value in page.items() if key not in {"id", "extraction_id"}
        }
        anchor = str(semantic_page["anchor"])
        if anchor.startswith("document:") and "#page:" in anchor:
            semantic_page["anchor"] = f"legacy-document#page:{semantic_page['page_number']}"
        semantic_pages.append(semantic_page)
    semantic_pages.sort(key=lambda page: (page["page_number"], _canonical_json(page)))
    return {
        "document": semantic_document,
        "attachments": semantic_attachments,
        "extraction": semantic_extraction,
        "pages": semantic_pages,
    }


def backfill_legacy_evidence(conn: Any) -> dict[str, Any]:
    """Idempotently snapshot current legacy MFN rows without inventing retained bytes."""
    documents = [
        dict(row)
        for row in conn.execute(
            """SELECT * FROM research_documents
               WHERE source_type='mfn' AND company_id IS NOT NULL
               ORDER BY company_id, fetched_at, source_url, id"""
        ).fetchall()
    ]
    audit: dict[str, Any] = {
        "migration": "010_immutable_evidence_history.sql",
        "legacy_documents": len(documents),
        "candidates": 0,
        "metadata_only_artifacts": 0,
        "attachment_observations": 0,
        "extraction_snapshots": 0,
        "candidate_observations": 0,
        "asserted_relations": 0,
        "unresolved_relations": [],
        "unbound_extractions": [],
        "historical_packets": int(
            conn.execute("SELECT count(*) FROM evidence_packets").fetchone()[0]
        ),
        "historical_manifests": int(
            conn.execute("SELECT count(*) FROM evidence_selection_manifests").fetchone()[0]
        ),
        "packet_digest": _table_digest(conn, "evidence_packets", "id, packet_json"),
        "manifest_digest": _table_digest(conn, "evidence_selection_manifests", "id, manifest_json"),
    }
    with conn:
        company_previous: dict[int, str] = {}
        for document in documents:
            company_id = int(document["company_id"])
            metadata = _json_object(document.get("raw_metadata"))
            attachments = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM research_attachments WHERE document_id=? ORDER BY id",
                    (document["id"],),
                ).fetchall()
            ]
            legacy_extraction_row = conn.execute(
                "SELECT * FROM document_extractions WHERE document_id=?", (document["id"],)
            ).fetchone()
            legacy_extraction = (
                dict(legacy_extraction_row) if legacy_extraction_row is not None else None
            )
            legacy_pages = (
                [
                    dict(row)
                    for row in conn.execute(
                        "SELECT * FROM document_pages WHERE extraction_id=? ORDER BY page_number",
                        (legacy_extraction["id"],),
                    ).fetchall()
                ]
                if legacy_extraction is not None
                else []
            )
            duplicate_source = None
            if document.get("duplicate_of") is not None:
                duplicate_row = conn.execute(
                    "SELECT source_url FROM research_documents WHERE id=?",
                    (document["duplicate_of"],),
                ).fetchone()
                duplicate_source = str(duplicate_row[0]) if duplicate_row is not None else None
            snapshot = _legacy_semantic_state(
                document,
                attachments,
                legacy_extraction,
                legacy_pages,
                duplicate_source,
            )
            source_fingerprint = _stable_hash({"legacy_current_state": snapshot})
            ordered_semantic_attachments = [
                _legacy_semantic_attachment(attachment) for attachment in attachments
            ]
            semantic_extraction = snapshot["extraction"]
            semantic_pages = snapshot["pages"]
            rules = str(document.get("report_rules_fingerprint") or "legacy")
            as_of = str(document.get("published_at") or document.get("fetched_at") or "1970-01-01")[
                :10
            ]
            candidate_key = make_candidate_key(company_id, str(document["source_url"]))
            latest_import = conn.execute(
                """SELECT b.*
                   FROM evidence_candidate_observations o
                   JOIN evidence_candidates c ON c.id=o.candidate_id
                   JOIN evidence_observation_batches b ON b.id=o.batch_id
                   WHERE c.candidate_key=?
                     AND json_extract(o.raw_metadata, '$.legacy_import')=1
                   ORDER BY b.effective_at DESC, b.batch_id DESC LIMIT 1""",
                (candidate_key,),
            ).fetchone()
            matching_import = conn.execute(
                """SELECT b.*
                   FROM evidence_candidate_observations o
                   JOIN evidence_candidates c ON c.id=o.candidate_id
                   JOIN evidence_observation_batches b ON b.id=o.batch_id
                   WHERE c.candidate_key=?
                     AND json_extract(o.raw_metadata, '$.legacy_import')=1
                     AND b.source_input_fingerprint=?
                     AND b.report_rules_fingerprint=?
                     AND b.as_of=?
                   ORDER BY b.effective_at, b.batch_id LIMIT 1""",
                (candidate_key, source_fingerprint, rules, as_of),
            ).fetchone()
            previous = company_previous.get(company_id)
            if latest_import is not None and (
                previous is None
                or _legacy_datetime(str(latest_import["effective_at"])) > _legacy_datetime(previous)
            ):
                previous = str(latest_import["effective_at"])
            effective_at = (
                str(matching_import["effective_at"])
                if matching_import is not None
                else _legacy_effective_at(document.get("fetched_at"), previous)
            )
            if previous is None or _legacy_datetime(effective_at) > _legacy_datetime(previous):
                company_previous[company_id] = effective_at
            else:
                company_previous[company_id] = previous
            batch_value = ObservationBatchInput(
                company_id=company_id,
                as_of=as_of,
                source_input_fingerprint=source_fingerprint,
                report_rules_fingerprint=rules,
                effective_at=effective_at,
                first_recorded_at=effective_at,
            )
            batch = _append_legacy_observation_batch(conn, batch_value)
            candidate = append_candidate(
                conn,
                CandidateInput(company_id, str(document["source_url"]), effective_at),
            )
            audit["candidates"] += 1
            current_attachment = None
            for legacy_attachment in ordered_semantic_attachments:
                current_attachment = None
                digest = str(legacy_attachment.get("sha256") or "").lower()
                if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                    continue
                existing_artifact = _row(
                    conn, "evidence_artifacts", "artifact_id", f"sha256:{digest}"
                )
                artifact = append_artifact(
                    conn,
                    ArtifactInput(
                        digest,
                        int(legacy_attachment["byte_size"]),
                        (
                            str(existing_artifact["content_type"])
                            if existing_artifact is not None
                            else "application/pdf"
                        ),
                        effective_at,
                    ),
                )
                audit["metadata_only_artifacts"] += 1
                current_attachment = _append_attachment_observation(
                    conn,
                    AttachmentObservationInput(
                        candidate_key=str(candidate["candidate_key"]),
                        artifact_id=str(artifact["artifact_id"]),
                        batch_id=str(batch["batch_id"]),
                        attachment_source_url=str(legacy_attachment["source_url"]),
                        content_type=str(
                            legacy_attachment.get("content_type") or "application/pdf"
                        ),
                        http_status=legacy_attachment.get("http_status"),
                        feed_attachment_attested=bool(legacy_attachment.get("magic_valid")),
                        raw_metadata={
                            "legacy_import": True,
                            "legacy_raw_metadata": _json_object(
                                legacy_attachment.get("raw_metadata")
                            ),
                        },
                    ),
                    allow_later_identity=True,
                )
                audit["attachment_observations"] += 1
            extraction_record = None
            if semantic_extraction is not None and current_attachment is not None:
                artifact_row = conn.execute(
                    "SELECT artifact_id FROM evidence_artifacts WHERE id=?",
                    (current_attachment["artifact_id"],),
                ).fetchone()
                pages = tuple(
                    ExtractionPage(
                        page_number=int(page["page_number"]),
                        anchor=str(page["anchor"]),
                        text=str(page["text"]),
                        text_checksum=str(page["text_checksum"]),
                    )
                    for page in semantic_pages
                )
                limitations = list(json.loads(semantic_extraction.get("limitations") or "[]"))
                if "legacy_source_without_retained_bytes" not in limitations:
                    limitations.append("legacy_source_without_retained_bytes")
                extraction_record = append_extraction(
                    conn,
                    ExtractionInput(
                        artifact_id=str(artifact_row[0]),
                        extractor=str(semantic_extraction["extractor"]),
                        extractor_version="legacy-unknown",
                        config_fingerprint="legacy-unknown",
                        text_checksum=str(semantic_extraction.get("text_checksum") or ""),
                        page_count=int(semantic_extraction["page_count"]),
                        pages_included=semantic_extraction.get("pages_included"),
                        page_truncated=bool(semantic_extraction["page_truncated"]),
                        scanned=bool(semantic_extraction["scanned"]),
                        limitations=limitations,
                        extracted_at=effective_at,
                        pages=pages,
                    ),
                )
                audit["extraction_snapshots"] += 1
            elif semantic_extraction is not None:
                audit["unbound_extractions"].append(int(document["id"]))
            _append_candidate_observation(
                conn,
                CandidateObservationInput(
                    candidate_key=str(candidate["candidate_key"]),
                    batch_id=str(batch["batch_id"]),
                    authoritative_feed_title=document.get("title"),
                    detail_title=metadata.get("detail_title"),
                    published_at=document.get("published_at"),
                    language=document.get("ingested_lang"),
                    report_kind=metadata.get("report_kind"),
                    document_type=metadata.get("document_type"),
                    fiscal_period=metadata.get("fiscal_period"),
                    period_start=metadata.get("period_start"),
                    period_end=metadata.get("period_end"),
                    feed_report_identity=metadata.get("provider_event_id")
                    or metadata.get("mfn_event_id"),
                    invitation_veto=bool(metadata.get("invitation_veto")),
                    eligibility="incomplete",
                    eligibility_reason="legacy_source_without_retained_bytes",
                    attachment_observation_id=(
                        str(current_attachment["attachment_observation_id"])
                        if current_attachment is not None
                        else None
                    ),
                    extraction_id=(
                        str(extraction_record["extraction_id"])
                        if extraction_record is not None
                        else None
                    ),
                    report_rules_fingerprint=rules,
                    raw_metadata={"legacy_import": True, "legacy_metadata": metadata},
                ),
                allow_later_identity=True,
            )
            audit["candidate_observations"] += 1

        for document in documents:
            parent_id = document.get("duplicate_of")
            if parent_id is not None:
                audit["unresolved_relations"].append(
                    {"document_id": int(document["id"]), "duplicate_of": int(parent_id)}
                )
    audit["unresolved_relations"].sort(key=lambda item: (item["document_id"], item["duplicate_of"]))
    audit["unbound_extractions"].sort()
    return audit


def write_legacy_backfill_audit(audit: Mapping[str, Any], path: Path | str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(_canonical_json(dict(audit)) + "\n", encoding="utf-8")
