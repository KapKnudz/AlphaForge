from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from pypdf import PdfWriter

from alphaforge.config import Settings
from alphaforge.db.connection import get_connection
from alphaforge.db.migrations import migrate
from alphaforge.evidence.ingest import ResearchDocumentIngestionService, bilingual_dedupe
from alphaforge.providers.mfn.scraper import MfnScraper


def _connection():
    conn = get_connection(Settings.from_env(dsn="sqlite:///:memory:"))
    migrate(conn)
    return conn


def test_discover_feed_excludes_news_and_report_schedules():
    html = """
    <a class="title-link item-link" href="/a/acme/q1">Q1 2026 Interim Report</a>
    <a class="title-link item-link" href="/a/acme/q1-sv">Kvartalsrapport Q1 2026</a>
    <a class="title-link item-link" href="/a/acme/news">Acme wins a new contract</a>
    <a class="title-link item-link" href="/a/acme/calendar">Report Schedule 2026</a>
    <a class="title-link item-link" href="/about/annual-report">Annual Report navigation</a>
    <a class="title-link item-link" href="https://storage.mfn.se/acme/Annual-Report-2026.pdf">Annual Report 2026</a>
    """
    response = SimpleNamespace(status_code=200, text=html)
    scraper = MfnScraper(base_url="https://mfn.test")
    with (
        patch("alphaforge.providers.mfn.scraper.request_with_retry", return_value=response),
        patch("alphaforge.providers.mfn.scraper.time.sleep"),
    ):
        articles = scraper.discover_feed("all/a/acme")
    assert [article["title"] for article in articles] == [
        "Q1 2026 Interim Report",
        "Kvartalsrapport Q1 2026",
    ]
    assert articles[0]["report_kind"] == "quarterly"
    assert articles[1]["lang"] == "sv"


def test_scrape_details_extracts_timestamp_body_and_pdf():
    html = """
    <html>
      <head><meta property="article:published_time" content="2026-05-07T06:30:00+02:00"></head>
      <body>
        <h1>Acme Year-End Report 2025</h1>
        <article class="release-body"><p>Revenue grew.<br>Cash was stable.</p></article>
        <footer>Navigation should not be evidence.</footer>
        <a href="https://storage.mfn.se/uuid/id1.pdf">Presentation</a>
        <a href="https://storage.mfn.se/uuid/id2.pdf">Annual report PDF</a>
      </body>
    </html>
    """
    response = SimpleNamespace(status_code=200, text=html)
    scraper = MfnScraper(base_url="https://mfn.test")
    with (
        patch("alphaforge.providers.mfn.scraper.request_with_retry", return_value=response),
        patch("alphaforge.providers.mfn.scraper.time.sleep"),
    ):
        articles = scraper.scrape_details([{"url": "https://mfn.test/a/acme/annual"}])
    assert len(articles) == 1
    assert articles[0]["published_at"] == "2026-05-07T04:30:00Z"
    assert articles[0]["storage_url"].endswith("id2.pdf")
    assert articles[0]["content_text"] == "Revenue grew. Cash was stable."
    assert "Navigation should not be evidence." not in articles[0]["content_text"]


def test_scrape_details_prefers_published_json_timestamp():
    html = """
    <h1>Acme Year-End Report 2025</h1>
    <script type="application/ld+json">
      {"@graph": [
        {"@type": "WebPage", "dateCreated": "2026-05-01T08:00:00Z"},
        {"@type": "NewsArticle", "datePublished": "2026-05-07T06:30:00Z"}
      ]}
    </script>
    """
    response = SimpleNamespace(status_code=200, text=html)
    scraper = MfnScraper(base_url="https://mfn.test")
    with (
        patch("alphaforge.providers.mfn.scraper.request_with_retry", return_value=response),
        patch("alphaforge.providers.mfn.scraper.time.sleep"),
    ):
        articles = scraper.scrape_details([{"url": "https://mfn.test/a/acme/annual"}])
    assert articles[0]["published_at"] == "2026-05-07T06:30:00Z"


def test_scrape_details_normalises_human_timestamp():
    html = """
    <h1>Acme Year-End Report 2025</h1>
    <time>7 May 2026</time>
    """
    response = SimpleNamespace(status_code=200, text=html)
    scraper = MfnScraper(base_url="https://mfn.test")
    with (
        patch("alphaforge.providers.mfn.scraper.request_with_retry", return_value=response),
        patch("alphaforge.providers.mfn.scraper.time.sleep"),
    ):
        articles = scraper.scrape_details([{"url": "https://mfn.test/a/acme/annual"}])
    assert articles[0]["published_at"] == "2026-05-07T00:00:00"


def test_scrape_details_normalises_swedish_timestamp():
    html = """
    <h1>Acme Year-End Report 2025</h1>
    <time>7 maj 2026</time>
    """
    response = SimpleNamespace(status_code=200, text=html)
    scraper = MfnScraper(base_url="https://mfn.test")
    with (
        patch("alphaforge.providers.mfn.scraper.request_with_retry", return_value=response),
        patch("alphaforge.providers.mfn.scraper.time.sleep"),
    ):
        articles = scraper.scrape_details([{"url": "https://mfn.test/a/acme/annual"}])
    assert articles[0]["published_at"] == "2026-05-07T00:00:00"


def test_scrape_details_discards_unparseable_timestamp():
    html = """
    <h1>Acme Year-End Report 2025</h1>
    <time>not a publication date</time>
    """
    response = SimpleNamespace(status_code=200, text=html)
    scraper = MfnScraper(base_url="https://mfn.test")
    with (
        patch("alphaforge.providers.mfn.scraper.request_with_retry", return_value=response),
        patch("alphaforge.providers.mfn.scraper.time.sleep"),
    ):
        articles = scraper.scrape_details([{"url": "https://mfn.test/a/acme/annual"}])
    assert articles[0]["published_at"] is None


