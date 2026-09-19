"""Per-share growth — pure."""

from __future__ import annotations

from alphaforge.core.financial.helpers import calculate_ratio


def per_share_values(values: list[float | None], shares: list[float | None]) -> list[float | None]:
    return [calculate_ratio(value, share) for value, share in zip(values, shares, strict=False)]


def calculate_per_share_growth(
    current_value: float | None,
    current_shares: float | None,
    history_values: list[float | None],
    shares_history: list[float | None],
) -> tuple[float | None, int]:
    """Growth of per-share metric, mirroring FinancialCalculator._growth."""
    per_share_hist = per_share_values(history_values, shares_history)
    current_per_share = calculate_ratio(current_value, current_shares)
    if current_per_share is None or current_per_share <= 0 or not per_share_hist:
        return None, 1
    previous = per_share_hist[-1]
    if previous is None or previous <= 0:
        return None, 1
    periods = min(3, len(per_share_hist))
    baseline = per_share_hist[-periods]
    if baseline is None or baseline <= 0:
        periods = 1
        baseline = previous
    return (current_per_share / baseline) ** (1 / periods) - 1, periods


def dilution_flag(share_count_growth: float | None) -> bool:
    return bool(share_count_growth is not None and share_count_growth > 0.05)


def share_count_growth(
    current_shares: float | None, shares_history: list[float | None]
) -> float | None:
    if current_shares is None or current_shares <= 0 or not shares_history:
        return None
    previous = shares_history[-1]
    if previous is None or previous <= 0:
        return None
    periods = min(3, len(shares_history))
    baseline = shares_history[-periods]
    if baseline is None or baseline <= 0:
        periods = 1
        baseline = previous
    return (current_shares / baseline) ** (1 / periods) - 1
