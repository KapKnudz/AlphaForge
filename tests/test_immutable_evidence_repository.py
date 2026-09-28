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
    if effective_at.endswith("Z") and "." not in effective_at:
        effective_at = f"{effective_at[:-1]}.000000Z"
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
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
        connection.execute(
            """INSERT INTO evidence_observation_batches
               (batch_id, company_id, provider, as_of, source_input_fingerprint,
                report_rules_fingerprint, effective_at, first_recorded_at)
               VALUES ('invalid', 1, 'mfn', '2026-9-24', 'source', 'rules',
                       '2026-09-24T10:00:00Z', '2026-09-24T10:00:00Z')"""
        )


def test_batch_chronology_requires_canonical_utc_and_date(conn):
    canonical = _batch(1, "canonical", "2026-09-24T10:00:00Z")
    with pytest.raises(ValueError, match="effective_at must be canonical UTC"):
        append_observation_batch(
            conn,
            replace(canonical, effective_at="2026-09-24T11:00:00+02:00"),
        )
    with pytest.raises(ValueError, match="first_recorded_at must be canonical UTC"):
        append_observation_batch(conn, replace(canonical, first_recorded_at="not-a-time"))
    with pytest.raises(ValueError, match="as_of must be a canonical date"):
        append_observation_batch(conn, replace(canonical, as_of="2026-9-24"))
    assert conn.execute("SELECT count(*) FROM evidence_observation_batches").fetchone()[0] == 0


def test_append_history_is_idempotent_and_preserves_same_url_revisions(conn):
    batch1_value = _batch(1, "one", "2026-09-24T10:00:00Z")
    batch1 = append_observation_batch(conn, batch1_value)
    assert append_observation_batch(conn, batch1_value)["id"] == batch1["id"]
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

    batch3 = append_observation_batch(conn, _batch(1, "one", "2026-09-24T12:00:00Z"))
    attachment3 = append_attachment_observation(
        conn,
        AttachmentObservationInput(
            candidate["candidate_key"],
            artifact1["artifact_id"],
            batch3["batch_id"],
            "https://example.test/report.pdf",
            "application/pdf",
            200,
            True,
            {"etag": "one-again"},
        ),
    )
    observation3 = append_candidate_observation(
        conn,
        replace(
            observation1_value,
            batch_id=batch3["batch_id"],
            attachment_observation_id=attachment3["attachment_observation_id"],
        ),
    )
    assert append_observation_batch(conn, batch1_value)["id"] == batch1["id"]
    current = current_candidate_observations(conn, company_id=1, as_of="2026-09-24")
    assert current[0]["candidate_observation_id"] == observation3["candidate_observation_id"]
    assert current[0]["eligibility"] == "eligible"
    assert conn.execute("SELECT count(*) FROM evidence_artifacts").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM evidence_attachment_observations").fetchone()[0] == 3
    assert conn.execute("SELECT count(*) FROM evidence_artifact_objects").fetchone()[0] == 1
    assert object1["verified_sha256"] == digest1
    with pytest.raises(ImmutableEvidenceConflict):
        append_artifact(conn, ArtifactInput(digest1, 99, "application/pdf", "later"))


