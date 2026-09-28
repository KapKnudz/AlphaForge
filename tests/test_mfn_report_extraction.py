from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pypdf import PdfWriter

from alphaforge.config import Settings
from alphaforge.db.connection import get_connection
from alphaforge.db.migrations import migrate
from alphaforge.db.repositories import (
    find_complete_evidence_attachment,
    find_complete_evidence_document,
    persist_evidence_document,
    persist_evidence_sibling,
)
from alphaforge.evidence.ingest import (
    PDF_LANGUAGE_MIN_HITS,
    ResearchDocumentIngestionService,
    _document_type_for_identity,
    _language,
    _numeric_key_figure_fingerprint,
    _numeric_similarity,
    bilingual_dedupe,
    resolve_document_language,
)
from alphaforge.evidence.mfn_taxonomy import document_type, is_invitation_or_presentation
from alphaforge.providers.mfn.scraper import (
    MfnScraper,
    _canonical_issuer,
    _cis_release_issuer,
    _is_mfn_release_url,
    _issuer_token,
    _parse_html,
)

FIXTURES = Path(__file__).parent / "fixtures" / "mfn"


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
        <article>
          <header>Wrapper navigation should not be evidence.</header>
          <div class="release-body"><p>Revenue grew.<br>Cash was stable.</p></div>
          <aside>Aside navigation should not be evidence.</aside>
        </article>
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
    assert "navigation" not in articles[0]["content_text"].lower()


def test_scrape_details_prefers_published_json_timestamp():
    html = """
    <h1>Acme Year-End Report 2025</h1>
    <time class="updated">1 May 2026</time>
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


def test_scrape_details_does_not_promote_json_created_timestamp():
    html = """
    <h1>Acme Year-End Report 2025</h1>
    <script type="application/ld+json">
      {"@type": "WebPage", "dateCreated": "2026-05-01T08:00:00Z"}
    </script>
    """
    response = SimpleNamespace(status_code=200, text=html)
    scraper = MfnScraper(base_url="https://mfn.test")
    with (
        patch("alphaforge.providers.mfn.scraper.request_with_retry", return_value=response),
        patch("alphaforge.providers.mfn.scraper.time.sleep"),
    ):
        articles = scraper.scrape_details([{"url": "https://mfn.test/a/acme/annual"}])
    assert articles[0]["published_at"] is None


def test_scrape_details_normalises_human_timestamp():
    html = """
    <h1>Acme Year-End Report 2025</h1>
    <time class="published">7 May 2026</time>
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
    <time class="publication-date">7 maj 2026</time>
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


def test_bilingual_semantic_period_without_strong_correspondence_remains_separate():
    docs = [
        {
            "title": "Clas Ohlson delårsrapport Q1 2026/2027",
            "source_url": "https://mfn.test/a/clas-ohlson/delarsrapport-q1-2026-2027",
            "storage_url": "https://storage.mfn.test/clas/sv-q1.pdf",
            "mfn_slug": "all/a/clas-ohlson",
            "report_kind": "quarterly",
            "lang": "sv",
        },
        {
            "title": "Clas Ohlson Interim Report Q1 2026/2027",
            "source_url": "https://mfn.test/a/clas-ohlson/interim-report-q1-2026-2027",
            "storage_url": "https://storage.mfn.test/clas/en-q1.pdf",
            "mfn_slug": "all/a/clas-ohlson",
            "report_kind": "quarterly",
            "lang": "en",
        },
    ]

    selected = bilingual_dedupe(docs)

    assert len(selected) == 2
    assert {document["lang"] for document in selected} == {"sv", "en"}
    assert all("_suppressed_variants" not in document for document in selected)


def test_duplicate_feed_url_resolves_to_complete_canonical_document():
    conn = _connection()
    try:
        conn.execute("INSERT INTO companies (borsdata_id, name) VALUES (502, 'Canonical AB')")
        company_id = conn.execute("SELECT id FROM companies WHERE borsdata_id=502").fetchone()[0]
        persist_evidence_document(
            conn,
            company_id=company_id,
            article={
                "title": "Canonical Annual Report 2025",
                "source_url": "https://mfn.test/a/canonical/en",
                "published_at": "2026-03-01T00:00:00Z",
                "ingested_lang": "en",
            },
            attachment={
                "source_url": "https://storage.mfn.test/canonical.pdf",
                "content_type": "application/pdf",
                "byte_size": 8,
                "sha256": "canonical-pdf",
                "magic_valid": True,
                "http_status": 200,
            },
            extraction={
                "extractor": "pypdf",
                "text_checksum": "canonical-text",
                "page_count": 1,
                "pages_included": "1",
            },
            pages=[{"page_number": 1, "text": "Evidence"}],
        )
        persist_evidence_sibling(
            conn,
            company_id=company_id,
            canonical_source_url="https://mfn.test/a/canonical/en",
            sibling={
                "source_url": "https://mfn.test/a/canonical/sv",
                "title": "Canonical Årsredovisning 2025",
                "published_at": "2026-03-01T00:00:00Z",
                "lang": "sv",
            },
        )

        document = find_complete_evidence_document(
            conn, company_id, "https://mfn.test/a/canonical/sv"
        )

        assert document is not None
        assert document["source_url"] == "https://mfn.test/a/canonical/en"
    finally:
        conn.close()


def test_revision_marker_groups_pair_and_preserves_revision_relationship():
    docs = [
        {
            "title": "Acme Interim Report Q1 2025",
            "source_url": "https://mfn.test/a/acme/en",
            "content_text": "Revenue 100 MSEK; EBIT 10 MSEK.",
            "mfn_slug": "acme",
            "report_kind": "quarterly",
            "fiscal_period": "Q1 2025",
            "lang": "en",
        },
        {
            "title": "Acme Delårsrapport Q1 2025 correction",
            "source_url": "https://mfn.test/a/acme/sv",
            "content_text": "Revenue 100 MSEK; EBIT 10 MSEK.",
            "mfn_slug": "acme",
            "report_kind": "quarterly",
            "fiscal_period": "Q1 2025",
            "lang": "sv",
        },
    ]

    selected = bilingual_dedupe(docs)

    assert len(selected) == 1
    assert selected[0]["_suppressed_variants"][0]["relationship"] == "REVISION"


