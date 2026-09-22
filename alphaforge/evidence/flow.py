"""One-company deterministic MFN report evidence flow."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from alphaforge.core.frozen_packet import stable_packet_hash, validate_frozen_packet
from alphaforge.db.repositories import (
    complete_evidence_identity_documents,
    find_complete_evidence_attachment,
    find_complete_evidence_document,
    get_mfn_mapping_review,
    get_verified_mfn_mapping,
    persist_evidence_document,
    persist_evidence_packet,
    persist_evidence_sibling,
    persist_mfn_issuer_candidates,
    record_job,
    record_mfn_feed_check,
)
from alphaforge.evidence.ingest import (
    ResearchDocumentIngestionService,
    _variant_relationship,
    ambiguous_variant_pairs,
    bilingual_dedupe,
    resolve_document_language,
)
from alphaforge.evidence.mfn_taxonomy import document_type, is_report
from alphaforge.providers.http import MAX_RETRIES, request_with_retry
from alphaforge.providers.mfn.errors import MfnAcquisitionError
from alphaforge.providers.mfn.issuer import MfnIssuerAcquisitionError, MfnIssuerResolver

if TYPE_CHECKING:
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
    if declared_length is not None:
        try:
            parsed_length = int(declared_length)
        except (TypeError, ValueError):
            parsed_length = None
        if parsed_length is not None and parsed_length > limits.max_pdf_bytes:
            raise PdfAcquisitionError(
                "resource_limit", "PDF Content-Length exceeds the configured limit"
            )
    content = bytes(getattr(response, "content", b"") or b"")
    if len(content) > limits.max_pdf_bytes:
        raise PdfAcquisitionError("resource_limit", "PDF payload exceeds the configured byte limit")
    if "text/html" in content_type or "application/xhtml" in content_type:
        raise PdfAcquisitionError(
            "invalid_content_type", f"attachment content type is {content_type}"
        )
    if content_type and "pdf" not in content_type and content_type != "application/octet-stream":
        raise PdfAcquisitionError(
            "invalid_content_type", f"attachment content type is {content_type}"
        )
    if not content.startswith(b"%PDF-"):
        raise PdfAcquisitionError(
            "invalid_pdf_magic", "attachment does not start with PDF magic bytes"
        )
    return PdfDownload(
        source_url=source_url,
        content_type=content_type or "application/pdf",
        http_status=status,
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _sibling_entry(sibling: Any, document_id: int) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    try:
        loaded = json.loads(sibling["raw_metadata"]) if sibling["raw_metadata"] else None
        if isinstance(loaded, dict):
            metadata = loaded
    except (TypeError, ValueError, KeyError, IndexError):
        metadata = {}
    language = sibling["ingested_lang"] or metadata.get("language") or "en"
    relationship = metadata.get("relationship") or "UNRESOLVED"
    return {
        "source_url": sibling["source_url"],
        "title": sibling["title"] or "",
        "publication_date": sibling["published_at"],
        "language": language,
        "duplicate_of": f"document:{document_id}",
        "selected_variant_source_url": metadata.get("duplicate_of_source_url"),
        "variant_group_id": metadata.get("bilingual_group_id"),
        "selection_state": f"suppressed_by_{str(relationship).casefold()}",
        "selection_reason": (
            "REVISION"
            if relationship == "REVISION"
            else ("PREFERRED_LANGUAGE" if language == "en" else "FALLBACK_LANGUAGE")
        ),
        "relationship": relationship,
    }


def _is_pdf_backed_language_evidence(value: Any) -> bool:
    evidence = str(value or "")
    return evidence == "filename" or evidence.startswith("pdf_text:")


def _prepare_selected_article(
    variant: dict[str, Any], downloaded: PdfDownload, extracted: Any
) -> dict[str, Any]:
    release_lang = variant.get("lang") or variant.get("ingested_lang") or ""
    pdf_first_pages = "\n".join(str(page.get("text") or "") for page in extracted.pages[:3])
    pdf_language, language_evidence = resolve_document_language(
        filename=downloaded.source_url,
        pdf_text=pdf_first_pages,
        release_title=str(variant.get("title") or ""),
        release_body=str(variant.get("content_text") or variant.get("body") or ""),
        release_lang=str(release_lang),
    )
    return {
        **variant,
        "pdf_language": pdf_language,
        "pdf_checksum": downloaded.sha256,
        "language_evidence": language_evidence,
        "ingested_lang": pdf_language or release_lang or "en",
        "document_type": variant.get("document_type")
        or document_type(str(variant.get("title") or "")),
        "period_start": variant.get("period_start")
        or variant.get("report_period_start")
        or _period_start(variant),
        "period_end": variant.get("period_end")
        or variant.get("report_period_end")
        or _body_period_end(variant),
        "observation_date": _observation_date(variant),
        "observation_date_authoritative": any(
            variant.get(key) not in (None, "")
            for key in ("observation_date", "period_end", "report_period_end")
        ),
    }


def _coverage_facts(sources: list[dict[str, Any]], limitations: set[str]) -> dict[str, Any]:
    return {
        "source_count": len(sources),
        "source_ids": [source["source_id"] for source in sources],
        "report_kinds": sorted(
            {str(source["report_kind"]) for source in sources if source.get("report_kind")}
        ),
        "languages": sorted(
            {str(source["language"]) for source in sources if source.get("language")}
        ),
        "limitations": sorted(limitations),
    }


_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "januari": 1,
    "februari": 2,
    "mars": 3,
    "maj": 5,
    "juni": 6,
    "juli": 7,
    "augusti": 8,
    "oktober": 10,
}


def _date_value(day: str, month: str, year: str | None) -> str | None:
    month_number = _MONTHS.get(month.casefold().rstrip("."))
    if month_number is None or year is None:
        return None
    try:
        return datetime(int(year), month_number, int(day), tzinfo=UTC).date().isoformat()
    except ValueError:
        return None


def _has_fiscal_year_span(title: str) -> bool:
    return bool(re.search(r"\b20\d{2}\s*[/\-]\s*(?:20)?\d{2}\b", title))


def _range_start(
    start_day: str, start_month: str, start_year: str | None, end_iso: str
) -> str | None:
    """Infer a range start date, rolling back one year across year boundaries."""
    if start_year is not None:
        return _date_value(start_day, start_month, start_year)
    end_month = int(end_iso[5:7])
    start_month_number = _MONTHS.get(start_month.casefold().rstrip("."))
    if start_month_number is None:
        return None
    year = int(end_iso[:4])
    if start_month_number > end_month:
        year -= 1
    return _date_value(start_day, start_month, str(year))


def _body_period_range(article: dict[str, Any]) -> tuple[str | None, str | None]:
    """Extract an explicitly stated fiscal (start, end) range from the body.

    Fiscal Q1 is not necessarily January--March (Clas Ohlson's Q1 is
    May--July).  The release body is the authoritative deterministic source
    when it states the covered interval; using a calendar-quarter default here
    silently shifted the observation date by four months.
    """
    body = str(article.get("content_text") or article.get("body") or "").casefold()
    if not body:
        return None, None
    months = "|".join(sorted(_MONTHS, key=len, reverse=True))
    title = str(article.get("title") or "")
    year_match = (
        None
        if _has_fiscal_year_span(title)
        else re.search(r"\bq\s*[1-4]\s*(?:fy\s*)?(20\d{2})", title, re.IGNORECASE)
    )
    fiscal_year = year_match.group(1) if year_match else None
    # Both ``1 May 2026 – 31 July 2026`` and the common abbreviated form
    # ``1 May – 31 July 2026`` are emitted by MFN pages.
    range_pattern = re.compile(
        rf"\b(\d{{1,2}})\s+({months})(?:\s+(20\d{{2}}))?\s*"
        rf"(?:-|–|—|to|through|till|till och med)\s*"
        rf"(\d{{1,2}})\s+({months})(?:\s+(20\d{{2}}))?\b"
    )
    candidates: list[tuple[int, str | None, str]] = []
    for match in range_pattern.finditer(body):
        end = _date_value(match.group(4), match.group(5), match.group(6) or fiscal_year)
        if end is None:
            continue
        start = _range_start(match.group(1), match.group(2), match.group(3), end)
        before = body[max(0, match.start() - 48) : match.start()]
        if any(
            term in before
            for term in (
                "compared",
                "comparative",
                "comparison",
                "previous",
                "prior",
                "same period",
                "last year",
                "jämförelse",
                "föregående",
                "tidigare",
                "samma period",
                "förra året",
            )
        ):
            continue
        context = body[max(0, match.start() - 48) : min(len(body), match.end() + 48)]
        score = 0
        score += sum(
            context.count(term)
            for term in (
                "covered",
                "covers",
                "quarter",
                "period",
                "months ended",
                "three months",
                "kvartalet",
                "omfattade",
                "omfattar",
                "perioden",
                "månader",
            )
        )
        score -= sum(
            context.count(term)
            for term in (
                "compared",
                "comparative",
                "comparison",
                "previous",
                "prior",
                "same period",
                "last year",
                "jämförelse",
                "föregående",
                "tidigare",
                "samma period",
                "förra året",
            )
        )
        candidates.append((score, start, end))
    if candidates:
        candidates.sort(key=lambda candidate: candidate[0], reverse=True)
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            if candidates[0][2] != candidates[1][2]:
                return None, None
        return candidates[0][1], candidates[0][2]
    month_first = re.compile(
        rf"\b({months})\s+(\d{{1,2}}),?\s+(20\d{{2}})?\s*"
        rf"(?:-|–|—|to|through)\s*({months})\s+(\d{{1,2}}),?\s+(20\d{{2}})?\b"
    )
    month_candidates: list[tuple[int, str | None, str]] = []
    for match in month_first.finditer(body):
        end = _date_value(match.group(5), match.group(4), match.group(6) or fiscal_year)
        if end is None:
            continue
        start = _range_start(match.group(2), match.group(1), match.group(3), end)
        before = body[max(0, match.start() - 48) : match.start()]
        if any(
            term in before
            for term in (
                "compared",
                "comparative",
                "comparison",
                "previous",
                "prior",
                "same period",
                "last year",
                "jämförelse",
                "föregående",
                "tidigare",
                "samma period",
                "förra året",
            )
        ):
            continue
        context = body[max(0, match.start() - 48) : min(len(body), match.end() + 48)]
        score = sum(
            context.count(term)
            for term in (
                "covered",
                "covers",
                "quarter",
                "period",
                "months ended",
                "three months",
                "kvartalet",
                "omfattade",
                "omfattar",
                "perioden",
                "månader",
            )
        )
        month_candidates.append((score, start, end))
    if month_candidates:
        month_candidates.sort(key=lambda candidate: candidate[0], reverse=True)
        if len(month_candidates) > 1 and month_candidates[0][0] == month_candidates[1][0]:
            if month_candidates[0][2] != month_candidates[1][2]:
                return None, None
        return month_candidates[0][1], month_candidates[0][2]
    # ``for the three months ended 31 July 2026`` is also unambiguous.
    ended_pattern = re.compile(
        rf"\b(?:ended|ending|per|slutade)\s+(\d{{1,2}})\s+({months})(?:\s+(20\d{{2}}))?\b"
    )
    ended_candidates: list[tuple[int, str | None, str]] = []
    for ended in ended_pattern.finditer(body):
        end = _date_value(ended.group(1), ended.group(2), ended.group(3) or fiscal_year)
        if end is None:
            continue
        before = body[max(0, ended.start() - 48) : ended.start()]
        if any(
            term in before
            for term in (
                "compared",
                "comparative",
                "comparison",
                "previous",
                "prior",
                "same period",
                "last year",
                "jämförelse",
                "föregående",
                "tidigare",
                "samma period",
                "förra året",
            )
        ):
            continue
        context = body[max(0, ended.start() - 48) : min(len(body), ended.end() + 48)]
        score = sum(
            context.count(term)
            for term in (
                "covered",
                "covers",
                "quarter",
                "period",
                "months ended",
                "three months",
                "kvartalet",
                "omfattade",
                "omfattar",
                "perioden",
                "månader",
            )
        )
        ended_candidates.append((score, None, end))
    if ended_candidates:
        ended_candidates.sort(key=lambda candidate: candidate[0], reverse=True)
        if len(ended_candidates) > 1 and ended_candidates[0][0] == ended_candidates[1][0]:
            if ended_candidates[0][2] != ended_candidates[1][2]:
                return None, None
        return ended_candidates[0][1], ended_candidates[0][2]
    return None, None


def _body_period_end(article: dict[str, Any]) -> str | None:
    """Extract an explicitly stated fiscal range end before title heuristics."""
    _start, end = _body_period_range(article)
    return end


def _period_start(article: dict[str, Any]) -> str | None:
    for key in ("period_start", "report_period_start"):
        value = article.get(key)
        if value:
            return str(value)[:10]
    start, _end = _body_period_range(article)
    return start


def _observation_date(article: dict[str, Any]) -> str | None:
    for key in ("observation_date", "period_end", "report_period_end"):
        value = article.get(key)
        if value:
            return str(value)[:10]
    return _body_period_end(article)


class NoEvidenceReason(StrEnum):
    """Typed reasons for a deterministic lane with no model-ready evidence."""

    NO_PUBLISHED_RELEASE = "no_published_release"
    ALL_RELEASES_AFTER_CUTOFF = "all_releases_after_cutoff"
    NO_COMPLETE_SOURCE = "no_complete_source"


def build_frozen_evidence_packet(
    conn: Any,
    *,
    company_id: int,
    as_of: str,
    mapping: dict[str, Any] | None = None,
    additional_limitations: list[str] | None = None,
    publication_cutoff: str | None = None,
    excluded_source_urls: set[str] | None = None,
) -> dict[str, Any]:
    """Build canonical point-in-time JSON from persisted page anchors."""
    mapping = mapping or get_verified_mfn_mapping(conn, company_id)
    if mapping is None:
        raise ValueError("cannot build evidence packet without a verified MFN mapping")
    cutoff = min(as_of[:10], publication_cutoff[:10]) if publication_cutoff else as_of[:10]
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
        (company_id, cutoff),
    ).fetchall()
    sources: list[dict[str, Any]] = []
    limitations: set[str] = set()
    excluded_source_urls = excluded_source_urls or set()
    for row in rows:
        if str(row["source_url"]) in excluded_source_urls:
            continue
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
            SELECT source_url, title, published_at, ingested_lang, duplicate_of, raw_metadata
            FROM research_documents
            WHERE duplicate_of=?
              AND published_at IS NOT NULL
              AND substr(published_at, 1, 10) <= ?
            ORDER BY source_url
            """,
            (document_id, cutoff),
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
        body_paragraphs = [
            paragraph.strip() for paragraph in re.split(r"\n\s*\n", body_text) if paragraph.strip()
        ]
        observation_date = _observation_date(
            {
                "title": row["title"] or "",
                "content_text": body_text,
                "observation_date": (
                    raw_metadata.get("observation_date")
                    if raw_metadata.get("observation_date_authoritative")
                    else None
                ),
                "period_end": raw_metadata.get("period_end"),
                "report_period_end": raw_metadata.get("report_period_end"),
            }
        )
        source_language = row["ingested_lang"] or raw_metadata.get("language") or "en"
        source = {
            "source_id": source_id,
            "source_url": row["source_url"],
            "title": row["title"] or "",
            "report_kind": raw_metadata.get("report_kind"),
            "document_type": raw_metadata.get("document_type"),
            "fiscal_period": raw_metadata.get("fiscal_period") or raw_metadata.get("report_period"),
            "period_start": raw_metadata.get("period_start"),
            "period_end": raw_metadata.get("period_end") or raw_metadata.get("report_period_end"),
            "observation_date": observation_date,
            "language": source_language,
            "variant_group_id": raw_metadata.get("bilingual_group_id"),
            "selection_state": "selected",
            "selection_reason": (
                "PREFERRED_LANGUAGE" if source_language == "en" else "FALLBACK_LANGUAGE"
            ),
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
                _sibling_entry(sibling, document_id) for sibling in sibling_rows
            ],
        }
        sources.append(source)
    sources.sort(
        key=lambda source: (source["publication_date"], source["source_url"], source["source_id"])
    )
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
        "evidence_catalog": {"canonical_source_ids": [source["source_id"] for source in sources]},
        "limitations": sorted(limitations | set(additional_limitations or [])),
    }
    base["coverage_facts"] = _coverage_facts(
        sources,
        set(base["limitations"]),
    )
    base["packet_hash"] = stable_packet_hash(base)
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
    no_evidence_reason: NoEvidenceReason | None = None
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
            "no_evidence_reason": (
                self.no_evidence_reason.value if self.no_evidence_reason is not None else None
            ),
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
        if scraper is None:
            from alphaforge.providers.mfn.scraper import MfnScraper

            scraper = MfnScraper()
        self.scraper = scraper
        self.resolver = resolver or MfnIssuerResolver(base_url=self.scraper.base_url)
        self.limits = limits
        self.now = now or (lambda: datetime.now(UTC))

    def run(
        self,
        company_id: int,
        *,
        as_of: str,
        dry_run: bool = False,
        shadow_citation: Mapping[str, Any] | None = None,
        shadow_claim: str | None = None,
        shadow_missing_item: str | Mapping[str, Any] | None = None,
        shadow_specialist_requirement: str | None = None,
    ) -> EvidenceFlowResult:
        try:
            return self._run(
                company_id,
                as_of=as_of,
                dry_run=dry_run,
                shadow_citation=shadow_citation,
                shadow_claim=shadow_claim,
                shadow_missing_item=shadow_missing_item,
                shadow_specialist_requirement=shadow_specialist_requirement,
            )
        except Exception as exc:
            if not dry_run:
                try:
                    row = self.conn.execute(
                        "SELECT borsdata_id FROM companies WHERE id=?", (company_id,)
                    ).fetchone()
                    if row is not None:
                        record_job(
                            self.conn,
                            "evidence",
                            company_id=company_id,
                            borsdata_id=(
                                int(row["borsdata_id"]) if row["borsdata_id"] is not None else None
                            ),
                            status="failed",
                            error={"code": "unhandled_error", "message": str(exc)},
                            begin_attempt=False,
                        )
                except Exception:
                    pass
            raise

    def _run(
        self,
        company_id: int,
        *,
        as_of: str,
        dry_run: bool = False,
        shadow_citation: Mapping[str, Any] | None = None,
        shadow_claim: str | None = None,
        shadow_missing_item: str | Mapping[str, Any] | None = None,
        shadow_specialist_requirement: str | None = None,
    ) -> EvidenceFlowResult:
        row = self.conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
        if row is None:
            result = EvidenceFlowResult(
                "company_missing", company_id, message="company id not found"
            )
            if not dry_run:
                record_job(
                    self.conn,
                    "evidence",
                    company_id=None,
                    borsdata_id=None,
                    status="failed",
                    error={"code": result.status, "message": result.message},
                )
            return result
        company = dict(row)
        borsdata_id = company.get("borsdata_id")
        if not dry_run:
            record_job(
                self.conn,
                "evidence",
                company_id=company_id,
                borsdata_id=int(borsdata_id) if borsdata_id is not None else None,
                status="running",
            )

        def finish(result: EvidenceFlowResult) -> EvidenceFlowResult:
            if dry_run:
                return result
            if result.status in {"complete", "no_evidence"}:
                job_status = "success" if result.status == "complete" else "partial"
            else:
                job_status = "failed"
            error = None
            if result.status != "complete":
                error = {
                    "code": result.status,
                    "message": result.message,
                    "no_evidence_reason": (
                        result.no_evidence_reason.value
                        if result.no_evidence_reason is not None
                        else None
                    ),
                    "diagnostic": result.diagnostic(),
                }
            record_job(
                self.conn,
                "evidence",
                company_id=company_id,
                borsdata_id=int(borsdata_id) if borsdata_id is not None else None,
                status=job_status,
                error=error,
                begin_attempt=False,
            )
            return result

        mapping = get_verified_mfn_mapping(self.conn, company_id)
        if mapping is None:
            # A reviewed `ambiguous` mapping is authoritative until re-reviewed:
            # later exact-identifier discovery is queued as candidates, never
            # applied. Fresh discovery keeps working for never-reviewed issuers.
            review = get_mfn_mapping_review(self.conn, company_id)
            if review is not None and review.get("status") == "ambiguous":
                try:
                    resolution = self.resolver.discover(company)
                except MfnIssuerAcquisitionError as exc:
                    return finish(
                        EvidenceFlowResult(
                            "acquisition_failed",
                            company_id,
                            mapping_status="ambiguous",
                            skipped={exc.code: 1},
                            message=str(exc),
                        )
                    )
                if not dry_run:
                    persist_mfn_issuer_candidates(
                        self.conn,
                        company_id,
                        list(resolution.candidates),
                        discovery_source="mfn_search_or_index",
                    )
                return finish(
                    EvidenceFlowResult(
                        "mapping_ambiguous",
                        company_id,
                        mapping_status="ambiguous",
                        skipped={"issuer_mapping_review_required": 1},
                        message=(
                            "reviewed ambiguous MFN issuer mapping blocks discovery "
                            "until re-reviewed"
                        ),
                    )
                )
            try:
                resolution = self.resolver.discover(company)
            except MfnIssuerAcquisitionError as exc:
                return finish(
                    EvidenceFlowResult(
                        "acquisition_failed",
                        company_id,
                        mapping_status="unavailable",
                        skipped={exc.code: 1},
                        message=str(exc),
                    )
                )
            if not dry_run:
                self.resolver.persist_resolution(self.conn, company, resolution)
            if resolution.status != "mapped":
                return finish(
                    EvidenceFlowResult(
                        "mapping_" + resolution.status,
                        company_id,
                        mapping_status=resolution.status,
                        skipped={"issuer_mapping_review_required": 1},
                        message=(
                            "no deterministic MFN issuer mapping"
                            if not resolution.candidates
                            else "multiple deterministic MFN issuer candidates require review"
                        ),
                    )
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
            return finish(
                EvidenceFlowResult("mapping_unavailable", company_id, mapping_status="unmapped")
            )
        now = self.now()
        try:
            feed = self.scraper.discover_feed(mapping["mfn_slug"], reports_only=True)
            if now.weekday() == 6:
                feed.extend(_discover_feed_page(self.scraper, mapping["mfn_slug"], 2))
        except MfnAcquisitionError as exc:
            return finish(
                EvidenceFlowResult(
                    "acquisition_failed",
                    company_id,
                    mapping_status="mapped",
                    skipped={exc.code: 1},
                    message=str(exc),
                )
            )
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
        today = now.date()
        future_dated_complete_release = False
        not_yet_published_complete_release = False
        for entry in unique_feed:
            entry_url = (
                entry if isinstance(entry, str) else entry.get("url") or entry.get("source_url")
            )
            if entry_url:
                complete = find_complete_evidence_document(self.conn, company_id, entry_url)
                if complete is not None:
                    published_at = complete.get("published_at")
                    published_date = str(published_at or "")[:10]
                    if published_date > as_of[:10]:
                        future_dated_complete_release = True
                    elif published_date > today.isoformat():
                        not_yet_published_complete_release = True
                    continue
            unseen_feed.append(entry)
        complete_documents = complete_evidence_identity_documents(self.conn, company_id, as_of=None)
        for document in complete_documents:
            published_date = str(document.get("published_at") or "")[:10]
            if published_date > as_of[:10]:
                future_dated_complete_release = True
            elif published_date > today.isoformat():
                not_yet_published_complete_release = True
        if not dry_run:
            record_mfn_feed_check(
                self.conn,
                company_id,
                mapping["mfn_slug"],
                len(unique_feed),
                len(unseen_feed),
            )
            self.conn.commit()
        try:
            details = self.scraper.scrape_details(unseen_feed, reports_only=True)
        except MfnAcquisitionError as exc:
            return finish(
                EvidenceFlowResult(
                    "acquisition_failed",
                    company_id,
                    mapping_status="mapped",
                    discovered=len(unseen_feed),
                    skipped={exc.code: 1},
                    message=str(exc),
                )
            )
        result = EvidenceFlowResult(
            "dry_run" if dry_run else "running",
            company_id,
            mapping_status="mapped",
            discovered=len(details),
        )
        if future_dated_complete_release:
            result.skipped["future_dated_release"] = 1
        if not_yet_published_complete_release:
            result.skipped["not_yet_published_release"] = 1
        eligible: list[dict[str, Any]] = []
        pre_cutoff_report = False
        for article in details:
            title = article.get("title") or ""
            if not is_report(title):
                result.skipped["non_report_release"] = (
                    result.skipped.get("non_report_release", 0) + 1
                )
                continue
            published_at = article.get("published_at")
            if not published_at:
                result.skipped["missing_publication_timestamp"] = (
                    result.skipped.get("missing_publication_timestamp", 0) + 1
                )
                continue
            published_date = str(published_at)[:10]
            if published_date > as_of[:10]:
                result.skipped["future_dated_release"] = (
                    result.skipped.get("future_dated_release", 0) + 1
                )
                continue
            if published_date > today.isoformat():
                result.skipped["not_yet_published_release"] = (
                    result.skipped.get("not_yet_published_release", 0) + 1
                )
                continue
            pre_cutoff_report = True
            eligible.append(
                {
                    **article,
                    # Identity fallbacks need the reviewed issuer token even
                    # when a scraper fixture/detail page omits it.
                    "mfn_slug": mapping["mfn_slug"],
                    "company_id": company_id,
                }
            )
        # Resolve identity dates once per article so variant grouping compares
        # real fiscal periods instead of synthesized calendar quarters, and a
        # later English edition can attach even when published on another day.
        for article in eligible:
            if article.get("document_type") is None:
                article["document_type"] = document_type(str(article.get("title") or ""))
            if article.get("period_start") is None:
                article["period_start"] = article.get("report_period_start") or _period_start(
                    article
                )
            if article.get("period_end") is None:
                article["period_end"] = article.get("report_period_end") or _body_period_end(
                    article
                )
            if article.get("observation_date") is None:
                article["observation_date"] = _observation_date(article)
        persisted_identity = []
        persisted_cutoff = min(as_of[:10], today.isoformat())
        for persisted in complete_evidence_identity_documents(
            self.conn, company_id, as_of=persisted_cutoff
        ):
            metadata = {}
            if persisted["raw_metadata"]:
                try:
                    loaded = json.loads(persisted["raw_metadata"])
                    if isinstance(loaded, dict):
                        metadata = loaded
                except (TypeError, ValueError):
                    metadata = {}
            pdf_language = (
                metadata.get("pdf_language")
                if _is_pdf_backed_language_evidence(metadata.get("language_evidence"))
                else ""
            )
            persisted_identity.append(
                {
                    "source_url": persisted["source_url"],
                    "title": persisted["title"],
                    "published_at": persisted["published_at"],
                    "content_text": persisted["content_text"],
                    "mfn_slug": metadata.get("mfn_slug"),
                    "report_kind": metadata.get("report_kind"),
                    "document_type": metadata.get("document_type"),
                    "fiscal_period": metadata.get("fiscal_period") or metadata.get("report_period"),
                    "period_start": metadata.get("period_start"),
                    "period_end": metadata.get("period_end") or metadata.get("report_period_end"),
                    "observation_date": (
                        metadata.get("observation_date")
                        if metadata.get("observation_date_authoritative")
                        else None
                    ),
                    "provider_event_id": metadata.get("provider_event_id"),
                    "mfn_event_id": metadata.get("mfn_event_id"),
                    "pdf_language": pdf_language,
                    "attachment_url": persisted["attachment_url"],
                    "attachment_checksum": persisted["attachment_checksum"],
                    "pdf_checksum": persisted["attachment_checksum"],
                    "ingested_lang": persisted["ingested_lang"] if pdf_language else "",
                    "lang": persisted["ingested_lang"] if pdf_language else "",
                    "_pdf_language_unresolved": not bool(pdf_language),
                    "_bilingual_group_id": metadata.get("bilingual_group_id"),
                    "_persisted_evidence": True,
                }
            )
        identity_candidates = persisted_identity + eligible
        if dry_run:
            deduped = bilingual_dedupe(identity_candidates)
            result.eligible = sum(
                1 for article in deduped if not article.get("_persisted_evidence")
            )
            result.status = "dry_run"
            return result

        ingestion = ResearchDocumentIngestionService(self.conn)
        resolved_pdf_cache: dict[str, tuple[PdfDownload, Any, str, str]] = {}
        unresolved_existing_source_urls: set[str] = set()

        def mark_pdf_language_unresolved(
            index: int, candidate: dict[str, Any], existing: dict[str, Any] | None
        ) -> None:
            identity_candidates[index] = {
                **candidate,
                "pdf_language": "",
                "language_evidence": "unresolved",
                "ingested_lang": "",
                "lang": "",
                "_pdf_language_unresolved": True,
            }
            if existing is not None and existing.get("canonical_source_url"):
                unresolved_existing_source_urls.add(str(existing["canonical_source_url"]))

        for index, candidate in enumerate(identity_candidates):
            attachment_url = candidate.get("attachment_url") or candidate.get("storage_url")
            if not attachment_url:
                continue
            existing = find_complete_evidence_attachment(
                self.conn, str(attachment_url), company_id
            )
            try:
                candidate_download = download_pdf(str(attachment_url), limits=self.limits)
                candidate_extracted = ingestion.extract_pdf_pages(
                    candidate_download.content, max_pages=self.limits.max_pages
                )
            except Exception:
                mark_pdf_language_unresolved(index, candidate, existing)
                continue
            if not candidate_extracted.pages:
                mark_pdf_language_unresolved(index, candidate, existing)
                continue
            pdf_first_pages = "\n".join(
                str(page.get("text") or "") for page in candidate_extracted.pages[:3]
            )
            release_lang = candidate.get("lang") or candidate.get("ingested_lang") or ""
            pdf_language, language_evidence = resolve_document_language(
                filename=candidate_download.source_url,
                pdf_text=pdf_first_pages,
                release_title=str(candidate.get("title") or ""),
                release_body=str(candidate.get("content_text") or candidate.get("body") or ""),
                release_lang=str(release_lang),
            )
            resolved = {
                **candidate,
                "pdf_language": pdf_language,
                "pdf_checksum": candidate_download.sha256,
                "language_evidence": language_evidence,
                "ingested_lang": pdf_language or release_lang or "en",
                "_pdf_language_unresolved": False,
            }
            identity_candidates[index] = resolved
            if existing is not None and existing.get("canonical_source_url"):
                unresolved_existing_source_urls.discard(str(existing["canonical_source_url"]))
            resolved_pdf_cache[str(attachment_url)] = (
                candidate_download,
                candidate_extracted,
                pdf_language,
                language_evidence,
            )

        shadow_variant_pairs = ambiguous_variant_pairs(identity_candidates)
        deduped = bilingual_dedupe(identity_candidates)
        result.eligible = sum(1 for article in deduped if not article.get("_persisted_evidence"))
        for article in deduped:
            variants = [article, *article.get("_suppressed_variants", [])]
            selected = None
            downloaded = None
            extracted = None
            existing_canonical_source_url = None
            failures: dict[str, int] = {}
            prepared_variants: list[dict[str, Any]] = []
            candidate_options: list[
                tuple[dict[str, Any], PdfDownload | None, Any, dict[str, Any] | None]
            ] = []
            for variant in variants:
                attachment_url = variant.get("attachment_url") or variant.get("storage_url")
                if not attachment_url:
                    prepared_variants.append(variant)
                    failures["missing_pdf_attachment"] = (
                        failures.get("missing_pdf_attachment", 0) + 1
                    )
                    continue
                existing = find_complete_evidence_attachment(
                    self.conn, str(attachment_url), company_id
                )
                resolved_pdf = resolved_pdf_cache.get(str(attachment_url))
                if resolved_pdf is not None:
                    candidate_download, candidate_extracted, pdf_language, language_evidence = (
                        resolved_pdf
                    )
                    release_lang = variant.get("lang") or variant.get("ingested_lang") or ""
                    prepared = {
                        **variant,
                        "pdf_language": pdf_language,
                        "pdf_checksum": candidate_download.sha256,
                        "language_evidence": language_evidence,
                        "ingested_lang": pdf_language or release_lang or "en",
                        "_pdf_language_unresolved": False,
                    }
                    if existing is not None and existing.get("canonical_source_url"):
                        unresolved_existing_source_urls.discard(
                            str(existing["canonical_source_url"])
                        )
                    prepared_variants.append(prepared)
                    candidate_options.append(
                        (prepared, candidate_download, candidate_extracted, existing)
                    )
                    continue
                try:
                    candidate_download = download_pdf(str(attachment_url), limits=self.limits)
                except PdfAcquisitionError as exc:
                    prepared_variants.append(variant)
                    failures[exc.code] = failures.get(exc.code, 0) + 1
                    continue
                try:
                    candidate_extracted = ingestion.extract_pdf_pages(
                        candidate_download.content, max_pages=self.limits.max_pages
                    )
                except Exception:
                    prepared_variants.append(variant)
                    failures["pdf_extraction_failed"] = failures.get("pdf_extraction_failed", 0) + 1
                    continue
                if not candidate_extracted.pages:
                    prepared_variants.append(variant)
                    failures["pdf_extraction_failed"] = failures.get("pdf_extraction_failed", 0) + 1
                    continue
                pdf_first_pages = "\n".join(
                    str(page.get("text") or "") for page in candidate_extracted.pages[:3]
                )
                release_lang = variant.get("lang") or variant.get("ingested_lang") or ""
                pdf_language, language_evidence = resolve_document_language(
                    filename=candidate_download.source_url,
                    pdf_text=pdf_first_pages,
                    release_title=str(variant.get("title") or ""),
                    release_body=str(variant.get("content_text") or variant.get("body") or ""),
                    release_lang=str(release_lang),
                )
                prepared = {
                    **variant,
                    "pdf_language": pdf_language,
                    "pdf_checksum": candidate_download.sha256,
                    "language_evidence": language_evidence,
                    "ingested_lang": pdf_language or release_lang or "en",
                    "_pdf_language_unresolved": False,
                }
                if existing is not None and existing.get("canonical_source_url"):
                    unresolved_existing_source_urls.discard(
                        str(existing["canonical_source_url"])
                    )
                prepared_variants.append(prepared)
                candidate_options.append((prepared, candidate_download, candidate_extracted, None))
            variants = prepared_variants

            def persist_option(
                article: dict[str, Any],
                candidate_download: PdfDownload,
                candidate_extracted: Any,
                siblings: list[dict[str, Any]],
            ) -> None:
                persist_evidence_document(
                    self.conn,
                    company_id=company_id,
                    article=article,
                    attachment={
                        "source_url": candidate_download.source_url,
                        "content_type": candidate_download.content_type,
                        "byte_size": len(candidate_download.content),
                        "sha256": candidate_download.sha256,
                        "magic_valid": True,
                        "http_status": candidate_download.http_status,
                    },
                    extraction={
                        "extractor": "pypdf",
                        "text_checksum": hashlib.sha256(
                            candidate_extracted.text.encode("utf-8")
                        ).hexdigest(),
                        "page_count": candidate_extracted.page_count,
                        "pages_included": candidate_extracted.pages_included,
                        "page_truncated": candidate_extracted.page_truncated,
                        "scanned": candidate_extracted.scanned,
                        "limitations": candidate_extracted.limitations,
                    },
                    pages=list(candidate_extracted.pages),
                    suppressed_variants=siblings,
                )

            selected_existing = None
            if candidate_options:
                selected, downloaded, extracted, selected_existing = sorted(
                    candidate_options,
                    key=lambda option: (
                        0 if option[0].get("pdf_language") == "en" else 1,
                        str(option[0].get("source_url") or option[0].get("url") or ""),
                    ),
                )[0]
                if selected_existing is not None:
                    existing_canonical_source_url = str(selected_existing["canonical_source_url"])
                    if (
                        selected.get("_persisted_evidence")
                        and downloaded is not None
                        and extracted is not None
                    ):
                        persist_option(
                            _prepare_selected_article(selected, downloaded, extracted),
                            downloaded,
                            extracted,
                            [],
                        )
                    successful_variants = {id(option[0]) for option in candidate_options}
                    for option in candidate_options:
                        if option[0] is selected:
                            continue
                        relation = _variant_relationship(selected, option[0])
                        if relation in {"TRANSLATION", "REVISION"}:
                            persist_evidence_sibling(
                                self.conn,
                                company_id=company_id,
                                canonical_source_url=existing_canonical_source_url,
                                sibling={**option[0], "relationship": relation},
                            )
                        elif option[3] is None and option[1] is not None and option[2] is not None:
                            independent_article = _prepare_selected_article(
                                option[0], option[1], option[2]
                            )
                            independent_article = {
                                key: value
                                for key, value in independent_article.items()
                                if key
                                not in {
                                    "_bilingual_group_id",
                                    "bilingual_selection_rule",
                                    "relationship",
                                    "duplicate_of",
                                    "ingest_status",
                                    "_suppressed_variants",
                                }
                            }
                            persist_option(independent_article, option[1], option[2], [])
                            result.downloaded += 1
                    for variant in variants:
                        if id(variant) in successful_variants or variant is selected:
                            continue
                        relation = _variant_relationship(selected, variant)
                        if relation in {"TRANSLATION", "REVISION"}:
                            persist_evidence_sibling(
                                self.conn,
                                company_id=company_id,
                                canonical_source_url=existing_canonical_source_url,
                                sibling={**variant, "relationship": relation},
                            )
                    continue
            if selected is None or downloaded is None or extracted is None:
                for code, count in failures.items():
                    result.skipped[code] = result.skipped.get(code, 0) + count
                continue
            selected_variant = selected
            successful_variants = {id(option[0]) for option in candidate_options}
            related_variants: list[dict[str, Any]] = []
            independent_options: list[
                tuple[dict[str, Any], PdfDownload | None, Any, dict[str, Any] | None]
            ] = []
            for option in candidate_options:
                if option[0] is selected_variant:
                    continue
                relation = _variant_relationship(selected_variant, option[0])
                if relation in {"TRANSLATION", "REVISION"}:
                    related_variants.append({**option[0], "relationship": relation})
                else:
                    independent_options.append(option)
            for variant in variants:
                if id(variant) in successful_variants or variant is selected_variant:
                    continue
                relation = _variant_relationship(selected_variant, variant)
                if relation in {"TRANSLATION", "REVISION"}:
                    related_variants.append({**variant, "relationship": relation})

            selected_article = _prepare_selected_article(selected_variant, downloaded, extracted)
            persist_option(selected_article, downloaded, extracted, related_variants)
            result.downloaded += 1
            for variant, candidate_download, candidate_extracted, existing in independent_options:
                if (
                    existing is not None
                    or candidate_download is None
                    or candidate_extracted is None
                ):
                    continue
                independent_article = _prepare_selected_article(
                    variant, candidate_download, candidate_extracted
                )
                independent_article = {
                    key: value
                    for key, value in independent_article.items()
                    if key
                    not in {
                        "_bilingual_group_id",
                        "bilingual_selection_rule",
                        "relationship",
                        "duplicate_of",
                        "ingest_status",
                        "_suppressed_variants",
                    }
                }
                persist_option(independent_article, candidate_download, candidate_extracted, [])
                result.downloaded += 1
        packet_limitations = [
            f"evidence_flow_{code}:{count}"
            for code, count in sorted(result.skipped.items())
            if code
            not in {
                "non_report_release",
                "future_dated_release",
                "not_yet_published_release",
            }
        ]
        packet = build_frozen_evidence_packet(
            self.conn,
            company_id=company_id,
            as_of=as_of,
            mapping=mapping,
            additional_limitations=packet_limitations,
            publication_cutoff=today.isoformat(),
            excluded_source_urls=unresolved_existing_source_urls,
        )
        if not packet.get("sources"):
            if (
                result.skipped.get("future_dated_release", 0)
                and not result.skipped.get("not_yet_published_release", 0)
                and not pre_cutoff_report
                and not result.eligible
                and not result.skipped.get("missing_publication_timestamp", 0)
            ):
                reason = NoEvidenceReason.ALL_RELEASES_AFTER_CUTOFF
                message = "all discovered releases are after the requested point-in-time cutoff"
            elif (
                pre_cutoff_report
                or result.eligible
                or result.downloaded
                or result.skipped.get("missing_pdf_attachment")
                or result.skipped.get("missing_publication_timestamp")
                or result.skipped.get("not_yet_published_release")
            ):
                reason = NoEvidenceReason.NO_COMPLETE_SOURCE
                message = "no complete evidence source was available at the requested cutoff"
            else:
                reason = NoEvidenceReason.NO_PUBLISHED_RELEASE
                message = "no published evidence release was available at the requested cutoff"
            return finish(
                EvidenceFlowResult(
                    "no_evidence",
                    company_id,
                    mapping_status="mapped",
                    discovered=result.discovered,
                    eligible=result.eligible,
                    downloaded=result.downloaded,
                    skipped=result.skipped,
                    no_evidence_reason=reason,
                    message=message,
                )
            )
        if not validate_frozen_packet(packet):
            return finish(
                EvidenceFlowResult(
                    "packet_invalid",
                    company_id,
                    mapping_status="mapped",
                    discovered=result.discovered,
                    eligible=result.eligible,
                    downloaded=result.downloaded,
                    skipped=result.skipped,
                    message="canonical packet failed its self-hash or contains an invalid complete source",
                )
            )
        if shadow_citation is not None or shadow_missing_item is not None or shadow_variant_pairs:
            try:
                from alphaforge.llm import run_shadow_signals

                run_shadow_signals(
                    packet,
                    citation=shadow_citation,
                    claim=shadow_claim,
                    missing_item=shadow_missing_item,
                    specialist_requirement=shadow_specialist_requirement,
                    variant_pairs=shadow_variant_pairs,
                    conn=self.conn,
                )
            except Exception:
                pass
        persist_evidence_packet(self.conn, company_id=company_id, as_of=as_of, packet=packet)
        result.status = "complete"
        result.packet = packet
        result.packet_hash = packet["packet_hash"]
        return finish(result)
