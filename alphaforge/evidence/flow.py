"""One-company deterministic MFN report evidence flow."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from alphaforge.core.frozen_packet import canonical_packet_hash, validate_frozen_packet
from alphaforge.db.repositories import (
    complete_evidence_language_majority,
    complete_evidence_source_urls,
    find_complete_evidence_attachment,
    find_complete_evidence_document,
    get_verified_mfn_mapping,
    persist_evidence_document,
    persist_evidence_packet,
    record_mfn_feed_check,
)
from alphaforge.evidence.ingest import (
    ResearchDocumentIngestionService,
    bilingual_dedupe,
    canonical_release_url,
)
from alphaforge.evidence.mfn_taxonomy import is_report
from alphaforge.providers.http import MAX_RETRIES, request_with_retry
from alphaforge.providers.mfn.issuer import MfnIssuerResolver
from alphaforge.providers.mfn.scraper import MfnScraper


@dataclass(frozen=True)
class EvidenceResourceLimits:
    max_pdf_bytes: int = 25 * 1024 * 1024
    max_pages: int = 50
    max_retries: int = MAX_RETRIES


DEFAULT_RESOURCE_LIMITS = EvidenceResourceLimits()


class PdfAcquisitionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code.replace('_', ' ')}: {message}")


@dataclass(frozen=True)
class PdfDownload:
    source_url: str
    content_type: str
    http_status: int
    content: bytes
    sha256: str


def download_pdf(
    source_url: str,
    *,
    limits: EvidenceResourceLimits = DEFAULT_RESOURCE_LIMITS,
    request: Callable[..., Any] | None = None,
) -> PdfDownload:
    """Download one bounded PDF and validate headers plus magic bytes."""
    requester = request or request_with_retry
    try:
        response = requester(
            "GET",
            source_url,
            timeout=60,
            max_retries=limits.max_retries,
        )
    except Exception as exc:
        raise PdfAcquisitionError("transport_error", f"PDF request failed: {exc}") from exc
    status = int(getattr(response, "status_code", 0) or 0)
    if status != 200:
        raise PdfAcquisitionError("http_status", f"PDF request returned HTTP {status}")
    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("Content-Type") or headers.get("content-type") or "").lower()
    declared_length = headers.get("Content-Length") or headers.get("content-length")
    try:
        if declared_length is not None and int(declared_length) > limits.max_pdf_bytes:
            raise PdfAcquisitionError("resource_limit", "PDF Content-Length exceeds the configured limit")
    except (TypeError, ValueError):
        pass
    content = bytes(getattr(response, "content", b"") or b"")
    if len(content) > limits.max_pdf_bytes:
        raise PdfAcquisitionError("resource_limit", "PDF payload exceeds the configured byte limit")
    if "text/html" in content_type or "application/xhtml" in content_type:
        raise PdfAcquisitionError("invalid_content_type", f"attachment content type is {content_type}")
    if content_type and "pdf" not in content_type and content_type != "application/octet-stream":
        raise PdfAcquisitionError("invalid_content_type", f"attachment content type is {content_type}")
    if not content.startswith(b"%PDF-"):
        raise PdfAcquisitionError("invalid_pdf_magic", "attachment does not start with PDF magic bytes")
    return PdfDownload(
        source_url=source_url,
        content_type=content_type or "application/pdf",
        http_status=status,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _observation_date(article: dict[str, Any]) -> str | None:
    for key in ("observation_date", "period_end", "report_period_end"):
        value = article.get(key)
        if value:
            return str(value)[:10]
    title = str(article.get("title") or "").lower()
    year_match = re.search(r"\b(20\d{2})\b", title)
    if year_match is None:
        return None
    year = int(year_match.group(1))
    quarter_match = re.search(r"\bq([1-4])\b", title)
    if quarter_match:
        return (f"{year}-" + {"1": "03-31", "2": "06-30", "3": "09-30", "4": "12-31"}[quarter_match.group(1)])
    if any(term in title for term in ("annual", "year-end", "year end", "årsredovisning", "bokslut")):
        return f"{year}-12-31"
    return None


def build_frozen_evidence_packet(
    conn: Any,
    *,
    company_id: int,
    as_of: str,
    mapping: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build canonical point-in-time JSON from persisted page anchors."""
    mapping = mapping or get_verified_mfn_mapping(conn, company_id)
    if mapping is None:
        raise ValueError("cannot build evidence packet without a verified MFN mapping")
    rows = conn.execute(
        """
        SELECT d.id AS document_id, d.source_url, d.title, d.published_at,
               d.fetched_at, d.ingested_lang, d.raw_metadata, d.content_text AS release_body,
               a.source_url AS attachment_url, a.content_type, a.byte_size,
               a.sha256 AS attachment_sha256,
               e.extractor, e.text_checksum, e.page_count, e.pages_included,
               e.page_truncated, e.scanned, e.limitations
        FROM research_documents d
        JOIN research_attachments a ON a.document_id=d.id
        JOIN document_extractions e ON e.document_id=d.id
        WHERE d.company_id=? AND d.duplicate_of IS NULL
          AND d.published_at IS NOT NULL AND substr(d.published_at, 1, 10) <= ?
          AND EXISTS (
              SELECT 1 FROM document_pages p WHERE p.extraction_id=e.id
          )
        ORDER BY d.source_url, a.sha256, d.id
        """,
        (company_id, as_of[:10]),
    ).fetchall()
    sources: list[dict[str, Any]] = []
    limitations: set[str] = set()
    for row in rows:
        document_id = int(row["document_id"])
        page_rows = conn.execute(
            """
            SELECT page_number, anchor, text, text_checksum
            FROM document_pages
            WHERE extraction_id=(SELECT id FROM document_extractions WHERE document_id=?)
            ORDER BY page_number
            """,
            (document_id,),
        ).fetchall()
        sibling_rows = conn.execute(
            """
            SELECT source_url, title, published_at, ingested_lang, duplicate_of
            FROM research_documents WHERE duplicate_of=? ORDER BY source_url
            """,
            (document_id,),
        ).fetchall()
        row_limitations: list[str] = []
        if row["limitations"]:
            try:
                raw = json.loads(row["limitations"])
                if isinstance(raw, list):
                    row_limitations = sorted({str(item) for item in raw})
            except (TypeError, ValueError):
                row_limitations = ["invalid_extraction_limitations_metadata"]
        limitations.update(row_limitations)
        raw_metadata: dict[str, Any] = {}
        if row["raw_metadata"]:
            try:
                loaded = json.loads(row["raw_metadata"])
                if isinstance(loaded, dict):
                    raw_metadata = loaded
            except (TypeError, ValueError):
                limitations.add("invalid_document_metadata")
        source_id = f"document:{document_id}"
        body_text = str(row["release_body"] or "").strip()
        body_paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", body_text) if paragraph.strip()]
        source = {
            "source_id": source_id,
            "source_url": row["source_url"],
            "title": row["title"] or "",
            "report_kind": raw_metadata.get("report_kind"),
            "fiscal_period": raw_metadata.get("fiscal_period") or raw_metadata.get("report_period"),
            "observation_date": raw_metadata.get("observation_date"),
            "language": row["ingested_lang"] or raw_metadata.get("language") or "en",
            "publication_date": row["published_at"],
            "publication_timestamp_authoritative": bool(
                raw_metadata.get("authoritative_publication_timestamp")
            ),
            "ingestion_date": row["fetched_at"],
            "body": {
                "text": body_text,
                "paragraphs": [
                    {
                        "anchor": f"{source_id}#paragraph:{index}",
                        "text": paragraph,
                    }
                    for index, paragraph in enumerate(body_paragraphs, start=1)
                ],
            },
            "attachment": {
                "source_url": row["attachment_url"],
                "content_type": row["content_type"],
                "byte_size": int(row["byte_size"]),
                "sha256": row["attachment_sha256"],
            },
            "extraction": {
                "extractor": row["extractor"],
                "text_checksum": row["text_checksum"],
                "page_count": int(row["page_count"]),
                "pages_included": row["pages_included"] or "",
                "page_truncated": bool(row["page_truncated"]),
                "scanned": bool(row["scanned"]),
                "limitations": row_limitations,
            },
            "pages": [
                {
                    "page_number": int(page["page_number"]),
                    "anchor": page["anchor"],
                    "text": page["text"],
                    "text_checksum": page["text_checksum"],
                }
                for page in page_rows
            ],
            "bilingual_siblings": [
                {
                    "source_url": sibling["source_url"],
                    "title": sibling["title"] or "",
                    "publication_date": sibling["published_at"],
                    "language": sibling["ingested_lang"] or "en",
                    "duplicate_of": f"document:{document_id}",
                }
                for sibling in sibling_rows
            ],
        }
        sources.append(source)
    sources.sort(key=lambda source: (source["publication_date"], source["source_url"], source["source_id"]))
    base: dict[str, Any] = {
        "schema_version": "evidence-packet-v1",
        "frozen": True,
        "company_id": int(company_id),
        "as_of": as_of[:10],
        "issuer": {
            "mfn_slug": mapping["mfn_slug"],
            "source_url": mapping["source_url"],
            "discovery_source": mapping["discovery_source"],
            "verified_at": mapping["verified_at"],
            "identity_evidence": mapping.get("identity_evidence"),
        },
        "sources": sources,
        "evidence_catalog": {
            "canonical_source_ids": [source["source_id"] for source in sources]
        },
        "limitations": sorted(limitations),
    }
    base["packet_hash"] = canonical_packet_hash(base)
    return base


