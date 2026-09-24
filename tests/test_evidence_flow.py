from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pypdf import PdfWriter

from alphaforge.cli.main import cmd_mfn_map
from alphaforge.config import Settings
from alphaforge.core.frozen_packet import EVIDENCE_RULES_VERSION
from alphaforge.core.gate.readiness import AgentReadinessGate
from alphaforge.db.connection import get_connection
from alphaforge.db.migrations import migrate
from alphaforge.db.repositories import (
    describe_evidence_state,
    get_mfn_mapping_review,
    get_verified_mfn_mapping,
    load_evidence_packet,
    persist_evidence_diagnostic,
    persist_evidence_document,
    upsert_company,
    upsert_mfn_issuer_mapping,
)
from alphaforge.evidence.flow import (
    EvidenceResourceLimits,
    NoEvidenceReason,
    OneCompanyEvidenceFlow,
    _observation_date,
    build_frozen_evidence_packet,
    download_pdf,
    validate_frozen_packet,
)
from alphaforge.evidence.report_rules import report_rules_metadata
from alphaforge.providers.mfn.issuer import (
    IssuerResolution,
    MfnIssuerResolver,
    parse_mfn_company_search_candidates,
)

FIXTURES = Path(__file__).parent / "fixtures" / "mfn"


def _connection():
    conn = get_connection(Settings.from_env(dsn="sqlite:///:memory:"))
    migrate(conn)
    return conn


