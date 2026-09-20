from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pypdf import PdfWriter

from alphaforge.config import Settings
from alphaforge.core.gate.readiness import AgentReadinessGate
from alphaforge.db.connection import get_connection
from alphaforge.db.migrations import migrate
from alphaforge.db.repositories import (
    get_mfn_mapping_review,
    get_verified_mfn_mapping,
    upsert_company,
    upsert_mfn_issuer_mapping,
)
from alphaforge.evidence.flow import (
    EvidenceResourceLimits,
    OneCompanyEvidenceFlow,
    download_pdf,
    validate_frozen_packet,
)
from alphaforge.providers.mfn.issuer import MfnIssuerResolver

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
        return [{"url": article["source_url"], "title": article["title"]} for article in self.articles]

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


def test_flow_filters_missing_and_future_dates_and_is_idempotent():
    conn = _connection()
    company_id = _mapped_company(conn)
    articles = [
        {
            "source_url": "https://mfn.test/a/flow/q1",
            "title": "Flow AB Interim Report Q1 2026",
            "published_at": "2026-05-01T08:00:00Z",
            "attachment_url": "https://storage.mfn.test/q1.pdf",
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
        "SELECT discovered_count, unseen_count FROM mfn_feed_checks ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert tuple(feed_check) == (3, 3)
    assert first.packet_hash and validate_frozen_packet(first.packet)
    with patch("alphaforge.evidence.flow.request_with_retry", side_effect=AssertionError("redownload")):
        second = flow.run(company_id, as_of="2026-09-20")
    assert second.status == "complete"
    assert second.downloaded == 0
    assert second.packet_hash == first.packet_hash
    second_scrape_urls = {entry["url"] for entry in scraper.scrape_calls[-1]}
    assert "https://mfn.test/a/flow/q1" not in second_scrape_urls
    assert second_scrape_urls == {
        "https://mfn.test/a/flow/future",
        "https://mfn.test/a/flow/missing",
    }
    assert conn.execute("SELECT count(*) FROM evidence_packets").fetchone()[0] == 1


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
        },
        {
            "source_url": "https://mfn.test/a/flow/report/en",
            "title": "Flow AB Interim Report Q1 2026",
            "published_at": "2026-05-01T08:00:00Z",
            "attachment_url": "https://storage.mfn.test/q1-en.pdf",
            "body": "Material disclosure from the release body.",
            "lang": "en",
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


@pytest.mark.parametrize(
    ("content_type", "content", "code"),
    [
        ("text/html", b"<html>not a PDF</html>", "invalid_content_type"),
        ("application/pdf", b"not a PDF", "invalid_pdf_magic"),
    ],
)
def test_pdf_acquisition_rejects_non_pdf_payloads(content_type, content, code):
    response = SimpleNamespace(status_code=200, headers={"Content-Type": content_type}, content=content)
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
        "frozen": True,
        "sources": [
            {
                "source_id": "document:1",
                "publication_date": "2026-05-01T00:00:00Z",
                "publication_timestamp_authoritative": True,
                "attachment": {"sha256": "abc"},
                "pages": [{"anchor": "document:1#page:1"}],
            }
        ],
        "limitations": [],
    }
    packet["packet_hash"] = __import__("hashlib").sha256(
        json.dumps({key: value for key, value in packet.items() if key != "packet_hash"}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    candidate.research_evidence["evidence_packet"] = packet
    assert gate.assess(candidate).status == "ready"
