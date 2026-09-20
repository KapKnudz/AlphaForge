"""Deterministic MFN issuer discovery and reviewed mapping seam.

The resolver only promotes a candidate observed on an MFN-owned surface and
matched by an exact company identifier. It never constructs a slug from a
company name and has no model or fuzzy-linking fallback.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

from alphaforge.db.repositories import (
    persist_mfn_issuer_candidates,
    upsert_mfn_issuer_mapping,
)
from alphaforge.providers.http import MAX_RETRIES, request_with_retry

BASE_URL = "https://mfn.se"


class MfnIssuerAcquisitionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _canonical_issuer_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            "",
            "",
        )
    )
_RELEASE_SEGMENTS = {"a", "cision", "release", "releases"}
_INDEX_SEGMENTS = {"all", "company", "companies", "issuer", "issuers", "search"}
_EXTERNAL_IDENTITY_ATTRIBUTE_KEYS = {
    "ticker",
    "isin",
    "borsdataid",
    "insid",
    "instrumentid",
}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalise(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _exact_identifier_values(company: dict[str, Any]) -> set[str]:
    values: set[str] = set()
    for key in ("ticker", "isin", "borsdata_id", "insId"):
        value = company.get(key)
        if value not in (None, ""):
            values.add(_normalise(value))
    return values


def _explicit_identity_values(attrs: dict[str, str]) -> set[str]:
    values: set[str] = set()
    for key, value in attrs.items():
        normalised_key = re.sub(r"[^a-z0-9]", "", key.lower()).removeprefix("data")
        if normalised_key in _EXTERNAL_IDENTITY_ATTRIBUTE_KEYS and value:
            values.add(_normalise(value))
    return values


class _IssuerSurfaceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str, dict[str, str]]] = []
        self._href: str | None = None
        self._label: list[str] = []
        self._attrs: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        values = {key.lower(): value or "" for key, value in attrs}
        href = values.get("href") or values.get("data-url")
        if href:
            self._href = href
            self._label = []
            self._attrs = values

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._label.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._label).strip(), self._attrs))
            self._href = None
            self._label = []
            self._attrs = {}


def _candidate_slug(url: str, base_url: str) -> str | None:
    parsed = urlsplit(url)
    base = urlsplit(base_url)
    if parsed.netloc.lower() != base.netloc.lower() or parsed.scheme != base.scheme:
        return None
    segments = [segment for segment in parsed.path.split("/") if segment]
    lower_segments = [segment.lower() for segment in segments]
    namespaced_issuer_path = lower_segments[:2] == ["all", "a"]
    if any(
        segment in _RELEASE_SEGMENTS and not (segment == "a" and namespaced_issuer_path)
        for segment in lower_segments
    ) or (lower_segments and lower_segments[0] in {"a", "cision"}):
        return None
    if not segments:
        return None
    # A candidate is an observed issuer/index href. Preserve the observed path
    # (MFN uses namespaced issuer paths such as ``all/a/acme``) rather than
    # reducing it to a guessed company-name slug.
    if len(segments) > 3 and namespaced_issuer_path:
        return None
    slug = "/".join(segment.strip() for segment in segments)
    if slug.lower() in _INDEX_SEGMENTS or slug.isdigit() and len(segments) == 1:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._~/-]{1,160}", slug):
        return None
    return slug


def _json_links(payload: Any) -> Iterable[tuple[str, str, dict[str, str]]]:
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key.lower() in {"url", "source_url", "issuer_url", "profile_url", "href"} and isinstance(value, str):
                yield value, str(payload.get("name") or payload.get("title") or ""), {
                    str(k): str(v) for k, v in payload.items() if v is not None
                }
            else:
                yield from _json_links(value)
    elif isinstance(payload, list):
        for value in payload:
            yield from _json_links(value)


def parse_mfn_issuer_candidates(
    payload: str | dict[str, Any] | list[Any],
    *,
    company: dict[str, Any],
    surface_url: str,
    base_url: str = BASE_URL,
    discovery_source: str = "mfn_surface",
) -> list[dict[str, Any]]:
    """Extract exact-identifier issuer candidates from an MFN surface."""
    if isinstance(payload, str):
        parser = _IssuerSurfaceParser()
        parser.feed(payload)
        links = parser.links
        try:
            loaded = json.loads(payload)
        except (TypeError, ValueError):
            loaded = None
        if loaded is not None:
            links = [*links, *_json_links(loaded)]
    else:
        links = list(_json_links(payload))
    identifiers = _exact_identifier_values(company)
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for href, label, attrs in links:
        absolute = urljoin(surface_url, href)
        slug = _candidate_slug(absolute, base_url)
        if not slug:
            continue
        candidate_identifiers = _explicit_identity_values(attrs)
        matched_identifiers = sorted(identifiers.intersection(candidate_identifiers))
        if not matched_identifiers:
            continue
        canonical_url = _canonical_issuer_url(absolute)
        key = (slug.lower(), canonical_url)
        candidates[key] = {
            "mfn_slug": slug,
            "source_url": canonical_url,
            "discovery_source": discovery_source,
            "match_basis": "exact_identifier",
            "identity_evidence": {
                "provenance": surface_url,
                "reason": "matched explicit external identifier(s): " + ", ".join(matched_identifiers),
                "surface_url": surface_url,
                "label": label,
                "matched_identifiers": matched_identifiers,
            },
        }
    return sorted(candidates.values(), key=lambda item: (item["mfn_slug"].lower(), item["source_url"]))


@dataclass(frozen=True)
class IssuerResolution:
    status: str
    candidates: tuple[dict[str, Any], ...]
    selected: dict[str, Any] | None = None
    verified_at: str | None = None


class MfnIssuerResolver:
    """Resolve one issuer using MFN-owned index/search surfaces only."""

    def __init__(self, *, base_url: str = BASE_URL, max_surfaces: int = 5) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_surfaces = max(1, max_surfaces)

    def discover(
        self,
        company: dict[str, Any],
        *,
        surfaces: list[tuple[str, str]] | None = None,
    ) -> IssuerResolution:
        """Discover and classify candidates without writing a mapping.

        ``surfaces`` is a fixture/operator seam of ``(surface_url, payload)``.
        In live mode the resolver queries MFN's index and exact-identifier
        search surfaces, bounded to a small deterministic set.
        """
        if surfaces is None:
            query_values = [company.get("name"), company.get("ticker"), company.get("isin"), company.get("borsdata_id")]
            urls = [self.base_url]
            for value in query_values:
                if value not in (None, ""):
                    urls.append(f"{self.base_url}/search?q={quote(str(value))}")
            fetched: list[tuple[str, str]] = []
            for url in urls[: self.max_surfaces]:
                try:
                    response = request_with_retry("GET", url, timeout=30, max_retries=MAX_RETRIES)
                except Exception as exc:
                    raise MfnIssuerAcquisitionError(
                        "mfn_issuer_fetch_failed", f"MFN issuer request failed: {exc}"
                    ) from exc
                if response.status_code >= 500 or response.status_code in {408, 429}:
                    raise MfnIssuerAcquisitionError(
                        "mfn_issuer_http_status",
                        f"MFN issuer request returned HTTP {response.status_code}",
                    )
                if response.status_code == 200:
                    fetched.append((url, response.text))
            surfaces = fetched
        candidates: list[dict[str, Any]] = []
        for surface_url, payload in surfaces:
            candidates.extend(
                parse_mfn_issuer_candidates(
                    payload,
                    company=company,
                    surface_url=surface_url,
                    base_url=self.base_url,
                    discovery_source="mfn_search_or_index",
                )
            )
        unique = {
            (candidate["mfn_slug"].lower(), _canonical_issuer_url(candidate["source_url"])): candidate
            for candidate in candidates
        }
        ordered = tuple(sorted(unique.values(), key=lambda item: (item["mfn_slug"].lower(), item["source_url"])))
        if len(ordered) == 1:
            return IssuerResolution("mapped", ordered, ordered[0], _now())
        if ordered:
            return IssuerResolution("ambiguous", ordered)
        return IssuerResolution("unmapped", ())

    def persist_resolution(
        self,
        conn: Any,
        company: dict[str, Any],
        resolution: IssuerResolution,
        *,
        discovery_source: str = "mfn_search_or_index",
    ) -> None:
        company_id = int(company["id"])
        persist_mfn_issuer_candidates(
            conn,
            company_id,
            list(resolution.candidates),
            discovery_source=discovery_source,
        )
        if resolution.status == "mapped" and resolution.selected:
            selected = resolution.selected
            upsert_mfn_issuer_mapping(
                conn,
                company_id,
                status="mapped",
                mfn_slug=selected["mfn_slug"],
                source_url=selected["source_url"],
                discovery_source=discovery_source,
                verified_at=resolution.verified_at or _now(),
                identity_evidence=selected.get("identity_evidence"),
            )
        else:
            upsert_mfn_issuer_mapping(
                conn,
                company_id,
                status=resolution.status,
                discovery_source=discovery_source,
                identity_evidence={
                    "candidate_count": len(resolution.candidates),
                    "candidate_ids": [
                        {"mfn_slug": item["mfn_slug"], "source_url": item["source_url"]}
                        for item in resolution.candidates
                    ],
                },
            )


def load_reviewed_mapping_seed(path: str) -> list[dict[str, Any]]:
    """Load a small, reviewed operator seed file without guessing identities."""
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    records = payload.get("mappings", []) if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise ValueError("MFN mapping seed must be a JSON list or {mappings: [...]} object")
    return [record for record in records if isinstance(record, dict)]


def apply_reviewed_mapping_seed(conn: Any, path: str) -> int:
    records = load_reviewed_mapping_seed(path)
    applied = 0
    for record in records:
        if record.get("reviewed") is not True:
            continue
        company_id = record.get("company_id")
        if company_id is None:
            raise ValueError("each reviewed MFN mapping seed requires company_id")
        status = record.get("status", "mapped")
        identity_evidence = record.get("identity_evidence")
        upsert_mfn_issuer_mapping(
            conn,
            int(company_id),
            status=status,
            mfn_slug=record.get("mfn_slug"),
            source_url=record.get("source_url"),
            discovery_source=record.get("discovery_source", "reviewed_seed"),
            verified_at=record.get("verified_at") or (_now() if status == "mapped" else None),
            identity_evidence=identity_evidence,
        )
        applied += 1
    return applied