def test_revision_joins_existing_translation_group():
    docs = [
        {
            "title": "Acme Interim Report Q1 2025",
            "source_url": "https://mfn.test/a/acme/en",
            "mfn_slug": "acme",
            "report_kind": "quarterly",
            "fiscal_period": "Q1 2025",
            "provider_event_id": "acme-q1-2025",
            "lang": "en",
        },
        {
            "title": "Acme Delårsrapport Q1 2025",
            "source_url": "https://mfn.test/a/acme/sv",
            "mfn_slug": "acme",
            "report_kind": "quarterly",
            "fiscal_period": "Q1 2025",
            "provider_event_id": "acme-q1-2025",
            "lang": "sv",
        },
        {
            "title": "Acme Interim Report Q1 2025 correction",
            "source_url": "https://mfn.test/a/acme/en-correction",
            "mfn_slug": "acme",
            "report_kind": "quarterly",
            "fiscal_period": "Q1 2025",
            "provider_event_id": "acme-q1-2025",
            "lang": "en",
        },
    ]

    selected = bilingual_dedupe(docs)

    assert len(selected) == 1
    assert {variant["relationship"] for variant in selected[0]["_suppressed_variants"]} == {
        "TRANSLATION",
        "REVISION",
    }


def test_unresolved_pdf_languages_never_merge_as_revision_or_translation():
    original = {
        "title": "Acme Interim Report Q1 2025",
        "source_url": "https://mfn.test/a/acme/en",
        "mfn_slug": "acme",
        "report_kind": "quarterly",
        "fiscal_period": "Q1 2025",
        "provider_event_id": "acme-q1-2025",
        "lang": "en",
        "_pdf_language_unresolved": True,
    }
    corrected = {
        **original,
        "title": "Acme Interim Report Q1 2025 correction",
        "source_url": "https://mfn.test/a/acme/en-correction",
    }
    selected = bilingual_dedupe([original, corrected])
    assert len(selected) == 2
    assert all("_suppressed_variants" not in document for document in selected)

    translated = {
        **original,
        "title": "Acme Delårsrapport Q1 2025",
        "source_url": "https://mfn.test/a/acme/sv",
        "lang": "sv",
    }
    selected = bilingual_dedupe([original, translated])
    assert len(selected) == 2


def test_shared_pdf_checksum_overrides_numeric_translation_mismatch():
    docs = [
        {
            "title": "Acme Interim Report Q1 2025",
            "source_url": "https://mfn.test/a/acme/en",
            "content_text": "Revenue 100 MSEK; EBIT 10 MSEK.",
            "mfn_slug": "acme",
            "report_kind": "quarterly",
            "fiscal_period": "Q1 2025",
            "published_at": "2025-05-01",
            "pdf_checksum": "same-pdf",
            "lang": "en",
        },
        {
            "title": "Acme Delårsrapport Q1 2025",
            "source_url": "https://mfn.test/a/acme/sv",
            "content_text": "Revenue 100 MSEK; EBIT 11 MSEK.",
            "mfn_slug": "acme",
            "report_kind": "quarterly",
            "fiscal_period": "Q1 2025",
            "published_at": "2025-05-01",
            "pdf_checksum": "same-pdf",
            "lang": "sv",
        },
    ]

    selected = bilingual_dedupe(docs)

    assert len(selected) == 1
    assert selected[0]["lang"] == "en"
    assert selected[0]["_suppressed_variants"][0]["lang"] == "sv"


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
                "pdf_language": "en",
                "language_evidence": "filename",
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
                "pdf_language": "sv",
                "language_evidence": "filename",
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


def test_persist_articles_keeps_unresolved_language_variants_separate():
    conn = _connection()
    try:
        result = ResearchDocumentIngestionService(conn).persist_articles(
            None,
            [
                {
                    "title": "Annual Report 2025",
                    "source_url": "https://mfn.test/a/reports/en",
                    "content_text": "Omsättning 100 MSEK; EBIT 10 MSEK.",
                    "provider_event_id": "reports-2025",
                    "mfn_slug": "reports",
                    "report_kind": "annual",
                    "fiscal_period": "2025",
                    "lang": "sv",
                },
                {
                    "title": "Årsredovisning 2025",
                    "source_url": "https://mfn.test/a/reports/sv",
                    "content_text": "Revenue 100 MSEK; EBIT 10 MSEK.",
                    "provider_event_id": "reports-2025",
                    "mfn_slug": "reports",
                    "report_kind": "annual",
                    "fiscal_period": "2025",
                    "lang": "en",
                },
            ],
        )
        rows = conn.execute(
            "SELECT duplicate_of, raw_metadata FROM research_documents ORDER BY source_url"
        ).fetchall()
        assert result.inserted == 2
        assert result.suppressed == 0
        assert all(row["duplicate_of"] is None for row in rows)
        assert all(json.loads(row["raw_metadata"])["language"] == "" for row in rows)
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


def test_pypdf_extraction_keeps_late_page_tail_with_resource_cap():
    writer = PdfWriter()
    for _ in range(121):
        writer.add_blank_page(width=72, height=72)
    import io

    stream = io.BytesIO()
    writer.write(stream)
    result = ResearchDocumentIngestionService(None).extract_pdf_pages(
        stream.getvalue(), max_pages=50
    )
    assert result.page_count == 121
    assert result.pages_included == "1-50,81-90"
    assert result.page_truncated == 1
    assert [page["page_number"] for page in result.pages] == list(range(1, 51)) + list(
        range(81, 91)
    )


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


def _clas_ohlson_pair():
    sv_body = "Omsättning 2847 Msek. Rörelseresultat 312 Msek. Resultat per aktie 12 kronor."
    en_body = "Revenue 2847 MSEK. Operating profit 312 MSEK. Earnings per share 12 SEK."
    return [
        {
            "title": "Clas Ohlson delårsrapport Q1 2026/2027",
            "source_url": "https://mfn.test/a/clas-ohlson/delarsrapport-q1-2026-2027",
            "storage_url": "https://storage.mfn.test/clas/delarsrapport-q1-sv.pdf",
            "published_at": "2026-09-09T07:30:00Z",
            "content_text": sv_body,
            "mfn_slug": "all/a/clas-ohlson",
            "report_kind": "quarterly",
            "lang": "sv",
        },
        {
            "title": "Clas Ohlson Interim report Q1 2026/27",
            "source_url": "https://mfn.test/a/clas-ohlson/interim-report-q1-2026-27",
            "storage_url": "https://storage.mfn.test/clas/interim-report-q1-en.pdf",
            "published_at": "2026-09-09T07:30:00Z",
            "content_text": en_body,
            "mfn_slug": "all/a/clas-ohlson",
            "report_kind": "quarterly",
            "lang": "en",
        },
    ]