def _pdf(*, pages: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def test_exact_mfn_candidate_is_observed_and_can_be_persisted():
    conn = _connection()
    company_id = upsert_company(
        conn,
        {"insId": 7001, "name": "Exact AB", "ticker": "EXACT", "isin": "SE0000007001"},
    )
    company = dict(conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone())
    resolution = MfnIssuerResolver(base_url="https://mfn.test").discover(
        company,
        surfaces=[
            (
                "https://mfn.test/companies",
                (FIXTURES / "issuer_index.html").read_text(encoding="utf-8"),
            )
        ],
    )
    assert resolution.status == "mapped"
    assert resolution.selected["mfn_slug"] == "all/a/exact"
    MfnIssuerResolver(base_url="https://mfn.test").persist_resolution(conn, company, resolution)
    mapping = get_verified_mfn_mapping(conn, company_id)
    assert mapping is not None
    assert mapping["mfn_slug"] == "all/a/exact"
    assert mapping["source_url"].endswith("/all/a/exact")
    assert mapping["identity_evidence"]["matched_identifiers"] == ["exact"]


def test_mfn_resolver_queries_borsdata_identity_surface():
    company = {
        "id": 7,
        "borsdata_id": 7001,
        "name": "Exact AB",
        "ticker": "EXACT",
        "isin": "SE0000007001",
    }
    requested = []

    def request(method, url, **kwargs):
        requested.append(url)
        return SimpleNamespace(status_code=404, text="")

    with patch("alphaforge.providers.mfn.issuer.request_with_retry", side_effect=request):
        MfnIssuerResolver(base_url="https://mfn.test").discover(company)
    assert "https://mfn.test/search/companies?limit=10&query=7001" in requested


def test_mfn_company_search_json_discovers_exact_slug_and_identity():
    company = {
        "id": 101,
        "borsdata_id": 156,
        "name": "NIBE Industrier",
        "ticker": "NIBE B",
        "isin": "SE0015988019",
    }
    payload = {
        "entity_id": "nibe-entity",
        "slug": "nibe-industrier",
        "name": "NIBE Industrier",
        "isins": ["SE0015988019"],
        "tickers": ["XSTO:NIBE B", "XLON:0RH0"],
    }
    surface = "https://mfn.test/search/companies?limit=10&query=NIBE%20B"

    candidates = parse_mfn_company_search_candidates(
        [payload], company=company, surface_url=surface, base_url="https://mfn.test"
    )
    resolution = MfnIssuerResolver(base_url="https://mfn.test").discover(
        company, surfaces=[(surface, json.dumps([payload]))]
    )

    assert len(candidates) == 1
    assert candidates[0]["mfn_slug"] == "all/a/nibe-industrier"
    assert candidates[0]["source_url"] == "https://mfn.test/all/a/nibe-industrier"
    assert candidates[0]["identity_evidence"]["matched_identifiers"] == [
        "nibe b",
        "se0015988019",
    ]
    assert resolution.status == "mapped"
    assert resolution.selected == candidates[0]


def test_mfn_company_search_multiple_exact_records_remain_ambiguous():
    company = {"id": 101, "name": "NIBE Industrier", "ticker": "NIBE B", "isin": "SE0015988019"}
    surface = "https://mfn.test/search/companies?limit=10&query=NIBE%20B"
    payload = [
        {
            "entity_id": "one",
            "slug": "nibe-industrier",
            "name": "NIBE Industrier",
            "isins": ["SE0015988019"],
            "tickers": ["XSTO:NIBE B"],
        },
        {
            "entity_id": "two",
            "slug": "nibe-industrier-legacy",
            "name": "NIBE Industrier legacy",
            "isins": ["SE0015988019"],
            "tickers": ["XSTO:NIBE B"],
        },
    ]

    resolution = MfnIssuerResolver(base_url="https://mfn.test").discover(
        company, surfaces=[(surface, json.dumps(payload))]
    )

    assert resolution.status == "ambiguous"
    assert len(resolution.candidates) == 2


def test_mapped_mfn_mapping_requires_structured_provenance_and_reason():
    conn = _connection()
    company_id = upsert_company(conn, {"insId": 7004, "name": "Evidence AB", "ticker": "EVID"})
    with pytest.raises(ValueError, match="structured provenance and reason"):
        upsert_mfn_issuer_mapping(
            conn,
            company_id,
            status="mapped",
            mfn_slug="all/a/evidence",
            source_url="https://mfn.test/all/a/evidence",
            discovery_source="operator_mapping",
            verified_at="2026-09-20T00:00:00Z",
            identity_evidence={"note": "x"},
        )
    upsert_mfn_issuer_mapping(
        conn,
        company_id,
        status="mapped",
        mfn_slug="all/a/evidence",
        source_url="https://mfn.test/all/a/evidence",
        discovery_source="operator_mapping",
        verified_at="2026-09-20T00:00:00Z",
        identity_evidence={"provenance": "operator", "reason": "reviewed issuer page"},
    )
    assert get_verified_mfn_mapping(conn, company_id)["mfn_slug"] == "all/a/evidence"


def test_ambiguous_mfn_candidates_remain_unmapped_and_visible():
    conn = _connection()
    company_id = upsert_company(conn, {"insId": 7002, "name": "Ambiguous AB", "ticker": "AMB"})
    company = dict(conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone())
    resolver = MfnIssuerResolver(base_url="https://mfn.test")
    resolution = resolver.discover(
        company,
        surfaces=[
            (
                "https://mfn.test/search?q=AMB",
                '<a href="/companies/one" data-ticker="AMB">One</a>'
                '<a href="/companies/two" data-ticker="AMB">Two</a>',
            )
        ],
    )
    assert resolution.status == "ambiguous"
    resolver.persist_resolution(conn, company, resolution)
    assert get_verified_mfn_mapping(conn, company_id) is None
    review = get_mfn_mapping_review(conn, company_id)
    assert review["status"] == "ambiguous"
    assert [candidate["mfn_slug"] for candidate in review["candidates"]] == [
        "companies/one",
        "companies/two",
    ]


class _PagedFakeScraper:
    base_url = "https://mfn.test"

    def __init__(self, articles):
        self.articles = articles

    def discover_feed(self, mfn_slug, *, page=1, reports_only=True):
        return [article for article in self.articles if article["page"] == page]

    def scrape_details(self, entries, *, reports_only=True):
        return list(entries)


class _FakeScraper:
    base_url = "https://mfn.test"

    def __init__(self, articles):
        self.articles = articles
        self.scrape_calls = []

    def discover_feed(self, mfn_slug, *, reports_only=True):
        feed = []
        for article in self.articles:
            entry = {"url": article["source_url"], "title": article["title"]}
            for key in ("provider_event_id", "pdf_checksum", "attachment_checksum"):
                if article.get(key) is not None:
                    entry[key] = article[key]
            feed.append(entry)
        return feed

    def scrape_details(self, entries, *, reports_only=True):
        self.scrape_calls.append(list(entries))
        urls = {
            entry if isinstance(entry, str) else entry.get("url") or entry.get("source_url")
            for entry in entries
        }
        return [article for article in self.articles if article["source_url"] in urls]


def _mapped_company(conn):
    company_id = upsert_company(conn, {"insId": 7003, "name": "Flow AB", "ticker": "FLOW"})
    conn.execute(
        """
        INSERT INTO mfn_issuer_mappings
            (company_id, mfn_slug, source_url, status, discovery_source, verified_at, identity_evidence)
        VALUES (?, 'all/a/flow', 'https://mfn.test/all/a/flow', 'mapped', 'fixture',
                '2026-09-20T00:00:00Z',
                '{"provenance":"fixture","reason":"explicit fixture mapping"}')
        """,
        (company_id,),
    )
    conn.commit()
    return company_id


def test_flow_runs_weekly_page_two_backstop():
    conn = _connection()
    company_id = _mapped_company(conn)
    articles = [
        {
            "page": 1,
            "source_url": "https://mfn.test/a/flow/q1",
            "title": "Flow AB Interim Report Q1 2026",
            "published_at": "2026-05-01T08:00:00Z",
        },
        {
            "page": 2,
            "source_url": "https://mfn.test/a/flow/annual",
            "title": "Flow AB Annual Report 2025",
            "published_at": "2026-03-01T08:00:00Z",
        },
    ]
    result = OneCompanyEvidenceFlow(
        conn,
        scraper=_PagedFakeScraper(articles),
        now=lambda: datetime(2026, 9, 20, tzinfo=UTC),
    ).run(company_id, as_of="2026-09-20", dry_run=True)
    assert result.status == "dry_run"
    assert result.discovered == 2
    assert result.eligible == 2


def test_fiscal_observation_date_uses_reported_period_end_not_calendar_quarter():
    assert (
        _observation_date(
            {
                "title": "Clas Ohlson Interim Report Q1 2026/2027",
                "body": "The first quarter covered 1 May–31 July 2026.",
            }
        )
        == "2026-07-31"
    )
    assert (
        _observation_date(
            {
                "title": "Clas Ohlson Interim Report Q1 2026/2027",
                "body": "The first quarter covered May 1 - July 31, 2026.",
            }
        )
        == "2026-07-31"
    )
    assert (
        _observation_date(
            {
                "title": "Clas Ohlson Interim Report Q1 2026/2027",
                "body": "The first quarter covered May 1 - July 31.",
            }
        )
        == "2026-07-31"
    )


def test_flow_filters_missing_and_future_dates_and_is_idempotent():
    conn = _connection()
    company_id = _mapped_company(conn)
    articles = [
        {
            "source_url": "https://mfn.test/a/flow/q1",
            "title": "Flow AB Interim Report Q1 2026",
            "published_at": "2026-05-01T08:00:00Z",
            "attachment_url": "https://storage.mfn.test/q1.pdf",
            "attachment_tier": "mfn-primary",
            "lang": "en",
        },
        {
            "source_url": "https://mfn.test/a/flow/future",
            "title": "Flow AB Annual Report 2027",
            "published_at": "2027-05-01T08:00:00Z",
            "attachment_url": "https://storage.mfn.test/future.pdf",
            "lang": "en",
        },
        {
            "source_url": "https://mfn.test/a/flow/missing",
            "title": "Flow AB Annual Report 2025",
            "published_at": None,
            "attachment_url": "https://storage.mfn.test/missing.pdf",
            "lang": "en",
        },
    ]
    response = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "application/pdf"},
        content=_pdf(),
    )
    scraper = _FakeScraper(articles)
    flow = OneCompanyEvidenceFlow(conn, scraper=scraper)
    with patch("alphaforge.evidence.flow.request_with_retry", return_value=response):
        first = flow.run(company_id, as_of="2026-09-20")
    assert first.status == "complete"
    assert first.skipped == {"future_dated_release": 1, "missing_publication_timestamp": 1}
    feed_check = conn.execute(
        "SELECT discovered_count, unseen_count FROM mfn_feed_checks ORDER BY checked_at DESC LIMIT 1"
    ).fetchone()
    assert tuple(feed_check) == (3, 3)
    assert first.packet_hash and validate_frozen_packet(first.packet)
    assert "pdf_language_fallback:1" in first.packet["limitations"]
    evidence_job = conn.execute(
        "SELECT status, error, attempt, started_at, finished_at FROM jobs WHERE job_type='evidence' AND company_id=?",
        (company_id,),
    ).fetchone()
    assert evidence_job[0] == "success"
    assert evidence_job[1] is None
    assert evidence_job[2] == 1
    assert evidence_job[3] is not None
    assert evidence_job[4] is not None
    with patch("alphaforge.evidence.flow.request_with_retry", return_value=response):
        second = flow.run(company_id, as_of="2026-09-20")
    assert second.status == "no_evidence"
    assert second.downloaded == 0
    assert second.packet is None
    second_scrape_urls = {entry["url"] for entry in scraper.scrape_calls[-1]}
    assert "https://mfn.test/a/flow/q1" not in second_scrape_urls
    assert second_scrape_urls == {
        "https://mfn.test/a/flow/future",
        "https://mfn.test/a/flow/missing",
    }
    assert conn.execute("SELECT count(*) FROM evidence_packets").fetchone()[0] == 1


