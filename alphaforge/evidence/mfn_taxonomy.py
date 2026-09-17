"""Keyword taxonomy for MFN releases (deterministic pre-pass S5)."""

from __future__ import annotations

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
    "low": ["inbjudan", "invitation", "presentation", "webcast"],
}

REPORT_TERMS_SE: list[str] = [
    "delårsrapport",
    "bokslutskommuniké",
    "kvartalsrapport",
    "årsredovisning",
    "year-end report",
    "interim report",
    "annual report",
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


def is_report(title: str) -> bool:
    lower = title.lower()
    return any(term.lower() in lower for term in REPORT_TERMS_SE)
