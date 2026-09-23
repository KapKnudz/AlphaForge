from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pypdf import PdfWriter

from alphaforge.config import Settings
from alphaforge.core.frozen_packet import (
    EVIDENCE_RULES_VERSION,
    is_stale_evidence_packet,
    packet_rules_version,
    stable_packet_hash,
    validate_frozen_packet,
)
from alphaforge.core.gate.readiness import AgentReadinessGate
from alphaforge.db.connection import get_connection
from alphaforge.db.migrations import migrate
from alphaforge.db.repositories import (
    describe_evidence_state,
    load_evidence_packet,
    persist_evidence_packet,
    upsert_company,
)
from alphaforge.evidence.flow import NoEvidenceReason, OneCompanyEvidenceFlow
from alphaforge.evidence.ingest import bilingual_dedupe
from alphaforge.providers.mfn.scraper import MfnScraper

FIXTURES = Path(__file__).parent / "fixtures" / "mfn"
BASE = "https://mfn.se"


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


def _mapped_company(conn, *, ins_id=8001, slug="all/a/flow"):
    company_id = upsert_company(conn, {"insId": ins_id, "name": "Flow AB", "ticker": "FLOW"})
    conn.execute(
        """
        INSERT INTO mfn_issuer_mappings
            (company_id, mfn_slug, source_url, status, discovery_source, verified_at, identity_evidence)
        VALUES (?, ?, ?, 'mapped', 'fixture',
                '2026-09-20T00:00:00Z',
                '{"provenance":"fixture","reason":"explicit fixture mapping"}')
        """,
        (company_id, slug, f"{BASE}/{slug}"),
    )
    conn.commit()
    return company_id


class _FakeCisionScraper:
    base_url = "https://mfn.test"

    def __init__(self, feed, details):
        self.feed = feed
        self.details = details

    def discover_feed(self, mfn_slug, *, reports_only=True):
        return list(self.feed)

    def scrape_details(self, entries, *, reports_only=True):
        urls = {
            entry if isinstance(entry, str) else entry.get("url") or entry.get("source_url")
            for entry in entries
        }
        return [
            article
            for article in self.details
            if (article.get("url") or article.get("source_url")) in urls
        ]


def _report_article(slug_path, title, *, canonical_issuer="flow", tier=None, attachment=True):
    url = f"https://mfn.test/cis/a/flow/{slug_path}" if slug_path else None
    article = {
        "url": url or "https://mfn.test/a/flow/report",
        "source_url": url or "https://mfn.test/a/flow/report",
        "title": title,
        "published_at": "2026-05-01T08:00:00Z",
        "lang": "en",
    }
    if canonical_issuer is not None and url is not None:
        article["canonical_url"] = f"https://mfn.test/all/a/{canonical_issuer}/page"
    if tier is not None:
        article["attachment_tier"] = tier
    if attachment:
        article["attachment_url"] = "https://storage.mfn.test/flow/report.pdf"
    return article


def _pdf_response(content):
    return SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "application/pdf"},
        content=content,
    )


def test_cis_bilingual_page_pair_flows_through_dedupe_without_group_id():
    """The bilingual unit is the sv/en page pair, never feed group_id pairing."""
    seeds = [
        {"url": "https://mfn.se/cis/a/clas-ohlson/x-a047c4e1", "lang": "sv"},
        {"url": "https://mfn.se/cis/a/clas-ohlson/x-d5fb2095", "lang": "en"},
    ]
    pages = {
        seeds[0]["url"]: (FIXTURES / "cis_q3_report_sv.html").read_text(encoding="utf-8"),
        seeds[1]["url"]: (FIXTURES / "cis_q3_report_en.html").read_text(encoding="utf-8"),
    }

    def transport(method, url, **kwargs):
        return SimpleNamespace(status_code=200, text=pages[url])

    scraper = MfnScraper(base_url=BASE)
    with (
        patch("alphaforge.providers.mfn.scraper.request_with_retry", side_effect=transport),
        patch("alphaforge.providers.mfn.scraper.time.sleep"),
    ):
        articles = scraper.scrape_details(seeds)
    assert len(articles) == 2
    assert [article["storage_url"] for article in articles] == [
        "https://mb.cision.com/Main/1116/4356813/4130101.pdf",
        "https://mb.cision.com/Main/1116/4356813/4130102.pdf",
    ]
    assert {article["attachment_tier"] for article in articles} == {"mfn-primary"}
    assert {article["lang"] for article in articles} == {"sv", "en"}
    assert all("group_id" not in article for article in articles)

    # The older trailing-window pair groups exactly like the proven
    # newest-report path: one group, en selected, sv as TRANSLATION.
    selected = bilingual_dedupe(articles)
    assert len(selected) == 1
    assert selected[0]["lang"] == "en"
    assert selected[0]["bilingual_selection_rule"] == "deterministic_en_fallback"
    suppressed = selected[0]["_suppressed_variants"][0]
    assert suppressed["lang"] == "sv"
    assert suppressed["relationship"] == "TRANSLATION"