def test_batch_children_cannot_reference_future_facts(conn):
    batch1 = append_observation_batch(conn, _batch(1, "one", "2026-09-24T10:00:00Z"))
    first = append_candidate(
        conn, CandidateInput(1, "https://example.test/first", "2026-09-24T10:00:00Z")
    )
    artifact = append_artifact(conn, ArtifactInput("a" * 64, 1, "application/pdf", "now"))
    old_attachment = append_attachment_observation(
        conn,
        AttachmentObservationInput(
            first["candidate_key"],
            artifact["artifact_id"],
            batch1["batch_id"],
            "https://example.test/old.pdf",
            "application/pdf",
            200,
            True,
        ),
    )
    first_observation = append_candidate_observation(
        conn,
        _observation(
            first["candidate_key"],
            batch1["batch_id"],
            attachment_id=old_attachment["attachment_observation_id"],
        ),
    )
    batch2 = append_observation_batch(conn, _batch(1, "two", "2026-09-24T11:00:00Z"))
    second = append_candidate(
        conn, CandidateInput(1, "https://example.test/second", "2026-09-24T11:00:00Z")
    )
    reused = append_candidate_observation(
        conn,
        _observation(
            first["candidate_key"],
            batch2["batch_id"],
            attachment_id=old_attachment["attachment_observation_id"],
        ),
    )
    with pytest.raises(ValueError, match="rules fingerprint"):
        append_candidate_observation(
            conn,
            replace(
                _observation(second["candidate_key"], batch2["batch_id"]),
                report_rules_fingerprint="other-rules",
            ),
        )
    second_observation = append_candidate_observation(
        conn, _observation(second["candidate_key"], batch2["batch_id"])
    )
    batch3 = append_observation_batch(conn, _batch(1, "three", "2026-09-24T12:00:00Z"))
    future_attachment = append_attachment_observation(
        conn,
        AttachmentObservationInput(
            second["candidate_key"],
            artifact["artifact_id"],
            batch3["batch_id"],
            "https://example.test/future.pdf",
            "application/pdf",
            200,
            True,
        ),
    )
    third = append_candidate(
        conn, CandidateInput(1, "https://example.test/third", "2026-09-24T12:00:00Z")
    )
    third_observation = append_candidate_observation(
        conn, _observation(third["candidate_key"], batch3["batch_id"])
    )

    with pytest.raises(ValueError, match="attachment postdates"):
        append_candidate_observation(
            conn,
            _observation(
                second["candidate_key"],
                batch2["batch_id"],
                attachment_id=future_attachment["attachment_observation_id"],
            ),
        )
    with pytest.raises(ValueError, match="postdates"):
        append_relation_observation(
            conn,
            RelationObservationInput(
                batch2["batch_id"],
                second_observation["candidate_observation_id"],
                third_observation["candidate_observation_id"],
                "TRANSLATION",
                "withdrawn",
                {"reason": "future"},
                "relation-rules-1",
            ),
        )
    withdrawal = append_relation_observation(
        conn,
        RelationObservationInput(
            batch3["batch_id"],
            first_observation["candidate_observation_id"],
            second_observation["candidate_observation_id"],
            "TRANSLATION",
            "withdrawn",
            {"reason": "later-review"},
            "relation-rules-1",
        ),
    )
    assert reused["attachment_observation_id"] == old_attachment["id"]
    assert withdrawal["disposition"] == "withdrawn"


def test_current_state_prefers_newest_cutoff_before_acquisition_time(conn):
    candidate = append_candidate(
        conn, CandidateInput(1, "https://example.test/cutoff", "2026-09-24T10:00:00Z")
    )
    current_batch = append_observation_batch(
        conn, _batch(1, "current-cutoff", "2026-09-24T10:00:00Z")
    )
    eligible = append_candidate_observation(
        conn, _observation(candidate["candidate_key"], current_batch["batch_id"])
    )
    historical_batch = append_observation_batch(
        conn,
        replace(
            _batch(1, "historical-cutoff", "2026-09-24T11:00:00Z"),
            as_of="2026-09-20",
        ),
    )
    revoked = append_candidate_observation(
        conn,
        _observation(
            candidate["candidate_key"], historical_batch["batch_id"], eligibility="revoked"
        ),
    )

    assert (
        current_candidate_observations(conn, company_id=1, as_of="2026-09-24")[0][
            "candidate_observation_id"
        ]
        == eligible["candidate_observation_id"]
    )
    assert (
        current_candidate_observations(conn, company_id=1, as_of="2026-09-20")[0][
            "candidate_observation_id"
        ]
        == revoked["candidate_observation_id"]
    )


