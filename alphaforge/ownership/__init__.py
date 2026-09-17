"""Ownership — free stack."""

from __future__ import annotations

from typing import Any

from alphaforge.ownership.parser import LIMITATIONS, parse_top10_holders, tag_mfn_events


def build_ownership_evidence(texts: list[str]) -> dict[str, Any]:
    """Build ownership evidence from MFN document texts.

    Returns dict with holders, events, and the three permanent limitations
    that are always carried on the thesis limitations[].
    """
    holders = []
    events = []
    for text in texts:
        holders.extend(parse_top10_holders(text))
        events.extend(tag_mfn_events(text))
    return {
        "holders": holders,
        "events": events,
        "limitations": list(LIMITATIONS),
    }