def test_ambiguous_attachment_fails_lane_visibly():
    conn = _connection()
    company_id = _mapped_company(conn)
    articles = [
        _report_article(
            None,
            "Flow AB Interim Report Q1 2026",
            canonical_issuer=None,
            tier="unresolved",
        )
    ]
    feed = [{"url": articles[0]["url"], "title": articles[0]["title"]}]
    with patch(
        "alphaforge.evidence.flow.request_with_retry",
        return_value=_pdf_response(_pdf()),
    ):
        result = OneCompanyEvidenceFlow(conn, scraper=_FakeCisionScraper(feed, articles)).run(
            company_id, as_of="2026-09-20"
        )
    assert result.status == "evidence_incomplete"
    assert result.packet is None
    assert result.skipped.get("ambiguous_selection") == 1
    assert result.diagnostic()["ambiguous_selection"] == 1
    assert "blocked identity/selection check" in (result.message or "")
    rows = conn.execute("SELECT COUNT(*) FROM research_documents").fetchone()[0]
    assert rows == 0


def test_canonical_mismatch_fails_lane():
    conn = _connection()
    company_id = _mapped_company(conn)
    # The release URL carries the resolved issuer, but MFN's own canonical
    # link binds the page to a different issuer: fail closed, never persist.
    articles = [
        _report_article(
            "flow-year-end-report-2025-c41def1c",
            "Flow AB Year-End Report 2025",
            canonical_issuer="other-issuer",
            tier="mfn-primary",
        )
    ]
    feed = [{"url": articles[0]["url"], "title": articles[0]["title"]}]
    with patch(
        "alphaforge.evidence.flow.request_with_retry",
        return_value=_pdf_response(_pdf()),
    ):
        result = OneCompanyEvidenceFlow(conn, scraper=_FakeCisionScraper(feed, articles)).run(
            company_id, as_of="2026-09-20"
        )
    assert result.status == "evidence_incomplete"
    assert result.skipped.get("issuer_mismatch") == 1
    rows = conn.execute("SELECT COUNT(*) FROM research_documents").fetchone()[0]
    assert rows == 0


def test_missing_canonical_confirmation_fails_lane():
    conn = _connection()
    company_id = _mapped_company(conn)
    articles = [
        _report_article(
            "flow-year-end-report-2025-c41def1c",
            "Flow AB Year-End Report 2025",
            canonical_issuer=None,
            tier="mfn-primary",
        )
    ]
    feed = [{"url": articles[0]["url"], "title": articles[0]["title"]}]
    with patch(
        "alphaforge.evidence.flow.request_with_retry",
        return_value=_pdf_response(_pdf()),
    ):
        result = OneCompanyEvidenceFlow(conn, scraper=_FakeCisionScraper(feed, articles)).run(
            company_id, as_of="2026-09-20"
        )
    assert result.status == "evidence_incomplete"
    assert result.skipped.get("canonical_issuer_unconfirmed") == 1


def test_foreign_issuer_release_filtered_before_download():
    conn = _connection()
    company_id = _mapped_company(conn)
    good = {
        "url": "https://mfn.test/a/flow/interim-report-q1-2026",
        "source_url": "https://mfn.test/a/flow/interim-report-q1-2026",
        "title": "Flow AB Interim Report Q1 2026",
        "published_at": "2026-05-01T08:00:00Z",
        "attachment_url": "https://storage.mfn.test/flow/q1.pdf",
        "lang": "en",
    }
    feed = [
        {"url": good["url"], "title": good["title"]},
        {
            "url": "https://mfn.test/cis/a/other-issuer/report-c41def1c",
            "title": "Other Interim Report Q1 2026",
        },
    ]
    with patch(
        "alphaforge.evidence.flow.request_with_retry",
        return_value=_pdf_response(_pdf()),
    ):
        result = OneCompanyEvidenceFlow(conn, scraper=_FakeCisionScraper(feed, [good])).run(
            company_id, as_of="2026-09-20"
        )
    assert result.status == "complete"
    assert result.skipped.get("issuer_mismatch") == 1
    assert len(result.packet["sources"]) == 1