def test_rerun_does_not_trust_evidence_without_attachment_tier():
    conn = _connection()
    company_id = _mapped_company(conn)
    article = {
        "source_url": "https://mfn.test/a/flow/q1",
        "title": "Flow AB Interim Report Q1 2026",
        "published_at": "2026-05-01T08:00:00Z",
        "attachment_url": "https://storage.mfn.test/q1.pdf",
        "lang": "en",
    }
    response = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "application/pdf"},
        content=_pdf(),
    )
    with patch("alphaforge.evidence.flow.request_with_retry", return_value=response):
        first = OneCompanyEvidenceFlow(conn, scraper=_FakeScraper([article])).run(
            company_id, as_of="2026-09-20"
        )
    assert first.status == "no_evidence"
    assert first.packet is None
    assert load_evidence_packet(conn, company_id, "2026-09-20") is None
    assert conn.execute("SELECT count(*) FROM research_documents").fetchone()[0] == 1
    with patch("alphaforge.evidence.flow.request_with_retry", return_value=response):
        second = OneCompanyEvidenceFlow(conn, scraper=_FakeScraper([article])).run(
            company_id, as_of="2026-09-20"
        )
    assert second.status == "evidence_incomplete"
    assert second.packet is None
    assert second.completeness == {"quarterly": {"expected": 1, "retained": 0}}
    assert load_evidence_packet(conn, company_id, "2026-09-20") is None


def test_describe_evidence_state_replays_two_as_of_diagnostics():
    conn = _connection()
    company_id = _mapped_company(conn)
    fingerprint = report_rules_metadata()["fingerprint"]
    diagnostic_a = {
        "status": "evidence_incomplete",
        "company_id": company_id,
        "as_of": "2026-09-20",
    }
    diagnostic_b = {"status": "no_evidence", "company_id": company_id, "as_of": "2026-09-21"}
    persist_evidence_diagnostic(
        conn,
        company_id=company_id,
        as_of="2026-09-20",
        status="evidence_incomplete",
        diagnostic=diagnostic_a,
        report_rules_fingerprint=fingerprint,
    )
    persist_evidence_diagnostic(
        conn,
        company_id=company_id,
        as_of="2026-09-21",
        status="no_evidence",
        diagnostic=diagnostic_b,
        report_rules_fingerprint=fingerprint,
    )
    assert describe_evidence_state(conn, company_id=company_id, as_of="2026-09-20") == diagnostic_a
    assert describe_evidence_state(conn, company_id=company_id, as_of="2026-09-21") == diagnostic_b


def test_packet_query_excludes_legacy_document_beside_current_document():
    conn = _connection()
    company_id = _mapped_company(conn)
    mapping = get_verified_mfn_mapping(conn, company_id)

    def persist(source_url: str, published_at: str, checksum: str) -> None:
        persist_evidence_document(
            conn,
            company_id=company_id,
            article={
                "source_url": source_url,
                "title": "Flow AB Interim Report Q1 2026",
                "published_at": published_at,
                "content_text": "The quarter covered 1 May - 31 July 2026.",
                "report_kind": "quarterly",
                "attachment_tier": "mfn-primary",
                "ingested_lang": "en",
            },
            attachment={
                "source_url": source_url + ".pdf",
                "content_type": "application/pdf",
                "byte_size": 8,
                "sha256": checksum,
                "magic_valid": True,
                "http_status": 200,
            },
            extraction={
                "extractor": "pypdf",
                "text_checksum": checksum + "-text",
                "page_count": 1,
                "pages_included": "1",
            },
            pages=[{"page_number": 1, "text": "Evidence"}],
        )

    persist("https://mfn.test/a/current", "2026-05-01T08:00:00Z", "current-checksum")
    persist("https://mfn.test/a/legacy", "2026-06-01T08:00:00Z", "legacy-checksum")
    conn.execute(
        "UPDATE research_documents SET report_rules_fingerprint='legacy' WHERE source_url=?",
        ("https://mfn.test/a/legacy",),
    )
    conn.commit()

    packet = build_frozen_evidence_packet(
        conn, company_id=company_id, as_of="2026-09-20", mapping=mapping
    )
    assert [source["source_url"] for source in packet["sources"]] == ["https://mfn.test/a/current"]


def test_packet_query_excludes_refusal_tier_document_beside_current_document():
    conn = _connection()
    company_id = _mapped_company(conn)
    mapping = get_verified_mfn_mapping(conn, company_id)

    def persist(source_url: str, published_at: str, checksum: str, tier: str | None) -> None:
        article: dict[str, object] = {
            "source_url": source_url,
            "title": "Flow AB Interim Report Q1 2026",
            "published_at": published_at,
            "content_text": "The quarter covered 1 May - 31 July 2026.",
            "report_kind": "quarterly",
            "ingested_lang": "en",
        }
        if tier is not None:
            article["attachment_tier"] = tier
        persist_evidence_document(
            conn,
            company_id=company_id,
            article=article,
            attachment={
                "source_url": source_url + ".pdf",
                "content_type": "application/pdf",
                "byte_size": 8,
                "sha256": checksum,
                "magic_valid": True,
                "http_status": 200,
            },
            extraction={
                "extractor": "pypdf",
                "text_checksum": checksum + "-text",
                "page_count": 1,
                "pages_included": "1",
            },
            pages=[{"page_number": 1, "text": "Evidence"}],
        )

    persist("https://mfn.test/a/current", "2026-05-01T08:00:00Z", "current-checksum", "main-path")
    persist("https://mfn.test/a/refused", "2026-06-01T08:00:00Z", "refused-checksum", "none")
    persist("https://mfn.test/a/untiered", "2026-07-01T08:00:00Z", "untiered-checksum", None)
    conn.commit()

    packet = build_frozen_evidence_packet(
        conn, company_id=company_id, as_of="2026-09-20", mapping=mapping
    )
    assert [source["source_url"] for source in packet["sources"]] == ["https://mfn.test/a/current"]


def test_unhandled_error_persists_replayable_terminal_diagnostic():
    conn = _connection()
    company_id = _mapped_company(conn)

    class _BoomScraper(_FakeScraper):
        def discover_feed(self, mfn_slug, *, reports_only=True):
            raise RuntimeError("feed exploded")

    with pytest.raises(RuntimeError, match="feed exploded"):
        OneCompanyEvidenceFlow(conn, scraper=_BoomScraper([])).run(
            company_id, as_of="2026-09-20"
        )
    state = describe_evidence_state(conn, company_id=company_id, as_of="2026-09-20")
    assert state is not None
    assert state["status"] == "unhandled_error"
    assert state["company_id"] == company_id
    assert state["message"] == "feed exploded"
    job = conn.execute(
        "SELECT status FROM jobs WHERE job_type='evidence' AND company_id=?",
        (company_id,),
    ).fetchone()
    assert job[0] == "failed"


