"""Deterministic report-document ingestion and pypdf extraction."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from alphaforge.evidence.mfn_taxonomy import document_type, is_report
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


def _language_unresolved(doc: dict[str, Any]) -> bool:
    evidence = str(doc.get("language_evidence") or "")
    return bool(doc.get("_pdf_language_unresolved")) or evidence.startswith("release_hint:")


def _language(doc: dict[str, Any], pdf_language: str | None = None) -> str:
    # The PDF itself outranks MFN release-language metadata: an explicit PDF
    # decision (filename marker or first-pages word scoring) always wins.
    if _language_unresolved(doc):
        return ""
    pdf = (pdf_language or doc.get("pdf_language") or "").lower()
    if pdf in {"sv", "en"}:
        return pdf
    explicit = (doc.get("ingested_lang") or doc.get("lang") or "").lower()
    if explicit in {"sv", "en"}:
        return explicit
    detected, _confidence = _detect_lang(
        f"{doc.get('title') or ''} {doc.get('content_text') or doc.get('body') or ''}"
    )
    return detected


# Deterministic Swedish-vs-English word lists for PDF language detection.
# The extracted PDF language has greater authority than MFN release metadata.
EN_PDF_WORDS = (
    "the",
    "and",
    "for",
    "of",
    "year",
    "report",
    "with",
    "from",
    "this",
    "quarter",
    "financial",
    "revenue",
    "profit",
    "million",
)
PDF_LANGUAGE_MIN_HITS = 2

SV_PDF_WORDS = (
    "och",
    "att",
    "för",
    "av",
    "året",
    "rapport",
    "med",
    "från",
    "till",
    "som",
    "inte",
    "samt",
    "eller",
    "bokslut",
    "delår",
    "kvartalet",
    "omsättning",
    "miljoner",
    "kronor",
)


def _pdf_filename_language(filename: str) -> str:
    stem = str(filename or "").lower().split("?", 1)[0].rsplit("/", 1)[-1]
    if re.search(r"(?:^|[-_.\s])(en|eng|english)(?:[-_.\s]|$)", stem):
        return "en"
    if re.search(r"(?:^|[-_.\s])(sv|swe|swedish|svenska)(?:[-_.\s]|$)", stem):
        return "sv"
    return ""


def _word_hits(text: str, words: tuple[str, ...]) -> int:
    tokens = set(re.findall(r"\w+", text.casefold()))
    return sum(1 for word in words if word in tokens)


def _detect_pdf_language(text: str) -> tuple[str, str]:
    """Decide PDF language from first-page word hits or return a named fallback case."""
    if not str(text or "").strip():
        return "", "pdf_text_empty"
    sv_hits = _word_hits(text, SV_PDF_WORDS)
    en_hits = _word_hits(text, EN_PDF_WORDS)
    if max(sv_hits, en_hits) < PDF_LANGUAGE_MIN_HITS:
        return "", f"pdf_text_insufficient:sv={sv_hits},en={en_hits}"
    if sv_hits == en_hits:
        return "", f"pdf_text_tie:sv={sv_hits},en={en_hits}"
    evidence = f"pdf_text:sv={sv_hits},en={en_hits}"
    if sv_hits > en_hits:
        return "sv", evidence
    return "en", evidence


def resolve_document_language(
    doc: dict[str, Any] | None = None,
    *,
    filename: str = "",
    pdf_text: str = "",
    release_title: str = "",
    release_body: str = "",
    release_lang: str = "",
) -> tuple[str, str]:
    """Decide document language with PDF authority over release metadata."""
    if doc is not None:
        filename = filename or _attachment_filename(doc)
        release_title = release_title or str(doc.get("title") or "")
        release_body = release_body or str(doc.get("content_text") or doc.get("body") or "")
        release_lang = release_lang or str(doc.get("lang") or doc.get("ingested_lang") or "")
    filename_lang = _pdf_filename_language(filename)
    if filename_lang:
        return filename_lang, "filename"
    pdf_lang, pdf_evidence = _detect_pdf_language(pdf_text)
    if pdf_lang:
        return pdf_lang, pdf_evidence
    fallback_evidence = f"release_hint:{pdf_evidence}"
    if release_lang.lower() in {"sv", "en"}:
        return release_lang.lower(), fallback_evidence
    detected, _confidence = _detect_lang(f"{release_title} {release_body}".strip())
    return detected, fallback_evidence


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


def _document_type_for_identity(doc: dict[str, Any]) -> str:
    """Return the additive variant-identity type, preferring an explicit key."""
    explicit = str(doc.get("document_type") or "").strip().upper()
    if explicit in {
        "INTERIM_Q1",
        "INTERIM_Q2",
        "INTERIM_Q3",
        "YEAR_END_REPORT",
        "ANNUAL_REPORT",
    }:
        return explicit
    derived = document_type(str(doc.get("title") or ""))
    return derived or ""


_DOCUMENT_TYPE_IDENTITY = {
    "INTERIM_Q1": "quarterly",
    "INTERIM_Q2": "quarterly",
    "INTERIM_Q3": "quarterly",
    "YEAR_END_REPORT": "year_end",
    "ANNUAL_REPORT": "annual",
}


def _report_kind_for_identity(doc: dict[str, Any]) -> str:
    typed = _document_type_for_identity(doc)
    if typed:
        # YEAR_END vs ANNUAL stay distinguishable so the two editions of one
        # fiscal year never merge as translations of each other.
        return _DOCUMENT_TYPE_IDENTITY[typed]
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


def _translation_neutral_title(doc: dict[str, Any], issuer: str) -> str:
    title = str(doc.get("title") or "").casefold()
    for token in re.split(r"[^\w]+", issuer):
        if token:
            title = re.sub(rf"\b{re.escape(token)}\b", " ", title)
    for quarter, month_start, month_end in (
        ("q1", "january|januari", "march|mars"),
        ("q1", "may|maj", "july|juli"),
        ("q2", "april", "june|juni"),
        ("q2", "august|augusti", "october|oktober"),
        ("q3", "july|juli", "september"),
        ("q4", "october|oktober", "december"),
    ):
        title = re.sub(
            rf"\b(?:\d{{1,2}}\s+)?(?:{month_start})\s*[-–]\s*"
            rf"(?:\d{{1,2}}\s+)?(?:{month_end})\b",
            f" {quarter} ",
            title,
        )
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


def _expand_fiscal_year(start_year: str, end_part: str | None) -> str:
    """Normalize a fiscal-range end year, accepting two-digit ends (2026/27)."""
    if not end_part:
        return start_year
    if len(end_part) == 4:
        return end_part
    expanded = f"{start_year[:2]}{end_part}"
    if int(expanded) < int(start_year):
        expanded = str(int(expanded) + 100)
    return expanded


_NON_COVERED_FISCAL_CONTEXT = re.compile(
    r"\b(?:forecast|outlook|compared|comparison|previous|prognos|föregående|jämfört|jämförelse)\b",
    re.IGNORECASE,
)


def _quarter_period(text: str) -> str | None:
    text = text.casefold().replace(",", " ")
    text = re.sub(r"[‐‑‒–—]", "-", text)
    # Normalize in place: a later year-end comparator must not outrank coverage.
    text = re.sub(
        r"\b(?:year[- ]end\s+report|bokslutskommunik[eé])\s*[:–-]?\s*"
        r"(20\d{2})(?:\s*/\s*((?:20)?\d{2}))?\b",
        lambda match: f"q4 {match.group(1)}/{_expand_fiscal_year(match.group(1), match.group(2))}",
        text,
    )
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
    text = re.sub(
        r"\b(?:\d{1,2}\s+)?(?:may|maj)\s*[-–]\s*"
        r"(?:\d{1,2}\s+)?(?:july|juli)\s+(20\d{2})\b",
        lambda match: f"q1 {match.group(1)}/{int(match.group(1)) + 1}",
        text,
    )
    text = re.sub(r"\b(?:january|januari)\s*[-–]\s*(?:march|mars)\b", "q1", text)
    text = re.sub(r"\b(?:january|januari)\s*[-–]\s*(?:june|juni)\b", "q2", text)
    text = re.sub(r"\b(?:january|januari)\s*[-–]\s*september\b", "q3", text)
    text = re.sub(r"\b(?:january|januari)\s*[-–]\s*december\b", "q4", text)
    text = re.sub(r"\bapril\s*[-–]\s*(?:june|juni)\b", "q2", text)
    text = re.sub(r"\b(?:july|juli)\s*[-–]\s*september\b", "q3", text)
    text = re.sub(r"\b(?:october|oktober)\s*[-–]\s*december\b", "q4", text)
    quarter_first = re.search(
        r"\bq\s*([1-4])\s*[-–:]?\s*(?:fy\s*)?(20\d{2})(?:\s*[/\-]\s*((?:20)?\d{2}))?\b",
        text,
    )
    year_first = re.search(
        r"\b(20\d{2})(?:\s*[/\-]\s*((?:20)?\d{2}))?\s*[-/]?\s*q\s*([1-4])\b",
        text,
    )
    if quarter_first and (not year_first or quarter_first.start() < year_first.start()):
        end_year = _expand_fiscal_year(quarter_first.group(2), quarter_first.group(3))
        return f"{quarter_first.group(2)}/{end_year}-q{quarter_first.group(1)}"
    if year_first:
        end_year = _expand_fiscal_year(year_first.group(1), year_first.group(2))
        return f"{year_first.group(1)}/{end_year}-q{year_first.group(3)}"
    return None


def _year_period(text: str) -> str | None:
    """Preserve a single covered year/range, never invent its calendar dates."""
    periods = {
        f"{start}/{_expand_fiscal_year(start, end)}" if end else start
        for start, end in re.findall(r"\b(20\d{2})(?:\s*/\s*((?:20)?\d{2}))?\b", text)
    }
    return next(iter(periods)) if len(periods) == 1 else None


def resolve_fiscal_identity(doc: dict[str, Any]) -> tuple[str | None, str, str | None]:
    """Resolve covered identity, never a forecast/comparator or publication year.

    Provider fields outrank report titles; body fallback requires a report-labelled
    heading. Previously derived fields are outputs, not new provider assertions.
    Conflicting covered headings remain explicitly unresolved. No calendar dates
    are invented from quarter labels.
    """
    title = _NON_COVERED_FISCAL_CONTEXT.split(str(doc.get("title") or ""), maxsplit=1)[0]
    annual = doc.get("report_kind") == "annual"
    title_period = None if annual else _quarter_period(title)
    if not title_period and is_report(title):
        title_period = _year_period(title)
    explicit_key = next(
        (key for key in ("fiscal_period", "report_period", "period") if doc.get(key)), None
    )
    basis = str(doc.get("fiscal_period_source") or "")
    provider_input = (
        doc.get("fiscal_period_input")
        if basis.startswith("provider_metadata") or basis == "conflicting_provider_title"
        else None
    )
    if provider_input:
        explicit_key = doc.get("fiscal_period_input_key") or (
            basis.removeprefix("provider_metadata:").split("+")[0]
            if basis.startswith("provider_metadata:")
            else "fiscal_period"
        )
    if explicit_key and (provider_input or not basis or basis.startswith("provider_metadata")):
        value = str(provider_input or doc[explicit_key]).strip()
        normalized = _quarter_period(value) or value.casefold()
        if annual and re.fullmatch(r"20\d{2}\s*/\s*(?:20)?\d{2}", value):
            normalized = _year_period(value) or normalized
        if title_period and normalized != title_period:
            # A provider year and the same year's covered quarter are compatible.
            # A bare provider year does not prove a two-year annual range.
            if "-q" in title_period and normalized == title_period.split("/")[0]:
                return title_period, f"provider_metadata:{explicit_key}+title", None
            return None, "conflicting_provider_title", "fiscal_identity_ambiguous"
        return value, f"provider_metadata:{explicit_key}", None
    if title_period:
        return title_period, "report_title", None
    body = str(doc.get("content_text") or doc.get("body") or "")
    labelled = re.compile(
        r"\b(?:interim\s+report|quarterly\s+report|year[- ]end\s+report|"
        r"annual\s+report|delårsrapport|delarsrapport|kvartalsrapport|"
        r"bokslutskommunik[eé]|årsredovisning|arsredovisning)"
        r"\s*(?:for\b|för\b|[:–-])?\s*([^.!?\n]{0,100}?20\d{2}(?:\s*/\s*(?:20)?\d{2})?)",
        re.IGNORECASE,
    )
    periods: set[str] = set()
    for match in labelled.finditer(body):
        # Match the heading itself and its own sentence prefix, not a preceding
        # independent forecast/comparator sentence. Use the label's delimiters.
        sentence_start = max(body.rfind(mark, 0, match.start()) for mark in ".!?\n") + 1
        if _NON_COVERED_FISCAL_CONTEXT.search(body[sentence_start : match.end()]):
            continue
        heading = match.group(0)
        period = None if annual else _quarter_period(heading)
        if not period:
            period = _year_period(match.group(1))
        if period:
            periods.add(period)
    if len(periods) == 1:
        return next(iter(periods)), "covered_report_heading", None
    if len(periods) > 1:
        return None, "conflicting_covered_headings", "fiscal_identity_ambiguous"
    return None, "unresolved", "fiscal_identity_unresolved"


def _fiscal_period(doc: dict[str, Any]) -> str:
    if str(doc.get("fiscal_period_source") or "").startswith("conflicting_"):
        return ""  # An unresolved conflict cannot become a relation's period proof.
    return resolve_fiscal_identity(doc)[0] or ""


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
    keys: set[str] = set()
    event_id = doc.get("provider_event_id") or doc.get("mfn_event_id")
    if event_id:
        keys.add(f"event:{event_id}")
    attachment_checksum = (
        doc.get("pdf_checksum") or doc.get("attachment_checksum") or doc.get("document_checksum")
    )
    if attachment_checksum:
        keys.add(f"pdf:{attachment_checksum}")
    body = doc.get("content_text") or doc.get("body")
    if body:
        normalized_body = " ".join(str(body).split()).casefold()
        keys.add(f"body:{hashlib.sha256(normalized_body.encode()).hexdigest()}")
    canonical = canonical_release_url(str(doc.get("source_url") or doc.get("url") or ""))
    if canonical:
        keys.add(f"url:{canonical}")
    if issuer and kind and period:
        keys.add(f"period:{issuer}|{kind}|{period}")
    published = str(doc.get("published_at") or "")[:10]
    if published:
        keys.add(f"published:{published}")
    title = _translation_neutral_title(doc, issuer) if issuer else ""
    if title:
        keys.add(f"title:{title}")
    return keys


def _resolved_date_identity(doc: dict[str, Any]) -> str:
    for key in ("observation_date", "period_end", "report_period_end"):
        value = doc.get(key)
        if value not in (None, ""):
            return str(value)[:10]
    return ""


def _fiscal_year_config(doc: dict[str, Any]) -> str:
    value = doc.get("fiscal_year_start_month") or doc.get("fiscal_year_end_month")
    if value not in (None, ""):
        return str(value).casefold().strip()
    raw = doc.get("raw_metadata")
    if isinstance(raw, dict):
        value = raw.get("fiscal_year_start_month") or raw.get("fiscal_year_end_month")
        if value not in (None, ""):
            return str(value).casefold().strip()
    return ""


def _same_value(left: Any, right: Any) -> bool:
    return left not in (None, "") and left == right


def _numeric_key_figure_fingerprint(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    text = str(value)
    figures: list[str] = []
    token_pattern = re.compile(r"(?<![\w/])(?:\d{1,3}(?:[ .]\d{3})+|\d+(?:[.,]\d+)?)(?![\w/])")
    unit_aliases = {
        "%": "%",
        "sek": "currency",
        "msek": "currency",
        "mkr": "currency",
        "kr": "currency",
        "kronor": "currency",
        "tkr": "currency",
        "mdr": "currency",
        "eur": "currency",
        "meur": "currency",
        "usd": "currency",
        "miljon": "million",
        "miljoner": "million",
        "million": "million",
        "millions": "million",
        "mn": "million",
        "m": "million",
        "miljard": "billion",
        "miljarder": "billion",
        "billion": "billion",
        "billions": "billion",
        "bn": "billion",
    }
    for match in token_pattern.finditer(text):
        raw = match.group(0)
        compact = raw.replace(" ", "")
        if "," in compact and "." in compact:
            separator = "." if compact.rfind(".") > compact.rfind(",") else ","
            decimal = compact.rsplit(separator, 1)
            compact = decimal[0].replace(",", "").replace(".", "") + "." + decimal[1]
        elif "," in compact or "." in compact:
            separator = "," if "," in compact else "."
            before, after = compact.rsplit(separator, 1)
            compact = before + after if len(after) == 3 else before + "." + after
        try:
            number = f"{float(compact):g}"
        except ValueError:
            continue
        if number.isdigit() and len(number) == 4 and 1900 <= int(number) <= 2100:
            continue
        window = text[max(0, match.start() - 16) : min(len(text), match.end() + 16)].casefold()
        unit = ""
        for alias, normalized in unit_aliases.items():
            if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", window):
                unit = normalized
                break
        figures.append(f"{number}|{unit}")
    return tuple(sorted(figures)) if len(figures) >= 2 else ()


# Filename/metadata markers for a corrected or revised edition. A revised
# financial report is never merely another language edition.
REVISION_MARKERS = ("correct", "revis", "rättelse", "uppdaterad", "amend")


def _has_revision_markers(doc: dict[str, Any]) -> bool:
    filename = _attachment_filename(doc)
    haystacks = [
        filename,
        str(doc.get("title") or "").casefold(),
    ]
    raw = doc.get("raw_metadata")
    if isinstance(raw, dict):
        haystacks.extend(str(value).casefold() for value in raw.values())
    elif isinstance(raw, str):
        haystacks.append(raw.casefold())
    return any(marker in haystack for haystack in haystacks for marker in REVISION_MARKERS)


def _numeric_similarity(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    """Jaccard similarity over normalized numeric key-figure tokens."""
    union = set(left) | set(right)
    if not union:
        return 0.0
    return len(set(left) & set(right)) / len(union)


def _variant_relationship(left: dict[str, Any], right: dict[str, Any]) -> str:
    """Deterministic TRANSLATION / REVISION / DIFFERENT_REPORT label."""
    issuer = _issuer_identity(left)
    same_issuer = bool(issuer) and issuer == _issuer_identity(right)
    kind = _report_kind_for_identity(left)
    same_kind = bool(kind) and kind == _report_kind_for_identity(right)
    period_left = _fiscal_period(left)
    same_period = bool(period_left) and period_left == _fiscal_period(right)
    if same_issuer and same_kind and same_period:
        if _language_unresolved(left) or _language_unresolved(right):
            return "DIFFERENT_REPORT"
        if _language(left) == _language(right):
            if _has_revision_markers(left) or _has_revision_markers(right):
                return "REVISION"
            return "DIFFERENT_REPORT"
        if _has_revision_markers(left) or _has_revision_markers(right):
            return "REVISION"
        if _cross_language_correspondence(left, right):
            return "TRANSLATION"
    return "DIFFERENT_REPORT"


def _cross_language_correspondence(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_language = _language(left)
    right_language = _language(right)
    if not left_language or not right_language or left_language == right_language:
        return False
    issuer = _issuer_identity(left)
    if not issuer or issuer != _issuer_identity(right):
        return False
    kind = _report_kind_for_identity(left)
    if not kind or kind != _report_kind_for_identity(right):
        return False
    left_fiscal_config = _fiscal_year_config(left)
    right_fiscal_config = _fiscal_year_config(right)
    if left_fiscal_config and right_fiscal_config and left_fiscal_config != right_fiscal_config:
        return False
    if _has_revision_markers(left) or _has_revision_markers(right):
        # A corrected edition is never merely a translation; fail safe.
        return False
    strong_corroborator = False
    derived_corroborators = 0
    event_left = left.get("provider_event_id") or left.get("mfn_event_id")
    event_right = right.get("provider_event_id") or right.get("mfn_event_id")
    shared_event = _same_value(event_left, event_right)
    strong_corroborator |= shared_event
    checksum_left = left.get("pdf_checksum") or left.get("attachment_checksum")
    checksum_right = right.get("pdf_checksum") or right.get("attachment_checksum")
    shared_checksum = _same_value(checksum_left, checksum_right)
    strong_corroborator |= shared_checksum
    body_left = left.get("content_text") or left.get("body")
    body_right = right.get("content_text") or right.get("body")
    numeric_left = _numeric_key_figure_fingerprint(body_left)
    numeric_right = _numeric_key_figure_fingerprint(body_right)
    # Numeric similarity corroborates but never vetoes: translations of the
    # same report carry nearly identical figures in different prose.
    numeric_corroborator = _numeric_similarity(numeric_left, numeric_right) >= 0.5
    strong_corroborator |= numeric_corroborator
    if not shared_event and not shared_checksum and not numeric_corroborator:
        return False
    period_left = _fiscal_period(left)
    period_right = _fiscal_period(right)
    derived_corroborators += int(bool(period_left and period_left == period_right))
    date_left = _resolved_date_identity(left)
    date_right = _resolved_date_identity(right)
    derived_corroborators += int(bool(date_left and date_left == date_right))
    published_left = str(left.get("published_at") or "")[:10]
    published_right = str(right.get("published_at") or "")[:10]
    derived_corroborators += int(bool(published_left and published_left == published_right))
    title_left = _translation_neutral_title(left, issuer)
    title_right = _translation_neutral_title(right, issuer)
    derived_corroborators += int(bool(title_left and title_left == title_right))
    return strong_corroborator and derived_corroborators >= 2


def ambiguous_variant_pairs(
    docs: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return opposite-language pairs deterministic identity cannot resolve."""
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for index, left in enumerate(docs):
        for right in docs[index + 1 :]:
            if _variant_relationship(left, right) != "DIFFERENT_REPORT":
                continue
            left_language = _language(left)
            right_language = _language(right)
            if not left_language or not right_language or left_language == right_language:
                continue
            issuer = _issuer_identity(left)
            if not issuer or issuer != _issuer_identity(right):
                continue
            kind = _report_kind_for_identity(left)
            if not kind or kind != _report_kind_for_identity(right):
                continue
            period = _fiscal_period(left)
            if not period or period != _fiscal_period(right):
                continue
            if _has_revision_markers(left) or _has_revision_markers(right):
                continue
            pairs.append((left, right))
    return pairs