def test_stray_pdf_does_not_mark_lane_ready():
    conn = _connection()
    company_id = _mapped_company(conn)
    articles = [
        {
            "url": "https://mfn.test/a/flow/interim-report-q1-2026",
            "source_url": "https://mfn.test/a/flow/interim-report-q1-2026",
            "title": "Flow AB Interim Report Q1 2026",
            "published_at": "2026-05-01T08:00:00Z",
            "attachment_url": "https://storage.mfn.test/flow/q1.pdf",
            "lang": "en",
        },
        {
            "url": "https://mfn.test/a/flow/interim-report-q2-2026",
            "source_url": "https://mfn.test/a/flow/interim-report-q2-2026",
            "title": "Flow AB Interim Report Q2 2026",
            "published_at": "2026-08-01T08:00:00Z",
            "attachment_url": "https://storage.mfn.test/flow/q2.pdf",
            "lang": "en",
        },
    ]

    def transport(method, url, **kwargs):
        if url.endswith("q2.pdf"):
            raise RuntimeError("connection reset")
        return _pdf_response(_pdf())

    feed = [{"url": article["url"], "title": article["title"]} for article in articles]
    with patch("alphaforge.evidence.flow.request_with_retry", side_effect=transport):
        result = OneCompanyEvidenceFlow(conn, scraper=_FakeCisionScraper(feed, articles)).run(
            company_id, as_of="2026-09-20"
        )
    assert result.status == "evidence_incomplete"
    assert result.packet is None
    assert result.downloaded == 1
    assert result.diagnostic()["download_failed"] == 1
    assert result.completeness["quarterly"] == {"expected": 2, "retained": 1}


def test_invitation_only_feed_stays_no_evidence():
    conn = _connection()
    company_id = _mapped_company(conn)
    articles = [
        {
            "url": "https://mfn.test/a/flow/briefing-q2",
            "source_url": "https://mfn.test/a/flow/briefing-q2",
            "title": "Invitation to media briefing for Flow Q2 2026 report",
            "published_at": "2026-07-10T08:00:00Z",
            "attachment_url": "https://storage.mfn.test/flow/briefing.pdf",
            "lang": "en",
        }
    ]
    feed = [{"url": articles[0]["url"], "title": articles[0]["title"]}]
    with patch(
        "alphaforge.evidence.flow.request_with_retry",
        return_value=_pdf_response(_pdf()),
    ):
        result = OneCompanyEvidenceFlow(conn, scraper=_FakeCisionScraper(feed, articles)).run(
            company_id, as_of="2026-09-20"
        )
    assert result.status == "no_evidence"
    assert result.no_evidence_reason == NoEvidenceReason.NO_PUBLISHED_RELEASE
    assert result.skipped.get("invitation_or_presentation_release") == 1


def test_empty_feed_stays_no_published_release():
    conn = _connection()
    company_id = _mapped_company(conn)
    result = OneCompanyEvidenceFlow(conn, scraper=_FakeCisionScraper([], [])).run(
        company_id, as_of="2026-09-20"
    )
    assert result.status == "no_evidence"
    assert result.no_evidence_reason == NoEvidenceReason.NO_PUBLISHED_RELEASE


def _detail_html(*, title, canonical, published_at, body, attachments):
    links = "\n".join(
        f'      <a class="{css_class}" href="{href}">{label}</a>'
        for href, label, css_class in attachments
    )
    return f"""<html>
  <head>
    <link rel="canonical" href="{canonical}">
    <meta property="article:published_time" content="{published_at}">
  </head>
  <body>
    <h1>{title}</h1>
    <article><div class="release-body">{body}</div></article>
    <div class="attachment-tray">
{links}
    </div>
  </body>
</html>
"""