def test_all_future_cutoff_is_typed_no_evidence_and_audited():
    conn = _connection()
    company_id = _mapped_company(conn)
    article = {
        "source_url": "https://mfn.test/a/flow/future-only",
        "title": "Flow AB Interim Report Q1 2027",
        "published_at": "2027-05-01T08:00:00Z",
        "attachment_url": "https://storage.mfn.test/future-only.pdf",
        "lang": "en",
    }
    result = OneCompanyEvidenceFlow(
        conn,
        scraper=_FakeScraper([article]),
        now=lambda: datetime(2026, 9, 21, tzinfo=UTC),
    ).run(company_id, as_of="2026-09-20")

    assert result.status == "no_evidence"
    assert result.no_evidence_reason == NoEvidenceReason.ALL_RELEASES_AFTER_CUTOFF
    assert result.packet is None
    job = conn.execute(
        "SELECT status, error FROM jobs WHERE job_type='evidence' AND company_id=?",
        (company_id,),
    ).fetchone()
    assert job[0] == "partial"
    assert json.loads(job[1])["code"] == "no_evidence"
    assert json.loads(job[1])["no_evidence_reason"] == "all_releases_after_cutoff"


def test_persisted_release_after_future_cutoff_is_all_releases_after_cutoff():
    conn = _connection()
    company_id = _mapped_company(conn)
    persist_evidence_document(
        conn,
        company_id=company_id,
        article={
            "source_url": "https://mfn.test/a/flow/persisted-future",
            "title": "Flow AB Interim Report Q1 2027",
            "published_at": "2027-05-01T08:00:00Z",
            "ingested_lang": "en",
        },
        attachment={
            "source_url": "https://storage.mfn.test/persisted-future.pdf",
            "content_type": "application/pdf",
            "byte_size": 8,
            "sha256": "persisted-future-pdf",
            "magic_valid": True,
            "http_status": 200,
        },
        extraction={
            "extractor": "pypdf",
            "text_checksum": "persisted-future-text",
            "page_count": 1,
            "pages_included": "1",
        },
        pages=[{"page_number": 1, "text": "Evidence"}],
    )

    result = OneCompanyEvidenceFlow(
        conn,
        scraper=_FakeScraper(
            [
                {
                    "source_url": "https://mfn.test/a/flow/persisted-future",
                    "title": "Flow AB Interim Report Q1 2027",
                    "published_at": "2027-05-01T08:00:00Z",
                }
            ]
        ),
        now=lambda: datetime(2026, 9, 21, tzinfo=UTC),
    ).run(company_id, as_of="2027-01-01")

    assert result.status == "no_evidence"
    assert result.no_evidence_reason == NoEvidenceReason.ALL_RELEASES_AFTER_CUTOFF


def test_flow_rechecks_document_without_complete_evidence_artifacts():
    conn = _connection()
    company_id = _mapped_company(conn)
    article = {
        "source_url": "https://mfn.test/a/flow/incomplete",
        "title": "Flow AB Interim Report Q1 2026",
        "published_at": "2026-05-01T08:00:00Z",
        "attachment_url": "https://storage.mfn.test/incomplete.pdf",
        "lang": "en",
    }
    conn.execute(
        "INSERT INTO research_documents (company_id, source_url, source_type, title, published_at) VALUES (?, ?, 'mfn', ?, ?)",
        (company_id, article["source_url"], article["title"], article["published_at"]),
    )
    incomplete_document_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO research_attachments (document_id, source_url, byte_size, sha256, magic_valid) VALUES (?, ?, 4, 'incomplete', 1)",
        (incomplete_document_id, article["attachment_url"]),
    )
    conn.execute(
        "INSERT INTO document_extractions (document_id, extractor, page_count, page_truncated, scanned) VALUES (?, 'pypdf', 0, 0, 0)",
        (incomplete_document_id,),
    )
    conn.commit()
    result = OneCompanyEvidenceFlow(
        conn,
        scraper=_FakeScraper([article]),
        now=lambda: datetime(2026, 9, 21, tzinfo=UTC),
    ).run(company_id, as_of="2026-09-21", dry_run=True)
    assert result.status == "dry_run"
    assert result.discovered == 1
    assert result.eligible == 1


def test_flow_preserves_bilingual_sibling_and_scanned_limitations():
    conn = _connection()
    company_id = _mapped_company(conn)
    articles = [
        {
            "source_url": "https://mfn.test/a/flow/report/sv",
            "title": "Flow AB delårsrapport Q1 2026",
            "published_at": "2026-05-01T08:00:00Z",
            "attachment_url": "https://storage.mfn.test/q1-sv.pdf",
            "lang": "sv",
            "provider_event_id": "flow-q1-2026",
        },
        {
            "source_url": "https://mfn.test/a/flow/report/en",
            "title": "Flow AB Interim Report Q1 2026",
            "published_at": "2026-05-01T08:00:00Z",
            "attachment_url": "https://storage.mfn.test/q1-en.pdf",
            "attachment_tier": "mfn-primary",
            "body": "Material disclosure from the release body.",
            "lang": "en",
            "provider_event_id": "flow-q1-2026",
        },
    ]
    response = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "application/pdf"},
        content=_pdf(pages=6),
    )
    with patch("alphaforge.evidence.flow.request_with_retry", return_value=response):
        result = OneCompanyEvidenceFlow(
            conn,
            scraper=_FakeScraper(articles),
            limits=EvidenceResourceLimits(max_pages=3),
        ).run(company_id, as_of="2026-09-20")
    assert result.status == "complete"
    packet = result.packet
    assert len(packet["sources"]) == 1
    assert packet["sources"][0]["language"] == "en"
    assert packet["sources"][0]["body"]["paragraphs"] == [
        {
            "anchor": f"{packet['sources'][0]['source_id']}#paragraph:1",
            "text": "Material disclosure from the release body.",
        }
    ]
    assert packet["sources"][0]["bilingual_siblings"][0]["language"] == "sv"
    assert "scanned_pdf_no_ocr" in packet["limitations"]
    assert "page_resource_limit" in packet["limitations"]
    source = packet["sources"][0]
    assert source["selection_state"] == "selected"
    assert source["selection_reason"] == "PREFERRED_LANGUAGE"
    assert source["variant_group_id"]
    sibling = source["bilingual_siblings"][0]
    assert sibling["selection_state"] == "suppressed_by_translation"
    assert sibling["selection_reason"] == "FALLBACK_LANGUAGE"
    assert sibling["selected_variant_source_url"] == source["source_url"]
    assert sibling["variant_group_id"] == source["variant_group_id"]
    assert sibling["relationship"] == "TRANSLATION"
    with patch(
        "alphaforge.evidence.flow.request_with_retry", side_effect=AssertionError("redownload")
    ):
        second = OneCompanyEvidenceFlow(
            conn,
            scraper=_FakeScraper([articles[0]]),
            limits=EvidenceResourceLimits(max_pages=3),
            now=lambda: datetime(2026, 9, 21, tzinfo=UTC),
        ).run(company_id, as_of="2026-09-20")
    assert second.status == "complete"
    assert second.packet_hash == result.packet_hash


