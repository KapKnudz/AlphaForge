from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace

import pytest

from alphaforge.config import SCHEMA_VERSION
from alphaforge.db.connection import get_connection, init_db
from alphaforge.db.evidence_repository import (
    ArtifactInput,
    ArtifactObjectInput,
    AttachmentObservationInput,
    CandidateInput,
    CandidateObservationInput,
    ExtractionInput,
    ExtractionPage,
    ImmutableEvidenceConflict,
    ObservationBatchInput,
    RelationObservationInput,
    append_artifact,
    append_artifact_object,
    append_attachment_observation,
    append_candidate,
    append_candidate_observation,
    append_extraction,
    append_observation_batch,
    append_relation_observation,
    backfill_legacy_evidence,
    current_candidate_observations,
    current_relation_observations,
    make_batch_id,
    make_candidate_key,
    write_legacy_backfill_audit,
)
from alphaforge.db.migrations import migrate, set_user_version

NEW_TABLES = {
    "evidence_observation_batches",
    "evidence_candidates",
    "evidence_artifacts",
    "evidence_artifact_objects",
    "evidence_attachment_observations",
    "evidence_artifact_extractions",
    "evidence_artifact_pages",
    "evidence_candidate_observations",
    "evidence_candidate_relation_observations",
}


@pytest.fixture
def conn():
    connection = get_connection(path=":memory:")
    init_db(connection)
    connection.execute("INSERT INTO companies (borsdata_id, name) VALUES (1, 'Acme')")
    connection.commit()
    yield connection
    connection.close()


def _batch(company_id: int, suffix: str, effective_at: str) -> ObservationBatchInput:
    return ObservationBatchInput(
        company_id=company_id,
        as_of="2026-09-24",
        source_input_fingerprint=f"source-{suffix}",
        report_rules_fingerprint="rules-v2",
        effective_at=effective_at,
        first_recorded_at=effective_at,
    )


def _observation(
    candidate_key: str,
    batch_id: str,
    *,
    eligibility: str = "eligible",
    title: str = "Quarterly report",
    attachment_id: str | None = None,
    extraction_id: str | None = None,
) -> CandidateObservationInput:
    return CandidateObservationInput(
        candidate_key=candidate_key,
        batch_id=batch_id,
        authoritative_feed_title=title,
        detail_title=title,
        published_at="2026-09-20T08:00:00Z",
        language="en",
        report_kind="quarterly",
        document_type="interim_report",
        fiscal_period="Q2-2026",
        period_start="2026-01-01",
        period_end="2026-06-30",
        feed_report_identity="event-1",
        invitation_veto=False,
        eligibility=eligibility,
        eligibility_reason="report_rules_passed" if eligibility == "eligible" else eligibility,
        attachment_observation_id=attachment_id,
        extraction_id=extraction_id,
        report_rules_fingerprint="rules-v2",
        raw_metadata={"source": "fixture"},
    )


def test_migration_adds_exact_nine_tables_and_append_only_guards(tmp_path):
    database = tmp_path / "upgrade.db"
    connection = get_connection(path=database)
    init_db(connection)
    for table in NEW_TABLES:
        connection.execute(f"DROP TABLE {table}")
    set_user_version(connection, 9)
    connection.commit()

    migrate(connection)

    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'evidence_%'"
        )
    }
    assert NEW_TABLES <= tables
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 10
    connection.execute("INSERT INTO companies (borsdata_id, name) VALUES (1, 'Acme')")
    batch = append_observation_batch(connection, _batch(1, "one", "2026-09-24T10:00:00Z"))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        connection.execute(
            "UPDATE evidence_observation_batches SET as_of='2020-01-01' WHERE id=?",
            (batch["id"],),
        )