def test_clas_ohlson_history_recovers_full_window():
    """End-to-end: 8 in-window reports (legacy /a/ + /cis/a/) all retained.

    Regression for the post-merge 2/8 symptom: with the old allowlist the
    six /cis/a/ items died at discovery and readiness still looked green
    from the two legacy PDFs. The feed fixture also carries two noise items
    (monthly sales + off-host URL) that must stay filtered with counts.
    """
    conn = _connection()
    company_id = _mapped_company(conn, ins_id=8002, slug="all/a/clas-ohlson")
    feed = json.loads((FIXTURES / "clas_ohlson_history_feed.json").read_text(encoding="utf-8"))

    bodies = {
        "q1": "Revenue 2847 MSEK. The quarter covered 1 May - 31 July 2026.",
        "annual": "Net sales 12000 MSEK. The financial year covered 1 May 2025 - 30 April 2026.",
        "year-end": "Net sales 11900 MSEK. The financial year covered 1 May 2025 - 30 April 2026.",
        "q3": "Revenue 3100 MSEK. The quarter covered 1 November 2025 - 31 January 2026.",
    }
    detail_pages = {}
    pdf_by_url = {}
    for index, item in enumerate(feed["items"]):
        url = item["url"]
        if not url.startswith(BASE):
            continue
        title = item["content"]["title"]
        published = item["content"]["publish_date"]
        slug = url.rsplit("/", 1)[-1]
        canonical = f"{BASE}/all/a/clas-ohlson/{slug}"
        lowered = title.lower()
        pair = (
            "q1"
            if "q1" in lowered
            else "annual"
            if "årsredovisning" in lowered or "annual report" in lowered
            else "year-end"
            if "year-end" in lowered or "bokslutskommuniké" in lowered
            else "q3"
            if "q3" in lowered
            else None
        )
        if pair is None:
            continue  # noise item: no detail page served
        if url.startswith(f"{BASE}/a/"):
            attachments = [
                (f"https://storage.mfn.se/clas/{slug}.pdf", "Interim Report PDF", ""),
            ]
        else:
            release_id = 4356813 + index
            attachments = [
                (
                    f"https://mb.cision.com/Main/1116/{release_id}/4130290.pdf",
                    "PDF",
                    "mfn-primary",
                ),
                (
                    f"https://mb.cision.com/Public/1116/{release_id}/97221daf694bcc42.pdf",
                    "PDF",
                    "",
                ),
            ]
        detail_pages[url] = _detail_html(
            title=title,
            canonical=canonical,
            published_at=published,
            body=bodies[pair],
            attachments=attachments,
        )
        pdf_by_url[attachments[0][0]] = _pdf(pages=index + 1)

    assert len(detail_pages) == 8

    def scraper_transport(method, url, **kwargs):
        if "offset=" in url:
            body = json.dumps(feed)
            return SimpleNamespace(
                status_code=200,
                headers={"Content-Type": "application/json"},
                text=body,
                content=body.encode(),
            )
        html = detail_pages[url]
        return SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "text/html"},
            text=html,
            content=html.encode(),
        )

    def pdf_transport(method, url, **kwargs):
        return SimpleNamespace(
            status_code=200,
            headers={"Content-Type": "application/pdf"},
            content=pdf_by_url[url],
        )

    with (
        patch(
            "alphaforge.providers.mfn.scraper.request_with_retry",
            side_effect=scraper_transport,
        ),
        patch("alphaforge.providers.mfn.scraper.time.sleep"),
        patch("alphaforge.evidence.flow.request_with_retry", side_effect=pdf_transport),
    ):
        result = OneCompanyEvidenceFlow(conn, scraper=MfnScraper(base_url=BASE)).run(
            company_id, as_of="2026-09-20"
        )

    assert result.status == "complete"
    assert result.discovered == 8
    diagnostic = result.diagnostic()
    assert diagnostic["filtered_before_download"] >= 2
    assert diagnostic["ambiguous_selection"] == 0
    assert diagnostic["download_failed"] == 0
    assert diagnostic["retained"] == 8
    # The sv bokslutskommuniké maps to quarterly under the frozen taxonomy
    # (report_kind), while its en year-end twin maps to annual.
    assert diagnostic["completeness"] == {
        "annual": {"expected": 3, "retained": 3},
        "quarterly": {"expected": 5, "retained": 5},
    }
    assert diagnostic["attachment_selection"] == {
        "label-score": 2,
        "mfn-primary": 6,
    }
    # Every in-window report title is retained verbatim as a packet source:
    # full history recovered, nothing silently dropped or over-collected.
    expected_titles = sorted(
        item["content"]["title"].lower()
        for item in feed["items"]
        if item["url"].startswith(BASE) and "monthly-sales" not in item["url"]
    )
    assert len(expected_titles) == 8
    assert sorted(source["title"].lower() for source in result.packet["sources"]) == (
        expected_titles
    )
    assert "attachment_selection_label-score:2" in result.packet["limitations"]