def test_bilingual_dedupe_uses_pdf_identity_and_keeps_suppressed_provenance():
    docs = [
        {
            "title": "Goobit Group AB offentliggör bokslutskommuniké 2025",
            "source_url": "https://mfn.se/a/goobit/swedish-release",
            "storage_url": "https://storage.mfn.se/sv/annual-report.pdf",
            "mfn_slug": "goobit",
            "report_kind": "annual",
            "fiscal_period": "2025",
            "pdf_checksum": "same-pdf-bytes",
            "lang": "sv",
        },
        {
            "title": "Goobit Group AB has published its year-end report 2025",
            "source_url": "https://mfn.se/a/goobit/english-release",
            "storage_url": "https://storage.mfn.se/en/other-uuid.pdf",
            "mfn_slug": "goobit",
            "report_kind": "annual",
            "fiscal_period": "2025",
            "pdf_checksum": "same-pdf-bytes",
            "lang": "en",
        },
    ]
    selected = bilingual_dedupe(docs)
    assert len(selected) == 1
    assert selected[0]["lang"] == "en"
    assert selected[0]["bilingual_selection_rule"] == "deterministic_en_fallback"
    assert selected[0]["_suppressed_variants"][0]["lang"] == "sv"
    assert (
        selected[0]["_suppressed_variants"][0]["_bilingual_group_id"]
        == selected[0]["_bilingual_group_id"]
    )


def test_identical_bilingual_pdf_checksums_are_both_auditable():
    conn = _connection()
    try:
        conn.execute("INSERT INTO companies (borsdata_id, name) VALUES (501, 'Reports AB')")
        company_id = conn.execute("SELECT id FROM companies WHERE borsdata_id=501").fetchone()[0]
        docs = [
            {
                "title": "Annual Report 2025",
                "source_url": "https://mfn.test/a/reports/en",
                "content_text": "English report",
                "pdf_checksum": "identical-pdf",
                "mfn_slug": "reports",
                "report_kind": "annual",
                "fiscal_period": "2025",
                "lang": "en",
            },
            {
                "title": "Årsredovisning 2025",
                "source_url": "https://mfn.test/a/reports/sv",
                "content_text": "Svensk rapport",
                "pdf_checksum": "identical-pdf",
                "mfn_slug": "reports",
                "report_kind": "annual",
                "fiscal_period": "2025",
                "lang": "sv",
            },
        ]
        result = ResearchDocumentIngestionService(conn).persist_articles(company_id, docs)
        assert result.inserted == 1
        assert result.suppressed == 1
        rows = conn.execute(
            "SELECT source_url, duplicate_of, checksum, raw_metadata FROM research_documents "
            "WHERE company_id=? ORDER BY source_url",
            (company_id,),
        ).fetchall()
        assert len(rows) == 2
        assert sum(row["duplicate_of"] is not None for row in rows) == 1
        assert sum(row["duplicate_of"] is None for row in rows) == 1
        assert rows[0]["checksum"] == rows[1]["checksum"] == "identical-pdf"
        assert "bilingual_group_id" in json.loads(rows[0]["raw_metadata"])
    finally:
        conn.close()


def test_cross_run_checksum_dedupes_bilingual_documents():
    conn = _connection()
    try:
        service = ResearchDocumentIngestionService(conn)
        first = service.persist_articles(
            None,
            [
                {
                    "title": "Annual Report 2025",
                    "source_url": "https://mfn.test/a/reports/en",
                    "content_text": "English report",
                    "pdf_checksum": "same-pdf",
                    "report_kind": "annual",
                    "fiscal_period": "2025",
                    "lang": "en",
                }
            ],
        )
        second = service.persist_articles(
            None,
            [
                {
                    "title": "Årsredovisning 2025",
                    "source_url": "https://mfn.test/a/reports/sv",
                    "content_text": "Svensk rapport",
                    "pdf_checksum": "same-pdf",
                    "report_kind": "annual",
                    "fiscal_period": "2025",
                    "lang": "sv",
                }
            ],
        )
        row = conn.execute(
            "SELECT duplicate_of FROM research_documents WHERE source_url=?",
            ("https://mfn.test/a/reports/sv",),
        ).fetchone()
        assert first.inserted == 1
        assert second.inserted == 0
        assert second.suppressed == 1
        assert row[0] is not None
    finally:
        conn.close()


def test_persist_articles_is_idempotent_without_company_id():
    conn = _connection()
    try:
        article = {
            "title": "Annual Report 2025",
            "source_url": "https://mfn.test/a/reports/annual-2025",
            "content_text": "Annual report evidence",
            "report_kind": "annual",
            "fiscal_period": "2025",
        }
        first = ResearchDocumentIngestionService(conn).persist_articles(None, [article])
        second = ResearchDocumentIngestionService(conn).persist_articles(None, [article])
        count = conn.execute(
            "SELECT count(*) FROM research_documents WHERE company_id IS NULL AND source_url=?",
            (article["source_url"],),
        ).fetchone()[0]
        assert first.inserted == 1
        assert second.inserted == 0
        assert count == 1
    finally:
        conn.close()


def test_pypdf_extraction_keeps_all_page_anchors():
    writer = PdfWriter()
    for _ in range(51):
        writer.add_blank_page(width=72, height=72)
    import io

    stream = io.BytesIO()
    writer.write(stream)
    result = ResearchDocumentIngestionService(None).extract_pdf_pages(stream.getvalue())
    assert result.page_count == 51
    assert result.pages_included == "1-51"
    assert result.page_truncated == 0
    assert result.scanned is True
    assert len(result.pages) == 51
