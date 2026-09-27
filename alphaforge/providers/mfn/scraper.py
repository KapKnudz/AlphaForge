"""Deterministic MFN discovery for quarterly and annual reports.

The ship slice deliberately excludes general-news ingestion. HTTP requests
remain the default acquisition path; a browser fallback belongs behind a
fixture-proven client-rendering check, not in this deterministic parser.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

from alphaforge.evidence.mfn_taxonomy import (
    ATTACHMENT_HOST_MARKERS,
    ATTACHMENT_TIERS,  # noqa: F401 -- intentional re-export (see note below)
    CIS_RELEASE_PATH_RE,
    NON_REPORT_ATTACHMENT_TERMS,
    REPORT_ANNUAL_TAG,
    REPORT_ATTACHMENT_TERMS,
    REPORT_FEED_TAG,
    REPORT_INTERIM_TAG_PREFIX,
    REPORT_PDF_ATTACHMENT_TAG,
    document_type,
    is_invitation_or_presentation,
    is_report,
    report_kind,
)
from alphaforge.providers.http import MAX_RETRIES, request_with_retry
from alphaforge.providers.mfn.errors import MfnAcquisitionError

BASE_URL = "https://mfn.se"
MAX_ARTICLES = 24


_HTML_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
# Rule inputs live authoritatively in mfn_taxonomy (leaf module) so the
# deterministic core can fingerprint them without importing providers.
# These aliases preserve the internal uses below.
_REPORT_ATTACHMENT_TERMS = REPORT_ATTACHMENT_TERMS
_NON_REPORT_ATTACHMENT_TERMS = NON_REPORT_ATTACHMENT_TERMS
_CIS_RELEASE_PATH_RE = CIS_RELEASE_PATH_RE
_ATTACHMENT_HOST_MARKERS = ATTACHMENT_HOST_MARKERS
_SWEDISH_MONTHS = {
    "januari": "january",
    "februari": "february",
    "mars": "march",
    "april": "april",
    "maj": "may",
    "juni": "june",
    "juli": "july",
    "augusti": "august",
    "september": "september",
    "oktober": "october",
    "november": "november",
    "december": "december",
}


def _normalise_timestamp(value: str | None) -> str | None:
    if not value:
        return None
    text = " ".join(value.split())
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        localized_text = re.sub(
            r"\b(?:" + "|".join(_SWEDISH_MONTHS) + r")\b",
            lambda match: _SWEDISH_MONTHS[match.group(0).lower()],
            text,
            flags=re.IGNORECASE,
        )
        for date_text in (text, localized_text):
            for date_format in ("%d %B %Y", "%d %b %Y"):
                try:
                    parsed = datetime.strptime(date_text, date_format)
                    break
                except ValueError:
                    continue
            else:
                continue
            break
        else:
            return None
    if parsed.tzinfo is None:
        return parsed.isoformat(timespec="seconds")
    return parsed.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _language_hint(text: str) -> str | None:
    lower = text.lower()
    if any(
        marker in lower
        for marker in ("årsredovisning", "bokslutskommuniké", "delårsrapport", "kvartalsrapport")
    ):
        return "sv"
    if any(marker in lower for marker in ("annual report", "year-end report", "interim report")):
        return "en"
    return None


class _MfnHtmlParser(HTMLParser):
    """Small, fixture-friendly parser for MFN's rendered HTML surface."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.attachment_links: list[tuple[str, str, str]] = []
        self.canonical_url: str | None = None
        self.h1_parts: list[str] = []
        self.body_parts: list[str] = []
        self.release_body_parts: list[str] = []
        self.release_body_seen = False
        self.timestamps: list[str] = []
        self.generic_timestamps: list[str] = []
        self.json_published_timestamps: list[str] = []
        self.json_parts: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_parts: list[str] = []
        self._anchor_class: str = ""
        self._h1_active = False
        self._article_depth = 0
        self._release_body_depth = 0
        self._time_active = False
        self._time_publication_active = False
        self._time_parts: list[str] = []
        self._json_active = False

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {key.lower(): value or "" for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = self._attrs(attrs)
        lower_tag = tag.lower()
        if lower_tag == "a" and values.get("href"):
            self._anchor_href = values["href"]
            self._anchor_parts = []
            self._anchor_class = values.get("class", "")
        if lower_tag == "h1":
            self._h1_active = True
        if lower_tag == "link" and values.get("href") and self.canonical_url is None:
            rel = values.get("rel", "").lower()
            if "canonical" in rel.split():
                self.canonical_url = values["href"]
        classes = values.get("class", "").lower().split()
        if lower_tag == "article":
            self._article_depth = max(self._article_depth, 1)
        elif self._article_depth and lower_tag not in _HTML_VOID_TAGS:
            self._article_depth += 1
        if "release-body" in classes:
            self.release_body_seen = True
            self._release_body_depth = max(self._release_body_depth, 1)
        elif self._release_body_depth and lower_tag not in _HTML_VOID_TAGS:
            self._release_body_depth += 1
        if lower_tag == "time":
            self._time_active = True
            self._time_parts = []
            label = " ".join(
                values.get(key, "") for key in ("class", "data-type", "aria-label", "itemprop")
            ).lower()
            self._time_publication_active = bool(
                re.search(r"publish|publication|release date|released|utgiv", label)
            )
            timestamp = values.get("datetime") or values.get("data-datetime")
            if timestamp and self._time_publication_active:
                self.timestamps.append(timestamp)
        if lower_tag == "meta":
            name = (values.get("property") or values.get("name") or "").lower()
            if name in {"article:published_time", "datepublished", "publish_date", "published_at"}:
                if values.get("content"):
                    self.timestamps.append(values["content"])
        if lower_tag == "script" and values.get("type", "").lower() == "application/ld+json":
            self._json_active = True
            self.json_parts = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        lower_tag = tag.lower()
        if lower_tag == "a" and self._anchor_href is not None:
            self.links.append((self._anchor_href, " ".join(self._anchor_parts).strip()))
            self.attachment_links.append(
                (
                    self._anchor_href,
                    " ".join(self._anchor_parts).strip(),
                    self._anchor_class,
                )
            )
            self._anchor_href = None
            self._anchor_parts = []
            self._anchor_class = ""
        if lower_tag == "h1":
            self._h1_active = False
        if lower_tag == "time":
            if self._time_parts and self._time_publication_active:
                self.generic_timestamps.append(" ".join(self._time_parts))
            self._time_active = False
            self._time_publication_active = False
            self._time_parts = []
        if lower_tag == "script" and self._json_active:
            self._json_active = False
            self._extract_json_dates("".join(self.json_parts))
            self.json_parts = []
        if lower_tag not in _HTML_VOID_TAGS:
            if self._article_depth:
                self._article_depth -= 1
            if self._release_body_depth:
                self._release_body_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._anchor_href is not None:
            self._anchor_parts.append(data)
        if self._h1_active:
            self.h1_parts.append(data)
        if self._article_depth:
            self.body_parts.append(data)
        if self._release_body_depth:
            self.release_body_parts.append(data)
        if self._time_active:
            self._time_parts.append(data)
        if self._json_active:
            self.json_parts.append(data)

    def _extract_json_dates(self, raw: str) -> None:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return

        def walk(item: Any) -> None:
            if isinstance(item, dict):
                for key in ("datePublished", "published_at"):
                    candidate = item.get(key)
                    if isinstance(candidate, str):
                        self.json_published_timestamps.append(candidate)
                for child in item.values():
                    walk(child)
            elif isinstance(item, list):
                for child in item:
                    walk(child)

        walk(value)


def _attachment_score(url: str, label: str = "") -> int:
    name = f"{unquote(urlsplit(url).path)} {label}".lower()
    if any(term in name for term in _NON_REPORT_ATTACHMENT_TERMS):
        return 0
    if any(term in name for term in _REPORT_ATTACHMENT_TERMS):
        return 2
    return 1


# ATTACHMENT_TIERS is imported above from mfn_taxonomy (authoritative
# definition) and remains available as scraper.ATTACHMENT_TIERS.


def _label_report_score(label: str) -> int:
    name = label.lower()
    if any(term in name for term in _NON_REPORT_ATTACHMENT_TERMS):
        return 0
    if any(term in name for term in _REPORT_ATTACHMENT_TERMS):
        return 2
    return 1


def _is_main_path_pdf(url: str) -> bool:
    parts = urlsplit(url)
    return parts.netloc.lower() == "mb.cision.com" and "/main/" in parts.path.lower()


def _dedupe_identical_targets(
    pdf_links: list[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    """Collapse repeated anchors with exactly the same href string.

    A release page may print the same PDF link twice (body copy plus
    attachment list); those duplicates are one attachment, not two
    candidates. Only identical href strings collapse — genuinely
    distinct target URLs are preserved so multi-attachment ambiguity
    still refuses. No URL canonicalization is applied.
    """
    grouped: dict[str, list[tuple[str, str, str]]] = {}
    for entry in pdf_links:
        grouped.setdefault(entry[0], []).append(entry)
    deduped: list[tuple[str, str, str]] = []
    for entries in grouped.values():
        # Stable sort: keep the strongest same-target signal (explicit
        # primary marker, then report-like link text), first anchor
        # winning any remaining tie.
        best = sorted(
            entries,
            key=lambda entry: (
                "mfn-primary" in entry[2].lower().split(),
                _label_report_score(entry[1]),
            ),
            reverse=True,
        )[0]
        deduped.append(best)
    return deduped


def _select_attachment(
    pdf_links: list[tuple[str, str, str]], *, page_is_report: bool
) -> tuple[str | None, str]:
    """Select exactly one PDF by ranked identity, or refuse with a tier.

    Rank: explicit ``mfn-primary`` marker, then Cision ``Main/`` path on the
    Cision attachment host, then report-like link text alone (which
    additionally requires the corroborating page-level report title —
    enforced by ``page_is_report``). Repeated anchors with an identical
    target href count as one attachment before ranking. Ties at any tier,
    or no positive link-text signal at all, yield ``(None, "unresolved")``;
    an empty candidate set yields ``(None, "none")``.
    """
    viable = _dedupe_identical_targets(
        [
            (href, text, css_class)
            for href, text, css_class in pdf_links
            if _attachment_score(href, text) > 0
        ]
    )
    if not viable:
        return None, "none"
    if not page_is_report:
        return None, "none"
    primaries = [
        (href, text, css_class)
        for href, text, css_class in viable
        if "mfn-primary" in css_class.lower().split()
    ]
    if len(primaries) == 1:
        return primaries[0][0], "mfn-primary"
    if len(primaries) > 1:
        return None, "unresolved"
    mains = [(href, text, css_class) for href, text, css_class in viable if _is_main_path_pdf(href)]
    if len(mains) == 1:
        return mains[0][0], "main-path"
    if len(mains) > 1:
        return None, "unresolved"
    scored = sorted(
        ((_label_report_score(text), href) for href, text, _ in viable),
        reverse=True,
    )
    if scored[0][0] == 2 and (len(scored) == 1 or scored[1][0] < 2):
        return scored[0][1], "label-score"
    return None, "unresolved"


def _parse_html(html: str, *, corroborated_report: bool = False) -> dict[str, Any]:
    parser = _MfnHtmlParser()
    parser.feed(html)
    pdf_links = [
        (href, text, css_class)
        for href, text, css_class in parser.attachment_links
        if any(marker in href.lower() for marker in _ATTACHMENT_HOST_MARKERS)
        and ".pdf" in href.lower()
    ]
    title = " ".join(" ".join(parser.h1_parts).split())
    page_is_report = bool(title) and (is_report(title) or corroborated_report)
    page_is_invitation = bool(title) and is_invitation_or_presentation(title)
    if page_is_invitation:
        storage_url, tier = None, "none"
    else:
        storage_url, tier = _select_attachment(pdf_links, page_is_report=page_is_report)
    body_parts = parser.release_body_parts if parser.release_body_seen else parser.body_parts
    body = " ".join(" ".join(body_parts).split())
    published_at = next(
        (normalised for raw in parser.timestamps if (normalised := _normalise_timestamp(raw))),
        None,
    )
    if published_at is None:
        published_at = next(
            (
                normalised
                for raw in parser.json_published_timestamps
                if (normalised := _normalise_timestamp(raw))
            ),
            None,
        )
    if published_at is None:
        published_at = next(
            (
                normalised
                for raw in parser.generic_timestamps
                if (normalised := _normalise_timestamp(raw))
            ),
            None,
        )
    return {
        "title": title,
        "body": body,
        "published_at": published_at,
        "storage_url": storage_url,
        "canonical_url": parser.canonical_url,
        "attachment_tier": tier,
    }


def _is_mfn_release_url(url: str, base_url: str) -> bool:
    candidate = urlsplit(url)
    base = urlsplit(base_url)
    if candidate.scheme != base.scheme or candidate.netloc.lower() != base.netloc.lower():
        return False
    path = candidate.path
    if path.startswith("/cis/a/"):
        # Cision-distribution publishing: only the stable
        # /cis/a/<issuer>/<slug>-<8hex> shape, never a bare prefix.
        return _CIS_RELEASE_PATH_RE.match(path) is not None
    return path.startswith(("/a/", "/cision/"))


def _cis_release_issuer(url: str) -> str | None:
    """Return the issuer segment of a ``/cis/a/`` release URL, else None."""
    match = _CIS_RELEASE_PATH_RE.match(urlsplit(url).path)
    return match.group(1) if match else None


def _canonical_issuer(canonical_url: str | None) -> str | None:
    """Return the issuer segment of an MFN canonical link, else None.

    MFN declares release identity as ``/all/a/<issuer>/...`` on every
    rendered page, legacy and Cision-distribution alike.
    """
    if not canonical_url:
        return None
    segments = [segment for segment in urlsplit(canonical_url).path.split("/") if segment]
    if len(segments) >= 3 and segments[0].lower() == "all" and segments[1].lower() == "a":
        return segments[2]
    return None


def _issuer_token(mfn_slug: str) -> str:
    """Return the stable issuer token of a resolved mapping slug.

    The mapping slug is exact-identifier resolved (never a fuzzy company
    name); its last path segment (e.g. ``clas-ohlson`` in
    ``all/a/clas-ohlson``) is the identity the release and canonical
    issuer segments must match.
    """
    return mfn_slug.strip().strip("/").split("/")[-1].lower()


def _feed_report_attachment_url(entry: dict[str, Any]) -> str | None:
    content = entry.get("content") if isinstance(entry.get("content"), dict) else {}
    attachments = content.get("attachments") if isinstance(content.get("attachments"), list) else []
    matches = {
        str(attachment.get("url") or "").strip()
        for attachment in attachments
        if isinstance(attachment, dict)
        and str(attachment.get("content_type") or "").lower() == "application/pdf"
        and REPORT_PDF_ATTACHMENT_TAG
        in {str(tag).lower() for tag in attachment.get("tags", []) if isinstance(tag, str)}
        and str(attachment.get("url") or "").strip()
    }
    return next(iter(matches)) if len(matches) == 1 else None


def _feed_report_identity(entry: dict[str, Any]) -> tuple[str, str | None] | None:
    """Return report identity only when independent MFN feed signals agree."""
    properties = entry.get("properties") if isinstance(entry.get("properties"), dict) else {}
    tags = {str(tag).lower() for tag in properties.get("tags", []) if isinstance(tag, str)}
    report_pdf_url = _feed_report_attachment_url(entry)
    if REPORT_FEED_TAG not in tags or report_pdf_url is None:
        return None
    if REPORT_ANNUAL_TAG in tags and not any(
        tag.startswith(REPORT_INTERIM_TAG_PREFIX) for tag in tags
    ):
        return "annual", "ANNUAL_REPORT"
    quarters = sorted(
        tag.removeprefix(REPORT_INTERIM_TAG_PREFIX)
        for tag in tags
        if tag.startswith(REPORT_INTERIM_TAG_PREFIX)
        and tag.removeprefix(REPORT_INTERIM_TAG_PREFIX) in {"1", "2", "3", "4"}
    )
    if len(quarters) != 1:
        return None
    quarter = quarters[0]
    return "quarterly", f"INTERIM_Q{quarter}" if quarter != "4" else "YEAR_END_REPORT"


def _report_identity_seed(article: dict[str, Any]) -> dict[str, Any]:
    title = article.get("title") or ""
    return {
        "report_kind": article.get("report_kind") or report_kind(title),
        "document_type": article.get("document_type") or document_type(title),
        "lang": article.get("lang") or _language_hint(title),
    }


class MfnScraper:
    def __init__(self, *, base_url: str = BASE_URL, max_articles: int = MAX_ARTICLES) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_articles = max_articles
        self._discovery_skips: dict[str, int] = {}
        self._detail_skips: dict[str, int] = {}
        self._discovery_dispositions: dict[str, dict[str, Any]] = {}

    def _count_discovery(self, reason: str) -> None:
        self._discovery_skips[reason] = self._discovery_skips.get(reason, 0) + 1

    def _count_detail(self, reason: str) -> None:
        self._detail_skips[reason] = self._detail_skips.get(reason, 0) + 1

    def drain_discovery_skips(self) -> dict[str, int]:
        """Return discovery drop counts since the last drain and clear them."""
        drained = dict(self._discovery_skips)
        self._discovery_skips = {}
        return drained

    def drain_detail_skips(self) -> dict[str, int]:
        """Return detail drop counts since the last drain and clear them."""
        drained = dict(self._detail_skips)
        self._detail_skips = {}
        return drained

    def drain_discovery_dispositions(self) -> dict[str, dict[str, Any]]:
        """Return current feed classification facts keyed by exact release URL."""
        drained = {url: dict(value) for url, value in self._discovery_dispositions.items()}
        self._discovery_dispositions = {}
        return drained

    def _parse_json_feed_items(
        self, payload: Any, *, reports_only: bool = True
    ) -> list[dict[str, Any]]:
        """Parse MFN JSON feed ``items`` into the same shape as HTML discovery."""
        items: list[dict[str, Any]]
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
            items = payload["items"]
        elif isinstance(payload, list):
            items = payload
        else:
            return []
        articles: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in items:
            if not isinstance(entry, dict):
                continue
            content = entry.get("content") if isinstance(entry.get("content"), dict) else {}
            title = str(content.get("title") or entry.get("title") or "").strip()
            url = str(entry.get("url") or content.get("url") or "").strip()
            if not url:
                url = str(entry.get("source_url") or "").strip()
            if not url or not title:
                continue
            absolute = urljoin(f"{self.base_url}/", url)
            if absolute in seen:
                continue
            if not _is_mfn_release_url(absolute, self.base_url):
                self._count_discovery("non_release_url")
                continue
            feed_identity = _feed_report_identity(entry)
            normalized_title = " ".join(title.split())
            invited = is_invitation_or_presentation(normalized_title)
            admitted_by_title = is_report(normalized_title) and not invited
            attachment_url = _feed_report_attachment_url(entry)
            self._discovery_dispositions[absolute] = {
                "source_url": absolute,
                "title": normalized_title,
                "title_admitted": admitted_by_title,
                "report_kind": feed_identity[0] if feed_identity is not None else report_kind(title),
                "document_type": (
                    feed_identity[1] if feed_identity is not None else document_type(title)
                ),
                "feed_report_identity": (
                    "mfn-report-tag+archive-report-pdf" if feed_identity is not None else None
                ),
                "feed_report_attachment_url": attachment_url,
                "invitation_veto": invited,
            }
            if reports_only and not (admitted_by_title or feed_identity):
                self._count_discovery("non_report_title")
                continue
            seen.add(absolute)
            article: dict[str, Any] = {
                "url": absolute,
                "source_url": absolute,
                "title": normalized_title,
                "published_at": content.get("publish_date") or entry.get("publish_date"),
            }
            if feed_identity is not None:
                article["report_kind"], article["document_type"] = feed_identity
                article["feed_report_identity"] = "mfn-report-tag+archive-report-pdf"
                article["feed_report_attachment_url"] = attachment_url
            # Preserve feed-level language when available
            props = entry.get("properties") if isinstance(entry.get("properties"), dict) else {}
            feed_lang = str(props.get("lang") or "").lower()
            if feed_lang in {"sv", "en"}:
                article["lang"] = feed_lang
            article.update(_report_identity_seed(article))
            articles.append(article)
            if len(articles) >= self.max_articles:
                break
        return articles

    def discover_feed(
        self,
        mfn_slug: str,
        *,
        page: int = 1,
        reports_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Discover report links from an issuer-scoped MFN page.

        The default intentionally excludes general news, releases, and report
        calendar notices. Detail pages remain the authoritative timestamp
        source because feed cards do not expose a stable one-to-one date
        association in every MFN layout.
        """
        # ``?page=`` is legacy HTML pagination; MFN's real pagination is
        # ``offset``/``limit`` (JSON feed or ``*.html`` fragment).  Map page
        # to the offset/limit contract so ``page=2`` actually advances.
        if page > 1:
            limit = self.max_articles if self.max_articles else 48
            offset = (page - 1) * limit
            paginated = self.discover_feed_paginated(
                mfn_slug, offset=offset, limit=limit, reports_only=reports_only
            )
            return paginated[0]
        url = f"{self.base_url}/{mfn_slug.lstrip('/')}"
        time.sleep(1.0)
        try:
            resp = request_with_retry("GET", url, timeout=30, max_retries=MAX_RETRIES)
        except Exception as exc:
            raise MfnAcquisitionError(
                "mfn_feed_fetch_failed", f"MFN feed request failed: {exc}"
            ) from exc
        if resp.status_code != 200:
            raise MfnAcquisitionError(
                "mfn_feed_http_status", f"MFN feed request returned HTTP {resp.status_code}"
            )
        parser = _MfnHtmlParser()
        parser.feed(resp.text)
        articles: list[dict[str, Any]] = []
        seen: set[str] = set()
        for href, raw_title in parser.links:
            title = " ".join(raw_title.split())
            absolute = urljoin(f"{self.base_url}/", href)
            if not absolute or not title or absolute in seen:
                continue
            if not _is_mfn_release_url(absolute, self.base_url):
                self._count_discovery("non_release_url")
                continue
            if reports_only and not is_report(title):
                self._count_discovery("non_report_title")
                continue
            seen.add(absolute)
            article = {
                "url": absolute,
                "source_url": absolute,
                "title": title,
                "published_at": None,
            }
            article.update(_report_identity_seed(article))
            articles.append(article)
            if len(articles) >= self.max_articles:
                break
        return articles

    def discover_feed_paginated(
        self,
        mfn_slug: str,
        *,
        offset: int = 0,
        limit: int = 48,
        reports_only: bool = True,
    ) -> tuple[list[dict[str, Any]], int | None]:
        """Paginated discovery via MFN JSON feed with HTML fragment fallback.

        Returns ``(articles, next_offset)`` where ``next_offset`` is ``None``
        at the tail (``len(items) < limit`` or no ``next_url``).  ``articles``
        are already filtered by ``is_report`` when ``reports_only`` is true
        and capped by ``max_articles``.  The JSON feed at
        ``/{slug}?offset=&limit=`` is preferred; ``/{slug}.html?offset=&limit=``
        is the HTML fragment that the ``show-more`` JS consumes.
        """
        # Try JSON feed first (``Accept: application/json``).  MFN returns
        # ``application/json`` for ``?offset=&limit=``.
        json_url = f"{self.base_url}/{mfn_slug.lstrip('/')}?offset={offset}&limit={limit}"
        time.sleep(1.0)
        saw_http_200 = False
        try:
            resp = request_with_retry(
                "GET",
                json_url,
                timeout=30,
                max_retries=MAX_RETRIES,
                headers={"Accept": "application/json"},
            )
        except Exception:
            resp = None  # type: ignore[assignment]
        if resp is not None and resp.status_code == 200:
            saw_http_200 = True
            ctype = str(
                (getattr(resp, "headers", {}) or {}).get("Content-Type")
                or (getattr(resp, "headers", {}) or {}).get("content-type")
                or ""
            ).lower()
            body = getattr(resp, "text", None)
            if body is None:
                try:
                    body = resp.content.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
                except Exception:
                    body = ""
            if "application/json" in ctype or (body and body.lstrip().startswith("{")):
                try:
                    payload = (
                        json.loads(body) if isinstance(body, str) else json.loads(body.decode())
                    )  # type: ignore[arg-type]
                    articles = self._parse_json_feed_items(payload, reports_only=reports_only)
                    # Derive next_offset from feed metadata when available.
                    next_offset: int | None = None
                    if isinstance(payload, dict):
                        nxt = payload.get("next_url")
                        if isinstance(nxt, str) and nxt:
                            import re as _re

                            m = _re.search(r"offset=(\d+)", nxt)
                            if m:
                                try:
                                    next_offset = int(m.group(1))
                                except ValueError:
                                    next_offset = None
                        # Fallback: infer from counts when no next_url.
                        if next_offset is None:
                            items = (
                                payload.get("items")
                                if isinstance(payload.get("items"), list)
                                else None
                            )
                            if isinstance(items, list) and len(items) >= limit:
                                next_offset = offset + limit
                    return articles, next_offset
                except Exception:
                    pass  # fall through to HTML fragment
        # HTML fragment fallback (``*.html?offset=&limit=``)
        html_url = f"{self.base_url}/{mfn_slug.lstrip('/')}?offset={offset}&limit={limit}"
        # Some deployments serve fragments at ``.html`` suffix.
        fragment_url = f"{self.base_url}/{mfn_slug.lstrip('/')}.html?offset={offset}&limit={limit}"
        for url in (html_url, fragment_url):
            time.sleep(0.5)
            try:
                resp = request_with_retry("GET", url, timeout=30, max_retries=MAX_RETRIES)
            except Exception:
                continue
            if resp.status_code != 200:
                continue
            saw_http_200 = True
            text = getattr(resp, "text", "") or ""
            if not text or "<a" not in text.lower():
                continue
            parser = _MfnHtmlParser()
            parser.feed(text)
            articles: list[dict[str, Any]] = []
            seen: set[str] = set()
            for href, raw_title in parser.links:
                title = " ".join(raw_title.split())
                absolute = urljoin(f"{self.base_url}/", href)
                if not absolute or not title or absolute in seen:
                    continue
                if not _is_mfn_release_url(absolute, self.base_url):
                    self._count_discovery("non_release_url")
                    continue
                if reports_only and not is_report(title):
                    self._count_discovery("non_report_title")
                    continue
                seen.add(absolute)
                article = {
                    "url": absolute,
                    "source_url": absolute,
                    "title": title,
                    "published_at": None,
                }
                article.update(_report_identity_seed(article))
                articles.append(article)
                if len(articles) >= self.max_articles:
                    break
            # HTML fragments have no JSON next_url; infer tail via link count.
            # If fewer raw links than limit, we are at tail.
            raw_links = len(parser.links)
            next_off = offset + limit if raw_links >= limit else None
            return articles, next_off
        if not saw_http_200:
            raise MfnAcquisitionError(
                "mfn_feed_fetch_failed",
                f"MFN paginated feed request failed for {mfn_slug} offset {offset}",
            )
        return [], None

    def scrape_details(
        self,
        entries: list[str | dict[str, Any]],
        *,
        reports_only: bool = True,
    ) -> list[dict[str, Any]]:
        """Fetch unseen MFN details and return report/PDF metadata.

        Entries may be URLs or feed dictionaries. Supporting dictionaries
        preserves the feed's title/language hints while the detail page supplies
        the canonical publication timestamp and attachment URL.
        """
        out: list[dict[str, Any]] = []
        for entry in entries:
            seed = entry if isinstance(entry, dict) else {"url": entry}
            url = seed.get("url") or seed.get("source_url") or ""
            if not url:
                continue
            time.sleep(1.0)
            try:
                resp = request_with_retry("GET", url, timeout=60, max_retries=MAX_RETRIES)
            except Exception as exc:
                raise MfnAcquisitionError(
                    "mfn_detail_fetch_failed", f"MFN detail request failed: {exc}"
                ) from exc
            if resp.status_code != 200:
                raise MfnAcquisitionError(
                    "mfn_detail_http_status", f"MFN detail request returned HTTP {resp.status_code}"
                )
            feed_title = " ".join(str(seed.get("title") or "").split())
            feed_report_identity = bool(seed.get("feed_report_identity"))
            feed_title_admitted = is_report(feed_title)
            parsed = _parse_html(resp.text, corroborated_report=False)
            detail_title = parsed["title"] or ""
            if detail_title and (feed_report_identity or feed_title_admitted):
                parsed = _parse_html(resp.text, corroborated_report=True)
                detail_title = parsed["title"] or ""
            if not detail_title:
                self._count_detail("non_report_title")
                continue
            if reports_only and not (
                is_report(detail_title) or feed_title_admitted or feed_report_identity
            ):
                self._count_detail("non_report_title")
                continue
            if is_invitation_or_presentation(feed_title) or is_invitation_or_presentation(
                detail_title
            ):
                # An invitation/presentation about a report is not the report:
                # its attachments must never become report evidence.
                self._count_detail("invitation_or_presentation_release")
                continue
            body = parsed["body"]
            article = {
                "url": url,
                "source_url": url,
                "source_release_url": url,
                "title": feed_title or detail_title,
                "detail_title": detail_title,
                "body": body,
                "content_text": body,
                "storage_url": parsed["storage_url"],
                "attachment_url": parsed["storage_url"],
                "canonical_url": parsed["canonical_url"],
                "attachment_tier": parsed["attachment_tier"],
                # Feed-card dates are not a stable publication authority; only
                # the detail page's timestamp metadata may enter the frozen
                # evidence lane.
                "published_at": parsed["published_at"],
                "lang": seed.get("lang") or _language_hint(feed_title or detail_title),
                "report_kind": seed.get("report_kind"),
                "document_type": seed.get("document_type"),
                "feed_report_identity": seed.get("feed_report_identity"),
                "feed_report_attachment_url": seed.get("feed_report_attachment_url"),
            }
            article.update(_report_identity_seed(article))
            out.append(article)
        return out