def _lane_candidate(packet):
    return SimpleNamespace(
        ranking_model="general",
        research_evidence={"evidence_lane": True, "evidence_packet": packet},
        full_results={
            "reverse_dcf": {"status": "available"},
            "valuation": {"ev_ebit_guardrail_low": 5.0, "ev_ebit_guardrail_high": 20.0},
        },
        company_id=1,
        ticker="FLOW",
    )


def _versioned_packet_variant(packet, version):
    """Represent a packet frozen under older rules with a valid self-hash."""
    variant = {key: value for key, value in packet.items() if key != "packet_hash"}
    if version is None:
        variant.pop("evidence_rules_version", None)
    else:
        variant["evidence_rules_version"] = version
    variant["packet_hash"] = stable_packet_hash(variant)
    return variant


def _quarterly_article(slug, title, published_at, attachment=None):
    article = {
        "url": f"https://mfn.test/a/flow/{slug}",
        "source_url": f"https://mfn.test/a/flow/{slug}",
        "title": title,
        "published_at": published_at,
        "lang": "en",
    }
    if attachment is not None:
        article["attachment_url"] = attachment
    return article


def test_old_rule_packet_is_hash_valid_but_not_trusted_by_readiness():
    """Stale is defined narrowly as an older evidence/filter/completeness rule version.

    A packet frozen under older rules keeps validating against the frozen
    model (self-hash plus schema), but readiness must block it with
    ``stale_evidence_packet`` instead of reporting ready.
    """
    conn = _connection()
    company_id = _mapped_company(conn)
    article = _quarterly_article(
        "interim-report-q1-2026",
        "Flow AB Interim Report Q1 2026",
        "2026-05-01T08:00:00Z",
        "https://storage.mfn.test/flow/q1.pdf",
    )
    feed = [{"url": article["url"], "title": article["title"]}]
    with patch(
        "alphaforge.evidence.flow.request_with_retry",
        return_value=_pdf_response(_pdf()),
    ):
        result = OneCompanyEvidenceFlow(conn, scraper=_FakeCisionScraper(feed, [article])).run(
            company_id, as_of="2026-09-20"
        )
    assert result.status == "complete"
    packet = result.packet
    assert packet["evidence_rules_version"] == EVIDENCE_RULES_VERSION
    assert packet_rules_version(packet) == EVIDENCE_RULES_VERSION
    assert not is_stale_evidence_packet(packet)
    assert AgentReadinessGate().assess(_lane_candidate(packet)).status == "ready"

    old_packet = _versioned_packet_variant(packet, EVIDENCE_RULES_VERSION - 1)
    assert packet_rules_version(old_packet) == EVIDENCE_RULES_VERSION - 1
    assert validate_frozen_packet(old_packet)
    assert is_stale_evidence_packet(old_packet)

    # A pre-versioning packet without the marker is stale too (fail closed).
    unmarked = _versioned_packet_variant(packet, None)
    assert packet_rules_version(unmarked) is None
    assert validate_frozen_packet(unmarked)
    assert is_stale_evidence_packet(unmarked)

    assessment = AgentReadinessGate().assess(_lane_candidate(old_packet))
    assert assessment.status == "evidence_blocked"
    assert [blocker.code for blocker in assessment.blockers] == ["stale_evidence_packet"]


