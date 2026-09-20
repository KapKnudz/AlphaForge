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

from alphaforge.evidence.mfn_taxonomy import is_report, report_kind
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
_REPORT_ATTACHMENT_TERMS = (
    "annual",
    "årsredovis",
    "year-end",
    "year_end",
    "interim",
    "quarter",
    "delårs",
    "bokslut",
    "report",
    "rapport",
)
_NON_REPORT_ATTACHMENT_TERMS = ("presentation", "slides", "webcast")
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
        if lower_tag == "h1":
            self._h1_active = True
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
            self._anchor_href = None
            self._anchor_parts = []
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


def _parse_html(html: str) -> dict[str, Any]:
    parser = _MfnHtmlParser()
    parser.feed(html)
    pdf_links = [
        (href, text)
        for href, text in parser.links
        if "storage.mfn.se/" in href.lower() and ".pdf" in href.lower()
    ]
    selected_attachment = max(
        enumerate(pdf_links),
        key=lambda item: (_attachment_score(*item[1]), -item[0]),
        default=None,
    )
    storage_url = (
        selected_attachment[1][0]
        if selected_attachment is not None and _attachment_score(*selected_attachment[1])
        else None
    )
    title = " ".join(" ".join(parser.h1_parts).split())
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
    }


def _is_mfn_release_url(url: str, base_url: str) -> bool:
    candidate = urlsplit(url)
    base = urlsplit(base_url)
    if candidate.scheme != base.scheme or candidate.netloc.lower() != base.netloc.lower():
        return False
    return candidate.path.startswith(("/a/", "/cision/"))


def _report_identity_seed(article: dict[str, Any]) -> dict[str, Any]:
    title = article.get("title") or ""
    return {
        "report_kind": article.get("report_kind") or report_kind(title),
        "lang": article.get("lang") or _language_hint(title),
    }


class MfnScraper:
    def __init__(self, *, base_url: str = BASE_URL, max_articles: int = MAX_ARTICLES) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_articles = max_articles

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
        url = f"{self.base_url}/{mfn_slug.lstrip('/')}"
        if page > 1:
            url += f"?page={page}"
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
            if (
                not absolute
                or not title
                or absolute in seen
                or not _is_mfn_release_url(absolute, self.base_url)
            ):
                continue
            if reports_only and not is_report(title):
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
            parsed = _parse_html(resp.text)
            title = parsed["title"] or seed.get("title") or ""
            if reports_only and not is_report(title):
                continue
            body = parsed["body"]
            article = {
                "url": url,
                "source_url": url,
                "source_release_url": url,
                "title": title,
                "body": body,
                "content_text": body,
                "storage_url": parsed["storage_url"],
                "attachment_url": parsed["storage_url"],
                # Feed-card dates are not a stable publication authority; only
                # the detail page's timestamp metadata may enter the frozen
                # evidence lane.
                "published_at": parsed["published_at"],
                "lang": seed.get("lang") or _language_hint(title),
            }
            article.update(_report_identity_seed(article))
            out.append(article)
        return out
