"""MFN top-10 deterministic parser + MFN event tagger.

Three permanent limitations (always carried on thesis limitations[]):
- Free-float % is unavailable
- Named large-holder coverage is unavailable beyond annual top 10
- Ownership-change history is unavailable at quarterly granularity
"""

from __future__ import annotations

import re

LIMITATIONS: list[str] = [
    "Free-float % is unavailable",
    "Named large-holder coverage is unavailable beyond annual top 10",
    "Ownership-change history is unavailable at quarterly granularity",
]


def parse_top10_holders(text: str) -> list[dict[str, object]]:
    """Deterministic top-10 holder extraction.

    Looks for holder table rows: Name — Shares — Pct patterns.
    Only returns when >2 rows with pct <100 and sum <100.
    """
    rows: list[dict[str, object]] = []
    # Pattern: name (2+ words) + share count (with space/comma) + pct with %
    # Handles Swedish thousands: "1 234 567" and "12,3 %"
    holder_pat = re.compile(
        r"(?P<name>[A-ZÅÄÖ][A-Za-zÅÄÖåäö .,&\-']{2,60}?)\s+"
        r"(?P<shares>[\d\s.,]+)\s+"
        r"(?P<pct>\d{1,2}[.,]\d+|\d{1,2})\s*%",
        re.UNICODE,
    )
    for m in holder_pat.finditer(text):
        name = m.group("name").strip(" .,-")
        if len(name) < 3 or name.lower() in {"aktieägare", "namn", "andel", "antal", "total"}:
            continue
        shares_raw = m.group("shares").replace(" ", "").replace(",", "")
        pct_raw = m.group("pct").replace(",", ".")
        try:
            shares = float(shares_raw)
            pct = float(pct_raw)
        except ValueError:
            continue
        if pct < 0 or pct > 100:
            continue
        if pct >= 100:
            continue
        rows.append({"holder_name": name, "shares": shares, "pct": pct, "raw_text": m.group(0)})

    if len(rows) <= 2:
        return []
    total_pct = sum(float(r["pct"]) for r in rows)  # type: ignore[arg-type]
    if total_pct >= 100:
        return []
    # Keep top 10 by pct descending if more than 10
    rows.sort(key=lambda r: float(r["pct"]), reverse=True)  # type: ignore[arg-type]
    return rows[:10]


def tag_mfn_events(text: str) -> list[dict[str, str]]:
    """Deterministic MFN release tagger for ownership events."""
    lower = text.lower()
    events: list[dict[str, str]] = []
    if re.search(r"flaggning|flagging|andel\s*%|change in.*holding", lower):
        # Extract pct if present
        m = re.search(r"(\d{1,2}[.,]\d+|\d{1,2})\s*%", text)
        pct = m.group(0) if m else ""
        events.append({"event_type": "flagging", "evidence": f"flaggning {pct}".strip()})
    if re.search(r"riktad emission|directed share issue|private placement", lower):
        events.append({"event_type": "placement", "evidence": "riktad emission"})
    if re.search(r"lock-?up", lower):
        # lock-up expiry
        m = re.search(r"lock-?up[^.]{0,60}?(\d{4}-\d{2}-\d{2}|\d+\s*dag)", lower)
        expiry = m.group(1) if m else ""
        events.append({"event_type": "lockup", "evidence": f"lock-up {expiry}".strip()})
    if re.search(r"aktieåterköp|share buyback|buyback", lower):
        events.append({"event_type": "share_change", "evidence": "aktieåterköp"})
    return events
