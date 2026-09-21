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


def _issuer_token(value: Any) -> str:
    text = str(value).casefold().strip("/")
    segments = [segment for segment in text.split("/") if segment]
    for marker in ("a", "cision"):
        if marker in segments:
            index = segments.index(marker)
            if index + 1 < len(segments):
                return segments[index + 1]
    return text


def _issuer_identity(doc: dict[str, Any]) -> str:
    """Return the stable issuer token used by report identity fallbacks."""
    explicit = doc.get("mfn_slug") or doc.get("slug") or doc.get("company_id")
    if explicit not in (None, ""):
        return _issuer_token(explicit)
    source_url = str(doc.get("source_url") or doc.get("url") or "")
    parsed = urlparse(source_url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    for marker in ("a", "cision"):
        if marker in segments:
            index = segments.index(marker)
            if index + 1 < len(segments):
                return segments[index + 1].casefold()
    return ""


def _report_kind_for_identity(doc: dict[str, Any]) -> str:
    kind = str(doc.get("report_kind") or "").casefold().strip()
    if kind:
        return kind
    title = str(doc.get("title") or "").casefold()
    if any(term in title for term in ("annual", "year-end", "year end", "årsredovis", "bokslut")):
        return "annual"
    if any(
        term in title
        for term in ("quarter", "interim", "delårs", "kvartals", "q1", "q2", "q3", "q4")
    ):
        return "quarterly"
    return ""


def _title_kind_for_identity(title: str, kind: str) -> str:
    title = title.casefold()
    if "årsredovisning" in title or "annual report" in title:
        return "annual_report"
    if "bokslutskommuniké" in title or "year-end report" in title or "year end report" in title:
        return "year_end_report"
    return kind


def _translation_neutral_title(doc: dict[str, Any], issuer: str) -> str:
    title = str(doc.get("title") or "").casefold()
    for token in re.split(r"[^\w]+", issuer):
        if token:
            title = re.sub(rf"\b{re.escape(token)}\b", " ", title)
    for month, number in {
        "january": 1,
        "januari": 1,
        "february": 2,
        "februari": 2,
        "march": 3,
        "mars": 3,
        "april": 4,
        "may": 5,
        "maj": 5,
        "june": 6,
        "juni": 6,
        "july": 7,
        "juli": 7,
        "august": 8,
        "augusti": 8,
        "september": 9,
        "october": 10,
        "oktober": 10,
        "november": 11,
        "december": 12,
    }.items():
        title = re.sub(rf"\b{re.escape(month)}\b", f"m{number}", title)
    for ordinal, quarter in (
        ("första|first", "q1"),
        ("andra|second", "q2"),
        ("tredje|third", "q3"),
        ("fjärde|fourth", "q4"),
    ):
        title = re.sub(
            rf"\b(?:{ordinal})\s+(?:kvartalet|quarter)\b",
            f" {quarter} ",
            title,
        )
    for word in (
        "interim",
        "quarterly",
        "quarter",
        "report",
        "delårsrapport",
        "delarsrapport",
        "kvartalsrapport",
        "rapport",
        "annual",
        "årsredovisning",
        "arsredovisning",
        "year",
        "end",
        "bokslutskommuniké",
        "bokslutskommunike",
        "publicerar",
        "offentliggör",
        "offentliggor",
        "has",
        "published",
        "its",
        "the",
        "för",
        "for",
        "ab",
        "aktiebolag",
        "ag",
        "corp",
        "corporation",
        "inc",
        "limited",
        "ltd",
        "nv",
        "plc",
        "sa",
    ):
        title = re.sub(rf"\b{re.escape(word)}\b", " ", title)
    return re.sub(r"[^a-z0-9]+", "-", title).strip("-")


def _quarter_period(text: str) -> str | None:
    text = text.casefold()
    for ordinal, quarter in (
        ("första|first", "q1"),
        ("andra|second", "q2"),
        ("tredje|third", "q3"),
        ("fjärde|fourth", "q4"),
    ):
        text = re.sub(
            rf"\b(?:{ordinal})\s+(?:kvartal(?:et)?|quarter)\b",
            f" {quarter} ",
            text,
        )
    text = re.sub(r"\b(?:january|januari)\s*[-–]\s*(?:march|mars)\b", "q1", text)
    text = re.sub(r"\bapril\s*[-–]\s*(?:june|juni)\b", "q2", text)
    text = re.sub(r"\b(?:july|juli)\s*[-–]\s*september\b", "q3", text)
    text = re.sub(r"\b(?:october|oktober)\s*[-–]\s*december\b", "q4", text)
    match = re.search(
        r"\bq\s*([1-4])\s*(?:fy\s*)?(20\d{2})(?:\s*[/\-]\s*(20\d{2}))?\b",
        text,
    )
    if match:
        end_year = match.group(3) or match.group(2)
        return f"{match.group(2)}/{end_year}-q{match.group(1)}"
    match = re.search(
        r"\b(20\d{2})(?:\s*[/\-]\s*(20\d{2}))?\s*[-/]?\s*q\s*([1-4])\b",
        text,
    )
    if match:
        end_year = match.group(2) or match.group(1)
        return f"{match.group(1)}/{end_year}-q{match.group(3)}"
    return None


def _fiscal_period(doc: dict[str, Any]) -> str:
    text = " ".join(str(doc.get(key) or "") for key in ("title", "content_text", "body"))
    explicit = next(
        (str(doc[key]).casefold().strip() for key in ("fiscal_period", "report_period", "period")
         if doc.get(key) not in (None, "")),
        None,
    )
    if explicit:
        normalized = _quarter_period(explicit)
        if normalized:
            return normalized
        title_period = _quarter_period(str(doc.get("title") or ""))
        if title_period:
            quarter = re.search(r"-q([1-4])$", title_period)
            years = re.fullmatch(r"(20\d{2})(?:[/\-](20\d{2}))?", explicit)
            if quarter and years:
                end_year = years.group(2) or years.group(1)
                return f"{years.group(1)}/{end_year}-q{quarter.group(1)}"
            return title_period
        return explicit
    title_period = _quarter_period(str(doc.get("title") or ""))
    if title_period:
        return title_period
    quarter_period = _quarter_period(text)
    if quarter_period:
        return quarter_period
    text = text.casefold()
    match = re.search(r"\b(?:h\s*([12])\s*)?(20\d{2})(?:\s*[/\-]\s*(20\d{2}))?\b", text)
    if match:
        end_year = match.group(3) or match.group(2)
        if match.group(1):
            return f"{match.group(2)}/{end_year}-h{match.group(1)}"
        return match.group(2)
    return ""


def canonical_release_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url.lower())
    segments = [segment for segment in parsed.path.split("/") if segment not in {"sv", "en"}]
    path = "/" + "/".join(segments)
    path = re.sub(r"[-_]\b(?:sv|en)\b(?=[-_/]|$)", "", path)
    return f"{parsed.netloc}{path}"