def test_bilingual_groups_clas_ohlson_detail_fixtures():
    fixtures = FIXTURES
    scraper = MfnScraper(base_url="https://mfn.test")
    articles = []
    for name, url in (
        ("clas_ohlson_q1_sv.html", "https://mfn.test/a/clas-ohlson/delarsrapport-q1-2026-2027"),
        ("clas_ohlson_q1_en.html", "https://mfn.test/a/clas-ohlson/interim-report-q1-2026-27"),
    ):
        html = (fixtures / name).read_text(encoding="utf-8")
        response = SimpleNamespace(status_code=200, text=html)
        with (
            patch(
                "alphaforge.providers.mfn.scraper.request_with_retry",
                return_value=response,
            ),
            patch("alphaforge.providers.mfn.scraper.time.sleep"),
        ):
            (article,) = scraper.scrape_details([{"url": url}])
        article["mfn_slug"] = "all/a/clas-ohlson"
        articles.append(article)
    assert articles[0]["title"] == "Clas Ohlson delårsrapport Q1 2026/2027"
    assert articles[1]["title"] == "Clas Ohlson Interim report Q1 2026/27"
    assert articles[0]["document_type"] == "INTERIM_Q1"
    assert articles[1]["document_type"] == "INTERIM_Q1"
    assert articles[0]["lang"] == "sv"
    assert articles[1]["lang"] == "en"

    selected = bilingual_dedupe(articles)

    assert len(selected) == 1
    assert selected[0]["lang"] == "en"
    assert selected[0]["_suppressed_variants"][0]["lang"] == "sv"


def test_bilingual_groups_clas_ohlson_title_shapes():
    selected = bilingual_dedupe(_clas_ohlson_pair())

    assert len(selected) == 1
    assert selected[0]["lang"] == "en"
    assert selected[0]["bilingual_selection_rule"] == "deterministic_en_fallback"
    suppressed = selected[0]["_suppressed_variants"][0]
    assert suppressed["lang"] == "sv"
    assert suppressed["duplicate_of"] == selected[0]["source_url"]
    assert suppressed["_bilingual_group_id"] == selected[0]["_bilingual_group_id"]
    assert suppressed["relationship"] == "TRANSLATION"


def test_document_type_distinguishes_year_end_from_annual():
    assert document_type("Acme bokslutskommuniké 2025") == "YEAR_END_REPORT"
    assert document_type("Acme Year-End Report 2025") == "YEAR_END_REPORT"
    assert document_type("Acme Årsredovisning 2025") == "ANNUAL_REPORT"
    assert document_type("Acme Annual Report 2025") == "ANNUAL_REPORT"
    assert document_type("Acme delårsrapport Q1 2026/2027") == "INTERIM_Q1"
    assert document_type("Acme Interim report Q1 2026/27") == "INTERIM_Q1"
    assert _document_type_for_identity({"title": "Acme Interim report Q4 2025"}) == ""
    year_end = {
        "title": "Acme bokslutskommuniké 2025",
        "source_url": "https://mfn.test/a/acme/year-end",
        "published_at": "2026-02-01",
        "content_text": "Resultat 100 Msek.",
        "mfn_slug": "acme",
        "lang": "sv",
    }
    annual = {
        "title": "Acme Annual Report 2025",
        "source_url": "https://mfn.test/a/acme/annual",
        "published_at": "2026-03-01",
        "content_text": "Profit 100 MSEK.",
        "mfn_slug": "acme",
        "lang": "en",
    }
    selected = bilingual_dedupe([year_end, annual])
    assert len(selected) == 2


def test_numeric_corroboration_survives_unit_wording():
    left = _numeric_key_figure_fingerprint("Revenue 100 Msek; EBIT 10 Msek.")
    right = _numeric_key_figure_fingerprint("Revenue 100 SEK million; EBIT 10 SEK million.")
    assert left and right
    assert _numeric_similarity(left, right) == 1.0
    assert _numeric_similarity((), ()) == 0.0


def test_swedish_currency_shorthands_map_to_currency():
    for shorthand in ("Mkr", "mkr", "kr", "KR", "kronor", "Kronor", "tkr", "Tkr", "mdr", "Mdr"):
        left = _numeric_key_figure_fingerprint(f"Belopp 100 {shorthand} och 200 {shorthand}.")
        right = _numeric_key_figure_fingerprint("Amount 100 SEK and 200 SEK.")
        assert left == ("100|currency", "200|currency"), (
            f"shorthand {shorthand!r} did not map to currency: {left}"
        )
        assert right == ("100|currency", "200|currency")
        assert _numeric_similarity(left, right) == 1.0


def test_live_clas_ohlson_mfn_body_formatting_groups_as_translation():
    sv_body = (
        "Nettoomsättningen uppgick till 3 278 Mkr (2 814), en ökning med 16 procent. "
        "Rörelseresultatet uppgick till 393 Mkr (278). "
        "Resultat per aktie 4,75 kr (3,27). "
        "Försäljning 734 Mkr (542). "
        "Fritt kassaflöde 363 miljoner kronor (306)."
    )
    en_body = (
        "Net sales amounted to 3,278 MSEK (2,814), an increase of 16%. "
        "Operating profit amounted to 393 MSEK (278). "
        "Earnings per share 4.75 SEK (3.27). "
        "Sales 734 MSEK (542). "
        "Free cash flow 363 MSEK (306)."
    )
    sv_fp = _numeric_key_figure_fingerprint(sv_body)
    en_fp = _numeric_key_figure_fingerprint(en_body)
    assert sv_fp and en_fp
    assert _numeric_similarity(sv_fp, en_fp) >= 0.5
    docs = [
        {
            "title": "Clas Ohlson delårsrapport Q1 2026/2027",
            "source_url": "https://mfn.se/a/clas-ohlson/clas-ohlsons-delarsrapport-q1-2026-2027",
            "published_at": "2026-09-03T07:00:00Z",
            "content_text": sv_body,
            "mfn_slug": "all/a/clas-ohlson",
            "report_kind": "quarterly",
            "lang": "sv",
        },
        {
            "title": "Clas Ohlson Interim report Q1 2026/27",
            "source_url": "https://mfn.se/a/clas-ohlson/clas-ohlson-interim-report-q1-2026-27",
            "published_at": "2026-09-03T07:00:00Z",
            "content_text": en_body,
            "mfn_slug": "all/a/clas-ohlson",
            "report_kind": "quarterly",
            "lang": "en",
        },
    ]
    selected = bilingual_dedupe(docs)
    assert len(selected) == 1
    assert selected[0]["lang"] == "en"
    assert selected[0]["bilingual_selection_rule"] == "deterministic_en_fallback"
    suppressed = selected[0]["_suppressed_variants"][0]
    assert suppressed["lang"] == "sv"
    assert suppressed["relationship"] == "TRANSLATION"