def test_flow_uses_pdf_language_before_variant_grouping():
    conn = _connection()
    company_id = _mapped_company(conn)
    articles = [
        {
            "source_url": "https://mfn.test/a/flow/pdf-authority/sv",
            "title": "Flow AB delårsrapport Q1 2026",
            "published_at": "2026-05-01T08:00:00Z",
            "attachment_url": "https://storage.mfn.test/q1-sv.pdf",
            "attachment_tier": "mfn-primary",
            "lang": "en",
            "provider_event_id": "flow-pdf-authority-q1",
        },
        {
            "source_url": "https://mfn.test/a/flow/pdf-authority/en",
            "title": "Flow AB Interim Report Q1 2026",
            "published_at": "2026-05-01T08:00:00Z",
            "attachment_url": "https://storage.mfn.test/q1-english.pdf",
            "attachment_tier": "mfn-primary",
            "lang": "sv",
            "provider_event_id": "flow-pdf-authority-q1",
        },
    ]
    response = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "application/pdf"},
        content=_pdf(),
    )
    with patch("alphaforge.evidence.flow.request_with_retry", return_value=response):
        result = OneCompanyEvidenceFlow(
            conn,
            scraper=_FakeScraper(articles),
        ).run(company_id, as_of="2026-09-20")

    assert result.status == "complete"
    assert len(result.packet["sources"]) == 1
    source = result.packet["sources"][0]
    assert source["language"] == "en"
    assert source["source_url"].endswith("/en")
    assert source["bilingual_siblings"][0]["language"] == "sv"
    assert source["bilingual_siblings"][0]["relationship"] == "TRANSLATION"


def test_flow_keeps_no_pdf_variant_out_of_grouping():
    conn = _connection()
    company_id = _mapped_company(conn)
    articles = [
        {
            "source_url": "https://mfn.test/a/flow/no-pdf/sv",
            "title": "Flow AB delårsrapport Q1 2026",
            "published_at": "2026-05-01T08:00:00Z",
            "lang": "sv",
            "provider_event_id": "flow-no-pdf-q1",
        },
        {
            "source_url": "https://mfn.test/a/flow/no-pdf/en",
            "title": "Flow AB Interim Report Q1 2026",
            "published_at": "2026-05-01T08:00:00Z",
            "attachment_url": "https://storage.mfn.test/q1-en.pdf",
            "attachment_tier": "mfn-primary",
            "lang": "en",
            "provider_event_id": "flow-no-pdf-q1",
        },
    ]
    response = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "application/pdf"},
        content=_pdf(),
    )
    with patch("alphaforge.evidence.flow.request_with_retry", return_value=response):
        result = OneCompanyEvidenceFlow(
            conn,
            scraper=_FakeScraper(articles),
        ).run(company_id, as_of="2026-09-20")

    assert result.status == "complete"
    assert len(result.packet["sources"]) == 1
    assert result.packet["sources"][0]["source_url"].endswith("/en")
    assert result.packet["sources"][0]["bilingual_siblings"] == []


@pytest.mark.parametrize(
    ("content_type", "content", "code"),
    [
        ("text/html", b"<html>not a PDF</html>", "invalid_content_type"),
        ("application/pdf", b"not a PDF", "invalid_pdf_magic"),
    ],
)
def test_pdf_acquisition_rejects_non_pdf_payloads(content_type, content, code):
    response = SimpleNamespace(
        status_code=200, headers={"Content-Type": content_type}, content=content
    )
    with pytest.raises(ValueError, match=code.replace("_", " ")):
        download_pdf(
            "https://storage.mfn.test/bad",
            limits=EvidenceResourceLimits(max_retries=0),
            request=lambda *args, **kwargs: response,
        )


def test_pdf_acquisition_enforces_resource_limit():
    response = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "application/pdf", "Content-Length": "100"},
        content=b"%PDF-1.7" + b"0" * 92,
    )
    with pytest.raises(ValueError, match="resource limit"):
        download_pdf(
            "https://storage.mfn.test/large",
            limits=EvidenceResourceLimits(max_pdf_bytes=10, max_retries=0),
            request=lambda *args, **kwargs: response,
        )


def test_readiness_rejects_stray_document_but_accepts_valid_frozen_packet():
    gate = AgentReadinessGate()
    base = {
        "ranking_model": "general",
        "research_evidence": {"documents": [{"id": 1}], "evidence_lane": True},
        "full_results": {
            "reverse_dcf": {"status": "available"},
            "valuation": {"ev_ebit_guardrail_low": 5.0, "ev_ebit_guardrail_high": 20.0},
        },
        "company_id": 1,
        "ticker": "TEST",
    }
    candidate = SimpleNamespace(**base)
    assert gate.assess(candidate).status == "evidence_blocked"
    packet = {
        "schema_version": "evidence-packet-v1",
        "frozen": True,
        "evidence_rules_version": EVIDENCE_RULES_VERSION,
        "report_rules": report_rules_metadata(),
        "company_id": 1,
        "as_of": "2026-05-01",
        "sources": [
            {
                "source_id": "document:1",
                "source_url": "https://mfn.test/report/1",
                "publication_date": "2026-05-01T00:00:00Z",
                "publication_timestamp_authoritative": True,
                "ingestion_date": "2026-05-02T00:00:00Z",
                "attachment": {
                    "source_url": "https://storage.mfn.test/report/1.pdf",
                    "sha256": "abc",
                },
                "extraction": {
                    "extractor": "pypdf",
                    "text_checksum": hashlib.sha256(b"[page 1]\nEvidence").hexdigest(),
                    "page_count": 1,
                },
                "pages": [
                    {
                        "page_number": 1,
                        "anchor": "document:1#page:1",
                        "text": "Evidence",
                        "text_checksum": hashlib.sha256(b"Evidence").hexdigest(),
                    }
                ],
            }
        ],
        "evidence_catalog": {"canonical_source_ids": ["document:1"]},
        "limitations": [],
    }
    packet["packet_hash"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in packet.items() if key != "packet_hash"},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    candidate.research_evidence["evidence_packet"] = packet
    assert gate.assess(candidate).status == "ready"


