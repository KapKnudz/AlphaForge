"""Ownership — free stack."""

from alphaforge.ownership.parser import LIMITATIONS, parse_top10_holders, tag_mfn_events


def build_ownership_evidence(holders_text: str = "", mfn_text: str = "") -> dict[str, object]:
    """Production ownership-evidence assembly — always carries the three permanent limitations."""
    return {
        "holders": parse_top10_holders(holders_text),
        "events": tag_mfn_events(mfn_text),
        "limitations": LIMITATIONS,
    }


__all__ = ["LIMITATIONS", "build_ownership_evidence", "parse_top10_holders", "tag_mfn_events"]
