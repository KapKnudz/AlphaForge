"""MfnScraper — Playwright MFN feed + detail fetch.

Rate limit: 1 rps (plan). Daily page-1 delta + Sunday page-2 backstop.
MAX_ARTICLES 24 report-prioritized.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from alphaforge.providers.http import MAX_RETRIES, request_with_retry

BASE_URL = "https://mfn.se"
MAX_ARTICLES = 24


@dataclass(frozen=True)
class MfnArticle:
    url: str
    title: str
    published_at: str | None
    body: str | None = None
    lang: str | None = None


class MfnScraper:
    def __init__(self, *, base_url: str = BASE_URL, max_articles: int = MAX_ARTICLES) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_articles = max_articles

    def discover_feed(self, mfn_slug: str, *, page: int = 1) -> list[dict[str, Any]]:
        """Discover feed URLs for a given MFN slug (company).

        Uses requests to fetch MFN feed page; parses a.title-link.item-link.
        Returns list of {url, title, published_at}.
        Rate limited to 1 rps.
        """
        url = f"{self.base_url}/{mfn_slug.lstrip('/')}"
        if page > 1:
            url += f"?page={page}"
        time.sleep(1.0)
        try:
            resp = request_with_retry("GET", url, timeout=30, max_retries=MAX_RETRIES)
        except Exception:
            return []
        if resp.status_code != 200:
            return []
        html = resp.text
        # Minimal regex parse — robust fallback when Playwright not available.
        # Looks for <a class="title-link item-link" href="...">Title</a>
        articles: list[dict[str, Any]] = []
        # Try title-link pattern first, then generic MFN link
        patterns = [
            r'<a[^>]*class="[^"]*title-link[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
            r'<a[^>]*href="(https://mfn\.se/[^"]+)"[^>]*>(.*?)</a>',
        ]
        seen: set[str] = set()
        for pat in patterns:
            for m in re.finditer(pat, html, re.IGNORECASE | re.DOTALL):
                href = m.group(1).strip()
                title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
                if not href or not title:
                    continue
                if href.startswith("/"):
                    href = self.base_url + href
                if href in seen:
                    continue
                seen.add(href)
                articles.append({"url": href, "title": title, "published_at": None})
                if len(articles) >= self.max_articles:
                    break
            if len(articles) >= self.max_articles:
                break
        return articles[: self.max_articles]

    def scrape_details(self, urls: list[str]) -> list[dict[str, Any]]:
        """Fetch detail pages for unseen URLs — Playwright semantics via requests.

        Returns list of {url, title, body, published_at, storage_url}.
        1 rps, MAX_RETRIES=3 with Retry-After.
        """
        out: list[dict[str, Any]] = []
        for url in urls:
            time.sleep(1.0)
            try:
                resp = request_with_retry("GET", url, timeout=60, max_retries=MAX_RETRIES)
            except Exception:
                continue
            if resp.status_code != 200:
                continue
            html = resp.text
            # Extract h1 title
            title_m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)
            title = re.sub(r"<[^>]+>", "", title_m.group(1)).strip() if title_m else ""
            # Extract body .release-body or article
            body_m = re.search(
                r'<div[^>]*class="[^"]*release-body[^"]*"[^>]*>(.*?)</div>',
                html,
                re.IGNORECASE | re.DOTALL,
            )
            if not body_m:
                body_m = re.search(
                    r"<article[^>]*>(.*?)</article>", html, re.IGNORECASE | re.DOTALL
                )
            body = ""
            if body_m:
                body = re.sub(r"<[^>]+>", " ", body_m.group(1))
                body = re.sub(r"\s+", " ", body).strip()
                body = re.sub(r"\n{3,}", "\n\n", body)
            # Attachment storage.mfn.se
            storage_m = re.search(
                r'href="(https://storage\.mfn\.se/[^"]+\.pdf[^"]*)"', html, re.IGNORECASE
            )
            storage_url = storage_m.group(1) if storage_m else None
            out.append(
                {
                    "url": url,
                    "title": title,
                    "body": body,
                    "storage_url": storage_url,
                    "published_at": None,
                }
            )
        return out