def _delayed_english_articles(*, with_english: bool):
    sv = {
        "source_url": "https://mfn.test/a/delayed/delarsrapport-q1-2026-2027",
        "title": "Delayed AB delårsrapport Q1 2026/2027",
        "published_at": "2026-09-09T07:30:00Z",
        "attachment_url": "https://storage.mfn.test/delayed-q1-sv.pdf",
        "attachment_tier": "mfn-primary",
        "body": (
            "Omsättning 2847 Msek. Rörelseresultat 312 Msek. "
            "Kvartalet omfattade 1 maj – 31 juli 2026."
        ),
        "content_text": (
            "Omsättning 2847 Msek. Rörelseresultat 312 Msek. "
            "Kvartalet omfattade 1 maj – 31 juli 2026."
        ),
        "lang": "sv",
    }
    if not with_english:
        return [sv]
    en = {
        "source_url": "https://mfn.test/a/delayed/interim-report-q1-2026-27",
        "title": "Delayed AB Interim report Q1 2026/27",
        "published_at": "2026-09-12T07:30:00Z",
        "attachment_url": "https://storage.mfn.test/delayed-q1-en.pdf",
        "attachment_tier": "mfn-primary",
        "body": (
            "Revenue 2847 MSEK. Operating profit 312 MSEK. "
            "The quarter covered 1 May - 31 July 2026."
        ),
        "content_text": (
            "Revenue 2847 MSEK. Operating profit 312 MSEK. "
            "The quarter covered 1 May - 31 July 2026."
        ),
        "lang": "en",
    }
    return [sv, en]


def test_delayed_english_attaches_to_existing_logical_report():
    conn = _connection()
    company_id = _mapped_company(conn)
    response = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "application/pdf"},
        content=_pdf(),
    )
    with patch("alphaforge.evidence.flow.request_with_retry", return_value=response):
        day_one = OneCompanyEvidenceFlow(
            conn, scraper=_FakeScraper(_delayed_english_articles(with_english=False))
        ).run(company_id, as_of="2026-09-20")
    assert day_one.status == "complete"
    assert len(day_one.packet["sources"]) == 1
    assert day_one.packet["sources"][0]["language"] == "sv"
    assert day_one.packet["sources"][0]["selection_reason"] == "FALLBACK_LANGUAGE"
    assert day_one.packet["sources"][0]["period_start"] == "2026-05-01"
    assert day_one.packet["sources"][0]["period_end"] == "2026-07-31"

    with patch("alphaforge.evidence.flow.request_with_retry", return_value=response):
        day_ten = OneCompanyEvidenceFlow(
            conn, scraper=_FakeScraper(_delayed_english_articles(with_english=True))
        ).run(company_id, as_of="2026-09-20")
    assert day_ten.status == "complete"
    assert len(day_ten.packet["sources"]) == 1
    source = day_ten.packet["sources"][0]
    assert source["language"] == "en"
    assert source["selection_reason"] == "PREFERRED_LANGUAGE"
    assert source["period_start"] == "2026-05-01"
    assert source["period_end"] == "2026-07-31"
    assert source["document_type"] == "INTERIM_Q1"
    assert len(source["bilingual_siblings"]) == 1
    assert source["bilingual_siblings"][0]["language"] == "sv"
    assert source["bilingual_siblings"][0]["relationship"] == "TRANSLATION"
    canonical = conn.execute(
        "SELECT source_url, duplicate_of FROM research_documents WHERE company_id=? ORDER BY source_url",
        (company_id,),
    ).fetchall()
    assert len(canonical) == 2
    by_url = {row[0]: row[1] for row in canonical}
    assert by_url["https://mfn.test/a/delayed/interim-report-q1-2026-27"] is None
    assert by_url["https://mfn.test/a/delayed/delarsrapport-q1-2026-2027"] is not None


def test_packet_hash_stable_across_database_document_ids():
    mapping = {
        "mfn_slug": "all/a/clas-ohlson",
        "source_url": "https://mfn.test/all/a/clas-ohlson",
        "discovery_source": "fixture",
        "verified_at": "2026-09-20T00:00:00Z",
        "identity_evidence": {"provenance": "fixture", "reason": "acceptance"},
    }
    article = {
        "source_url": "https://mfn.test/a/clas-ohlson/interim-report-q1-2026-27",
        "title": "Clas Ohlson Interim report Q1 2026/27",
        "published_at": "2026-09-09T07:30:00Z",
        "attachment_tier": "mfn-primary",
        "content_text": "Revenue 2847 MSEK. The quarter covered 1 May - 31 July 2026.",
        "ingested_lang": "en",
        "period_start": "2026-05-01",
        "period_end": "2026-07-31",
        "document_type": "INTERIM_Q1",
    }

    def build_packet(*, add_unrelated_row: bool, add_unrelated_company: bool = False) -> dict:
        conn = _connection()
        if add_unrelated_company:
            upsert_company(conn, {"insId": 9009, "name": "Unrelated AB", "ticker": "UNREL"})
        company_id = upsert_company(conn, {"insId": 9010, "name": "Clas Ohlson", "ticker": "CLA B"})
        if add_unrelated_row:
            conn.execute(
                "INSERT INTO research_documents (company_id, source_url, source_type, title, published_at) "
                "VALUES (?, ?, 'mfn', ?, ?)",
                (company_id, "https://mfn.test/a/unrelated", "Unrelated", "2026-01-01"),
            )
            conn.commit()
        persist_evidence_document(
            conn,
            company_id=company_id,
            article=article,
            attachment={
                "source_url": "https://storage.mfn.test/clas/interim-report-q1-en.pdf",
                "content_type": "application/pdf",
                "byte_size": 8,
                "sha256": "clas-pdf-checksum",
                "magic_valid": True,
                "http_status": 200,
            },
            extraction={
                "extractor": "pypdf",
                "text_checksum": hashlib.sha256(b"[page 1]\nEvidence").hexdigest(),
                "page_count": 1,
                "pages_included": "1",
            },
            pages=[{"page_number": 1, "text": "Evidence"}],
        )
        return build_frozen_evidence_packet(
            conn, company_id=company_id, as_of="2026-09-20", mapping=mapping
        )

    first = build_packet(add_unrelated_row=False)
    second = build_packet(add_unrelated_row=True, add_unrelated_company=True)

    assert first["sources"][0]["source_id"] != second["sources"][0]["source_id"]
    assert first["packet_hash"] == second["packet_hash"]


def test_packet_hash_stable_across_run_timestamps():
    conn = _connection()
    company_id = _mapped_company(conn)
    article = {
        "source_url": "https://mfn.test/a/flow/stable",
        "title": "Flow AB Interim Report Q1 2026",
        "published_at": "2026-05-01T08:00:00Z",
        "attachment_url": "https://storage.mfn.test/stable.pdf",
        "attachment_tier": "mfn-primary",
        "lang": "en",
    }
    response = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "application/pdf"},
        content=_pdf(),
    )
    with patch("alphaforge.evidence.flow.request_with_retry", return_value=response):
        first = OneCompanyEvidenceFlow(conn, scraper=_FakeScraper([article])).run(
            company_id, as_of="2026-09-20"
        )
    assert first.status == "complete"
    conn.execute("UPDATE research_documents SET fetched_at='2030-01-02T03:04:05Z'")
    conn.execute("UPDATE mfn_issuer_mappings SET verified_at='2030-01-03T04:05:06Z'")
    conn.commit()
    rebuilt = build_frozen_evidence_packet(
        conn,
        company_id=company_id,
        as_of="2026-09-20",
        additional_limitations=[
            limitation
            for limitation in first.packet["limitations"]
            if limitation.startswith("attachment_selection_")
        ],
    )
    assert validate_frozen_packet(rebuilt)
    assert rebuilt["packet_hash"] == first.packet_hash