def test_pdf_filename_markers_outrank_release_hint():
    language, evidence = resolve_document_language(
        filename="https://storage.mfn.test/uuid/interim-report-q1-english.pdf",
        pdf_text="",
        release_lang="sv",
    )
    assert (language, evidence) == ("en", "filename")
    language, evidence = resolve_document_language(
        filename="https://storage.mfn.test/uuid/delarsrapport-q1-sv.pdf",
        pdf_text="",
        release_lang="en",
    )
    assert (language, evidence) == ("sv", "filename")


def test_pdf_first_pages_word_scoring_outranks_release_hint():
    sv_text = "Omsättning och resultat för kvartalet. Rapporten omfattar perioden."
    language, evidence = resolve_document_language(
        filename="https://storage.mfn.test/uuid/q1.pdf",
        pdf_text=sv_text,
        release_lang="en",
    )
    assert language == "sv"
    assert evidence.startswith("pdf_text:")
    en_text = "Revenue and profit for the quarter. The report covers the financial year."
    language, evidence = resolve_document_language(
        filename="https://storage.mfn.test/uuid/q1.pdf",
        pdf_text=en_text,
        release_lang="sv",
    )
    assert language == "en"
    assert evidence.startswith("pdf_text:")


def test_pdf_language_falls_back_to_release_hint_outside_authoritative_flow():
    language, evidence = resolve_document_language(
        filename="https://storage.mfn.test/uuid/q1.pdf",
        pdf_text="",
        release_lang="sv",
    )
    assert language == "sv"
    assert evidence == "release_hint:pdf_text_empty"
    language, evidence = resolve_document_language(
        filename="https://storage.mfn.test/uuid/q1.pdf",
        pdf_text="Revenue",
        release_lang="sv",
    )
    assert language == "sv"
    assert evidence == "release_hint:pdf_text_insufficient:sv=0,en=1"
    tie_text = "the and och att"
    language, evidence = resolve_document_language(
        filename="https://storage.mfn.test/uuid/q1.pdf",
        pdf_text=tie_text,
        release_lang="sv",
    )
    assert language == "sv"
    assert evidence == "release_hint:pdf_text_tie:sv=2,en=2"
    assert PDF_LANGUAGE_MIN_HITS == 2
    assert _language({"title": "Acme Interim Report", "pdf_language": "sv"}) == "sv"
    assert _language({"title": "Acme Interim Report", "lang": "en"}) == "en"


def _persist_complete_edition(conn, company_id, source_url, attachment_url, lang, **kwargs):
    return persist_evidence_document(
        conn,
        company_id=company_id,
        article={
            "title": f"Demotion Report {lang}",
            "source_url": source_url,
            "published_at": "2026-09-09T07:30:00Z",
            "ingested_lang": lang,
        },
        attachment={
            "source_url": attachment_url,
            "content_type": "application/pdf",
            "byte_size": 8,
            "sha256": f"{lang}-pdf-bytes",
            "magic_valid": True,
            "http_status": 200,
        },
        extraction={
            "extractor": "pypdf",
            "text_checksum": f"{lang}-text",
            "page_count": 1,
            "pages_included": "1",
        },
        pages=[{"page_number": 1, "text": "Evidence"}],
        **kwargs,
    )


def _evidence_child_counts(conn, document_id):
    attachments = conn.execute(
        "SELECT COUNT(*) FROM research_attachments WHERE document_id=?", (document_id,)
    ).fetchone()[0]
    extractions = conn.execute(
        "SELECT COUNT(*) FROM document_extractions WHERE document_id=?", (document_id,)
    ).fetchone()[0]
    pages = conn.execute(
        "SELECT COUNT(*) FROM document_pages p "
        "JOIN document_extractions e ON e.id=p.extraction_id WHERE e.document_id=?",
        (document_id,),
    ).fetchone()[0]
    return (attachments, extractions, pages)


def test_superseded_edition_retains_metadata_only():
    conn = _connection()
    try:
        conn.execute("INSERT INTO companies (borsdata_id, name) VALUES (503, 'Demotion AB')")
        company_id = conn.execute("SELECT id FROM companies WHERE borsdata_id=503").fetchone()[0]
        sv_url = "https://mfn.test/a/demotion/sv"
        sv_attachment = "https://storage.mfn.test/demotion-sv.pdf"
        en_url = "https://mfn.test/a/demotion/en"
        en_attachment = "https://storage.mfn.test/demotion-en.pdf"
        _persist_complete_edition(conn, company_id, sv_url, sv_attachment, "sv")
        sv_id = conn.execute(
            "SELECT id FROM research_documents WHERE company_id=? AND source_url=?",
            (company_id, sv_url),
        ).fetchone()[0]
        assert _evidence_child_counts(conn, sv_id) == (1, 1, 1)
        _persist_complete_edition(
            conn,
            company_id,
            en_url,
            en_attachment,
            "en",
            suppressed_variants=[
                {
                    "source_url": sv_url,
                    "title": "Demotion Report sv",
                    "published_at": "2026-09-09T07:30:00Z",
                    "pdf_language": "sv",
                    "relationship": "TRANSLATION",
                }
            ],
        )
        sv_row = conn.execute(
            "SELECT id, duplicate_of, title, raw_metadata FROM research_documents "
            "WHERE company_id=? AND source_url=?",
            (company_id, sv_url),
        ).fetchone()
        en_id = conn.execute(
            "SELECT id FROM research_documents WHERE company_id=? AND source_url=?",
            (company_id, en_url),
        ).fetchone()[0]
        assert sv_row["duplicate_of"] == en_id
        assert sv_row["title"] == "Demotion Report sv"
        assert json.loads(sv_row["raw_metadata"])["relationship"] == "TRANSLATION"
        assert _evidence_child_counts(conn, sv_row["id"]) == (0, 0, 0)
        assert _evidence_child_counts(conn, en_id) == (1, 1, 1)
        assert find_complete_evidence_attachment(conn, sv_attachment, company_id) is None
        assert find_complete_evidence_attachment(conn, en_attachment, company_id) is not None
    finally:
        conn.close()