def bilingual_dedupe(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Select one report edition while retaining suppressed provenance.

    Selection is unconditional English-over-Swedish: English is used whenever
    it exists, Swedish is usable immediately when English is absent, and a
    later English edition supersedes a previously selected Swedish one.
    """
    groups: list[list[dict[str, Any]]] = []
    for doc in docs:
        matches = [
            index
            for index, variants in enumerate(groups)
            if any(
                _variant_relationship(doc, variant) in {"TRANSLATION", "REVISION"}
                for variant in variants
            )
        ]
        if not matches:
            groups.append([doc])
            continue
        target = matches[0]
        groups[target].append(doc)
        for index in reversed(matches[1:]):
            groups[target].extend(groups.pop(index))

    selection_rule = "deterministic_en_fallback"
    out: list[dict[str, Any]] = []
    for variants in groups:
        if len(variants) == 1:
            out.append(variants[0])
            continue
        group_keys = sorted(
            {key for variant in variants for key in _bilingual_identity_keys(variant)}
        )
        group_id = hashlib.sha256("|".join(group_keys).encode()).hexdigest()[:24]
        for variant in variants:
            variant["_bilingual_group_id"] = group_id
        variants_sorted = sorted(
            variants,
            key=lambda value: (
                0 if _language(value) == "en" else 1 if _language(value) == "sv" else 2,
                str(value.get("source_url") or value.get("url") or ""),
            ),
        )
        preferred = variants_sorted[0]
        preferred["bilingual_selection_rule"] = selection_rule
        out.append(preferred)
        for suppressed in variants_sorted[1:]:
            relationship = _variant_relationship(preferred, suppressed)
            suppressed["duplicate_of"] = preferred.get("source_url") or preferred.get("url")
            suppressed["ingest_status"] = f"superseded_by_{relationship.casefold()}"
            suppressed["_bilingual_group_id"] = group_id
            suppressed["bilingual_selection_rule"] = selection_rule
            suppressed["relationship"] = relationship
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
        "relationship",
        "document_type",
        "period_start",
        "period_end",
        "pdf_language",
        "language_evidence",
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
    ) -> IngestResult:
        """Persist report documents, suppressing one bilingual edition in packets."""
        eligible = [
            article
            for article in articles
            if not (
                reports_only and source_type == "mfn" and not is_report(article.get("title") or "")
            )
        ]
        eligible = [
            article
            if (
                article.get("pdf_language") in {"en", "sv"}
                and (
                    article.get("language_evidence") == "filename"
                    or str(article.get("language_evidence") or "").startswith("pdf_text:")
                )
            )
            else {**article, "_pdf_language_unresolved": True}
            for article in eligible
        ]
        deduped = bilingual_dedupe(eligible)
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
