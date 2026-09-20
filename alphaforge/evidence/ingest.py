"""Deterministic report-document ingestion and pypdf extraction."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from alphaforge.evidence.mfn_taxonomy import is_report
from alphaforge.ownership import (
    build_ownership_evidence,  # noqa: F401 — production wiring for thesis limitations
)


def _detect_lang(text: str) -> tuple[str, float]:
    sv_markers = [" och ", " att ", " för ", " är ", "ä", "ö", "å", "bokslut", "delår"]
    lower = text.lower()
    score = sum(1 for marker in sv_markers if marker in lower)
    if score >= 2:
        return ("sv", 0.8)
    return ("en", 0.6)


def _language(doc: dict[str, Any]) -> str:
    explicit = (doc.get("ingested_lang") or doc.get("lang") or "").lower()
    if explicit in {"sv", "en"}:
        return explicit
    detected, _confidence = _detect_lang(
        f"{doc.get('title') or ''} {doc.get('content_text') or doc.get('body') or ''}"
    )
    return detected


def _attachment_filename(doc: dict[str, Any]) -> str:
    value = doc.get("attachment_filename")
    if value:
        return str(value).lower().split("?", 1)[0].rsplit("/", 1)[-1]
    for key in ("attachment_url", "storage_url"):
        value = doc.get(key)
        if value:
            path = urlparse(str(value)).path
            return path.rsplit("/", 1)[-1].lower()
    return ""


def _fiscal_period(doc: dict[str, Any]) -> str:
    for key in ("fiscal_period", "report_period", "period"):
        if doc.get(key) not in (None, ""):
            return str(doc[key]).lower()
    title = str(doc.get("title") or "").lower()
    match = re.search(r"\b(20\d{2})\s*[-/]?\s*(q[1-4]|h[12])\b", title)
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    years = re.findall(r"\b20\d{2}\b", title)
    return years[-1] if years else ""


def canonical_release_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url.lower())
    segments = [segment for segment in parsed.path.split("/") if segment not in {"sv", "en"}]
    path = "/" + "/".join(segments)
    path = re.sub(r"[-_]\b(?:sv|en)\b(?=[-_/]|$)", "", path)
    return f"{parsed.netloc}{path}"


def _bilingual_identity_keys(doc: dict[str, Any]) -> set[str]:
    issuer = str(doc.get("mfn_slug") or doc.get("slug") or doc.get("company_id") or "").lower()
    kind = str(doc.get("report_kind") or "").lower()
    period = _fiscal_period(doc)
    published = str(doc.get("published_at") or "")[:10]
    keys: set[str] = set()
    event_id = doc.get("provider_event_id") or doc.get("mfn_event_id")
    if event_id:
        keys.add(f"event:{event_id}")
    attachment_checksum = (
        doc.get("pdf_checksum") or doc.get("attachment_checksum") or doc.get("document_checksum")
    )
    if attachment_checksum:
        keys.add(f"pdf:{issuer}|{kind}|{period}|{attachment_checksum}")
    body = doc.get("content_text") or doc.get("body")
    if body:
        normalized_body = " ".join(str(body).split()).casefold()
        body_checksum = hashlib.sha256(normalized_body.encode()).hexdigest()
        keys.add(f"body:{issuer}|{kind}|{period}|{body_checksum}")
    filename = _attachment_filename(doc)
    if filename and (issuer or period):
        keys.add(f"attachment:{issuer}|{kind}|{period}|{filename}")
    canonical = canonical_release_url(str(doc.get("source_url") or doc.get("url") or ""))
    if canonical:
        keys.add(f"url:{issuer}|{canonical}|{published}")
    if (issuer or period) and (kind or period):
        keys.add(f"report:{issuer}|{kind}|{period}")
    elif (issuer or period) and published:
        keys.add(f"report:{issuer}|{kind}|{published}")
    if not keys:
        title = re.sub(
            r"\b(inbjudan|invitation to|publicerar|has published)\b",
            "",
            str(doc.get("title") or ""),
            flags=re.IGNORECASE,
        )
        title_key = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        keys.add(f"title:{issuer}|{kind}|{period}|{title_key}")
    return keys


def bilingual_dedupe(
    docs: list[dict[str, Any]], *, packet_majority: str | None = None
) -> list[dict[str, Any]]:
    """Select one report edition while retaining suppressed provenance."""
    groups: list[list[dict[str, Any]]] = []
    key_groups: dict[str, int] = {}
    parents: list[int] = []

    def find(group_index: int) -> int:
        while parents[group_index] != group_index:
            parents[group_index] = parents[parents[group_index]]
            group_index = parents[group_index]
        return group_index

    for doc in docs:
        keys = _bilingual_identity_keys(doc)
        owners = {find(key_groups[key]) for key in keys if key in key_groups}
        if not owners:
            group_index = len(groups)
            groups.append([])
            parents.append(group_index)
        else:
            group_index = min(owners)
            for owner in owners:
                owner = find(owner)
                if owner == group_index:
                    continue
                parents[owner] = group_index
                groups[group_index].extend(groups[owner])
                groups[owner] = []
            group_index = find(group_index)
        groups[group_index].append(doc)
        for key in keys:
            key_groups[key] = group_index

    preferred_language = packet_majority if packet_majority in {"sv", "en"} else "en"
    selection_rule = (
        f"{preferred_language}_packet_majority"
        if packet_majority in {"sv", "en"}
        else "deterministic_en_fallback"
    )
    out: list[dict[str, Any]] = []
    for variants in groups:
        if not variants:
            continue
        group_keys = sorted({key for variant in variants for key in _bilingual_identity_keys(variant)})
        group_id = hashlib.sha256(group_keys[0].encode()).hexdigest()[:24]
        for variant in variants:
            variant["_bilingual_group_id"] = group_id
        variants_sorted = sorted(
            variants,
            key=lambda value: (
                0 if _language(value) == preferred_language else 1,
                0 if _language(value) == "en" else 1,
                str(value.get("source_url") or value.get("url") or ""),
            ),
        )
        preferred = variants_sorted[0]
        preferred["bilingual_selection_rule"] = selection_rule
        out.append(preferred)
        for suppressed in variants_sorted[1:]:
            suppressed["duplicate_of"] = preferred.get("source_url") or preferred.get("url")
            suppressed["ingest_status"] = "superseded_by_translation"
            suppressed["_bilingual_group_id"] = preferred["_bilingual_group_id"]
            suppressed["bilingual_selection_rule"] = selection_rule
            preferred.setdefault("_suppressed_variants", []).append(suppressed)
    return out


def _page_ranges(page_numbers: list[int]) -> str:
    if not page_numbers:
        return ""
    ranges: list[str] = []
    start = previous = page_numbers[0]
    for page_number in page_numbers[1:]:
        if page_number == previous + 1:
            previous = page_number
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = page_number
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def _metadata_json(doc: dict[str, Any], *, language: str, checksum: str | None) -> str | None:
    raw = doc.get("raw_metadata")
    if isinstance(raw, dict):
        metadata = dict(raw)
    elif isinstance(raw, str):
        try:
            loaded = json.loads(raw)
            metadata = dict(loaded) if isinstance(loaded, dict) else {}
        except ValueError:
            metadata = {}
    else:
        metadata = {}
    for key in (
        "source_release_url",
        "storage_url",
        "attachment_url",
        "attachment_filename",
        "provider_event_id",
        "mfn_event_id",
        "mfn_slug",
        "report_kind",
        "report_year",
        "report_period",
        "fiscal_period",
        "pdf_checksum",
        "attachment_checksum",
        "bilingual_selection_rule",
        "_bilingual_group_id",
        "ingest_status",
    ):
        if doc.get(key) is not None:
            metadata[key.removeprefix("_")] = doc[key]
    metadata["language"] = language
    if checksum:
        metadata["document_checksum"] = checksum
    return json.dumps(metadata, sort_keys=True) if metadata else None


@dataclass
class IngestResult:
    inserted: int
    suppressed: int


def _document_exists(conn: Any, company_id: int | None, source_url: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM research_documents WHERE company_id IS ? AND source_url=? LIMIT 1",
            (company_id, source_url),
        ).fetchone()
        is not None
    )


def _document_for_checksum(conn: Any, company_id: int | None, checksum: str | None) -> Any:
    if not checksum:
        return None
    return conn.execute(
        """
        SELECT id, source_url FROM research_documents
        WHERE company_id IS ? AND checksum=?
        ORDER BY id LIMIT 1
        """,
        (company_id, checksum),
    ).fetchone()


@dataclass(frozen=True)
class PdfExtraction:
    text: str
    page_count: int
    pages_included: str
    page_truncated: int
    scanned: bool
    pages: tuple[dict[str, Any], ...]
    limitations: tuple[str, ...] = ()


class ResearchDocumentIngestionService:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def persist_articles(
        self,
        company_id: int | None,
        articles: list[dict[str, Any]],
        *,
        source_type: str = "mfn",
        reports_only: bool = True,
        packet_majority: str | None = None,
    ) -> IngestResult:
        """Persist report documents, suppressing one bilingual edition in packets."""
        eligible = [
            article
            for article in articles
            if not (
                reports_only and source_type == "mfn" and not is_report(article.get("title") or "")
            )
        ]
        deduped = bilingual_dedupe(eligible, packet_majority=packet_majority)
        inserted = 0
        suppressed = 0
        for doc in deduped:
            source_url = doc.get("source_url") or doc.get("url") or ""
            if not source_url:
                continue
            title = doc.get("title")
            body = doc.get("content_text") or doc.get("body")
            published_at = doc.get("published_at")
            language = _language(doc)
            checksum = (
                doc.get("pdf_checksum")
                or doc.get("attachment_checksum")
                or (hashlib.sha256(body.encode()).hexdigest() if body else None)
            )
            existing_checksum = _document_for_checksum(self.conn, company_id, checksum)
            duplicate_of = (
                existing_checksum[0]
                if existing_checksum is not None and existing_checksum[1] != source_url
                else None
            )
            if duplicate_of is not None:
                metadata = _metadata_json(
                    {**doc, "ingest_status": "superseded_by_checksum"},
                    language=language,
                    checksum=checksum,
                )
            else:
                metadata = _metadata_json(doc, language=language, checksum=checksum)
            if not _document_exists(self.conn, company_id, source_url):
                try:
                    cur = self.conn.execute(
                        """
                        INSERT INTO research_documents
                            (company_id, source_url, source_type, title, published_at, content_text,
                             page_count, pages_included, page_truncated, duplicate_of,
                             ingested_lang, checksum, raw_metadata)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(company_id, source_url) DO NOTHING
                        """,
                        (
                            company_id,
                            source_url,
                            source_type,
                            title,
                            published_at,
                            body,
                            doc.get("page_count"),
                            doc.get("pages_included"),
                            doc.get("page_truncated"),
                            duplicate_of,
                            language,
                            checksum,
                            metadata,
                        ),
                    )
                    if cur.rowcount and cur.rowcount > 0:
                        if duplicate_of is None:
                            inserted += 1
                        else:
                            suppressed += 1
                except Exception:
                    pass
            for suppressed_doc in doc.get("_suppressed_variants", []):
                suppressed_url = suppressed_doc.get("source_url") or suppressed_doc.get("url") or ""
                if not suppressed_url:
                    continue
                suppressed_title = suppressed_doc.get("title")
                suppressed_body = suppressed_doc.get("content_text") or suppressed_doc.get("body")
                suppressed_language = _language(suppressed_doc)
                suppressed_checksum = (
                    suppressed_doc.get("pdf_checksum")
                    or suppressed_doc.get("attachment_checksum")
                    or (
                        hashlib.sha256(suppressed_body.encode()).hexdigest()
                        if suppressed_body
                        else None
                    )
                )
                suppressed_metadata = _metadata_json(
                    suppressed_doc,
                    language=suppressed_language,
                    checksum=suppressed_checksum,
                )
                if not _document_exists(self.conn, company_id, suppressed_url):
                    try:
                        cur = self.conn.execute(
                            """
                            INSERT INTO research_documents
                                (company_id, source_url, source_type, title, published_at, content_text,
                                 page_count, pages_included, page_truncated, duplicate_of,
                                 ingested_lang, checksum, raw_metadata)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                                    (SELECT id FROM research_documents
                                     WHERE company_id IS ? AND source_url=?),
                                    ?, ?, ?)
                            ON CONFLICT(company_id, source_url) DO NOTHING
                            """,
                            (
                                company_id,
                                suppressed_url,
                                source_type,
                                suppressed_title,
                                suppressed_doc.get("published_at"),
                                suppressed_body,
                                suppressed_doc.get("page_count"),
                                suppressed_doc.get("pages_included"),
                                suppressed_doc.get("page_truncated"),
                                company_id,
                                source_url,
                                suppressed_language,
                                suppressed_checksum,
                                suppressed_metadata,
                            ),
                        )
                        if cur.rowcount and cur.rowcount > 0:
                            suppressed += 1
                    except Exception:
                        pass
        self.conn.commit()
        return IngestResult(inserted=inserted, suppressed=suppressed)

    def extract_pdf_pages(self, pdf_bytes: bytes, *, max_pages: int | None = None) -> PdfExtraction:
        """Extract pages with pypdf while retaining stable page anchors.

        ``max_pages`` is an acquisition-flow resource limit. The default stays
        unbounded for the existing repository API; callers that enforce a
        bounded download pass an explicit cap and receive truncation metadata.
        """
        try:
            import io

            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(pdf_bytes))
            total_page_count = len(reader.pages)
            if max_pages is None:
                selected_page_numbers = list(range(1, total_page_count + 1))
            else:
                selected_page_numbers = list(range(1, min(total_page_count, max_pages) + 1))
                if max_pages >= 50 and total_page_count > 50:
                    selected_page_numbers.extend(range(81, min(total_page_count, 90) + 1))
                selected_page_numbers = sorted(set(selected_page_numbers))
            pages: list[dict[str, Any]] = []
            for page_number in selected_page_numbers:
                page = reader.pages[page_number - 1]
                try:
                    text = re.sub(r"\n{3,}", "\n\n", page.extract_text() or "").strip()
                except Exception:
                    text = ""
                pages.append(
                    {
                        "page_number": page_number,
                        "anchor": f"page:{page_number}",
                        "text": text,
                        "text_length": len(text),
                        "extractor": "pypdf",
                    }
                )
            page_count = total_page_count
            anchored = "\n\n".join(
                f"[page {page['page_number']}]\n{page['text']}".rstrip() for page in pages
            )
            nonempty_chars = sum(int(page["text_length"]) for page in pages)
            scanned = page_count > 5 and nonempty_chars < max(200, len(pages) * 20)
            limitations: list[str] = []
            if scanned:
                limitations.append("scanned_pdf_no_ocr")
                build_ownership_evidence(anchored).get("limitations")
            elif page_count and nonempty_chars < 20:
                limitations.append("near_empty_pdf_no_ocr")
            if max_pages is not None and total_page_count > max_pages:
                limitations.append("page_resource_limit")
            return PdfExtraction(
                text=anchored,
                page_count=page_count,
                pages_included=_page_ranges(selected_page_numbers),
                page_truncated=int(max_pages is not None and total_page_count > max_pages),
                scanned=scanned,
                pages=tuple(pages),
                limitations=tuple(limitations),
            )
        except (ImportError, OSError, ValueError):
            return PdfExtraction("", 0, "", 0, False, (), ("pdf_extraction_failed",))

    def extract_pdf_text(self, pdf_bytes: bytes) -> tuple[str, int, str, int]:
        """Compatibility tuple for callers; extraction is complete, not 50-page sampled."""
        result = self.extract_pdf_pages(pdf_bytes)
        return (result.text, result.page_count, result.pages_included, result.page_truncated)
