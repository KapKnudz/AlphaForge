"""Keyword taxonomy for MFN releases (deterministic pre-pass S5)."""

from __future__ import annotations

import re

CATEGORY_KEYWORDS_SE: dict[str, list[str]] = {
    "earnings": [
        "delårsrapport",
        "bokslutskommuniké",
        "kvartalsrapport",
        "årsredovisning",
        "year-end report",
        "interim report",
    ],
    "guidance": ["prognos", "guidance", "utsikter", "outlook"],
    "ownership_change": ["flaggning", "flagging", "ägarförteckning", "aktieägare"],
    "insider": ["insynshandel", "pdmr", "insider"],
    "buyback": ["aktieåterköp", "share buyback", "återköp"],
    "placement": ["riktad emission", "directed share issue", "private placement"],
    "lockup": ["lock-up", "lockup", "lock up"],
    "listing_change": ["listbyte", "listing change", "notering"],
    "contract_win": ["order", "avtal", "contract", "affär"],
}

IMPORTANCE_KEYWORDS_SE: dict[str, list[str]] = {
    "high": [
        "bokslutskommuniké",
        "årsredovisning",
        "flaggning",
        "riktad emission",
        "bud",
        "takeover",
    ],
    "medium": ["delårsrapport", "kvartalsrapport", "order", "avtal"],
    "low": [
        "inbjudan",
        "invitation",
        "presentation",
        "webcast",
        "earnings call",
        "conference call",
        "webinar",
    ],
}

REPORT_TERMS_SE: list[str] = [
    "delårsrapport",
    "bokslutskommuniké",
    "kvartalsrapport",
    "årsredovisning",
    "year-end report",
    "interim report",
    "annual report",
    "quarterly report",
    "year end report",
]

REPORT_TITLE_TERMS: list[str] = REPORT_TERMS_SE

# Combined for detection
ALL_REPORT_TERMS: list[str] = REPORT_TITLE_TERMS + [
    "bokslutskommuniké",
    "flaggning",
    "flagging",
    "riktad emission",
    "ägarförteckning",
    "aktieåterköp",
    "lock-up",
    "listbyte",
]


def classify_category(title: str, body: str = "") -> str:
    text = f"{title} {body}".lower()
    for cat, keywords in CATEGORY_KEYWORDS_SE.items():
        for kw in keywords:
            if kw.lower() in text:
                return cat
    return "other"


def classify_importance(title: str, body: str = "") -> str:
    text = f"{title} {body}".lower()
    for level in ("high", "medium", "low"):
        for kw in IMPORTANCE_KEYWORDS_SE[level]:
            if kw.lower() in text:
                return level
    return "low"


def report_kind(title: str) -> str | None:
    """Return the deterministic MFN report class, excluding schedule notices."""
    lower = " ".join(title.lower().split())
    if any(
        term in lower
        for term in ("årsredovisning", "annual report", "year-end report", "year end report")
    ):
        return "annual"
    if any(
        term in lower
        for term in REPORT_TERMS_SE
        if term not in {"årsredovisning", "annual report", "year-end report", "year end report"}
    ):
        return "quarterly"
    if re.search(r"\bq[1-4]\b", lower) and any(
        word in lower for word in ("report", "rapport", "kommuniké")
    ):
        return "quarterly"
    return None


def document_type(title: str) -> str | None:
    """Return the additive variant-identity document type for a report title.

    This never changes :func:`report_kind` (which keeps its ``annual`` /
    ``quarterly`` contract for the report filter and packets); it only keeps
    year-end and annual reports distinguishable for variant grouping. A Q4
    interim title without year-end markers maps to ``None`` so grouping falls
    back to the existing ``report_kind`` behavior.
    """
    lower = " ".join(title.lower().split())
    if any(term in lower for term in ("årsredovis", "annual report")):
        return "ANNUAL_REPORT"
    if any(term in lower for term in ("year-end report", "year end report", "bokslutskommunik")):
        return "YEAR_END_REPORT"
    quarter = re.search(r"\bq\s*([1-4])\b", lower)
    if quarter and any(
        word in lower for word in ("report", "rapport", "kommuniké", "interim", "delårs")
    ):
        if quarter.group(1) in {"1", "2", "3"}:
            return f"INTERIM_Q{quarter.group(1)}"
        return None
    return None


def is_report(title: str) -> bool:
    lower = " ".join(title.lower().split())
    if any(term in lower for term in ("report schedule", "rapportkalender", "financial calendar")):
        return False
    return report_kind(title) is not None


# Titles that announce an invitation, presentation, or webcast about a report —
# not the report itself. These titles can still match :func:`is_report` (e.g.
# "invitation to ... briefing for ... Q2 2026 report"), so the evidence lane
# must exclude them explicitly instead of absorbing their attachments.
INVITATION_MARKERS: tuple[str, ...] = tuple(IMPORTANCE_KEYWORDS_SE["low"])


def is_invitation_or_presentation(title: str) -> bool:
    """Return True when a release title is an invitation/presentation, not a report."""
    lower = " ".join(title.lower().split())
    return any(marker in lower for marker in INVITATION_MARKERS)