def test_relation_state_prefers_newest_cutoff_before_acquisition_time(conn):
    source_batch = append_observation_batch(
        conn,
        replace(_batch(1, "relation-source", "2026-09-24T09:00:00Z"), as_of="2026-09-20"),
    )
    en = append_candidate(
        conn, CandidateInput(1, "https://example.test/cutoff-en", "2026-09-24T09:00:00Z")
    )
    sv = append_candidate(
        conn, CandidateInput(1, "https://example.test/cutoff-sv", "2026-09-24T09:00:00Z")
    )
    en_observation = append_candidate_observation(
        conn, _observation(en["candidate_key"], source_batch["batch_id"])
    )
    sv_observation = append_candidate_observation(
        conn,
        replace(_observation(sv["candidate_key"], source_batch["batch_id"]), language="sv"),
    )
    current_batch = append_observation_batch(
        conn, _batch(1, "relation-current", "2026-09-24T10:00:00Z")
    )
    proof = {
        "strong_corroborator": {"kind": "shared_provider_event_id", "value": "event-1"},
        "compatible_signals": ["fiscal_period", "publication_date"],
    }
    asserted = append_relation_observation(
        conn,
        RelationObservationInput(
            current_batch["batch_id"],
            en_observation["candidate_observation_id"],
            sv_observation["candidate_observation_id"],
            "TRANSLATION",
            "asserted",
            proof,
            "relation-rules-1",
        ),
    )
    historical_batch = append_observation_batch(
        conn,
        replace(
            _batch(1, "relation-historical", "2026-09-24T11:00:00Z"),
            as_of="2026-09-20",
        ),
    )
    withdrawn = append_relation_observation(
        conn,
        RelationObservationInput(
            historical_batch["batch_id"],
            en_observation["candidate_observation_id"],
            sv_observation["candidate_observation_id"],
            "TRANSLATION",
            "withdrawn",
            {"reason": "historical-replay"},
            "relation-rules-1",
        ),
    )

    assert (
        current_relation_observations(conn, company_id=1, as_of="2026-09-24")[0][
            "relation_observation_id"
        ]
        == asserted["relation_observation_id"]
    )
    assert (
        current_relation_observations(conn, company_id=1, as_of="2026-09-20")[0][
            "relation_observation_id"
        ]
        == withdrawn["relation_observation_id"]
    )


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
    with pytest.raises(ValueError, match="structured corroboration"):
        append_relation_observation(
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
    with pytest.raises(ValueError, match="two compatible signals"):
        append_relation_observation(
            conn,
            RelationObservationInput(
                batch1["batch_id"],
                en_obs["candidate_observation_id"],
                sv_obs["candidate_observation_id"],
                "TRANSLATION",
                "asserted",
                {
                    "strong_corroborator": {
                        "kind": "shared_provider_event_id",
                        "value": "event-1",
                    },
                    "compatible_signals": ["fiscal_period"],
                },
                "relation-rules-1",
            ),
        )
    with pytest.raises(ValueError, match="strong corroborator"):
        append_relation_observation(
            conn,
            RelationObservationInput(
                batch1["batch_id"],
                en_obs["candidate_observation_id"],
                sv_obs["candidate_observation_id"],
                "TRANSLATION",
                "asserted",
                {
                    "strong_corroborator": {
                        "kind": "shared_provider_event_id",
                        "value": "invented-event",
                    },
                    "compatible_signals": ["fiscal_period", "publication_date"],
                },
                "relation-rules-1",
            ),
        )
    asserted = append_relation_observation(
        conn,
        RelationObservationInput(
            batch1["batch_id"],
            en_obs["candidate_observation_id"],
            sv_obs["candidate_observation_id"],
            "TRANSLATION",
            "asserted",
            {
                "strong_corroborator": {
                    "kind": "shared_provider_event_id",
                    "value": "event-1",
                },
                "compatible_signals": ["fiscal_period", "publication_date"],
            },
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


def test_asserted_relation_type_compatibility_uses_persisted_observations(conn):
    batch = append_observation_batch(conn, _batch(1, "relations", "2026-09-24T10:00:00Z"))

    def observed(
        url: str,
        *,
        language: str,
        kind: str,
        title: str,
        published_at: str = "2026-09-20T08:00:00Z",
    ):
        candidate = append_candidate(conn, CandidateInput(1, url, "2026-09-24T10:00:00Z"))
        return append_candidate_observation(
            conn,
            replace(
                _observation(candidate["candidate_key"], batch["batch_id"], title=title),
                language=language,
                report_kind=kind,
                published_at=published_at,
            ),
        )

    en = observed("https://example.test/base-en", language="en", kind="quarterly", title="Q2")
    sv = observed("https://example.test/base-sv", language="sv", kind="quarterly", title="Q2")
    annual = observed(
        "https://example.test/annual-sv", language="sv", kind="annual", title="Annual"
    )
    same_language = observed(
        "https://example.test/other-en", language="en", kind="quarterly", title="Q2"
    )
    corrected = observed(
        "https://example.test/corrected-en",
        language="en",
        kind="quarterly",
        title="Corrected Q2 report",
    )
    proof = {
        "strong_corroborator": {"kind": "shared_provider_event_id", "value": "event-1"},
        "compatible_signals": ["fiscal_period", "publication_date"],
    }

    with pytest.raises(ValueError, match="matching report kinds"):
        append_relation_observation(
            conn,
            RelationObservationInput(
                batch["batch_id"],
                en["candidate_observation_id"],
                annual["candidate_observation_id"],
                "TRANSLATION",
                "asserted",
                proof,
                "relation-rules-1",
            ),
        )
    with pytest.raises(ValueError, match="opposite languages"):
        append_relation_observation(
            conn,
            RelationObservationInput(
                batch["batch_id"],
                en["candidate_observation_id"],
                same_language["candidate_observation_id"],
                "TRANSLATION",
                "asserted",
                proof,
                "relation-rules-1",
            ),
        )
    with pytest.raises(ValueError, match="revision evidence"):
        append_relation_observation(
            conn,
            RelationObservationInput(
                batch["batch_id"],
                en["candidate_observation_id"],
                sv["candidate_observation_id"],
                "REVISION",
                "asserted",
                proof,
                "relation-rules-1",
            ),
        )

    revision = append_relation_observation(
        conn,
        RelationObservationInput(
            batch["batch_id"],
            en["candidate_observation_id"],
            corrected["candidate_observation_id"],
            "REVISION",
            "asserted",
            proof,
            "relation-rules-1",
        ),
    )
    assert revision["disposition"] == "asserted"

    titled_en = observed(
        "https://example.test/titled-en",
        language="en",
        kind="quarterly",
        title="Acme Interim Report Q2",
        published_at="2026-09-20T08:00:00Z",
    )
    titled_sv = observed(
        "https://example.test/titled-sv",
        language="sv",
        kind="quarterly",
        title="Acme Delårsrapport Q2",
        published_at="2026-09-21T08:00:00Z",
    )
    title_relation = append_relation_observation(
        conn,
        RelationObservationInput(
            batch["batch_id"],
            titled_en["candidate_observation_id"],
            titled_sv["candidate_observation_id"],
            "TRANSLATION",
            "asserted",
            {
                "strong_corroborator": {
                    "kind": "shared_provider_event_id",
                    "value": "event-1",
                },
                "compatible_signals": ["fiscal_period", "translation_neutral_title"],
            },
            "relation-rules-1",
        ),
    )
    assert title_relation["disposition"] == "asserted"


def test_numeric_relation_corroboration_is_recomputed_from_persisted_pages(conn):
    batch = append_observation_batch(conn, _batch(1, "numeric", "2026-09-24T10:00:00Z"))

    def extracted(url: str, language: str, text: str):
        candidate = append_candidate(conn, CandidateInput(1, url, "2026-09-24T10:00:00Z"))
        digest = hashlib.sha256(text.encode()).hexdigest()
        artifact = append_artifact(conn, ArtifactInput(digest, len(text), "application/pdf", "now"))
        attachment = append_attachment_observation(
            conn,
            AttachmentObservationInput(
                candidate["candidate_key"],
                artifact["artifact_id"],
                batch["batch_id"],
                f"{url}.pdf",
                "application/pdf",
                200,
                True,
            ),
        )
        extraction = append_extraction(
            conn,
            ExtractionInput(
                artifact["artifact_id"],
                "fixture",
                "1",
                "cfg",
                digest,
                1,
                "1",
                False,
                False,
                (),
                "2026-09-24T10:00:00Z",
                (ExtractionPage(1, "p1", text, digest),),
            ),
        )
        return append_candidate_observation(
            conn,
            replace(
                _observation(candidate["candidate_key"], batch["batch_id"]),
                language=language,
                feed_report_identity=None,
                attachment_observation_id=attachment["attachment_observation_id"],
                extraction_id=extraction["extraction_id"],
            ),
        )

    en = extracted("https://example.test/numeric-en", "en", "Revenue 100 MSEK; EBIT 10 MSEK")
    sv = extracted("https://example.test/numeric-sv", "sv", "Omsättning 999 MSEK; EBIT 88 MSEK")
    with pytest.raises(ValueError, match="strong corroborator"):
        append_relation_observation(
            conn,
            RelationObservationInput(
                batch["batch_id"],
                en["candidate_observation_id"],
                sv["candidate_observation_id"],
                "TRANSLATION",
                "asserted",
                {
                    "strong_corroborator": {
                        "kind": "numeric_key_figure_jaccard",
                        "value": 0.9,
                    },
                    "compatible_signals": ["fiscal_period", "publication_date"],
                },
                "relation-rules-1",
            ),
        )


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


def test_legacy_persisted_pages_can_corroborate_numeric_relation(conn):
    pages = {
        "en": "Revenue 100 MSEK; EBIT 10 MSEK.",
        "sv": "Omsättning 100 MSEK; EBIT 10 MSEK.",
    }
    for index, (language, text) in enumerate(pages.items(), start=1):
        metadata = json.dumps(
            {"report_kind": "quarterly", "fiscal_period": "Q2-2026"}
        )
        conn.execute(
            """INSERT INTO research_documents
               (company_id, source_url, source_type, title, published_at, fetched_at,
                ingested_lang, checksum, raw_metadata, report_rules_fingerprint)
               VALUES (1, ?, 'mfn', ?, '2026-07-15', ?, ?, ?, ?, 'legacy-rules')""",
            (
                f"https://example.test/numeric-{language}",
                "Q2 report" if language == "en" else "Q2 rapport",
                f"2026-07-15T10:0{index}:00Z",
                language,
                hashlib.sha256(f"pdf-{language}".encode()).hexdigest(),
                metadata,
            ),
        )
        document_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        artifact_digest = hashlib.sha256(f"artifact-{language}".encode()).hexdigest()
        conn.execute(
            """INSERT INTO research_attachments
               (document_id, source_url, content_type, byte_size, sha256,
                magic_valid, http_status)
               VALUES (?, ?, 'application/pdf', 100, ?, 1, 200)""",
            (document_id, f"https://example.test/numeric-{language}.pdf", artifact_digest),
        )
        text_digest = hashlib.sha256(text.encode()).hexdigest()
        conn.execute(
            """INSERT INTO document_extractions
               (document_id, extractor, text_checksum, page_count, pages_included,
                page_truncated, scanned, limitations)
               VALUES (?, 'pypdf', ?, 1, '1', 0, 0, '[]')""",
            (document_id, text_digest),
        )
        extraction_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """INSERT INTO document_pages
               (extraction_id, page_number, anchor, text, text_checksum)
               VALUES (?, 1, 'p1', ?, ?)""",
            (extraction_id, text, text_digest),
        )
    conn.commit()

    backfill_legacy_evidence(conn)
    observations = current_candidate_observations(conn, company_id=1, as_of="2026-07-15")
    en_observation = next(row for row in observations if row["language"] == "en")
    sv_observation = next(row for row in observations if row["language"] == "sv")
    relation_batch = append_observation_batch(
        conn,
        replace(
            _batch(1, "legacy-numeric-relation", "2027-01-01T00:00:00Z"),
            as_of="2026-07-15",
            report_rules_fingerprint="legacy-rules",
        ),
    )

    relation = append_relation_observation(
        conn,
        RelationObservationInput(
            relation_batch["batch_id"],
            en_observation["candidate_observation_id"],
            sv_observation["candidate_observation_id"],
            "TRANSLATION",
            "asserted",
            {
                "strong_corroborator": {
                    "kind": "numeric_key_figure_jaccard",
                    "value": 1.0,
                },
                "compatible_signals": ["fiscal_period", "publication_date"],
            },
            "relation-rules-1",
        ),
    )

    assert relation["disposition"] == "asserted"
    assert json.loads(relation["corroboration"])["strong_corroborator"] == {
        "kind": "numeric_key_figure_jaccard",
        "value": 1.0,
    }


def _insert_legacy_document(conn, *, title: str, fetched_at: str) -> int:
    conn.execute(
        """INSERT INTO research_documents
           (company_id, source_url, source_type, title, published_at, fetched_at,
            ingested_lang, checksum, raw_metadata, report_rules_fingerprint)
           VALUES (1, 'https://example.test/replay', 'mfn', ?, '2026-07-15', ?,
                   'en', ?, '{}', 'legacy-rules')""",
        (title, fetched_at, hashlib.sha256(title.encode()).hexdigest()),
    )
    document_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    return document_id


def test_legacy_stale_replay_reuses_first_occurrence(conn):
    document_id = _insert_legacy_document(
        conn, title="State A", fetched_at="2026-07-15T10:00:00Z"
    )
    backfill_legacy_evidence(conn)
    conn.execute("UPDATE research_documents SET title='State B' WHERE id=?", (document_id,))
    conn.commit()
    backfill_legacy_evidence(conn)
    conn.execute("UPDATE research_documents SET title='State A' WHERE id=?", (document_id,))
    conn.commit()

    backfill_legacy_evidence(conn)

    assert conn.execute("SELECT count(*) FROM evidence_observation_batches").fetchone()[0] == 2
    current = current_candidate_observations(conn, company_id=1, as_of="2026-07-15")
    assert current[0]["authoritative_feed_title"] == "State B"


def test_legacy_recurrence_requires_changed_source_timestamp(conn):
    document_id = _insert_legacy_document(
        conn, title="State A", fetched_at="2026-07-15T10:00:00Z"
    )
    backfill_legacy_evidence(conn)
    conn.execute("UPDATE research_documents SET title='State B' WHERE id=?", (document_id,))
    conn.commit()
    backfill_legacy_evidence(conn)
    conn.execute(
        """UPDATE research_documents
           SET title='State A', fetched_at='2026-07-15T10:05:00Z'
           WHERE id=?""",
        (document_id,),
    )
    conn.commit()

    backfill_legacy_evidence(conn)

    assert conn.execute("SELECT count(*) FROM evidence_observation_batches").fetchone()[0] == 3
    current = current_candidate_observations(conn, company_id=1, as_of="2026-07-15")
    assert current[0]["authoritative_feed_title"] == "State A"
    assert current[0]["effective_at"] == "2026-07-15T10:05:00.000000Z"


def test_legacy_backfill_deduplicates_bytes_across_observed_content_types(conn):
    digest = hashlib.sha256(b"shared legacy PDF").hexdigest()
    for index, content_type in enumerate(
        ("application/octet-stream", "application/pdf"), start=1
    ):
        conn.execute(
            """INSERT INTO research_documents
               (company_id, source_url, source_type, title, published_at, fetched_at,
                ingested_lang, checksum, raw_metadata, report_rules_fingerprint)
               VALUES (1, ?, 'mfn', ?, '2026-07-15', ?, 'en', ?, '{}', 'legacy-rules')""",
            (
                f"https://example.test/document-{index}",
                f"Report {index}",
                f"2026-07-15T10:0{index}:00Z",
                hashlib.sha256(f"document-{index}".encode()).hexdigest(),
            ),
        )
        document_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """INSERT INTO research_attachments
               (document_id, source_url, content_type, byte_size, sha256,
                magic_valid, http_status)
               VALUES (?, ?, ?, 100, ?, 1, 200)""",
            (
                document_id,
                f"https://example.test/document-{index}.pdf",
                content_type,
                digest,
            ),
        )
    conn.commit()

    backfill_legacy_evidence(conn)

    assert [
        row[0] for row in conn.execute("SELECT content_type FROM evidence_artifacts")
    ] == ["application/pdf"]
    assert [
        row[0]
        for row in conn.execute(
            "SELECT content_type FROM evidence_attachment_observations ORDER BY content_type"
        )
    ] == ["application/octet-stream", "application/pdf"]


def test_legacy_backfill_is_idempotent_metadata_only_and_audit_is_stable(conn, tmp_path):
    conn.execute(
        """INSERT INTO research_documents
           (company_id, source_url, source_type, title, published_at, fetched_at,
            ingested_lang, checksum, raw_metadata, report_rules_fingerprint)
           VALUES (1, 'https://example.test/en', 'mfn', 'Q2 report', '2026-07-15',
                   '2026-07-15T10:00:00.123Z', 'en', ?, ?, 'legacy-rules')""",
        (
            "a" * 64,
            json.dumps(
                {
                    "report_kind": "quarterly",
                    "fiscal_period": "Q2-2026",
                    "bilingual_group_id": "q2",
                    "provider_event_id": "legacy-event",
                }
            ),
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
                    "fiscal_period": "Q2-2026",
                    "bilingual_group_id": "q2",
                    "provider_event_id": "legacy-event",
                    "relationship": "TRANSLATION",
                }
            ),
        ),
    )
    child_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """INSERT INTO research_attachments
           (document_id, source_url, content_type, byte_size, sha256, magic_valid, http_status)
           VALUES (?, 'https://example.test/sv.pdf', 'application/pdf', 100, ?, 1, 200)""",
        (child_id, "a" * 64),
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
    assert first["metadata_only_artifacts"] == 2
    assert first["asserted_relations"] == 0
    assert first["unresolved_relations"] == [
        {"document_id": child_id, "duplicate_of": parent_id}
    ]
    assert (
        conn.execute("SELECT count(*) FROM evidence_candidate_relation_observations").fetchone()[0]
        == 0
    )
    assert (
        conn.execute(
            """SELECT effective_at FROM evidence_observation_batches
           WHERE source_input_fingerprint != 'source-staged-v2'
           ORDER BY effective_at LIMIT 1"""
        ).fetchone()[0]
        == "2026-07-15T10:00:00.123000Z"
    )
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
    assert make_batch_id(_batch(1, "one", "2026-09-24T10:00:00Z"))

    relation_batch = append_observation_batch(
        conn,
        replace(
            _batch(1, "legacy-relation-attempt", "2031-01-01T00:00:00Z"),
            as_of="2026-07-15",
            report_rules_fingerprint="legacy-rules",
        ),
    )
    legacy_observations = current_candidate_observations(
        conn, company_id=1, as_of="2026-07-15"
    )
    en_observation = next(row for row in legacy_observations if row["language"] == "en")
    sv_observation = next(row for row in legacy_observations if row["language"] == "sv")
    compatible_signals = ["fiscal_period", "publication_date"]
    for strong_corroborator in (
        {"kind": "shared_provider_event_id", "value": "legacy-event"},
        {"kind": "shared_attachment_checksum", "value": "a" * 64},
    ):
        with pytest.raises(ValueError, match="strong corroborator"):
            append_relation_observation(
                conn,
                RelationObservationInput(
                    relation_batch["batch_id"],
                    en_observation["candidate_observation_id"],
                    sv_observation["candidate_observation_id"],
                    "TRANSLATION",
                    "asserted",
                    {
                        "strong_corroborator": strong_corroborator,
                        "compatible_signals": compatible_signals,
                    },
                    "relation-rules-1",
                ),
            )
    assert (
        conn.execute("SELECT count(*) FROM evidence_candidate_relation_observations").fetchone()[0]
        == 0
    )

    extraction_count = conn.execute(
        "SELECT count(*) FROM evidence_artifact_extractions"
    ).fetchone()[0]
    conn.execute(
        "UPDATE document_extractions SET page_truncated=1 WHERE id=?",
        (extraction_id,),
    )
    conn.commit()
    metadata_corrected = backfill_legacy_evidence(conn)
    metadata_counts = {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in NEW_TABLES
    }
    assert backfill_legacy_evidence(conn) == metadata_corrected
    assert {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in NEW_TABLES
    } == metadata_counts
    assert conn.execute("SELECT count(*) FROM evidence_artifact_extractions").fetchone()[0] == (
        extraction_count + 1
    )
    current = current_candidate_observations(conn, company_id=1, as_of="2026-07-15")
    en_current = next(row for row in current if row["release_source_url"].endswith("/en"))
    assert conn.execute(
        "SELECT page_truncated FROM evidence_artifact_extractions WHERE id=?",
        (en_current["extraction_id"],),
    ).fetchone()[0] == 1

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

    conn.execute(
        "UPDATE document_pages SET text='hello', text_checksum='page-a' WHERE extraction_id=?",
        (extraction_id,),
    )
    conn.commit()
    reverted = backfill_legacy_evidence(conn)
    reverted_counts = {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in NEW_TABLES
    }
    assert backfill_legacy_evidence(conn) == reverted
    assert {
        table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in NEW_TABLES
    } == reverted_counts
    assert conn.execute("SELECT count(*) FROM evidence_observation_batches").fetchone()[0] == (
        batch_count + 1
    )
    current = current_candidate_observations(conn, company_id=1, as_of="2026-07-15")
    en_current = next(row for row in current if row["release_source_url"].endswith("/en"))
    page = conn.execute(
        "SELECT text FROM evidence_artifact_pages WHERE extraction_id=?",
        (en_current["extraction_id"],),
    ).fetchone()
    assert page[0] == "corrected"