def test_complete_packet_loads_under_current_rules_fingerprint():
    """A complete run stamps the single authoritative window, so the
    production loader returns it as current instead of treating it stale."""
    conn = _connection()
    company_id = _mapped_company(conn)
    article = _quarterly_article(
        "interim-report-q1-2026",
        "Flow AB Interim Report Q1 2026",
        "2026-05-01T08:00:00Z",
        "https://storage.mfn.test/flow/q1.pdf",
    )
    feed = [{"url": article["url"], "title": article["title"]}]
    with patch(
        "alphaforge.evidence.flow.request_with_retry",
        return_value=_pdf_response(_pdf()),
    ):
        result = OneCompanyEvidenceFlow(conn, scraper=_FakeCisionScraper(feed, [article])).run(
            company_id, as_of="2026-09-20"
        )
    assert result.status == "complete"
    loaded = load_evidence_packet(conn, company_id, "2026-09-20")
    assert loaded is not None
    assert loaded["packet_hash"] == result.packet["packet_hash"]
    assert not is_stale_evidence_packet(loaded)


def test_incomplete_rerun_with_old_rule_packet_fails_visibly():
    """Completeness gate, diagnostic buckets, and stale invalidation together.

    An older-rule complete packet sits in the packet table while the rerun
    sees one in-window report with no usable PDF. The lane must fail with
    ``evidence_incomplete`` (never fall back to the old packet), the
    non-complete exit must invalidate the stale row, and readiness must keep
    blocking that old packet as stale.
    """
    conn = _connection()
    company_id = _mapped_company(conn)
    gate = AgentReadinessGate()
    q1 = _quarterly_article(
        "interim-report-q1-2026",
        "Flow AB Interim Report Q1 2026",
        "2026-05-01T08:00:00Z",
        "https://storage.mfn.test/flow/q1.pdf",
    )
    q2 = _quarterly_article(
        "interim-report-q2-2026",
        "Flow AB Interim Report Q2 2026",
        "2026-08-01T08:00:00Z",
        "https://storage.mfn.test/flow/q2.pdf",
    )
    with patch(
        "alphaforge.evidence.flow.request_with_retry",
        return_value=_pdf_response(_pdf()),
    ):
        first = OneCompanyEvidenceFlow(
            conn, scraper=_FakeCisionScraper([{"url": q1["url"], "title": q1["title"]}], [q1])
        ).run(company_id, as_of="2026-09-20")
    assert first.status == "complete"

    # Seed the older-rule complete packet the stale check must invalidate.
    old_packet = _versioned_packet_variant(first.packet, EVIDENCE_RULES_VERSION - 1)
    persist_evidence_packet(conn, company_id=company_id, as_of="2026-09-20", packet=old_packet)

    def transport(method, url, **kwargs):
        if url.endswith("q2.pdf"):
            raise RuntimeError("connection reset")
        return _pdf_response(_pdf())

    rerun_feed = [{"url": article["url"], "title": article["title"]} for article in (q1, q2)]
    with patch("alphaforge.evidence.flow.request_with_retry", side_effect=transport):
        result = OneCompanyEvidenceFlow(conn, scraper=_FakeCisionScraper(rerun_feed, [q1, q2])).run(
            company_id, as_of="2026-09-20"
        )
    assert result.status == "evidence_incomplete"
    assert result.packet is None
    assert "quarterly expected" in (result.message or "")
    diagnostic = result.diagnostic()
    assert diagnostic["download_failed"] == 1
    quarterly = diagnostic["completeness"]["quarterly"]
    assert quarterly["retained"] < quarterly["expected"]

    # Any non-complete rerun tombstones every prior packet for this
    # point-in-time key.  No stale or current packet may keep readiness green.
    loaded = load_evidence_packet(conn, company_id, "2026-09-20")
    assert loaded is None
    rows = conn.execute(
        "SELECT usable, usable_reason FROM evidence_packets WHERE company_id=? AND as_of=?",
        (company_id, "2026-09-20"),
    ).fetchall()
    assert len(rows) == 2
    assert all(row[0] == 0 for row in rows)
    assert all(row[1] == "incomplete_run:evidence_incomplete" for row in rows)
    # The older-rule packet itself stays hash-valid but untrusted.
    assert validate_frozen_packet(old_packet)
    assert is_stale_evidence_packet(old_packet)
    assessment = gate.assess(_lane_candidate(old_packet))
    assert assessment.status == "evidence_blocked"
    assert "stale_evidence_packet" in [blocker.code for blocker in assessment.blockers]