def test_cis_release_url_requires_stable_shape():
    base = "https://mfn.test"
    assert _is_mfn_release_url("https://mfn.test/cis/a/acme/slug-c41def1c", base)
    assert _is_mfn_release_url("https://mfn.test/a/acme/anything", base)
    assert _is_mfn_release_url("https://mfn.test/cision/acme/anything", base)
    assert not _is_mfn_release_url("https://mfn.test/cis/a/acme/slug-without-hash", base)
    assert not _is_mfn_release_url("https://mfn.test/cis/a/acme/", base)
    assert not _is_mfn_release_url("https://mfn.test/cis/a/", base)
    assert not _is_mfn_release_url("https://mfn.test/cis/other/acme/x-c41def1c", base)
    assert not _is_mfn_release_url("https://other.test/cis/a/acme/slug-c41def1c", base)
    assert not _is_mfn_release_url("https://mfn.test/about/annual-report", base)


def test_json_discovery_accepts_corroborated_narrative_inwido_reports_only():
    payload = json.loads(
        (FIXTURES / "inwido_narrative_report_feed.json").read_text(encoding="utf-8")
    )
    scraper = MfnScraper(base_url="https://mfn.se", max_articles=48)

    articles = scraper._parse_json_feed_items(payload)

    assert len(articles) == 8
    narrative = [article for article in articles if article.get("feed_report_identity")]
    assert len(narrative) == 8
    assert [article["document_type"] for article in narrative[:6]] == [
        "YEAR_END_REPORT",
        "YEAR_END_REPORT",
        "INTERIM_Q1",
        "INTERIM_Q1",
        "INTERIM_Q2",
        "INTERIM_Q2",
    ]
    assert {article["report_kind"] for article in narrative[:6]} == {"quarterly"}
    assert narrative[6]["document_type"] == "INTERIM_Q3"
    assert narrative[7]["document_type"] == "ANNUAL_REPORT"
    assert scraper.drain_discovery_skips() == {"non_report_title": 3}
    dispositions = scraper.drain_discovery_dispositions()
    assert len(dispositions) == len(payload["items"])
    missing_q2 = payload["items"][4]
    assert dispositions[missing_q2["url"]] == {
        "source_url": missing_q2["url"],
        "title": missing_q2["content"]["title"],
        "title_admitted": False,
        "report_kind": "quarterly",
        "document_type": "INTERIM_Q2",
        "feed_report_identity": "mfn-report-tag+archive-report-pdf",
        "feed_report_attachment_url": missing_q2["content"]["attachments"][0]["url"],
        "invitation_veto": False,
    }
    commentary = payload["items"][-2]
    assert dispositions[commentary["url"]]["title_admitted"] is False
    assert dispositions[commentary["url"]]["feed_report_identity"] is None


def test_json_discovery_rejects_conflicting_report_subtypes():
    payload = {
        "items": [
            {
                "url": "https://mfn.se/a/acme/conflicting-report",
                "content": {
                    "title": "Acme financial update",
                    "attachments": [
                        {
                            "url": "https://storage.mfn.se/acme/report.pdf",
                            "content_type": "application/pdf",
                            "tags": ["archive:report:pdf"],
                        }
                    ],
                },
                "properties": {
                    "tags": ["sub:report", "sub:report:annual", "sub:report:interim:q4"]
                },
            }
        ]
    }
    scraper = MfnScraper(base_url="https://mfn.se", max_articles=48)

    assert scraper._parse_json_feed_items(payload) == []
    disposition = scraper.drain_discovery_dispositions()[payload["items"][0]["url"]]
    assert disposition["feed_report_identity"] is None


def test_json_discovery_records_all_dispositions_after_article_limit():
    payload = json.loads(
        (FIXTURES / "inwido_narrative_report_feed.json").read_text(encoding="utf-8")
    )
    scraper = MfnScraper(base_url="https://mfn.se", max_articles=2)

    articles = scraper._parse_json_feed_items(payload)
    dispositions = scraper.drain_discovery_dispositions()

    assert len(articles) == 2
    assert scraper.drain_discovery_truncated() is True
    assert set(dispositions) == {item["url"] for item in payload["items"]}
    commentary = payload["items"][-2]
    assert dispositions[commentary["url"]]["title_admitted"] is False
    assert dispositions[commentary["url"]]["feed_report_identity"] is None


def test_corroborated_narrative_identity_authorizes_detail_attachment_selection():
    payload = json.loads(
        (FIXTURES / "inwido_narrative_report_feed.json").read_text(encoding="utf-8")
    )
    scraper = MfnScraper(base_url="https://mfn.se", max_articles=48)
    narrative = scraper._parse_json_feed_items(payload)[:6]

    def transport(method, url, **kwargs):
        seed = next(article for article in narrative if article["url"] == url)
        pdf_url = next(
            item["content"]["attachments"][0]["url"]
            for item in payload["items"]
            if item["url"] == url
        )
        html = f"""
        <html><head>
          <meta property="article:published_time" content="{seed["published_at"]}">
          <link rel="canonical" href="{url}">
        </head><body><h1>{seed["title"]}</h1><div class="release-body">
          <a class="mfn-primary" href="{pdf_url}">Report PDF</a>
          <a class="mfn-primary" href="{pdf_url}">Report PDF</a>
        </div></body></html>
        """
        return SimpleNamespace(status_code=200, text=html)

    with (
        patch("alphaforge.providers.mfn.scraper.request_with_retry", side_effect=transport),
        patch("alphaforge.providers.mfn.scraper.time.sleep"),
    ):
        details = scraper.scrape_details(narrative)

    assert len(details) == 6
    assert all(article["attachment_tier"] == "mfn-primary" for article in details)
    assert [article["document_type"] for article in details] == [
        "YEAR_END_REPORT",
        "YEAR_END_REPORT",
        "INTERIM_Q1",
        "INTERIM_Q1",
        "INTERIM_Q2",
        "INTERIM_Q2",
    ]
    # Re-parsing the same immutable feed yields the same candidate identities,
    # which keeps the unchanged-run feed fingerprint and replay inputs stable.
    replay = scraper._parse_json_feed_items(payload)
    assert replay == scraper._parse_json_feed_items(payload)