def test_append_history_is_idempotent_and_preserves_same_url_revisions(conn):
    batch1_value = _batch(1, "one", "2026-09-24T10:00:00Z")
    batch1 = append_observation_batch(conn, batch1_value)
    assert (
        append_observation_batch(conn, replace(batch1_value, effective_at="2099-01-01"))["id"]
        == batch1["id"]
    )
    candidate = append_candidate(
        conn, CandidateInput(1, "https://example.test/releases/q2", "2026-09-24T10:00:00Z")
    )
    digest1 = hashlib.sha256(b"first PDF").hexdigest()
    artifact1 = append_artifact(
        conn, ArtifactInput(digest1, 9, "application/pdf", "2026-09-24T10:00:01Z")
    )
    object1 = append_artifact_object(
        conn,
        ArtifactObjectInput(
            artifact1["artifact_id"],
            f"file:evidence/{digest1}.pdf",
            "local_cas",
            digest1,
            9,
            "2026-09-24T10:00:02Z",
        ),
    )
    attachment1 = append_attachment_observation(
        conn,
        AttachmentObservationInput(
            candidate["candidate_key"],
            artifact1["artifact_id"],
            batch1["batch_id"],
            "https://example.test/report.pdf",
            "application/pdf",
            200,
            True,
            {"etag": "one"},
        ),
    )
    extraction1 = append_extraction(
        conn,
        ExtractionInput(
            artifact1["artifact_id"],
            "pypdf",
            "5.0",
            "config-1",
            hashlib.sha256(b"page one").hexdigest(),
            1,
            "1",
            False,
            False,
            (),
            "2026-09-24T10:00:03Z",
            (ExtractionPage(1, "p1", "page one", hashlib.sha256(b"page one").hexdigest()),),
        ),
    )
    observation1_value = _observation(
        candidate["candidate_key"],
        batch1["batch_id"],
        attachment_id=attachment1["attachment_observation_id"],
        extraction_id=extraction1["extraction_id"],
    )
    observation1 = append_candidate_observation(conn, observation1_value)
    assert append_candidate_observation(conn, observation1_value)["id"] == observation1["id"]
    with pytest.raises(ImmutableEvidenceConflict, match="different state"):
        append_candidate_observation(conn, replace(observation1_value, detail_title="conflict"))

    batch2 = append_observation_batch(conn, _batch(1, "two", "2026-09-24T11:00:00Z"))
    digest2 = hashlib.sha256(b"second PDF").hexdigest()
    artifact2 = append_artifact(
        conn, ArtifactInput(digest2, 10, "application/pdf", "2026-09-24T11:00:01Z")
    )
    attachment2 = append_attachment_observation(
        conn,
        AttachmentObservationInput(
            candidate["candidate_key"],
            artifact2["artifact_id"],
            batch2["batch_id"],
            "https://example.test/report.pdf",
            "application/pdf",
            200,
            True,
            {"etag": "two"},
        ),
    )
    observation2 = append_candidate_observation(
        conn,
        _observation(
            candidate["candidate_key"],
            batch2["batch_id"],
            eligibility="revoked",
            title="Corrected title",
            attachment_id=attachment2["attachment_observation_id"],
        ),
    )

    current = current_candidate_observations(conn, company_id=1, as_of="2026-09-24")
    assert [row["candidate_observation_id"] for row in current] == [
        observation2["candidate_observation_id"]
    ]
    assert current[0]["eligibility"] == "revoked"
    assert conn.execute("SELECT count(*) FROM evidence_artifacts").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM evidence_attachment_observations").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM evidence_artifact_objects").fetchone()[0] == 1
    assert object1["verified_sha256"] == digest1
    with pytest.raises(ImmutableEvidenceConflict):
        append_artifact(conn, ArtifactInput(digest1, 99, "application/pdf", "later"))