def test_same_rule_later_incomplete_run_tombstones_packet_and_replays_diagnostic():
    """A later same-rule failure cannot reuse the earlier complete packet."""
    conn = _connection()
    company_id = _mapped_company(conn)
    q1 = _quarterly_article(
        "interim-report-q1-2026",
        "Flow AB Interim Report Q1 2026",
        "2026-05-01T08:00:00Z",
        "https://storage.mfn.test/flow/q1.pdf",
    )
    q2 = _quarterly_article(
        "interim-report-q2-2026",
        "Flow AB Interim Report Q2 2026",
        "2026-08-01T08:00:00Z",
        "https://storage.mfn.test/flow/q2.pdf",
    )
    with patch(
        "alphaforge.evidence.flow.request_with_retry",
        return_value=_pdf_response(_pdf()),
    ):
        first = OneCompanyEvidenceFlow(
            conn, scraper=_FakeCisionScraper([{"url": q1["url"], "title": q1["title"]}], [q1])
        ).run(company_id, as_of="2026-09-20")
    assert first.status == "complete"

    def transport(method, url, **kwargs):
        if url.endswith("q2.pdf"):
            raise RuntimeError("connection reset")
        return _pdf_response(_pdf())

    feed = [{"url": article["url"], "title": article["title"]} for article in (q1, q2)]
    with patch("alphaforge.evidence.flow.request_with_retry", side_effect=transport):
        second = OneCompanyEvidenceFlow(conn, scraper=_FakeCisionScraper(feed, [q1, q2])).run(
            company_id, as_of="2026-09-20"
        )
    assert second.status == "evidence_incomplete"
    assert load_evidence_packet(conn, company_id, "2026-09-20") is None
    row = conn.execute(
        "SELECT usable, usable_reason FROM evidence_packets WHERE company_id=? AND as_of=?",
        (company_id, "2026-09-20"),
    ).fetchone()
    assert tuple(row) == (0, "incomplete_run:evidence_incomplete")
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM research_documents WHERE company_id=? AND source_url=?",
            (company_id, q1["source_url"]),
        ).fetchone()[0]
        == 1
    )
    persisted = describe_evidence_state(conn, company_id=company_id, as_of="2026-09-20")
    assert persisted == second.diagnostic()
    assert AgentReadinessGate().assess(_lane_candidate(None)).status == "evidence_blocked"


def test_no_attachment_group_stays_in_expected_denominator():
    """MFN-002: an in-window report with no usable PDF is a deficit, not a filter."""
    conn = _connection()
    company_id = _mapped_company(conn)
    q1 = _quarterly_article(
        "interim-report-q1-2026",
        "Flow AB Interim Report Q1 2026",
        "2026-05-01T08:00:00Z",
        "https://storage.mfn.test/flow/q1.pdf",
    )
    q2 = _quarterly_article(
        "interim-report-q2-2026",
        "Flow AB Interim Report Q2 2026",
        "2026-08-01T08:00:00Z",
    )
    feed = [{"url": article["url"], "title": article["title"]} for article in (q1, q2)]
    with patch(
        "alphaforge.evidence.flow.request_with_retry",
        return_value=_pdf_response(_pdf()),
    ):
        result = OneCompanyEvidenceFlow(conn, scraper=_FakeCisionScraper(feed, [q1, q2])).run(
            company_id, as_of="2026-09-20"
        )
    assert result.status == "evidence_incomplete"
    assert result.packet is None
    assert result.skipped.get("missing_pdf_attachment") == 1
    assert result.completeness == {"quarterly": {"expected": 2, "retained": 1}}
    assert "quarterly expected 2 retained 1" in (result.message or "")


def test_ambiguous_group_stays_in_expected_denominator():
    """MFN-002: a refused attachment guess blocks visibly and stays expected."""
    conn = _connection()
    company_id = _mapped_company(conn)
    articles = [
        _report_article(
            "flow-interim-report-q1-2026-c41def1c",
            "Flow AB Interim Report Q1 2026",
            canonical_issuer="flow",
            tier="unresolved",
        )
    ]
    feed = [{"url": articles[0]["url"], "title": articles[0]["title"]}]
    with patch(
        "alphaforge.evidence.flow.request_with_retry",
        return_value=_pdf_response(_pdf()),
    ):
        result = OneCompanyEvidenceFlow(conn, scraper=_FakeCisionScraper(feed, articles)).run(
            company_id, as_of="2026-09-20"
        )
    assert result.status == "evidence_incomplete"
    assert result.packet is None
    assert result.skipped.get("ambiguous_selection") == 1
    assert result.completeness == {"quarterly": {"expected": 1, "retained": 0}}
    assert "quarterly expected 1 retained 0" in (result.message or "")
    assert "blocked identity/selection check" in (result.message or "")
    rows = conn.execute("SELECT COUNT(*) FROM research_documents").fetchone()[0]
    assert rows == 0