def _bilingual_identity_keys(doc: dict[str, Any]) -> set[str]:
    issuer = _issuer_identity(doc)
    kind = _report_kind_for_identity(doc)
    period = _fiscal_period(doc)
    published = str(doc.get("published_at") or "")[:10]
    quarter_period = re.fullmatch(r"(20\d{2})(?:/(20\d{2}))?-q([1-4])", period)
    keys: set[str] = set()
    if issuer and kind and quarter_period:
        keys.add(
            f"report-quarter:{issuer}|{kind}|"
            f"{quarter_period.group(1)}-q{quarter_period.group(3)}"
        )
    event_id = doc.get("provider_event_id") or doc.get("mfn_event_id")
    if event_id:
        keys.add(f"event:{event_id}")
    attachment_checksum = (
        doc.get("pdf_checksum") or doc.get("attachment_checksum") or doc.get("document_checksum")
    )
    if attachment_checksum:
        keys.add(f"pdf:{issuer}|{kind}|{period}|{attachment_checksum}")
    body = doc.get("content_text") or doc.get("body")
    body_checksum = ""
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
    title = str(doc.get("title") or "")
    title_kind = _title_kind_for_identity(title, kind)
    if issuer and period and title_kind:
        title_key = _translation_neutral_title(doc, issuer)
        if title_key:
            keys.add(f"report-title:{issuer}|{title_kind}|{period}|{title_key}")
    identity_token = str(event_id or attachment_checksum or body_checksum)
    if (issuer or period) and title_kind and period and identity_token:
        keys.add(f"report:{issuer}|{title_kind}|{period}|{identity_token}")
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

    def strong_keys(keys: set[str]) -> set[str]:
        return {
            key
            for key in keys
            if not key.startswith("attachment:") and not key.startswith("title:")
        }

    for doc in docs:
        keys = _bilingual_identity_keys(doc)
        strong = strong_keys(keys)
        owners = {find(key_groups[key]) for key in strong if key in key_groups}
        if not owners and not strong:
            weak_owners = {find(key_groups[key]) for key in keys if key in key_groups}
            owners = {
                owner
                for owner in weak_owners
                if not any(
                    strong_keys(_bilingual_identity_keys(variant)) for variant in groups[owner]
                )
            }
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
        group_keys = sorted(
            {key for variant in variants for key in _bilingual_identity_keys(variant)}
        )
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
