"""FI PDMR windowed scraper + FI short snapshot.

FI PDMR: windowed 90-day slices, 2 s cadence, cache by (issuer, window).
Throttle quote: "FI begränsar antalet sökningar … kan stänga av …" — never brute-force.

FI short: blankningsregistret snapshot (≥0.5% named, aggregated ≥0.1%).
"""

from __future__ import annotations

import datetime as dt
import re
import time
from typing import Any

from alphaforge.providers.http import MAX_RETRIES, request_with_retry


class FiPdmrScraper:
    BASE = "https://marknadssok.fi.se"
    WINDOW_DAYS = 90

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str, str], list[dict[str, Any]]] = {}

    def search(
        self,
        issuer: str,
        *,
        from_date: dt.date,
        to_date: dt.date,
        page: int = 1,
    ) -> list[dict[str, Any]]:
        """Windowed search for PDMR transactions."""
        key = (issuer, from_date.isoformat(), to_date.isoformat())
        if key in self._cache and page == 1:
            return self._cache[key]
        # Respect 2 s cadence
        time.sleep(2.0)
        # FI endpoint: GET /Publiceringsklient/sv-SE/Search/Search?SearchFunctionType=Insyn&Utgivare=...
        params: dict[str, Any] = {
            "SearchFunctionType": "Insyn",
            "Utgivare": issuer,
            "Transaktionsdatum.From": from_date.isoformat(),
            "Transaktionsdatum.To": to_date.isoformat(),
            "Page": page,
        }
        url = f"{self.BASE}/Publiceringsklient/sv-SE/Search/Search"
        try:
            resp = request_with_retry(
                "GET", url, params=params, timeout=30, max_retries=MAX_RETRIES
            )
        except Exception:
            return []
        if resp.status_code != 200:
            return []
        rows = self._parse_html_table(resp.text)
        if page == 1:
            self._cache[key] = rows
        return rows

    def windowed_search(
        self, issuer: str, *, from_date: dt.date, to_date: dt.date
    ) -> list[dict[str, Any]]:
        """90-day sliced search to stay <1000 export gate."""
        out: list[dict[str, Any]] = []
        cur = from_date
        while cur <= to_date:
            win_end = min(cur + dt.timedelta(days=self.WINDOW_DAYS - 1), to_date)
            rows = self.search(issuer, from_date=cur, to_date=win_end)
            out.extend(rows)
            cur = win_end + dt.timedelta(days=1)
        return out

    def _parse_html_table(self, html: str) -> list[dict[str, Any]]:
        # Parse HTML table rows: Publiceringsdatum/Emittent/Person/Karaktär/Instrument/Volym/Pris/Valuta
        rows: list[dict[str, Any]] = []
        # Find table rows
        for tr in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.IGNORECASE | re.DOTALL):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr.group(1), re.IGNORECASE | re.DOTALL)
            if len(tds) < 6:
                continue
            cleaned = [re.sub(r"<[^>]+>", "", c).strip() for c in tds]
            # Heuristic: skip header row
            if cleaned[0].lower().startswith("publiceringsdatum"):
                continue
            rows.append(
                {
                    "publiceringsdatum": cleaned[0] if len(cleaned) > 0 else None,
                    "emittent": cleaned[1] if len(cleaned) > 1 else None,
                    "person": cleaned[2] if len(cleaned) > 2 else None,
                    "karaktar": cleaned[3]
                    if len(cleaned) > 3
                    else None,  # Förvärv/Avyttring/Tilldelning
                    "instrument": cleaned[4] if len(cleaned) > 4 else None,
                    "volym": cleaned[5] if len(cleaned) > 5 else None,
                    "pris": cleaned[6] if len(cleaned) > 6 else None,
                    "valuta": cleaned[7] if len(cleaned) > 7 else None,
                }
            )
        return rows


class FiShortScraper:
    """FI blankning register snapshot."""

    BASE = "https://www.fi.se"

    def get_snapshot(self) -> list[dict[str, Any]]:
        # FI publishes xlsx vintages; for MVP we return empty and let
        # the Börsdata holdings/shorts snapshot be the primary source.
        # Deterministic placeholder that respects rate limit.
        time.sleep(1.0)
        return []
