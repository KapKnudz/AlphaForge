"""AQ public-source manifest/repository replay fixture; no raw PDF bytes are committed.

These are the actual 27 release/PDF identities, checksums and persisted sibling
relationships from the preserved 2026-09-26 AQ run. This exercises the
manifest/repository/cache boundary, not independent PDF semantics or HTTP.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from alphaforge.config import Settings
from alphaforge.core.frozen_packet import (
    is_stale_evidence_packet,
    stable_packet_hash,
    validate_frozen_packet,
)
from alphaforge.db.connection import get_connection
from alphaforge.db.migrations import migrate
from alphaforge.db.repositories import (
    load_evidence_selection_manifest,
    persist_evidence_selection_manifest,
    upsert_company,
)
from alphaforge.evidence.flow import OneCompanyEvidenceFlow
from alphaforge.evidence.manifest_store import load_evidence_view
from alphaforge.evidence.report_rules import report_rules_metadata

FIXTURE = Path(__file__).parent / "fixtures" / "mfn" / "aq_replay_inventory.json"


def _fixture():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    reports = data["reports"]
    assert len(reports) == len({row["source_url"] for row in reports}) == 27
    assert len({row["attachment_url"] for row in reports}) == 27
    assert len([row for row in reports if row["selected"]]) == 16
    assert len([row for row in reports if not row["selected"]]) == 11
    assert all(row["source_url"].startswith("https://mfn.se/cis/a/aq-group/") for row in reports)
    assert all(
        row["attachment_url"].startswith("https://mb.cision.com/Main/11536/") for row in reports
    )
    # Digest independently computed over the archive's original MFN URL, PDF
    # URL and SHA tuples. A synthetic slug/checksum cannot silently replace it.
    observed = hashlib.sha256(
        json.dumps(
            sorted(
                (row["source_url"], row["attachment_url"], row["pdf_sha256"]) for row in reports
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert observed == "e9768beb72698c5e9ce7dd9651f90e5433b04d30169cf0d1e2eee976b7c3245b"
    return data


def _seed_inventory(data):
    conn = get_connection(Settings.from_env(dsn="sqlite:///:memory:"))
    migrate(conn)
    company_id = upsert_company(
        conn,
        {
            "insId": data["borsdata_id"],
            "name": "AQ Group",
            "ticker": data["ticker"],
            "isin": data["isin"],
        },
    )
    conn.execute(
        """INSERT INTO mfn_issuer_mappings
           (company_id, mfn_slug, source_url, status, discovery_source, verified_at, identity_evidence)
           VALUES (?, 'all/a/aq-group', 'https://mfn.se/all/a/aq-group', 'mapped',
                   'fixture', '2026-09-24T00:00:00Z',
                   '{"provenance":"fixture","reason":"source-recorded ISIN"}')""",
        (company_id,),
    )
    fingerprint = report_rules_metadata()["fingerprint"]
    document_ids = {}
    for row in data["reports"]:
        if not row["selected"]:
            continue
        metadata = {
            "report_kind": row["report_kind"],
            "attachment_tier": row["attachment_tier"],
            "authoritative_publication_timestamp": True,
            "pdf_language": row["pdf_language"],
            "language_evidence": row["language_evidence"],
        }
        if row.get("variant_group_id"):
            metadata["bilingual_group_id"] = row["variant_group_id"]
        cursor = conn.execute(
            """INSERT INTO research_documents
               (company_id, source_url, source_type, title, published_at, ingested_lang,
                raw_metadata, report_rules_fingerprint)
               VALUES (?, ?, 'mfn', ?, ?, ?, ?, ?)""",
            (
                company_id,
                row["source_url"],
                row["title"],
                row["published_at"],
                row["pdf_language"],
                json.dumps(metadata),
                fingerprint,
            ),
        )
        document_id = cursor.lastrowid
        document_ids[row["source_url"]] = document_id
        conn.execute(
            """INSERT INTO research_attachments
               (document_id, source_url, content_type, byte_size, sha256, magic_valid, http_status)
               VALUES (?, ?, 'application/pdf', ?, ?, 1, 200)""",
            (document_id, row["attachment_url"], row["byte_size"], row["pdf_sha256"]),
        )
        # The fixture contains PDF identity/metadata, not source PDF bytes or
        # extracted text. The title is a minimal schema-valid page placeholder;
        # this test asserts selection/provenance, not extraction fidelity.
        page_text = row["title"]
        first_page_text = f"[page {row['page_number']}]\n{page_text}".rstrip()
        extraction = conn.execute(
            """INSERT INTO document_extractions
               (document_id, extractor, text_checksum, page_count, pages_included,
                page_truncated, scanned)
               VALUES (?, 'pypdf', ?, ?, ?, ?, 0)""",
            (
                document_id,
                hashlib.sha256(first_page_text.encode("utf-8")).hexdigest(),
                row["page_count"],
                row["pages_included"],
                int(row["page_truncated"]),
            ),
        )
        conn.execute(
            """INSERT INTO document_pages
               (extraction_id, page_number, anchor, text, text_checksum)
               VALUES (?, ?, ?, ?, ?)""",
            (
                extraction.lastrowid,
                row["page_number"],
                f"document:{document_id}#page:{row['page_number']}",
                page_text,
                hashlib.sha256(page_text.encode("utf-8")).hexdigest(),
            ),
        )
    for row in data["reports"]:
        if row["selected"]:
            continue
        conn.execute(
            """INSERT INTO research_documents
               (company_id, source_url, source_type, title, published_at, duplicate_of,
                ingested_lang, raw_metadata)
               VALUES (?, ?, 'mfn', ?, ?, ?, 'sv', ?)""",
            (
                company_id,
                row["source_url"],
                row["title"],
                row["published_at"],
                document_ids[row["parent_source_url"]],
                json.dumps(
                    {
                        "bilingual_group_id": row["variant_group_id"],
                        "pdf_checksum": row["pdf_sha256"],
                        "pdf_language": row["pdf_language"],
                        "language_evidence": row["language_evidence"],
                        "relationship": row["relationship"],
                        "duplicate_of_source_url": row["parent_source_url"],
                    }
                ),
            ),
        )
    conn.commit()
    return conn, company_id


def _candidates(reports):
    # Pre-ingestion release facts do not carry a verified variant group id.
    return [
        {
            "source_url": row["source_url"],
            "title": row["title"],
            "published_at": row["published_at"],
            "report_kind": row["report_kind"],
            "attachment_url": row["attachment_url"],
        }
        for row in reports
    ]


def _feed_fingerprint(feed):
    return hashlib.sha256(
        json.dumps(
            sorted(feed, key=lambda entry: entry["url"]),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


class _CachedFeed:
    base_url = "https://mfn.se"

    def __init__(self, feed):
        self.feed = feed
        self.detail_calls = []

    def discover_feed(self, _slug, *, reports_only=True):
        return list(self.feed)

    def scrape_details(self, entries, *, reports_only=True):
        self.detail_calls.append(list(entries))
        assert not entries, "all 27 source URLs are already cache-complete"
        return []


def test_real_aq_inventory_is_preserved_through_manifest_and_cache_replay():
    data = _fixture()
    rows = data["reports"]
    conn, company_id = _seed_inventory(data)
    feed = [{"url": row["source_url"], "title": row["title"]} for row in rows]
    selected = {row["source_url"]: row for row in rows if row["selected"]}
    excluded = {row["source_url"]: row for row in rows if not row["selected"]}
    assert len({row["pdf_sha256"] for row in rows if row["selected"]}) == 16
    for sibling in excluded.values():
        parent = selected[sibling["parent_source_url"]]
        assert sibling["relationship"] == "TRANSLATION"
        assert sibling["variant_group_id"] == parent["variant_group_id"]
        assert sibling["pdf_sha256"] != parent["pdf_sha256"]

    first = load_evidence_selection_manifest(
        conn,
        company_id=company_id,
        as_of=data["as_of"],
        report_rules=report_rules_metadata(),
        candidate_records=_candidates(rows),
        source_input_fingerprint=_feed_fingerprint(feed),
    )
    assert first.completeness == {
        "annual": {"expected": 9, "retained": 9},
        "quarterly": {"expected": 7, "retained": 7},
    }
    assert {row["source_url"] for row in first.audit_history} == {row["source_url"] for row in rows}
    assert {row["source_url"] for row in first.packet_contents()} == set(selected)
    assert {row["source_url"] for row in first.rejected} == set(excluded)
    assert {row["reason"] for row in first.rejected} == {"corroborated_translation"}
    assert len(first.deduplication) == 16
    for sibling in excluded.values():
        group = next(
            group
            for group in first.deduplication
            if sibling["source_url"] in group["candidate_source_urls"]
        )
        assert sibling["parent_source_url"] in group["candidate_source_urls"]
        assert group["packet_source_urls"] == [sibling["parent_source_url"]]
    q2 = [
        row
        for row in rows
        if row["selected"]
        and any(marker in row["source_url"] for marker in ("june-2026", "juni-2026"))
    ]
    assert len(q2) == 2
    assert (
        len(
            {
                group["group_id"]
                for group in first.deduplication
                if any(row["source_url"] in group["packet_source_urls"] for row in q2)
            }
        )
        == 2
    )
    persist_evidence_selection_manifest(conn, first)

    _, read_view = load_evidence_view(conn, company_id=company_id, as_of=data["as_of"])
    assert read_view.completeness == first.completeness
    assert read_view.manifest_id == first.manifest_id
    scraper = _CachedFeed(feed)
    with patch(
        "alphaforge.evidence.flow.request_with_retry",
        side_effect=AssertionError("unexpected PDF HTTP"),
    ):
        replay = OneCompanyEvidenceFlow(conn, scraper=scraper).run(company_id, as_of=data["as_of"])
    assert replay.status == "complete", (replay.message, replay.diagnostic())
    assert replay.downloaded == 0
    assert replay.packet is not None and validate_frozen_packet(replay.packet)
    legacy = {k: v for k, v in replay.packet.items() if k != "packet_hash"}
    legacy["evidence_rules_version"] = 1
    legacy["packet_hash"] = stable_packet_hash(legacy)
    assert validate_frozen_packet(legacy)
    assert is_stale_evidence_packet(legacy)
    assert scraper.detail_calls == [[]]
    _, replay_view = load_evidence_view(conn, company_id=company_id, as_of=data["as_of"])
    assert replay_view.manifest_id == first.manifest_id
    assert {row["source_url"] for row in replay_view.audit_history} == {
        row["source_url"] for row in rows
    }
    assert {row["source_url"] for row in replay_view.rejected} == set(excluded)
    assert replay_view.completeness == first.completeness


def test_aq_manifest_current_read_tracks_a_to_b_to_a_relationship():
    data = _fixture()
    conn, company_id = _seed_inventory(data)
    sibling = next(row for row in data["reports"] if not row["selected"])
    saved = conn.execute(
        "SELECT raw_metadata FROM research_documents WHERE company_id=? AND source_url=?",
        (company_id, sibling["source_url"]),
    ).fetchone()[0]

    def current():
        manifest = load_evidence_selection_manifest(
            conn,
            company_id=company_id,
            as_of=data["as_of"],
            report_rules=report_rules_metadata(),
            candidate_records=_candidates(data["reports"]),
        )
        persist_evidence_selection_manifest(conn, manifest)
        _, view = load_evidence_view(conn, company_id=company_id, as_of=data["as_of"])
        assert view.manifest_id == manifest.manifest_id
        assert view.completeness == manifest.completeness
        return manifest

    a = current()
    conn.execute(
        "UPDATE research_documents SET raw_metadata=json_remove(raw_metadata, '$.relationship') WHERE company_id=? AND source_url=?",
        (company_id, sibling["source_url"]),
    )
    b = current()
    assert b.manifest_id != a.manifest_id
    assert sum(x["expected"] - x["retained"] for x in b.completeness.values()) == 1
    conn.execute(
        "UPDATE research_documents SET raw_metadata=? WHERE company_id=? AND source_url=?",
        (saved, company_id, sibling["source_url"]),
    )
    restored = current()
    assert restored.manifest_id == a.manifest_id
    assert restored.completeness == a.completeness


def test_aq_unmatched_sibling_remains_an_independent_incomplete_group():
    data = _fixture()
    rows = data["reports"]
    conn, company_id = _seed_inventory(data)
    sibling = next(row for row in rows if not row["selected"])
    feed = [{"url": row["source_url"], "title": row["title"]} for row in rows]
    verified = load_evidence_selection_manifest(
        conn,
        company_id=company_id,
        as_of=data["as_of"],
        report_rules=report_rules_metadata(),
        candidate_records=_candidates(rows),
        source_input_fingerprint=_feed_fingerprint(feed),
    )
    persist_evidence_selection_manifest(conn, verified)
    conn.execute(
        """UPDATE research_documents SET raw_metadata=json_remove(raw_metadata, '$.relationship')
           WHERE company_id=? AND source_url=?""",
        (company_id, sibling["source_url"]),
    )
    conn.commit()
    manifest = load_evidence_selection_manifest(
        conn,
        company_id=company_id,
        as_of=data["as_of"],
        report_rules=report_rules_metadata(),
        candidate_records=_candidates(rows),
    )
    assert sum(x["expected"] for x in manifest.completeness.values()) == 17
    assert sum(x["retained"] for x in manifest.completeness.values()) == 16
    assert any(
        group["candidate_source_urls"] == [sibling["source_url"]]
        and not group["packet_source_urls"]
        for group in manifest.deduplication
    )
    assert {row["source_url"] for row in manifest.audit_history} == {
        row["source_url"] for row in rows
    }
    scraper = _CachedFeed(feed)
    with patch(
        "alphaforge.evidence.flow.request_with_retry",
        side_effect=AssertionError("unexpected PDF HTTP"),
    ):
        replay = OneCompanyEvidenceFlow(conn, scraper=scraper).run(company_id, as_of=data["as_of"])
    assert replay.status == "evidence_incomplete", replay.diagnostic()
    assert replay.downloaded == 0
    assert sum(group["expected"] - group["retained"] for group in replay.completeness.values()) == 1
    assert scraper.detail_calls == [[]]
    _, read_view = load_evidence_view(conn, company_id=company_id, as_of=data["as_of"])
    assert read_view.completeness == replay.completeness
    assert {row["source_url"] for row in read_view.audit_history} == {
        row["source_url"] for row in rows
    }
    assert any(
        row["source_url"] == sibling["source_url"] and row["reason"] == "relationship_unresolved"
        for row in read_view.rejected
    )