@dataclass
class EvidenceFlowResult:
    status: str
    company_id: int
    mapping_status: str | None = None
    discovered: int = 0
    eligible: int = 0
    downloaded: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    packet_hash: str | None = None
    packet: dict[str, Any] | None = None
    message: str | None = None

    def diagnostic(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "company_id": self.company_id,
            "mapping_status": self.mapping_status,
            "discovered": self.discovered,
            "eligible": self.eligible,
            "downloaded": self.downloaded,
            "skipped": dict(sorted(self.skipped.items())),
            "packet_hash": self.packet_hash,
            "message": self.message,
        }


def _discover_feed_page(scraper: Any, mfn_slug: str, page: int) -> list[dict[str, Any]]:
    discover_feed = scraper.discover_feed
    try:
        parameters = inspect.signature(discover_feed).parameters.values()
    except (TypeError, ValueError):
        return []
    if "page" not in inspect.signature(discover_feed).parameters and not any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters
    ):
        return []
    return discover_feed(mfn_slug, page=page, reports_only=True)


class OneCompanyEvidenceFlow:
    """Compose deterministic mapping, release, PDF, extraction and packet steps."""

    def __init__(
        self,
        conn: Any,
        *,
        scraper: MfnScraper | None = None,
        resolver: MfnIssuerResolver | None = None,
        limits: EvidenceResourceLimits = DEFAULT_RESOURCE_LIMITS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.conn = conn
        self.scraper = scraper or MfnScraper()
        self.resolver = resolver or MfnIssuerResolver(base_url=self.scraper.base_url)
        self.limits = limits
        self.now = now or (lambda: datetime.now(UTC))

    def run(
        self,
        company_id: int,
        *,
        as_of: str,
        dry_run: bool = False,
    ) -> EvidenceFlowResult:
        row = self.conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
        if row is None:
            return EvidenceFlowResult("company_missing", company_id, message="company id not found")
        company = dict(row)
        mapping = get_verified_mfn_mapping(self.conn, company_id)
        if mapping is None:
            resolution = self.resolver.discover(company)
            if not dry_run:
                self.resolver.persist_resolution(self.conn, company, resolution)
            if resolution.status != "mapped":
                return EvidenceFlowResult(
                    "mapping_" + resolution.status,
                    company_id,
                    mapping_status=resolution.status,
                    skipped={"issuer_mapping_review_required": 1},
                    message=(
                        "no deterministic MFN issuer mapping" if not resolution.candidates else
                        "multiple deterministic MFN issuer candidates require review"
                    ),
                )
            mapping = resolution.selected
            if mapping is not None:
                mapping = {
                    **mapping,
                    "discovery_source": "mfn_search_or_index",
                    "verified_at": resolution.verified_at,
                    "identity_evidence": mapping.get("identity_evidence"),
                }
        if mapping is None:
            return EvidenceFlowResult("mapping_unavailable", company_id, mapping_status="unmapped")
        now = self.now()
        feed = self.scraper.discover_feed(mapping["mfn_slug"], reports_only=True)
        if now.weekday() == 6:
            feed.extend(_discover_feed_page(self.scraper, mapping["mfn_slug"], 2))
        complete_source_urls = set(complete_evidence_source_urls(self.conn, company_id))
        complete_canonical_urls = {
            canonical_release_url(url) for url in complete_source_urls
        }
        unique_feed: list[dict[str, Any]] = []
        seen_feed_urls: set[str] = set()
        for entry in feed:
            url = entry if isinstance(entry, str) else entry.get("url") or entry.get("source_url")
            if url and url in seen_feed_urls:
                continue
            if url:
                seen_feed_urls.add(url)
            unique_feed.append(entry)
        unseen_feed = []
        for entry in unique_feed:
            entry_url = entry if isinstance(entry, str) else entry.get("url") or entry.get("source_url")
            if entry_url and (
                find_complete_evidence_document(self.conn, company_id, entry_url) is not None
                or canonical_release_url(entry_url) in complete_canonical_urls
            ):
                continue
            unseen_feed.append(entry)
        if not dry_run:
            record_mfn_feed_check(
                self.conn,
                company_id,
                mapping["mfn_slug"],
                len(unique_feed),
                len(unseen_feed),
            )
            self.conn.commit()
        details = self.scraper.scrape_details(unseen_feed, reports_only=True)
        result = EvidenceFlowResult(
            "dry_run" if dry_run else "running",
            company_id,
            mapping_status="mapped",
            discovered=len(details),
        )
        eligible: list[dict[str, Any]] = []
        today = now.date()
        for article in details:
            title = article.get("title") or ""
            if not is_report(title):
                result.skipped["non_report_release"] = result.skipped.get("non_report_release", 0) + 1
                continue
            published_at = article.get("published_at")
            if not published_at:
                result.skipped["missing_publication_timestamp"] = result.skipped.get("missing_publication_timestamp", 0) + 1
                continue
            if str(published_at)[:10] > as_of[:10] or str(published_at)[:10] > today.isoformat():
                result.skipped["future_dated_release"] = result.skipped.get("future_dated_release", 0) + 1
                continue
            eligible.append(article)
        packet_majority = complete_evidence_language_majority(self.conn, company_id, as_of)
        deduped = bilingual_dedupe(eligible, packet_majority=packet_majority)
        result.eligible = len(deduped)
        if dry_run:
            result.status = "dry_run"
            return result
        ingestion = ResearchDocumentIngestionService(self.conn)
        for article in deduped:
            attachment_url = article.get("attachment_url") or article.get("storage_url")
            if not attachment_url:
                result.skipped["missing_pdf_attachment"] = result.skipped.get("missing_pdf_attachment", 0) + 1
                continue
            existing = find_complete_evidence_attachment(
                self.conn, str(attachment_url), company_id
            )
            if existing is not None:
                continue
            try:
                downloaded = download_pdf(str(attachment_url), limits=self.limits)
            except PdfAcquisitionError as exc:
                result.skipped[exc.code] = result.skipped.get(exc.code, 0) + 1
                continue
            try:
                extracted = ingestion.extract_pdf_pages(
                    downloaded.content, max_pages=self.limits.max_pages
                )
            except Exception:
                result.skipped["pdf_extraction_failed"] = result.skipped.get(
                    "pdf_extraction_failed", 0
                ) + 1
                continue
            if not extracted.pages:
                result.skipped["pdf_extraction_failed"] = result.skipped.get(
                    "pdf_extraction_failed", 0
                ) + 1
                continue
            article = {
                **article,
                "ingested_lang": article.get("lang") or article.get("ingested_lang") or "en",
                "observation_date": _observation_date(article),
            }
            persist_evidence_document(
                self.conn,
                company_id=company_id,
                article=article,
                attachment={
                    "source_url": downloaded.source_url,
                    "content_type": downloaded.content_type,
                    "byte_size": len(downloaded.content),
                    "sha256": downloaded.sha256,
                    "magic_valid": True,
                    "http_status": downloaded.http_status,
                },
                extraction={
                    "extractor": "pypdf",
                    "text_checksum": hashlib.sha256(extracted.text.encode("utf-8")).hexdigest(),
                    "page_count": extracted.page_count,
                    "pages_included": extracted.pages_included,
                    "page_truncated": extracted.page_truncated,
                    "scanned": extracted.scanned,
                    "limitations": extracted.limitations,
                },
                pages=list(extracted.pages),
                suppressed_variants=article.get("_suppressed_variants", []),
            )
            result.downloaded += 1
        packet = build_frozen_evidence_packet(
            self.conn,
            company_id=company_id,
            as_of=as_of,
            mapping=mapping,
        )
        if not validate_frozen_packet(packet):
            return EvidenceFlowResult(
                "packet_invalid",
                company_id,
                mapping_status="mapped",
                discovered=result.discovered,
                eligible=result.eligible,
                downloaded=result.downloaded,
                skipped=result.skipped,
                message="canonical packet failed its self-hash or contains no complete source",
            )
        persist_evidence_packet(self.conn, company_id=company_id, as_of=as_of, packet=packet)
        result.status = "complete"
        result.packet = packet
        result.packet_hash = packet["packet_hash"]
        return result