def test_authoritative_report_feed_title_admits_narrative_detail_title():
    feed_title = "Acme Interim Report Q2 2026"
    detail_title = "Second-quarter results"
    pdf_url = "https://storage.mfn.se/acme/q2.pdf"
    html = f"""
    <html><head>
      <meta property="article:published_time" content="2026-07-15T05:45:00Z">
    </head><body><h1>{detail_title}</h1>
      <a class="mfn-primary" href="{pdf_url}">Q2 report</a>
    </body></html>
    """
    scraper = MfnScraper(base_url="https://mfn.test")
    seed = {"url": "https://mfn.test/a/acme/q2", "title": feed_title, "lang": "en"}
    with (
        patch(
            "alphaforge.providers.mfn.scraper.request_with_retry",
            return_value=SimpleNamespace(status_code=200, text=html),
        ),
        patch("alphaforge.providers.mfn.scraper.time.sleep"),
    ):
        details = scraper.scrape_details([seed])

    assert len(details) == 1
    assert details[0]["title"] == feed_title
    assert details[0]["detail_title"] == detail_title
    assert details[0]["attachment_url"] == pdf_url
    assert details[0]["attachment_tier"] == "mfn-primary"


def test_authoritative_feed_title_does_not_override_detail_invitation_veto():
    html = """
    <html><body><h1>Invitation to Q2 results presentation</h1>
      <a class="mfn-primary" href="https://storage.mfn.test/acme/q2.pdf">Q2 report</a>
    </body></html>
    """
    scraper = MfnScraper(base_url="https://mfn.test")
    seed = {
        "url": "https://mfn.test/a/acme/q2",
        "title": "Acme Interim Report Q2 2026",
    }
    with (
        patch(
            "alphaforge.providers.mfn.scraper.request_with_retry",
            return_value=SimpleNamespace(status_code=200, text=html),
        ),
        patch("alphaforge.providers.mfn.scraper.time.sleep"),
    ):
        assert scraper.scrape_details([seed]) == []
    assert scraper.drain_detail_skips() == {"invitation_or_presentation_release": 1}
    disposition = scraper.drain_detail_dispositions()[seed["url"]]
    assert disposition["eligibility"] == "rejected"
    assert disposition["published_at"] is None


def test_corroborated_narrative_report_keeps_distinct_attachments_ambiguous():
    html = """
    <html><body><h1>Strong progress despite a difficult market</h1>
      <a class="mfn-primary" href="https://storage.mfn.se/acme/q2.pdf">Q2 report</a>
      <a class="mfn-primary" href="https://storage.mfn.se/acme/q2-appendix.pdf">Appendix</a>
    </body></html>
    """
    parsed = _parse_html(html, corroborated_report=True)
    assert parsed["storage_url"] is None
    assert parsed["attachment_tier"] == "unresolved"


def test_discovery_accepts_cis_shapes_with_counted_drops():
    payload = {
        "items": [
            {
                "url": "https://mfn.test/a/acme/q1",
                "content": {"title": "Acme Interim Report Q1 2026"},
            },
            {
                "url": "https://mfn.test/cis/a/acme/acme-year-end-report-2025-c41def1c",
                "content": {"title": "Acme Year-End Report 2025"},
            },
            {
                "url": "https://mfn.test/cision/acme/annual",
                "content": {"title": "Acme Annual Report 2025"},
            },
            {
                "url": "https://mfn.test/cis/a/acme/acme-monthly-sales-77aa00bb",
                "content": {"title": "Acme Monthly Sales April 2026"},
            },
            {
                "url": "https://press.example.com/acme-year-end-report-2025",
                "content": {"title": "Acme Year-End Report 2025"},
            },
            {
                "url": "https://mfn.test/cis/a/acme/missing-hash-suffix",
                "content": {"title": "Acme Year-End Report 2025"},
            },
        ]
    }
    scraper = MfnScraper(base_url="https://mfn.test")
    articles = scraper._parse_json_feed_items(payload)
    assert [article["url"] for article in articles] == [
        "https://mfn.test/a/acme/q1",
        "https://mfn.test/cis/a/acme/acme-year-end-report-2025-c41def1c",
        "https://mfn.test/cision/acme/annual",
    ]
    assert scraper.drain_discovery_skips() == {
        "non_report_title": 1,
        "non_release_url": 2,
    }
    assert scraper.drain_discovery_skips() == {}


def test_issuer_identity_helpers_anchor_on_resolved_slug():
    assert _issuer_token("all/a/clas-ohlson") == "clas-ohlson"
    assert _issuer_token("goobit") == "goobit"
    assert _cis_release_issuer("https://mfn.se/cis/a/clas-ohlson/slug-c41def1c") == "clas-ohlson"
    assert _cis_release_issuer("https://mfn.se/a/clas-ohlson/slug") is None
    assert _canonical_issuer("https://mfn.se/all/a/clas-ohlson/slug-c41def1c") == "clas-ohlson"
    assert _canonical_issuer("https://mfn.se/cis/a/clas-ohlson/slug-c41def1c") is None
    assert _canonical_issuer(None) is None
    assert _canonical_issuer("not a url") is None


def test_invitation_titles_are_not_reports_despite_report_words():
    assert is_invitation_or_presentation(
        "Invitation to media and analyst briefing for Ericsson Q2 2026 report"
    )
    assert is_invitation_or_presentation("Inbjudan till presentation av bokslutskommuniké")
    assert is_invitation_or_presentation(
        "Media and analyst conference-call for Flow Q2 2026 report"
    )
    assert is_invitation_or_presentation("Teleconference on Flow Q2 2026 report")
    assert is_invitation_or_presentation("Q1 webcast replay")
    assert not is_invitation_or_presentation("Clas Ohlson delårsrapport Q1 2026/27")
    assert not is_invitation_or_presentation("Clas Ohlson Annual Report 2025/26")
    assert not is_invitation_or_presentation("Acme Interim Report Q1 2026")


