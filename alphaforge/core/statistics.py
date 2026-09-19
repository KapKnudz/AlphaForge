"""Pure statistics helpers — no DB, no model imports."""

from __future__ import annotations

import math
from statistics import mean


def pearson(values, right=None) -> float:
    pairs = list(zip(values, right, strict=False)) if right is not None else list(values)
    if len(pairs) < 3:
        return 0.0
    xs, ys = zip(*pairs, strict=False)
    x_mean, y_mean = mean(xs), mean(ys)
    numerator = sum((x - x_mean) * (y - y_mean) for x, y in pairs)
    denominator = math.sqrt(sum((x - x_mean) ** 2 for x in xs) * sum((y - y_mean) ** 2 for y in ys))
    return numerator / denominator if denominator else 0.0


def safe_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return numerator / denominator


def cagr(start: float | None, end: float | None, periods: int) -> float | None:
    if start is None or end is None or start <= 0 or end <= 0 or periods <= 0:
        return None
    return (end / start) ** (1.0 / periods) - 1.0
