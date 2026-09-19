"""Financial helpers — pure."""

from __future__ import annotations

from alphaforge.core.statistics import cagr, safe_div


def calculate_ratio(numerator: float | None, denominator: float | None) -> float | None:
    return safe_div(numerator, denominator)


def is_turnaround(current_value: float | None, history: list[float | None]) -> bool:
    return bool(
        current_value is not None
        and current_value > 0
        and history
        and history[-1] is not None
        and history[-1] <= 0
    )


def is_deterioration(current_value: float | None, history: list[float | None]) -> bool:
    return bool(
        current_value is not None
        and current_value <= 0
        and history
        and history[-1] is not None
        and history[-1] > 0
    )


def has_earnings_one_off_risk(
    revenue_yoy: float | None,
    ebit_yoy: float | None,
    net_income_yoy: float | None,
) -> bool:
    if revenue_yoy is None:
        return False
    threshold = max(0.75, max(revenue_yoy, 0) * 2)
    return any(growth is not None and growth > threshold for growth in (ebit_yoy, net_income_yoy))


def calculate_yoy_change(current_value: float | None, history: list[float | None]) -> float | None:
    if current_value is None or not history or history[-1] in (None, 0):
        return None
    previous_value = history[-1]
    return (current_value - previous_value) / abs(previous_value)  # type: ignore[operator]


def calculate_pair_change(
    current_value: float | None, previous_value: float | None
) -> float | None:
    if current_value is None or previous_value in (None, 0):
        return None
    return (current_value - previous_value) / abs(previous_value)  # type: ignore[operator]


__all__ = [
    "calculate_ratio",
    "is_turnaround",
    "is_deterioration",
    "has_earnings_one_off_risk",
    "calculate_yoy_change",
    "calculate_pair_change",
    "safe_div",
    "cagr",
]