def test_multi_attachment_report_selects_primary_main_pdf():
    html = (FIXTURES / "cis_report_multi_attachment.html").read_text(encoding="utf-8")
    parsed = _parse_html(html)
    assert parsed["storage_url"] == "https://mb.cision.com/Main/1116/4356813/4130290.pdf"
    assert parsed["attachment_tier"] == "mfn-primary"
    assert (
        parsed["canonical_url"]
        == "https://mfn.se/all/a/clas-ohlson/clas-ohlson-year-end-report-2025-26-c41def1c"
    )


def test_multi_attachment_falls_back_to_main_path_without_primary_marker():
    html = (FIXTURES / "cis_report_multi_attachment.html").read_text(encoding="utf-8")
    stripped = html.replace('class="mfn-primary"', 'class=""')
    parsed = _parse_html(stripped)
    assert parsed["storage_url"] == "https://mb.cision.com/Main/1116/4356813/4130290.pdf"
    assert parsed["attachment_tier"] == "main-path"


def test_single_main_pdf_on_report_page_selects_main_path():
    html = """
    <html>
      <head>
        <link rel="canonical" href="https://mfn.se/all/a/investor/investor-interim-report-january-june-2026-7c51cc12">
        <meta property="article:published_time" content="2026-07-17T06:30:00Z">
      </head>
      <body>
        <h1>Investor Interim Report January-June 2026</h1>
        <article><div class="release-body">Net asset value increased.</div></article>
        <a href="https://mb.cision.com/Main/1084/4375173/4194086.pdf">PDF</a>
      </body>
    </html>
    """
    parsed = _parse_html(html)
    assert parsed["storage_url"] == "https://mb.cision.com/Main/1084/4375173/4194086.pdf"
    assert parsed["attachment_tier"] == "main-path"


def test_opaque_public_only_pdf_is_unresolved_not_accepted():
    html = """
    <html>
      <head><meta property="article:published_time" content="2026-06-03T06:30:00Z"></head>
      <body>
        <h1>Acme Year-End Report 2025</h1>
        <article><div class="release-body">Profit grew.</div></article>
        <a href="https://mb.cision.com/Public/1116/4356813/97221daf694bcc42.pdf">PDF</a>
      </body>
    </html>
    """
    parsed = _parse_html(html)
    assert parsed["storage_url"] is None
    assert parsed["attachment_tier"] == "unresolved"


def test_invitation_poison_page_yields_no_attachment():
    html = (FIXTURES / "cis_invitation_poison.html").read_text(encoding="utf-8")
    parsed = _parse_html(html)
    assert parsed["storage_url"] is None
    response = SimpleNamespace(status_code=200, text=html)
    scraper = MfnScraper(base_url="https://mfn.se")
    with (
        patch("alphaforge.providers.mfn.scraper.request_with_retry", return_value=response),
        patch("alphaforge.providers.mfn.scraper.time.sleep"),
    ):
        articles = scraper.scrape_details(
            [{"url": "https://mfn.se/cis/a/ericsson/invitation-b8df98ed"}]
        )
    assert articles == []
    assert scraper.drain_detail_skips() == {"invitation_or_presentation_release": 1}


def test_hyphenated_invitation_page_yields_no_attachment():
    html = """
    <html>
      <head><meta property="article:published_time" content="2026-07-17T06:30:00Z"></head>
      <body>
        <h1>Media and analyst conference-call for Flow Q2 2026 report</h1>
        <article><div class="release-body">Dial-in details.</div></article>
        <a href="https://mb.cision.com/Main/1116/4356813/4130290.pdf">Q2 interim report</a>
      </body>
    </html>
    """
    parsed = _parse_html(html)
    assert parsed["storage_url"] is None
    assert parsed["attachment_tier"] == "none"


def test_page_without_title_cannot_authorize_attachment():
    html = """
    <html>
      <head><meta property="article:published_time" content="2026-05-07T06:30:00Z"></head>
      <body>
        <article><div class="release-body">Profit grew.</div></article>
        <a href="https://storage.mfn.se/uuid/id2.pdf">Annual report PDF</a>
      </body>
    </html>
    """
    parsed = _parse_html(html)
    assert parsed["storage_url"] is None
    assert parsed["attachment_tier"] == "none"
    response = SimpleNamespace(status_code=200, text=html)
    scraper = MfnScraper(base_url="https://mfn.test")
    with (
        patch("alphaforge.providers.mfn.scraper.request_with_retry", return_value=response),
        patch("alphaforge.providers.mfn.scraper.time.sleep"),
    ):
        articles = scraper.scrape_details(
            [{"url": "https://mfn.test/a/acme/annual", "title": "Acme Year-End Report 2025"}]
        )
    assert articles == []
    assert scraper.drain_detail_skips() == {"non_report_title": 1}


def test_label_score_requires_corroborating_report_title():
    html = """
    <html>
      <head><meta property="article:published_time" content="2026-05-07T06:30:00Z"></head>
      <body>
        <h1>Acme Year-End Report 2025</h1>
        <article><div class="release-body">Profit grew.</div></article>
        <a href="https://storage.mfn.se/uuid/id2.pdf">Annual report PDF</a>
        <a href="https://storage.mfn.se/uuid/cover.jpg">File</a>
      </body>
    </html>
    """
    parsed = _parse_html(html)
    assert parsed["storage_url"] == "https://storage.mfn.se/uuid/id2.pdf"
    assert parsed["attachment_tier"] == "label-score"


def test_neutral_label_alone_is_unresolved():
    html = """
    <html>
      <head><meta property="article:published_time" content="2026-05-07T06:30:00Z"></head>
      <body>
        <h1>Acme Year-End Report 2025</h1>
        <article><div class="release-body">Profit grew.</div></article>
        <a href="https://storage.mfn.se/uuid/12345.pdf">Download file</a>
      </body>
    </html>
    """
    parsed = _parse_html(html)
    assert parsed["storage_url"] is None
    assert parsed["attachment_tier"] == "unresolved"


def test_report_like_url_with_neutral_label_is_unresolved():
    html = """
    <html>
      <head><meta property="article:published_time" content="2026-05-07T06:30:00Z"></head>
      <body>
        <h1>Acme Year-End Report 2025</h1>
        <article><div class="release-body">Profit grew.</div></article>
        <a href="https://storage.mfn.se/uuid/annual-report-2025.pdf">PDF</a>
      </body>
    </html>
    """
    parsed = _parse_html(html)
    assert parsed["storage_url"] is None
    assert parsed["attachment_tier"] == "unresolved"