def test_independent_candidates_and_explicit_relation_withdrawal(conn):
    en = append_candidate(
        conn, CandidateInput(1, "https://example.test/en", "2026-09-24T10:00:00Z")
    )
    sv = append_candidate(
        conn, CandidateInput(1, "https://example.test/sv", "2026-09-24T10:00:00Z")
    )
    assert en["candidate_key"] != sv["candidate_key"]
    batch1 = append_observation_batch(conn, _batch(1, "one", "2026-09-24T10:00:00Z"))
    en_obs = append_candidate_observation(
        conn, _observation(en["candidate_key"], batch1["batch_id"])
    )
    sv_obs = append_candidate_observation(
        conn,
        replace(_observation(sv["candidate_key"], batch1["batch_id"]), language="sv"),
    )
    asserted = append_relation_observation(
        conn,
        RelationObservationInput(
            batch1["batch_id"],
            en_obs["candidate_observation_id"],
            sv_obs["candidate_observation_id"],
            "TRANSLATION",
            "asserted",
            {"same_period": True},
            "relation-rules-1",
        ),
    )
    with pytest.raises(ImmutableEvidenceConflict, match="different state"):
        append_relation_observation(
            conn,
            RelationObservationInput(
                batch1["batch_id"],
                en_obs["candidate_observation_id"],
                sv_obs["candidate_observation_id"],
                "TRANSLATION",
                "withdrawn",
                {"reason": "conflict"},
                "relation-rules-1",
            ),
        )
    batch2 = append_observation_batch(conn, _batch(1, "two", "2026-09-24T11:00:00Z"))
    withdrawn = append_relation_observation(
        conn,
        RelationObservationInput(
            batch2["batch_id"],
            sv_obs["candidate_observation_id"],
            en_obs["candidate_observation_id"],
            "TRANSLATION",
            "withdrawn",
            {"reason": "period_mismatch"},
            "relation-rules-1",
        ),
    )

    current = current_relation_observations(conn, company_id=1, as_of="2026-09-24")
    assert len(current) == 1
    assert current[0]["relation_observation_id"] == withdrawn["relation_observation_id"]
    assert current[0]["disposition"] == "withdrawn"
    assert (
        conn.execute("SELECT count(*) FROM evidence_candidate_relation_observations").fetchone()[0]
        == 2
    )
    assert asserted["relation_key"] == withdrawn["relation_key"]