class _ExactMatchResolver:
    base_url = "https://mfn.test"

    def __init__(self, candidate):
        self.candidate = candidate

    def discover(self, company, **kwargs):
        return IssuerResolution("mapped", (self.candidate,), self.candidate, "2026-09-20T00:00:00Z")

    def persist_resolution(self, conn, company, resolution):
        upsert_mfn_issuer_mapping(
            conn,
            int(company["id"]),
            status="mapped",
            mfn_slug=self.candidate["mfn_slug"],
            source_url=self.candidate["source_url"],
            discovery_source="mfn_search_or_index",
            verified_at=resolution.verified_at,
            identity_evidence=self.candidate["identity_evidence"],
        )


def _ambiguous_company(conn):
    company_id = upsert_company(conn, {"insId": 7004, "name": "Reviewed AB", "ticker": "REVW"})
    upsert_mfn_issuer_mapping(
        conn,
        company_id,
        status="ambiguous",
        discovery_source="operator_review",
        identity_evidence={"provenance": "operator", "reason": "two similar issuer pages"},
        reviewed=True,
    )
    conn.commit()
    return company_id


def test_operator_ambiguous_mapping_persists_reviewed_provenance(tmp_path):
    database = tmp_path / "operator-mapping.db"
    dsn = f"sqlite:////{str(database).lstrip('/')}"
    conn = get_connection(Settings.from_env(dsn=dsn))
    migrate(conn)
    company_id = upsert_company(conn, {"insId": 7006, "name": "Operator AB", "ticker": "OPER"})
    conn.close()

    args = SimpleNamespace(
        dsn=dsn,
        company_id=company_id,
        ticker=None,
        status="ambiguous",
        slug=None,
        source_url=None,
        verified_at=None,
        discovery_source="operator_mapping",
        identity_evidence=json.dumps({"provenance": "cli", "reason": "operator review"}),
    )

    assert cmd_mfn_map(args) == 0
    conn = get_connection(Settings.from_env(dsn=dsn))
    try:
        review = get_mfn_mapping_review(conn, company_id)
        assert review["identity_evidence"]["reviewed"] is True
    finally:
        conn.close()


def test_automatic_ambiguous_mapping_allows_fresh_exact_discovery():
    conn = _connection()
    company_id = upsert_company(conn, {"insId": 7005, "name": "Automatic AB", "ticker": "AUTO"})
    upsert_mfn_issuer_mapping(
        conn,
        company_id,
        status="ambiguous",
        discovery_source="mfn_search_or_index",
        identity_evidence={"provenance": "automatic", "reason": "two candidates"},
    )
    candidate = {
        "mfn_slug": "all/a/automatic",
        "source_url": "https://mfn.test/all/a/automatic",
        "match_basis": "exact_ticker",
        "identity_evidence": {"provenance": "exact", "reason": "ticker match", "ticker": "AUTO"},
    }

    result = OneCompanyEvidenceFlow(
        conn,
        scraper=_FakeScraper([]),
        resolver=_ExactMatchResolver(candidate),
    ).run(company_id, as_of="2026-09-20")

    assert result.status == "no_evidence"
    mapping = get_verified_mfn_mapping(conn, company_id)
    assert mapping is not None
    assert mapping["mfn_slug"] == "all/a/automatic"


def test_reviewed_ambiguous_mapping_blocks_fresh_exact_discovery():
    conn = _connection()
    company_id = _ambiguous_company(conn)
    candidate = {
        "mfn_slug": "all/a/reviewed",
        "source_url": "https://mfn.test/all/a/reviewed",
        "match_basis": "exact_ticker",
        "identity_evidence": {"ticker": "REVW"},
    }
    result = OneCompanyEvidenceFlow(
        conn,
        scraper=_FakeScraper([]),
        resolver=_ExactMatchResolver(candidate),
    ).run(company_id, as_of="2026-09-20")
    assert result.status == "mapping_ambiguous"
    assert result.mapping_status == "ambiguous"
    assert get_verified_mfn_mapping(conn, company_id) is None
    review = get_mfn_mapping_review(conn, company_id)
    assert review["status"] == "ambiguous"
    assert [item["mfn_slug"] for item in review["candidates"]] == ["all/a/reviewed"]
    assert conn.execute("SELECT count(*) FROM research_documents").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM evidence_packets").fetchone()[0] == 0
    job = conn.execute(
        "SELECT status, error FROM jobs WHERE job_type='evidence' AND company_id=?",
        (company_id,),
    ).fetchone()
    assert job[0] == "failed"
    assert json.loads(job[1])["code"] == "mapping_ambiguous"


def test_re_review_clears_ambiguous_block():
    conn = _connection()
    company_id = _ambiguous_company(conn)
    candidate = {
        "mfn_slug": "all/a/reviewed",
        "source_url": "https://mfn.test/all/a/reviewed",
        "match_basis": "exact_ticker",
        "identity_evidence": {"ticker": "REVW"},
    }
    resolver = _ExactMatchResolver(candidate)
    blocked = OneCompanyEvidenceFlow(conn, scraper=_FakeScraper([]), resolver=resolver).run(
        company_id, as_of="2026-09-20"
    )
    assert blocked.status == "mapping_ambiguous"
    upsert_mfn_issuer_mapping(
        conn,
        company_id,
        status="mapped",
        mfn_slug="all/a/reviewed",
        source_url="https://mfn.test/all/a/reviewed",
        discovery_source="operator_mapping",
        verified_at="2026-09-20T00:00:00Z",
        identity_evidence={"provenance": "operator", "reason": "re-reviewed exact match"},
    )
    article = {
        "source_url": "https://mfn.test/a/reviewed/q1",
        "title": "Reviewed AB Interim Report Q1 2026",
        "published_at": "2026-05-01T08:00:00Z",
        "attachment_url": "https://storage.mfn.test/reviewed-q1-en.pdf",
        "attachment_tier": "mfn-primary",
        "lang": "en",
    }
    response = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "application/pdf"},
        content=_pdf(),
    )
    with patch("alphaforge.evidence.flow.request_with_retry", return_value=response):
        cleared = OneCompanyEvidenceFlow(
            conn, scraper=_FakeScraper([article]), resolver=resolver
        ).run(company_id, as_of="2026-09-20")
    assert cleared.status == "complete"
    assert cleared.packet_hash and validate_frozen_packet(cleared.packet)
    assert len(cleared.packet["sources"]) == 1