def test_main_path_on_non_cision_host_is_not_main_tier():
    html = """
    <html>
      <head><meta property="article:published_time" content="2026-05-07T06:30:00Z"></head>
      <body>
        <h1>Acme Year-End Report 2025</h1>
        <article><div class="release-body">Profit grew.</div></article>
        <a href="https://storage.mfn.se/Main/1116/4356813/4130290.pdf">PDF</a>
      </body>
    </html>
    """
    parsed = _parse_html(html)
    assert parsed["storage_url"] is None
    assert parsed["attachment_tier"] == "unresolved"


def test_tied_report_labels_are_unresolved():
    html = """
    <html>
      <head><meta property="article:published_time" content="2026-05-07T06:30:00Z"></head>
      <body>
        <h1>Acme Year-End Report 2025</h1>
        <article><div class="release-body">Profit grew.</div></article>
        <a href="https://storage.mfn.se/uuid/id1.pdf">Annual report PDF</a>
        <a href="https://storage.mfn.se/uuid/id2.pdf">Annual report PDF</a>
      </body>
    </html>
    """
    parsed = _parse_html(html)
    assert parsed["storage_url"] is None
    assert parsed["attachment_tier"] == "unresolved"


def test_sibling_persistence_strips_demoted_edition_children():
    conn = _connection()
    try:
        conn.execute("INSERT INTO companies (borsdata_id, name) VALUES (504, 'Sibling AB')")
        company_id = conn.execute("SELECT id FROM companies WHERE borsdata_id=504").fetchone()[0]
        sv_url = "https://mfn.test/a/sibling/sv"
        sv_attachment = "https://storage.mfn.test/sibling-sv.pdf"
        en_url = "https://mfn.test/a/sibling/en"
        _persist_complete_edition(conn, company_id, sv_url, sv_attachment, "sv")
        _persist_complete_edition(
            conn, company_id, en_url, "https://storage.mfn.test/sibling-en.pdf", "en"
        )
        persist_evidence_sibling(
            conn,
            company_id=company_id,
            canonical_source_url=en_url,
            sibling={
                "source_url": sv_url,
                "title": "Sibling Report sv",
                "published_at": "2026-09-09T07:30:00Z",
                "pdf_language": "sv",
                "relationship": "TRANSLATION",
            },
        )
        sv_row = conn.execute(
            "SELECT id, duplicate_of, title FROM research_documents "
            "WHERE company_id=? AND source_url=?",
            (company_id, sv_url),
        ).fetchone()
        en_id = conn.execute(
            "SELECT id FROM research_documents WHERE company_id=? AND source_url=?",
            (company_id, en_url),
        ).fetchone()[0]
        assert sv_row["duplicate_of"] == en_id
        assert sv_row["title"] == "Sibling Report sv"
        assert _evidence_child_counts(conn, sv_row["id"]) == (0, 0, 0)
        assert _evidence_child_counts(conn, en_id) == (1, 1, 1)
        assert find_complete_evidence_attachment(conn, sv_attachment, company_id) is None
    finally:
        conn.close()


def test_duplicate_identical_primary_pdf_counts_as_one_attachment():
    """Inwido 2025 annual EN shape: the same mfn-primary href printed twice.

    Structure mirrors the captured 2026-09-26 annual-report release pages
    (one PDF URL in two anchors, one with padded class whitespace); only
    the issuer-specific URL path is synthetic.
    """
    pdf_url = (
        "https://storage.mfn.se/11111111-2222-4333-8444-555555555555/acme-en-annual-report-2025.pdf"
    )
    html = f"""
    <html><body>
      <h1>Acme's Annual Report 2025 published</h1>
      <div class="release-body"><p>Annual report text.</p>
        <a class=" mfn-primary " href="{pdf_url}">Acme EN Annual Report 2025</a>
        <a class="mfn-primary" href="{pdf_url}">Acme EN Annual Report 2025</a>
      </div>
    </body></html>
    """
    parsed = _parse_html(html)
    assert parsed["storage_url"] == pdf_url
    assert parsed["attachment_tier"] == "mfn-primary"


def test_duplicate_href_keeps_viable_report_signal_before_deduplication():
    pdf_url = "https://storage.mfn.se/11111111-2222-4333-8444-555555555555/document.pdf"
    html = f"""
    <html><body>
      <h1>Acme's Annual Report 2025 published</h1>
      <div class="release-body">
        <a class="mfn-primary" href="{pdf_url}">Presentation</a>
        <a href="{pdf_url}">Annual report PDF</a>
      </div>
    </body></html>
    """
    parsed = _parse_html(html)
    assert parsed["storage_url"] == pdf_url
    assert parsed["attachment_tier"] == "label-score"


def test_duplicate_identical_primary_pdf_swedish_counts_as_one_attachment():
    """Inwido 2025 annual SV shape: same duplicate-href structure, Swedish title."""
    pdf_url = (
        "https://storage.mfn.se/66666666-7777-4888-8999-000000000000/acme-se-annual-report-2025.pdf"
    )
    html = f"""
    <html><body>
      <h1>Acmes årsredovisning för 2025 publicerad</h1>
      <div class="release-body"><p>Årsredovisningstext.</p>
        <a class=" mfn-primary " href="{pdf_url}">Acme SE Annual Report 2025</a>
        <a class="mfn-primary" href="{pdf_url}">Acme SE Annual Report 2025</a>
      </div>
    </body></html>
    """
    parsed = _parse_html(html)
    assert parsed["storage_url"] == pdf_url
    assert parsed["attachment_tier"] == "mfn-primary"


def test_distinct_primary_pdfs_remain_unresolved():
    """Negative control: two genuinely different mfn-primary targets stay ambiguous."""
    html = """
    <html><body>
      <h1>Acme's Annual Report 2025 published</h1>
      <div class="release-body"><p>Annual report text.</p>
        <a class="mfn-primary" href="https://storage.mfn.se/11111111-2222-4333-8444-555555555555/acme-en-annual-report-2025.pdf">Acme EN Annual Report 2025</a>
        <a class="mfn-primary" href="https://storage.mfn.se/aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee/acme-en-appendix-2025.pdf">Acme EN Appendix 2025</a>
      </div>
    </body></html>
    """
    parsed = _parse_html(html)
    assert parsed["storage_url"] is None
    assert parsed["attachment_tier"] == "unresolved"
