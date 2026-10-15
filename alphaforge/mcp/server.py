"""Read-only MCP server exposing Börsdata and MFN through AlphaForge adapters.

Run (from the AlphaForge project root, project .venv active):

    python -m alphaforge.mcp.server

Transport is stdio, for registration in an MCP client (Claude Code, Codex,
etc.). Every tool is read-only. Each response carries the provider name,
fetch timestamp (UTC), request parameters, row count and a SHA-256 hash of
the returned payload, so a caller can pin the exact bytes a valuation used
even though the underlying live data may later be restated.

Requires BORSDATA_API_KEY (environment, or a .env file at the project root)
for the Börsdata tools. The MFN tools need no key.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from alphaforge.providers.borsdata.adapter import BorsdataAdapter
from alphaforge.providers.mfn.scraper import MfnScraper

# Load BORSDATA_API_KEY from the project .env when the client did not export it.
load_dotenv()

mcp = FastMCP("alphaforge-data")

_borsdata: BorsdataAdapter | None = None
_mfn: MfnScraper | None = None

# In-process cache: repeated agent questions must not re-hit the providers.
# Börsdata documents 100 calls / 10 s; instruments list is ~1.7k rows.
_cache: dict[str, Any] = {}


def _bd() -> BorsdataAdapter:
    global _borsdata
    if _borsdata is None:
        _borsdata = BorsdataAdapter()
    return _borsdata


def _mfn_client(max_articles: int = 24) -> MfnScraper:
    global _mfn
    if _mfn is not None:
        return _mfn
    if max_articles != 24:
        return MfnScraper(max_articles=max_articles)
    _mfn = MfnScraper()
    return _mfn


def _mfn_all(mfn_slug: str, *, reports_only: bool, limit: int) -> list[dict[str, Any]]:
    """Collect MFN feed items across pages, up to `limit` (MFN caps at 480).

    The issuer feed paginates with offset/limit (e.g. /all/a/<issuer> uses
    the same JSON contract); a single-page discovery caps at 24 items, so
    walk pages of 48 until the tail or the requested limit is reached.
    """
    limit = max(1, min(limit, 480))
    # The full archive lives under /all/a/<issuer>; the plain issuer page
    # only exposes the most recent page. Map bare slugs to the archive.
    slug = mfn_slug.strip("/")
    if slug.startswith("a/"):
        slug = f"all/{slug}"
    mfn_slug = slug
    client = _mfn_client(max_articles=limit)
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset: int | None = 0
    while offset is not None and len(collected) < limit:
        page, next_offset = client.discover_feed_paginated(
            mfn_slug, offset=offset, limit=48, reports_only=reports_only
        )
        for article in page:
            url = str(article.get("url") or "")
            if url and url not in seen:
                seen.add(url)
                collected.append(article)
        if not page:
            break
        offset = next_offset
    return collected[:limit]


def _envelope(source: str, params: dict[str, Any], data: Any) -> dict[str, Any]:
    payload = json.dumps(data, sort_keys=True, default=str, ensure_ascii=False)
    return {
        "source": source,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "params": params,
        "count": len(data) if isinstance(data, list) else (1 if data is not None else 0),
        "sha256": hashlib.sha256(payload.encode()).hexdigest(),
        "data": data,
    }


def _require_key() -> str | None:
    if not os.environ.get("BORSDATA_API_KEY") and not _bd().api_key:
        return "BORSDATA_API_KEY is not set; Börsdata tools are unavailable."
    return None


def _sanitize(message: str) -> str:
    """Never let the API key (sent as a query parameter) reach a client."""
    key = _bd().api_key
    if key:
        message = message.replace(key, "***")
    return message


def _guard(source: str, params: dict[str, Any], fn: Callable[[], Any]) -> dict[str, Any]:
    try:
        return _envelope(source, params, fn())
    except Exception as exc:  # provider/contract errors become tool results
        return {"source": source, "params": params, "error": _sanitize(str(exc))}


# ---------------------------------------------------------------- Börsdata


@mcp.tool()
def borsdata_search_company(query: str, limit: int = 20) -> dict[str, Any]:
    """Search Börsdata Nordic instruments by ticker or company name.

    Returns matching instruments with their Börsdata instrument id (insId),
    which the other Börsdata tools require. Matching is case-insensitive
    substring on ticker and name.
    """
    if err := _require_key():
        return {"error": err}

    def fetch() -> Any:
        if "instruments" not in _cache:
            _cache["instruments"] = _bd().get_instruments()
        q = query.strip().lower()
        rows = [
            row
            for row in _cache["instruments"]
            if q in str(row.get("ticker") or "").lower()
            or q in str(row.get("name") or "").lower()
            or q in str(row.get("isin") or "").lower()
        ]
        return rows[:limit]

    return _guard("borsdata/instruments", {"query": query, "limit": limit}, fetch)


@mcp.tool()
def borsdata_reports(ins_id: int, period_type: str = "year", limit: int = 20) -> dict[str, Any]:
    """Fetch financial reports (income statement, balance sheet, cash flow).

    period_type is one of "year", "quarter" or "r12" (rolling twelve months).
    Amounts follow Börsdata report metadata units (millions of currency
    unless the row states otherwise). Rows are returned newest-first as
    flattened by the adapter; use the report_Start_Date / report_End_Date
    fields in each row for period identity.
    """
    if err := _require_key():
        return {"error": err}

    def fetch() -> Any:
        rows = _bd().get_reports([ins_id])
        rows = [r for r in rows if r.get("period_type") == period_type]
        rows.sort(key=lambda r: str(r.get("report_End_Date") or ""), reverse=True)
        return rows[:limit]

    return _guard(
        "borsdata/reports",
        {"ins_id": ins_id, "period_type": period_type, "limit": limit},
        fetch,
    )


@mcp.tool()
def borsdata_kpi_history(
    ins_id: int,
    kpi_id: int,
    report_type: str = "year",
    price_type: str = "mean",
    limit: int = 40,
) -> dict[str, Any]:
    """Fetch history for one KPI (e.g. margins, returns, valuation ratios).

    kpi_id refers to Börsdata KPI metadata ids. Values are {y, p, v} rows:
    year, period, value. Note Börsdata KPI histories carry no observation
    date beyond year/period; do not treat them as point-in-time vintages.
    """
    if err := _require_key():
        return {"error": err}
    return _guard(
        "borsdata/kpi-history",
        {
            "ins_id": ins_id,
            "kpi_id": kpi_id,
            "report_type": report_type,
            "price_type": price_type,
            "limit": limit,
        },
        lambda: _bd().get_kpi_history(ins_id, kpi_id, report_type, price_type)[:limit],
    )


@mcp.tool()
def borsdata_kpi_summary(ins_id: int, report_type: str = "year") -> dict[str, Any]:
    """Fetch the KPI summary block for one instrument and report type."""
    if err := _require_key():
        return {"error": err}
    return _guard(
        "borsdata/kpi-summary",
        {"ins_id": ins_id, "report_type": report_type},
        lambda: _bd().get_kpi_summary(ins_id, report_type),
    )


@mcp.tool()
def borsdata_kpi_metadata() -> dict[str, Any]:
    """List Börsdata KPI metadata (ids, names, formats) for kpi_id lookup."""
    if err := _require_key():
        return {"error": err}

    def fetch() -> Any:
        if "kpi_metadata" not in _cache:
            _cache["kpi_metadata"] = _bd().get_kpi_metadata()
        return _cache["kpi_metadata"]

    return _guard("borsdata/kpi-metadata", {}, fetch)


@mcp.tool()
def borsdata_prices(ins_id: int, limit: int = 250) -> dict[str, Any]:
    """Fetch daily stock prices for one instrument, oldest first.

    Returns at most `limit` most recent rows. Prices carry a session date
    only (no close timestamp) and no currency field; the quote currency is
    the instrument's listing currency.
    """
    if err := _require_key():
        return {"error": err}
    return _guard(
        "borsdata/prices",
        {"ins_id": ins_id, "limit": limit},
        lambda: _bd().get_stock_prices(ins_id, max_count=limit),
    )


@mcp.tool()
def borsdata_dividends(ins_ids: list[int]) -> dict[str, Any]:
    """Fetch dividend calendar rows (ex-date and amount) for instruments."""
    if err := _require_key():
        return {"error": err}
    return _guard("borsdata/dividends", {"ins_ids": ins_ids}, lambda: _bd().get_dividends(ins_ids))


@mcp.tool()
def borsdata_report_calendar(ins_ids: list[int]) -> dict[str, Any]:
    """Fetch scheduled report dates for instruments (future dates are
    schedules, not proof of publication)."""
    if err := _require_key():
        return {"error": err}
    return _guard(
        "borsdata/report-calendar",
        {"ins_ids": ins_ids},
        lambda: _bd().get_report_calendar(ins_ids),
    )


# --------------------------------------------------------------------- MFN


@mcp.tool()
def mfn_reports(mfn_slug: str, limit: int = 480) -> dict[str, Any]:
    """List financial-report releases for an issuer on MFN.se.

    mfn_slug is the issuer path on mfn.se, in the form "a/<issuer>"
    (e.g. "a/evolution" for Evolution, "a/clas-ohlson" for Clas Ohlson).
    Only report releases are returned (annual and interim reports), with
    title and source URL for each.
    """
    return _guard(
        "mfn/reports",
        {"mfn_slug": mfn_slug, "limit": limit},
        lambda: _mfn_all(mfn_slug, reports_only=True, limit=limit),
    )


@mcp.tool()
def mfn_releases(mfn_slug: str, limit: int = 480) -> dict[str, Any]:
    """List all press releases for an issuer on MFN.se (news and events,
    not only financial reports), with title and source URL for each.
    mfn_slug uses the form "a/<issuer>", e.g. "a/evolution"."""
    return _guard(
        "mfn/releases",
        {"mfn_slug": mfn_slug, "limit": limit},
        lambda: _mfn_all(mfn_slug, reports_only=False, limit=limit),
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
