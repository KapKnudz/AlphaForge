"""Gross → EBIT spread — pure."""

from __future__ import annotations


def gross_to_ebit_spread(
    gross_margin: float | None, operating_margin: float | None
) -> float | None:
    if gross_margin is None or operating_margin is None:
        return None
    return gross_margin - operating_margin


def margin_runway(gross_margin: float | None, operating_margin: float | None) -> float | None:
    """Alias for spread — defensible peak clues."""
    return gross_to_ebit_spread(gross_margin, operating_margin)