def test_rerun_retained_diagnostics_reflect_persisted_sources():
    """MFN-004: rerun diagnostics count persisted sources and keep their tiers."""
    conn = _connection()
    company_id = _mapped_company(conn)
    article = _quarterly_article(
        "interim-report-q1-2026",
        "Flow AB Interim Report Q1 2026",
        "2026-05-01T08:00:00Z",
        "https://storage.mfn.test/flow/q1-en.pdf",
    )
    article["attachment_tier"] = "mfn-primary"
    feed = [{"url": article["url"], "title": article["title"]}]
    with patch(
        "alphaforge.evidence.flow.request_with_retry",
        return_value=_pdf_response(_pdf()),
    ):
        first = OneCompanyEvidenceFlow(conn, scraper=_FakeCisionScraper(feed, [article])).run(
            company_id, as_of="2026-09-20"
        )
    assert first.status == "complete"
    assert first.packet["sources"][0]["attachment_tier"] == "mfn-primary"

    def no_transport(method, url, **kwargs):
        raise AssertionError(f"redownload of {url}")

    with patch("alphaforge.evidence.flow.request_with_retry", side_effect=no_transport):
        second = OneCompanyEvidenceFlow(conn, scraper=_FakeCisionScraper(feed, [article])).run(
            company_id, as_of="2026-09-20"
        )
    assert second.status == "complete"
    assert second.completeness == {"quarterly": {"expected": 1, "retained": 1}}
    diagnostic = second.diagnostic()
    assert diagnostic["retained"] == 1
    assert diagnostic["persisted"] == 1
    assert diagnostic["attachment_selection"] == {"mfn-primary": 1}
    assert second.packet["sources"][0]["attachment_tier"] == "mfn-primary"


def test_non_complete_boundary_invalidates_all_prior_packets():
    """review-3: acquisition/detail/no-evidence exits tombstone prior packets."""
    from alphaforge.providers.mfn.errors import MfnAcquisitionError

    conn = _connection()
    company_id = _mapped_company(conn)
    persist_evidence_packet(
        conn,
        company_id=company_id,
        as_of="2026-09-20",
        packet={"packet_hash": "c" * 64, "evidence_rules_version": EVIDENCE_RULES_VERSION},
    )
    persist_evidence_packet(
        conn,
        company_id=company_id,
        as_of="2026-09-20",
        packet={"packet_hash": "o" * 64, "evidence_rules_version": EVIDENCE_RULES_VERSION - 1},
    )

    class _FailingFeedScraper(_FakeCisionScraper):
        def discover_feed(self, mfn_slug, *, reports_only=True):
            raise MfnAcquisitionError("mfn_feed_fetch_failed", "feed unreachable")

    result = OneCompanyEvidenceFlow(conn, scraper=_FailingFeedScraper([], [])).run(
        company_id, as_of="2026-09-20"
    )
    assert result.status == "acquisition_failed"
    remaining = conn.execute(
        "SELECT packet_hash, usable, usable_reason FROM evidence_packets WHERE company_id=? AND as_of=? ORDER BY id",
        (company_id, "2026-09-20"),
    ).fetchall()
    assert [row[0] for row in remaining] == ["c" * 64, "o" * 64]
    assert all(row[1] == 0 for row in remaining)
    assert all(row[2] == "incomplete_run:acquisition_failed" for row in remaining)


def test_dry_run_does_not_invalidate_stale_packets():
    """review-3: dry runs never mutate the packet table."""
    conn = _connection()
    company_id = _mapped_company(conn)
    persist_evidence_packet(
        conn,
        company_id=company_id,
        as_of="2026-09-20",
        packet={"packet_hash": "o" * 64, "evidence_rules_version": EVIDENCE_RULES_VERSION - 1},
    )
    result = OneCompanyEvidenceFlow(conn, scraper=_FakeCisionScraper([], [])).run(
        company_id, as_of="2026-09-20", dry_run=True
    )
    assert result.status == "dry_run"
    remaining = conn.execute("SELECT COUNT(*) FROM evidence_packets").fetchone()[0]
    assert remaining == 1
