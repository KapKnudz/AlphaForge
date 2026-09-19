"""Per-share growth — pure."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from fractions import Fraction

from alphaforge.core.financial.helpers import calculate_ratio


def split_factor(split_type: str, ratio: str) -> float:
    """Return the multiplier that maps pre-event shares to post-event shares.

    Börsdata represents a forward split as ``S`` (for example ``5:1``) and a
    reverse split as ``RS`` (for example ``1:100``).  The provider's price
    history is already split-adjusted, but historical report share counts are
    not.  Invalid ratios are rejected instead of being treated as dilution.
    """
    try:
        left, right = (int(part.strip()) for part in str(ratio).split(":", 1))
        if left <= 0 or right <= 0:
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid stock split ratio: {ratio!r}") from exc
    kind = str(split_type).upper()
    if kind in {"S", "RS"}:
        # Börsdata's ``5:1`` means five post-split shares for one old share;
        # ``1:100`` is the corresponding one-for-one-hundred reverse split.
        return left / right
    raise ValueError(f"unsupported stock split type: {split_type!r}")


def cumulative_split_factor(
    splits: Iterable[tuple[str, str, date | str]],
    *,
    from_date: date | str | None = None,
    through_date: date | str | None = None,
) -> float:
    """Return the deterministic share-count factor over a date interval."""

    def as_date(value):
        return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])

    start = as_date(from_date) if from_date is not None else None
    end = as_date(through_date) if through_date is not None else None
    factor = Fraction(1, 1)
    for split_type, ratio, split_date in sorted(splits, key=lambda item: str(item[2])):
        event_date = as_date(split_date)
        if start is not None and event_date <= start:
            continue
        if end is not None and event_date > end:
            continue
        factor *= Fraction(str(split_factor(split_type, ratio)))
    return float(factor)


def adjust_historical_shares(
    shares: float | None,
    period_end: date | str,
    comparison_date: date | str,
    splits: Iterable[tuple[str, str, date | str]],
) -> float | None:
    """Adjust a historical share count into the comparison date's basis."""
    if shares is None:
        return None
    factor = cumulative_split_factor(splits, from_date=period_end, through_date=comparison_date)
    return shares * factor


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