class _PaginatedFakeScraper:
    base_url = "https://mfn.test"

    def __init__(self, pages: dict[int, list[dict[str, str]]]):
        self.pages = pages

    def discover_feed_paginated(self, mfn_slug, offset=0, limit=48, reports_only=True):
        items = list(self.pages.get(offset, []))
        # Simulate next_offset: next key sorted
        sorted_offsets = sorted(self.pages.keys())
        try:
            idx = sorted_offsets.index(offset)
            nxt = sorted_offsets[idx + 1] if idx + 1 < len(sorted_offsets) else None
        except ValueError:
            nxt = None
        return items, nxt

    def discover_feed(self, mfn_slug, page=1, reports_only=True):
        # Fallback shim for legacy path
        return list(self.pages.get(0, []))

    def scrape_details(self, entries, reports_only=True):
        # Entries are feed dicts; return them enriched with storage_url already present
        out: list[dict[str, str]] = []
        url_map = {a["source_url"]: dict(a) for page in self.pages.values() for a in page}
        for e in entries:
            url = e if isinstance(e, str) else e.get("url") or e.get("source_url") or ""
            if url in url_map:
                out.append(dict(url_map[url]))
            else:
                out.append(dict(e) if isinstance(e, dict) else {"source_url": url, "title": url})
        return out


def test_flow_paginated_history_retrieves_multiple_reports():
    """Regression: Clas Ohlson-style historical pagination must retain many reports.

    Before the fix, ``discover_feed`` only saw the first HTML page (≈1 logical
    report after bilingual dedupe).  With offset/limit pagination, a window
    covering several quarters must yield multiple packet sources.  Bilingual
    pairs must still collapse to one.
    """
    conn = _connection()
    company_id = _mapped_company(conn)
    pages = {
        0: [
            {
                "source_url": "https://mfn.test/a/flow/q1-2026",
                "title": "Flow AB Interim Report Q1 2026",
                "published_at": "2026-05-01T08:00:00Z",
                "attachment_url": "https://storage.mfn.test/q1-2026.pdf",
                "storage_url": "https://storage.mfn.test/q1-2026.pdf",
                "attachment_tier": "mfn-primary",
                "lang": "en",
                "report_kind": "quarterly",
                "document_type": "INTERIM_Q1",
            },
            {
                "source_url": "https://mfn.test/a/flow/q3-2025",
                "title": "Flow AB Interim Report Q3 2025",
                "published_at": "2025-03-12T08:00:00Z",
                "attachment_url": "https://storage.mfn.test/q3-2025.pdf",
                "storage_url": "https://storage.mfn.test/q3-2025.pdf",
                "attachment_tier": "mfn-primary",
                "lang": "en",
                "report_kind": "quarterly",
            },
        ],
        48: [
            {
                "source_url": "https://mfn.test/a/flow/annual-2024",
                "title": "Flow AB Annual Report 2024",
                "published_at": "2024-07-04T08:00:00Z",
                "attachment_url": "https://storage.mfn.test/annual-2024.pdf",
                "storage_url": "https://storage.mfn.test/annual-2024.pdf",
                "attachment_tier": "mfn-primary",
                "lang": "en",
                "report_kind": "annual",
                "document_type": "ANNUAL_REPORT",
            },
        ],
    }
    scraper = _PaginatedFakeScraper(pages)
    response = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "application/pdf"},
        content=_pdf(),
    )
    with patch("alphaforge.evidence.flow.request_with_retry", return_value=response):
        result = OneCompanyEvidenceFlow(
            conn,
            scraper=scraper,
            now=lambda: datetime(2026, 9, 20, tzinfo=UTC),
        ).run(company_id, as_of="2026-09-20")
    assert result.status == "complete"
    assert result.packet is not None
    assert len(result.packet["sources"]) == 3
    titles = {s["title"] for s in result.packet["sources"]}
    assert "Flow AB Interim Report Q1 2026" in titles
    assert "Flow AB Interim Report Q3 2025" in titles
    assert "Flow AB Annual Report 2024" in titles


def test_flow_history_window_truncates_old_reports():
    """Window must bound historical depth — reports older than the window are excluded."""
    conn = _connection()
    company_id = _mapped_company(conn)
    pages = {
        0: [
            {
                "source_url": "https://mfn.test/a/flow/q1-2026",
                "title": "Flow AB Interim Report Q1 2026",
                "published_at": "2026-05-01T08:00:00Z",
                "attachment_url": "https://storage.mfn.test/q1-2026.pdf",
                "storage_url": "https://storage.mfn.test/q1-2026.pdf",
                "attachment_tier": "mfn-primary",
                "lang": "en",
                "report_kind": "quarterly",
            },
        ],
        48: [
            {
                "source_url": "https://mfn.test/a/flow/annual-2018",
                "title": "Flow AB Annual Report 2018",
                "published_at": "2018-07-04T08:00:00Z",
                "attachment_url": "https://storage.mfn.test/annual-2018.pdf",
                "storage_url": "https://storage.mfn.test/annual-2018.pdf",
                "attachment_tier": "mfn-primary",
                "lang": "en",
                "report_kind": "annual",
            },
        ],
    }
    scraper = _PaginatedFakeScraper(pages)
    response = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "application/pdf"},
        content=_pdf(),
    )
    with patch("alphaforge.evidence.flow.request_with_retry", return_value=response):
        result = OneCompanyEvidenceFlow(
            conn,
            scraper=scraper,
            now=lambda: datetime(2026, 9, 20, tzinfo=UTC),
        ).run(company_id, as_of="2026-09-20")
    assert result.status == "complete"
    assert len(result.packet["sources"]) == 1
    assert result.packet["sources"][0]["title"] == "Flow AB Interim Report Q1 2026"


def test_discover_feed_page_two_uses_offset():
    """Legacy ``?page=2`` must map to offset/limit so Sunday backstop advances."""
    from alphaforge.providers.mfn.scraper import MfnScraper

    pages = {
        0: [{"source_url": "https://mfn.test/a/acme/q1", "title": "Q1 Report"}],
        24: [{"source_url": "https://mfn.test/a/acme/annual", "title": "Annual Report"}],
    }

    class _OffsetScraper(MfnScraper):
        def __init__(self):
            super().__init__(base_url="https://mfn.test", max_articles=24)
            self.calls: list[tuple[int, int]] = []

        def discover_feed_paginated(self, mfn_slug, offset=0, limit=48, reports_only=True):  # type: ignore[override]
            self.calls.append((offset, limit))
            items = pages.get(offset, [])
            nxt = 24 if offset == 0 and 24 in pages else None
            return items, nxt

    scraper = _OffsetScraper()
    # page=2 should become offset=24 ( (2-1)*24 )
    result = scraper.discover_feed("all/a/acme", page=2)
    assert scraper.calls == [(24, 24)]
    assert len(result) == 1
    assert result[0]["title"] == "Annual Report"