def test_extraction_insert_is_atomic_when_a_page_is_invalid(conn):
    artifact = append_artifact(
        conn,
        ArtifactInput("a" * 64, 1, "application/pdf", "2026-09-24T10:00:00Z"),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        with conn:
            append_extraction(
                conn,
                ExtractionInput(
                    artifact["artifact_id"],
                    "pypdf",
                    "5",
                    "cfg",
                    "text",
                    1,
                    "0",
                    False,
                    False,
                    (),
                    "2026-09-24T10:00:01Z",
                    (ExtractionPage(0, "bad", "text", "checksum"),),
                ),
            )
    assert conn.execute("SELECT count(*) FROM evidence_artifact_extractions").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM evidence_artifact_pages").fetchone()[0] == 0


def test_legacy_backfill_is_idempotent_metadata_only_and_audit_is_stable(conn, tmp_path):
    conn.execute(
        """INSERT INTO research_documents
           (company_id, source_url, source_type, title, published_at, fetched_at,
            ingested_lang, checksum, raw_metadata, report_rules_fingerprint)
           VALUES (1, 'https://example.test/en', 'mfn', 'Q2 report', '2026-07-15',
                   '2026-07-15T10:00:00.123Z', 'en', ?, ?, 'legacy-rules')""",
        (
            "a" * 64,
            json.dumps({"report_kind": "quarterly", "bilingual_group_id": "q2"}),
        ),
    )
    parent_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """INSERT INTO research_attachments
           (document_id, source_url, content_type, byte_size, sha256, magic_valid, http_status)
           VALUES (?, 'https://example.test/en.pdf', 'application/pdf', 100, ?, 1, 200)""",
        (parent_id, "a" * 64),
    )
    conn.execute(
        """INSERT INTO document_extractions
           (document_id, extractor, text_checksum, page_count, pages_included,
            page_truncated, scanned, limitations)
           VALUES (?, 'pypdf', 'text-a', 1, '1', 0, 0, '[]')""",
        (parent_id,),
    )
    extraction_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO document_pages (extraction_id, page_number, anchor, text, text_checksum) VALUES (?, 1, 'p1', 'hello', 'page-a')",
        (extraction_id,),
    )
    conn.execute(
        """INSERT INTO research_documents
           (company_id, source_url, source_type, title, published_at, fetched_at,
            duplicate_of, ingested_lang, checksum, raw_metadata, report_rules_fingerprint)
           VALUES (1, 'https://example.test/sv', 'mfn', 'Q2 rapport', '2026-07-15',
                   '2026-07-15T10:01:00Z', ?, 'sv', ?, ?, 'legacy-rules')""",
        (
            parent_id,
            "b" * 64,
            json.dumps(
                {
                    "report_kind": "quarterly",
                    "bilingual_group_id": "q2",
                    "relationship": "TRANSLATION",
                }
            ),
        ),
    )
    conn.execute(
        """INSERT INTO evidence_packets
           (company_id, as_of, packet_hash, packet_json)
           VALUES (1, '2026-09-24', 'old-packet', '{"legacy":true}')"""
    )
    conn.execute(
        """INSERT INTO evidence_selection_manifests
           (company_id, as_of, manifest_id, manifest_json, report_rules_fingerprint)
           VALUES (1, '2026-09-24', 'old-manifest', '{"version":1}', 'legacy-rules')"""
    )
    conn.commit()
    # Imported legacy chronology remains valid even if newer V2 facts were staged first.
    append_observation_batch(conn, _batch(1, "staged-v2", "2030-01-01T00:00:00Z"))
    conn.commit()
    packet_before = conn.execute("SELECT packet_json FROM evidence_packets").fetchone()[0]
    manifest_before = conn.execute(
        "SELECT manifest_json FROM evidence_selection_manifests"
    ).fetchone()[0]

    first = backfill_legacy_evidence(conn)
    counts = {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in NEW_TABLES
    }
    second = backfill_legacy_evidence(conn)
    second_counts = {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in NEW_TABLES
    }
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    write_legacy_backfill_audit(first, first_path)
    write_legacy_backfill_audit(second, second_path)

    assert counts == second_counts
    assert first == second
    assert first_path.read_bytes() == second_path.read_bytes()
    assert first["metadata_only_artifacts"] == 1
    assert first["asserted_relations"] == 0
    assert first["unresolved_relations"] == [
        {"document_id": parent_id + 1, "duplicate_of": parent_id}
    ]
    assert conn.execute(
        "SELECT count(*) FROM evidence_candidate_relation_observations"
    ).fetchone()[0] == 0
    assert conn.execute(
        """SELECT effective_at FROM evidence_observation_batches
           WHERE source_input_fingerprint != 'source-staged-v2'
           ORDER BY effective_at LIMIT 1"""
    ).fetchone()[0] == "2026-07-15T10:00:00.123000Z"
    assert conn.execute("SELECT count(*) FROM evidence_artifact_objects").fetchone()[0] == 0
    assert {
        row[0] for row in conn.execute("SELECT eligibility FROM evidence_candidate_observations")
    } == {"incomplete"}
    assert conn.execute("SELECT packet_json FROM evidence_packets").fetchone()[0] == packet_before
    assert (
        conn.execute("SELECT manifest_json FROM evidence_selection_manifests").fetchone()[0]
        == manifest_before
    )
    assert make_candidate_key(1, "https://example.test/en")
    assert make_batch_id(_batch(1, "one", "ignored"))

    batch_count = conn.execute("SELECT count(*) FROM evidence_observation_batches").fetchone()[0]
    conn.execute(
        "UPDATE document_pages SET text='corrected', text_checksum='page-corrected' WHERE extraction_id=?",
        (extraction_id,),
    )
    conn.commit()
    corrected = backfill_legacy_evidence(conn)
    corrected_again = backfill_legacy_evidence(conn)

    assert corrected == corrected_again
    assert conn.execute("SELECT count(*) FROM evidence_observation_batches").fetchone()[0] == (
        batch_count + 1
    )
    current = current_candidate_observations(conn, company_id=1, as_of="2026-07-15")
    en_current = next(row for row in current if row["release_source_url"].endswith("/en"))
    page = conn.execute(
        """SELECT p.text FROM evidence_artifact_pages p
           JOIN evidence_artifact_extractions e ON e.id=p.extraction_id
           WHERE e.id=?""",
        (en_current["extraction_id"],),
    ).fetchone()
    assert page[0] == "corrected"
