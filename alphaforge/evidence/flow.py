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

from alphaforge.core.frozen_packet import (
    EVIDENCE_RULES_VERSION,
    stable_packet_hash,
    validate_frozen_packet,
)
from alphaforge.db.repositories import (
    complete_evidence_identity_documents,
    describe_evidence_state,
    find_complete_evidence_attachment,
    find_complete_evidence_document,
    get_mfn_mapping_review,
    get_verified_mfn_mapping,
    load_evidence_selection_manifest,
    mark_evidence_packets_unusable,
    persist_evidence_diagnostic,
    persist_evidence_document,
    persist_evidence_selection_manifest,
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
from alphaforge.evidence.manifest import (
    EvidenceSelectionManifest,
    completeness as manifest_completeness,
    packet_contents,
)
from alphaforge.evidence.mfn_taxonomy import (
    ATTACHMENT_TIERS,
    document_type,
    is_invitation_or_presentation,
    is_report,
    report_kind,
)
from alphaforge.evidence.report_rules import (
    DEFAULT_HISTORY_WINDOW,
    ReportHistoryWindow,
    report_rules_metadata,
)
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


def _stored_pdf_language(existing: dict[str, Any] | None) -> tuple[str, str] | None:
    if existing is None or not existing.get("canonical_raw_metadata"):
        return None
    try:
        metadata = json.loads(existing["canonical_raw_metadata"])
    except (TypeError, ValueError):
        return None
    language = metadata.get("pdf_language") if isinstance(metadata, dict) else None
    evidence = metadata.get("language_evidence") if isinstance(metadata, dict) else None
    if language in {"en", "sv"} and _is_pdf_backed_language_evidence(evidence):
        return str(language), str(evidence)
    return None


def _has_current_attachment_provenance(raw_metadata: Any) -> bool:
    if isinstance(raw_metadata, str):
        try:
            raw_metadata = json.loads(raw_metadata)
        except (TypeError, ValueError):
            return False
    return (
        isinstance(raw_metadata, dict) and raw_metadata.get("attachment_tier") in ATTACHMENT_TIERS
    )


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
    authoritative_language = (
        pdf_language
        if pdf_language in {"en", "sv"} and _is_pdf_backed_language_evidence(language_evidence)
        else ""
    )
    return {
        **variant,
        "pdf_language": authoritative_language,
        "pdf_checksum": downloaded.sha256,
        "language_evidence": language_evidence,
        "ingested_lang": authoritative_language
        or (release_lang if variant.get("_pdf_language_unresolved") else ""),
        "_pdf_language_unresolved": not bool(authoritative_language),
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
    fiscal_span_match = re.search(r"\b(20\d{2})\s*[/\-]\s*(?:20)?\d{2}\b", title, re.IGNORECASE)
    year_match = (
        None
        if fiscal_span_match
        else re.search(r"\bq\s*[1-4]\s*(?:fy\s*)?(20\d{2})", title, re.IGNORECASE)
    )
    fiscal_year = (
        fiscal_span_match.group(1)
        if fiscal_span_match
        else year_match.group(1)
        if year_match
        else None
    )
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
        rf"\b({months})\s+(\d{{1,2}}),?(?:\s+(20\d{{2}}))?\s*"
        rf"(?:-|–|—|to|through)\s*({months})\s+(\d{{1,2}}),?(?:\s+(20\d{{2}}))?\b"
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
    report_rules: dict[str, Any] | None = None,
    evidence_diagnostic: dict[str, Any] | None = None,
    selection_manifest: EvidenceSelectionManifest | None = None,
) -> dict[str, Any]:
    """Build canonical point-in-time JSON from persisted page anchors."""
    mapping = mapping or get_verified_mfn_mapping(conn, company_id)
    if mapping is None:
        raise ValueError("cannot build evidence packet without a verified MFN mapping")
    active_rules = report_rules or report_rules_metadata()
    fingerprint = active_rules.get("fingerprint") if isinstance(active_rules, dict) else None
    history_values = active_rules.get("history_window") if isinstance(active_rules, dict) else None
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("cannot build evidence packet without active report-rule provenance")
    if not isinstance(history_values, dict):
        raise ValueError("cannot build evidence packet without an active history window")
    if selection_manifest is not None and selection_manifest.report_rules_fingerprint != fingerprint:
        raise ValueError("selection manifest does not match active report-rule provenance")
    if selection_manifest is None:
        selection_manifest = load_evidence_selection_manifest(
            conn,
            company_id=company_id,
            as_of=as_of,
            report_rules=active_rules,
            publication_cutoff=publication_cutoff,
            excluded_source_urls=excluded_source_urls,
        )
    rows = packet_contents(selection_manifest)
    sources: list[dict[str, Any]] = []
    limitations: set[str] = set()
    fallback_source_count = 0
    excluded_source_urls = excluded_source_urls or set()
    for row in rows:
        document_id = int(row["document_id"])
        page_rows = row.get("pages", [])
        sibling_rows = row.get("siblings", [])
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
        language_evidence = str(raw_metadata.get("language_evidence") or "")
        if language_evidence.startswith("release_hint:"):
            fallback_source_count += 1
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
            "attachment_tier": raw_metadata.get("attachment_tier"),
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
    if fallback_source_count:
        limitations.add(f"pdf_language_fallback:{fallback_source_count}")
    base: dict[str, Any] = {
        "schema_version": "evidence-packet-v1",
        "frozen": True,
        "evidence_rules_version": EVIDENCE_RULES_VERSION,
        "report_rules": active_rules,
        "selection_manifest_id": selection_manifest.manifest_id,
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
    if evidence_diagnostic is not None:
        base["evidence_diagnostic"] = evidence_diagnostic
    base["coverage_facts"] = _coverage_facts(
        sources,
        set(base["limitations"]),
    )
    base["packet_hash"] = stable_packet_hash(base)
    return base


# Skip reasons that record a download/parse transport failure rather than a
# pre-download filter decision. Everything else in ``skipped`` counts as
# filtered before download; ``ambiguous_selection`` gets its own bucket.
DOWNLOAD_FAILED_SKIP_REASONS = frozenset(
    {
        "transport_error",
        "http_status",
        "resource_limit",
        "invalid_content_type",
        "invalid_pdf_magic",
        "pdf_extraction_failed",
        "mfn_feed_fetch_failed",
        "mfn_detail_fetch_failed",
        "mfn_feed_http_status",
        "mfn_detail_http_status",
    }
)
AMBIGUOUS_SELECTION_SKIP_REASON = "ambiguous_selection"


@dataclass
class EvidenceFlowResult:
    status: str
    company_id: int
    mapping_status: str | None = None
    discovered: int = 0
    eligible: int = 0
    downloaded: int = 0
    persisted: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    packet_hash: str | None = None
    packet: dict[str, Any] | None = None
    no_evidence_reason: NoEvidenceReason | None = None
    message: str | None = None
    completeness: dict[str, dict[str, int]] = field(default_factory=dict)
    attachment_selection: dict[str, int] = field(default_factory=dict)
    _persisted_diagnostic: dict[str, Any] | None = field(default=None, repr=False)

    def filtered_before_download(self) -> int:
        return sum(
            count
            for reason, count in self.skipped.items()
            if reason != AMBIGUOUS_SELECTION_SKIP_REASON
            and reason not in DOWNLOAD_FAILED_SKIP_REASONS
        )

    def download_failed(self) -> int:
        return sum(
            count
            for reason, count in self.skipped.items()
            if reason in DOWNLOAD_FAILED_SKIP_REASONS
        )

    def ambiguous_selection(self) -> int:
        return self.skipped.get(AMBIGUOUS_SELECTION_SKIP_REASON, 0)

    def diagnostic(self) -> dict[str, Any]:
        if self._persisted_diagnostic is not None:
            return dict(self._persisted_diagnostic)
        return {
            "status": self.status,
            "company_id": self.company_id,
            "mapping_status": self.mapping_status,
            "discovered": self.discovered,
            "eligible": self.eligible,
            "downloaded": self.downloaded,
            "skipped": dict(sorted(self.skipped.items())),
            "filtered_before_download": self.filtered_before_download(),
            "download_failed": self.download_failed(),
            "ambiguous_selection": self.ambiguous_selection(),
            "retained": self.downloaded + self.persisted,
            "persisted": self.persisted,
            "completeness": {
                report_class: dict(counts) for report_class, counts in self.completeness.items()
            },
            "attachment_selection": dict(sorted(self.attachment_selection.items())),
            "packet_hash": self.packet_hash,
            "no_evidence_reason": (
                self.no_evidence_reason.value if self.no_evidence_reason is not None else None
            ),
            "message": self.message,
        }


def _drain_skips(scraper: Any, method: str) -> dict[str, int]:
    """Read counted scraper drops without breaking fake scrapers in tests."""
    drain = getattr(scraper, method, None)
    if not callable(drain):
        return {}
    try:
        drained = drain()
    except TypeError:
        return {}
    return dict(drained or {})


def _confirm_cis_issuer(
    release_url: str, canonical_url: str | None, *, issuer_token: str
) -> str | None:
    """Confirm a ``/cis/a/`` release belongs to the resolved issuer.

    Returns None when the release URL issuer segment and the page's MFN
    canonical issuer segment both match the resolved mapping token.
    Otherwise returns the ``skipped`` reason: ``issuer_mismatch`` when a
    present issuer binding points at a different issuer, else
    ``canonical_issuer_unconfirmed`` (missing, malformed, or off-host
    canonical). Non-``/cis/a/`` URLs return None (legacy path unchanged).
    """
    from urllib.parse import urlsplit as _urlsplit

    from alphaforge.providers.mfn.scraper import _canonical_issuer, _cis_release_issuer

    release_issuer = _cis_release_issuer(release_url)
    if release_issuer is None:
        return None
    if release_issuer.lower() != issuer_token:
        return "issuer_mismatch"
    canonical_issuer = _canonical_issuer(canonical_url)
    if canonical_issuer is None or canonical_issuer.lower() != issuer_token:
        if canonical_issuer is not None and canonical_issuer.lower() != issuer_token:
            return "issuer_mismatch"
        return "canonical_issuer_unconfirmed"
    if canonical_url is not None:
        release_host = _urlsplit(release_url).netloc.lower()
        if _urlsplit(canonical_url).netloc.lower() != release_host:
            return "canonical_issuer_unconfirmed"
    return None


def _completeness_class(article: dict[str, Any]) -> str:
    """Return the hard-gate coverage class (annual vs quarterly) of a group."""
    kind = article.get("report_kind") or report_kind(str(article.get("title") or ""))
    return kind if kind in {"annual", "quarterly"} else "quarterly"


def _persisted_anchor_in_window(
    anchor: dict[str, Any], *, as_of: str, window: ReportHistoryWindow
) -> bool:
    """Mirror the discovery window filter for a persisted report candidate."""
    published = str(anchor.get("published_at") or "")[:10]
    if not published or published > as_of[:10]:
        return False
    kind = anchor.get("report_kind")
    cutoff = _resolve_cutoff(as_of, window, kind if kind in {"annual", "quarterly"} else None)
    return published >= cutoff


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


def _resolve_cutoff(as_of: str, window: ReportHistoryWindow, report_kind: str | None) -> str:
    """Return the oldest publish date to keep for ``report_kind``."""
    as_year = int(as_of[:4])
    as_month = int(as_of[5:7])
    as_day = int(as_of[8:10])
    years = (
        window.annual_lookback_years if report_kind == "annual" else window.interim_lookback_years
    )
    # Year arithmetic is calendar-year based; keep month/day stable so the
    # window is deterministic and matches frozen-packet PIT semantics.
    cutoff_year = as_year - years
    try:
        return datetime(cutoff_year, as_month, as_day, tzinfo=UTC).date().isoformat()
    except ValueError:
        # Feb 29 → Feb 28 in non-leap cutoff year.
        return datetime(cutoff_year, as_month, 28, tzinfo=UTC).date().isoformat()


def _discover_historical_feed(
    scraper: Any,
    mfn_slug: str,
    *,
    as_of: str,
    window: ReportHistoryWindow,
    today_iso: str,
) -> list[dict[str, Any]]:
    """Collect paginated report feed within ``window``.

    Prefers ``discover_feed_paginated(offset, limit)`` (JSON feed) when
    available; falls back to repeated ``discover_feed(page=)`` for
    backward-compatible fakes.  Filtering mirrors ``_report_identity_seed``
    (report-only) and applies PIT/window cutoffs before detail fetch.
    """
    paginated = getattr(scraper, "discover_feed_paginated", None)
    # New path: offset/limit JSON pagination.
    if callable(paginated):
        out: list[dict[str, Any]] = []
        offset = 0
        limit = window.limit_per_offset
        for _ in range(window.max_offsets):
            try:
                articles, next_offset = paginated(
                    mfn_slug, offset=offset, limit=limit, reports_only=True
                )
            except TypeError:
                # Older fake scraper signature without reports_only.
                articles, next_offset = paginated(mfn_slug, offset=offset, limit=limit)
            if not articles and next_offset is None:
                break
            # Window filter: keep anything that *could* be in the future
            # window; rely on the later PIT gate for final published_at check.
            deepest_cutoff = _resolve_cutoff(as_of, window, "annual")
            page_max: str | None = None
            for art in articles:
                pub = str(art.get("published_at") or "")[:10]
                # When published_at is None (feed-card without timestamp),
                # keep it — detail page is authoritative.
                if pub:
                    if page_max is None or pub > page_max:
                        page_max = pub
                    kind = art.get("report_kind")
                    cutoff = _resolve_cutoff(
                        as_of, window, kind if kind in {"annual", "quarterly"} else None
                    )
                    if pub < cutoff or pub > as_of[:10] or pub > today_iso:
                        continue
                out.append(art)
                if len(out) >= window.max_detail_fetches:
                    return out
            if page_max is not None and page_max < deepest_cutoff:
                return out
            if next_offset is None:
                break
            offset = next_offset
        return out
    # Fallback: legacy page-based HTML discovery.  Preserve the Sunday
    # page-2 backstop for fakes that implement ``page=`` pagination (the
    # weekly sweep that catches FY reports pushed off page 1).
    try:
        feed = scraper.discover_feed(mfn_slug, reports_only=True)
        # Sunday page-2: extend with page=2 when the fake supports it.
        try:
            # Weekday derived from today_iso (YYYY-MM-DD) avoids needing now.
            y, m, d = (int(part) for part in today_iso.split("-"))
            wd = datetime(y, m, d, tzinfo=UTC).weekday()
            if wd == 6:
                feed = list(feed) + _discover_feed_page(scraper, mfn_slug, 2)
        except Exception:
            pass
    except Exception:
        return []
    # Window-filter the legacy feed as well.
    filtered: list[dict[str, Any]] = []
    for art in feed:
        pub = str(art.get("published_at") or "")[:10]
        if pub:
            deepest = _resolve_cutoff(as_of, window, "annual")
            if pub < deepest or pub > as_of[:10] or pub > today_iso:
                if pub and pub < deepest:
                    continue
                # page-based feed is single-page, so no early break needed
                continue
        filtered.append(art)
    return filtered


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
                        mark_evidence_packets_unusable(
                            self.conn,
                            company_id=company_id,
                            as_of=as_of,
                            reason=f"failed:unhandled_error:{exc}",
                            commit=False,
                        )
                        record_job(
                            self.conn,
                            "evidence",
                            company_id=company_id,
                            borsdata_id=(
                                int(row["borsdata_id"]) if row["borsdata_id"] is not None else None
                            ),
                            status="failed",
                            error={
                                "code": "unhandled_error",
                                "message": str(exc),
                                "as_of": as_of,
                            },
                            begin_attempt=False,
                        )
                except Exception:
                    # Preserve the original exception.  The normal terminal
                    # paths use the same mark-then-job transaction below.
                    try:
                        self.conn.rollback()
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
        window = DEFAULT_HISTORY_WINDOW
        active_rules = report_rules_metadata()
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
            if result.status != "complete":
                # Mark every prior packet for this point-in-time key before the
                # terminal job update.  The job commit then makes the two
                # writes one transaction while retaining packet audit rows.
                mark_evidence_packets_unusable(
                    self.conn,
                    company_id=company_id,
                    as_of=as_of,
                    reason=f"incomplete_run:{result.status}",
                    commit=False,
                )
            if result.status in {"complete", "no_evidence"}:
                job_status = "success" if result.status == "complete" else "partial"
            else:
                job_status = "failed"
            error = None
            if result.status != "complete":
                error = {
                    "code": result.status,
                    "message": result.message,
                    "as_of": as_of,
                    "no_evidence_reason": (
                        result.no_evidence_reason.value
                        if result.no_evidence_reason is not None
                        else None
                    ),
                    "diagnostic": result.diagnostic(),
                }
            persist_evidence_diagnostic(
                self.conn,
                company_id=company_id,
                as_of=as_of,
                status=result.status,
                diagnostic=result.diagnostic(),
                report_rules_fingerprint=active_rules["fingerprint"],
                packet_hash=result.packet_hash,
                commit=False,
            )
            record_job(
                self.conn,
                "evidence",
                company_id=company_id,
                borsdata_id=int(borsdata_id) if borsdata_id is not None else None,
                status=job_status,
                error=error,
                begin_attempt=False,
            )
            persisted = describe_evidence_state(
                self.conn,
                company_id=company_id,
                as_of=as_of,
                current_rules_fingerprint=active_rules["fingerprint"],
            )
            if persisted is not None:
                result._persisted_diagnostic = persisted
            return result

        mapping = get_verified_mfn_mapping(self.conn, company_id)
        if mapping is None:
            # A reviewed `ambiguous` mapping is authoritative until re-reviewed:
            # later exact-identifier discovery is queued as candidates, never
            # applied. Fresh discovery keeps working for never-reviewed issuers.
            review = get_mfn_mapping_review(self.conn, company_id)
            reviewed_evidence = review.get("identity_evidence") if review is not None else None
            is_reviewed_ambiguous = (
                review is not None
                and review.get("status") == "ambiguous"
                and isinstance(reviewed_evidence, dict)
                and reviewed_evidence.get("reviewed") is True
            )
            if is_reviewed_ambiguous:
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
        today = now.date()
        # Bounded historical retrieval: paginated offset/limit feed under
        # the authoritative history window, with the single-page plus
        # Sunday page-2 contract when paginated discovery is unavailable.
        try:
            if hasattr(self.scraper, "discover_feed_paginated"):
                feed = _discover_historical_feed(
                    self.scraper,
                    mapping["mfn_slug"],
                    as_of=as_of,
                    window=window,
                    today_iso=today.isoformat(),
                )
                # Preserve Sunday page-2 as an additional sweep only when
                # the paginated path returned a small page (HTML fallback).
                if now.weekday() == 6 and len(feed) < window.limit_per_offset:
                    try:
                        extra = _discover_feed_page(self.scraper, mapping["mfn_slug"], 2)
                        # Dedupe extra into feed preserving order.
                        seen_extra = {
                            e if isinstance(e, str) else e.get("url") or e.get("source_url")
                            for e in feed
                        }
                        for e in extra:
                            u = e if isinstance(e, str) else e.get("url") or e.get("source_url")
                            if u not in seen_extra:
                                feed.append(e)
                    except Exception:
                        pass
            else:
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
        from alphaforge.providers.mfn.scraper import _cis_release_issuer, _issuer_token

        issuer_token = _issuer_token(str(mapping["mfn_slug"]))
        early_skips: dict[str, int] = {}
        for reason, count in _drain_skips(self.scraper, "drain_discovery_skips").items():
            early_skips[reason] = early_skips.get(reason, 0) + count
        unique_feed: list[dict[str, Any]] = []
        seen_feed_urls: set[str] = set()
        for entry in feed:
            url = entry if isinstance(entry, str) else entry.get("url") or entry.get("source_url")
            if url and url in seen_feed_urls:
                continue
            if url:
                seen_feed_urls.add(url)
            release_issuer = _cis_release_issuer(str(url or ""))
            if release_issuer is not None and release_issuer.lower() != issuer_token:
                # A /cis/a/ page bound to a different issuer must never enter
                # this issuer's lane — drop it before any detail fetch.
                early_skips["issuer_mismatch"] = early_skips.get("issuer_mismatch", 0) + 1
                continue
            unique_feed.append(entry)
        unseen_feed = []
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
                    if complete.get("report_rules_fingerprint") == active_rules[
                        "fingerprint"
                    ] and _has_current_attachment_provenance(complete.get("raw_metadata")):
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
        for reason, count in _drain_skips(self.scraper, "drain_detail_skips").items():
            early_skips[reason] = early_skips.get(reason, 0) + count
        result = EvidenceFlowResult(
            "dry_run" if dry_run else "running",
            company_id,
            mapping_status="mapped",
            discovered=len(details),
            skipped=dict(early_skips),
        )
        if future_dated_complete_release:
            result.skipped["future_dated_release"] = 1
        if not_yet_published_complete_release:
            result.skipped["not_yet_published_release"] = 1
        eligible: list[dict[str, Any]] = []
        pre_cutoff_report = False
        hard_blocks = 0
        for article in details:
            title = article.get("title") or ""
            if not is_report(title):
                result.skipped["non_report_release"] = (
                    result.skipped.get("non_report_release", 0) + 1
                )
                continue
            if is_invitation_or_presentation(title):
                # Backstop for scrapers that bypass the detail-page guard:
                # invitations about reports are never report evidence.
                result.skipped["invitation_or_presentation_release"] = (
                    result.skipped.get("invitation_or_presentation_release", 0) + 1
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
            kind = article.get("report_kind")
            cutoff = _resolve_cutoff(
                as_of, window, kind if kind in {"annual", "quarterly"} else None
            )
            if published_date < cutoff:
                result.skipped["pre_cutoff_release"] = (
                    result.skipped.get("pre_cutoff_release", 0) + 1
                )
                continue
            release_url = str(article.get("url") or article.get("source_url") or "")
            issuer_failure = _confirm_cis_issuer(
                release_url, article.get("canonical_url"), issuer_token=issuer_token
            )
            if issuer_failure is not None:
                # A /cis/a/ page without MFN canonical confirmation binding
                # it to the resolved issuer blocks the lane visibly instead
                # of persisting possibly-foreign evidence.
                result.skipped[issuer_failure] = result.skipped.get(issuer_failure, 0) + 1
                hard_blocks += 1
                continue
            if article.get("attachment_tier") == "unresolved":
                # Ranked selection refused to guess between attachments. The
                # article never downloads, but its in-window report group
                # stays in the expected coverage denominator as unretained.
                result.skipped[AMBIGUOUS_SELECTION_SKIP_REASON] = (
                    result.skipped.get(AMBIGUOUS_SELECTION_SKIP_REASON, 0) + 1
                )
                hard_blocks += 1
                continue
            pre_cutoff_report = True
            tier = article.get("attachment_tier")
            if tier in ATTACHMENT_TIERS:
                result.attachment_selection[tier] = result.attachment_selection.get(tier, 0) + 1
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
            if not (article.get("attachment_url") or article.get("storage_url")):
                article["_pdf_language_unresolved"] = True
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
            if persisted.get("report_rules_fingerprint") != active_rules[
                "fingerprint"
            ] or not _has_current_attachment_provenance(metadata):
                continue
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
                    "attachment_tier": metadata.get("attachment_tier"),
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
        indeterminate_pdf_cache: dict[str, tuple[PdfDownload, Any, str, str]] = {}
        stored_pdf_language_cache: dict[str, tuple[str, str]] = {}
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
            existing = find_complete_evidence_attachment(self.conn, str(attachment_url), company_id)
            if (
                existing is not None
                and existing.get("canonical_report_rules_fingerprint")
                != active_rules["fingerprint"]
            ):
                stored_pdf_language = None
            else:
                stored_pdf_language = _stored_pdf_language(existing)
            if stored_pdf_language is not None:
                language, evidence = stored_pdf_language
                identity_candidates[index] = {
                    **candidate,
                    "pdf_language": language,
                    "language_evidence": evidence,
                    "ingested_lang": language,
                    "lang": language,
                    "_pdf_language_unresolved": False,
                }
                stored_pdf_language_cache[str(attachment_url)] = stored_pdf_language
                continue
            try:
                candidate_download = download_pdf(str(attachment_url), limits=self.limits)
                candidate_extracted = ingestion.extract_pdf_pages(
                    candidate_download.content, max_pages=self.limits.max_pages
                )
            except Exception:
                if stored_pdf_language is not None:
                    language, evidence = stored_pdf_language
                    identity_candidates[index] = {
                        **candidate,
                        "pdf_language": language,
                        "language_evidence": evidence,
                        "ingested_lang": language,
                        "lang": language,
                        "_pdf_language_unresolved": False,
                    }
                    stored_pdf_language_cache[str(attachment_url)] = stored_pdf_language
                else:
                    mark_pdf_language_unresolved(index, candidate, existing)
                continue
            if not candidate_extracted.pages:
                if stored_pdf_language is not None:
                    language, evidence = stored_pdf_language
                    identity_candidates[index] = {
                        **candidate,
                        "pdf_language": language,
                        "language_evidence": evidence,
                        "ingested_lang": language,
                        "lang": language,
                        "_pdf_language_unresolved": False,
                    }
                    stored_pdf_language_cache[str(attachment_url)] = stored_pdf_language
                else:
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
            if not (
                pdf_language in {"en", "sv"} and _is_pdf_backed_language_evidence(language_evidence)
            ):
                if release_lang.lower() in {"en", "sv"}:
                    indeterminate_pdf_cache[str(attachment_url)] = (
                        candidate_download,
                        candidate_extracted,
                        release_lang.lower(),
                        language_evidence,
                    )
                mark_pdf_language_unresolved(index, candidate, existing)
                continue
            resolved = {
                **candidate,
                "pdf_language": pdf_language,
                "pdf_checksum": candidate_download.sha256,
                "language_evidence": language_evidence,
                "ingested_lang": pdf_language,
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
        language_fallback_count = 0
        for group_index, article in enumerate(deduped):
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
                indeterminate_pdf = indeterminate_pdf_cache.get(str(attachment_url))
                if indeterminate_pdf is not None:
                    candidate_download, candidate_extracted, release_lang, language_evidence = (
                        indeterminate_pdf
                    )
                    prepared = {
                        **variant,
                        "pdf_language": "",
                        "pdf_checksum": candidate_download.sha256,
                        "language_evidence": language_evidence,
                        "ingested_lang": release_lang,
                        "_pdf_language_unresolved": True,
                    }
                    prepared_variants.append(prepared)
                    candidate_options.append(
                        (prepared, candidate_download, candidate_extracted, existing)
                    )
                    continue
                stored_pdf_language = stored_pdf_language_cache.get(str(attachment_url))
                if stored_pdf_language is not None:
                    language, language_evidence = stored_pdf_language
                    prepared = {
                        **variant,
                        "pdf_language": language,
                        "language_evidence": language_evidence,
                        "ingested_lang": language,
                        "lang": language,
                        "_pdf_language_unresolved": False,
                    }
                    prepared_variants.append(prepared)
                    candidate_options.append((prepared, None, None, existing))
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
                if not (
                    pdf_language in {"en", "sv"}
                    and _is_pdf_backed_language_evidence(language_evidence)
                ):
                    if release_lang.lower() not in {"en", "sv"}:
                        prepared_variants.append(variant)
                        failures["pdf_language_unresolved"] = (
                            failures.get("pdf_language_unresolved", 0) + 1
                        )
                        continue
                    prepared = {
                        **variant,
                        "pdf_language": "",
                        "pdf_checksum": candidate_download.sha256,
                        "language_evidence": language_evidence,
                        "ingested_lang": release_lang.lower(),
                        "_pdf_language_unresolved": True,
                    }
                    prepared_variants.append(prepared)
                    candidate_options.append(
                        (prepared, candidate_download, candidate_extracted, None)
                    )
                    continue
                prepared = {
                    **variant,
                    "pdf_language": pdf_language,
                    "pdf_checksum": candidate_download.sha256,
                    "language_evidence": language_evidence,
                    "ingested_lang": pdf_language,
                    "_pdf_language_unresolved": False,
                }
                if existing is not None and existing.get("canonical_source_url"):
                    unresolved_existing_source_urls.discard(str(existing["canonical_source_url"]))
                prepared_variants.append(prepared)
                candidate_options.append((prepared, candidate_download, candidate_extracted, None))
            variants = prepared_variants

            def persist_option(
                article: dict[str, Any],
                candidate_download: PdfDownload,
                candidate_extracted: Any,
                siblings: list[dict[str, Any]],
            ) -> None:
                nonlocal language_fallback_count
                if article.get("_pdf_language_unresolved"):
                    language_fallback_count += 1
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
                    report_rules_fingerprint=active_rules["fingerprint"],
                )
                article["_persisted_evidence"] = True

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
                    if not any(variant.get("_persisted_evidence") for variant in variants):
                        result.skipped["attachment_reused_by_different_report"] = (
                            result.skipped.get("attachment_reused_by_different_report", 0) + 1
                        )
                        continue
                    existing_canonical_source_url = str(selected_existing["canonical_source_url"])
                    if selected.get("_persisted_evidence"):
                        result.persisted += 1
                        persisted_tier = selected.get("attachment_tier")
                        if persisted_tier in ATTACHMENT_TIERS:
                            result.attachment_selection[persisted_tier] = (
                                result.attachment_selection.get(persisted_tier, 0) + 1
                            )
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
        selection_manifest = load_evidence_selection_manifest(
            self.conn,
            company_id=company_id,
            as_of=as_of,
            report_rules=active_rules,
            candidate_records=identity_candidates,
            publication_cutoff=today.isoformat(),
            excluded_source_urls=unresolved_existing_source_urls,
        )
        if not dry_run:
            persist_evidence_selection_manifest(
                self.conn, selection_manifest, commit=False
            )
        result.completeness = manifest_completeness(selection_manifest)
        expected = {
            report_class: counts["expected"]
            for report_class, counts in result.completeness.items()
        }
        retained = {
            report_class: counts["retained"]
            for report_class, counts in result.completeness.items()
        }
        missing = {
            report_class: expected[report_class] - retained.get(report_class, 0)
            for report_class in expected
            if expected[report_class] > retained.get(report_class, 0)
        }
        if hard_blocks or missing:
            # Completeness is a hard gate: one stray retained PDF must not
            # mark the lane ready when expected annual/quarterly coverage is
            # incomplete, and a refused guess must fail visibly. No packet is
            # frozen on this path; persisted documents remain for the next run.
            parts = [
                f"{report_class} expected {expected[report_class]}"
                f" retained {retained.get(report_class, 0)}"
                for report_class in sorted(missing)
            ]
            if hard_blocks:
                parts.append(f"{hard_blocks} blocked identity/selection check(s)")
            return finish(
                EvidenceFlowResult(
                    "evidence_incomplete",
                    company_id,
                    mapping_status="mapped",
                    discovered=result.discovered,
                    eligible=result.eligible,
                    downloaded=result.downloaded,
                    persisted=result.persisted,
                    skipped=result.skipped,
                    completeness=result.completeness,
                    attachment_selection=result.attachment_selection,
                    message="incomplete report history: " + "; ".join(parts),
                )
            )
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
        if language_fallback_count:
            packet_limitations.append(f"pdf_language_fallback:{language_fallback_count}")
        for tier in ATTACHMENT_TIERS:
            if tier in ("unresolved", "none"):
                continue
            tier_count = result.attachment_selection.get(tier, 0)
            if tier_count:
                packet_limitations.append(f"attachment_selection_{tier}:{tier_count}")
        # Freeze the diagnostic into the packet before hashing.  The
        # repository can therefore reproduce the same CLI result after the
        # in-memory flow has gone away.
        result.status = "complete"
        packet = build_frozen_evidence_packet(
            self.conn,
            company_id=company_id,
            as_of=as_of,
            mapping=mapping,
            additional_limitations=packet_limitations,
            publication_cutoff=today.isoformat(),
            excluded_source_urls=unresolved_existing_source_urls,
            report_rules=active_rules,
            evidence_diagnostic=result.diagnostic(),
            selection_manifest=selection_manifest,
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
                    persisted=result.persisted,
                    skipped=result.skipped,
                    completeness=result.completeness,
                    attachment_selection=result.attachment_selection,
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
                    persisted=result.persisted,
                    skipped=result.skipped,
                    completeness=result.completeness,
                    attachment_selection=result.attachment_selection,
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
        persist_evidence_selection_manifest(
            self.conn,
            selection_manifest,
            packet_hash=packet["packet_hash"],
            commit=False,
        )
        result.status = "complete"
        result.packet = packet
        result.packet_hash = packet["packet_hash"]
        return finish(result)
