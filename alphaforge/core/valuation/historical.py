"""Historical valuation helpers — pure."""

from __future__ import annotations


def percentile(value: float | None, history: list[float]) -> float | None:
    if value is None or not history:
        return None
    values_below = sum(1 for x in history if x <= value)
    return values_below / len(history) * 100


def history_bound(history: list[float], percentile: float) -> float | None:
    values = sorted(value for value in history if value > 0)
    if len(values) < 5:
        return None
    position = (len(values) - 1) * percentile
    lower_index = int(position)
    upper_index = min(lower_index + 1, len(values) - 1)
    fraction = position - lower_index
    return values[lower_index] + (values[upper_index] - values[lower_index]) * fraction
