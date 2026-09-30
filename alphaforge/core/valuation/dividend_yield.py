"""Current dividend yield: verified calendar TTM, no currency conversion."""

from __future__ import annotations

from calendar import monthrange
from dataclasses import asdict, dataclass
from datetime import date
from enum import StrEnum
from math import isfinite
from typing import Any

DIVIDEND_YIELD_POLICY_VERSION = "calendar-ttm-verified-v1"


class DividendYieldReason(StrEnum):
    COVERAGE_UNKNOWN = "dividend_coverage_unknown"
    COVERAGE_PARTIAL = "dividend_coverage_partial"
    CURRENCY_UNKNOWN = "dividend_currency_unknown"
    CURRENCY_MISMATCH = "dividend_currency_mismatch"
    PRICE_UNAVAILABLE = "dividend_price_unavailable"
    AMOUNT_UNAVAILABLE = "dividend_amount_unavailable"


def trailing_dividend_window(as_of: date) -> tuple[date, date]:
    """Calendar twelve months, (start,end]; Feb 29 anniversary clamps to Feb 28."""
    year = as_of.year - 1
    start = date(year, as_of.month, min(as_of.day, monthrange(year, as_of.month)[1]))
    return start, as_of


@dataclass(frozen=True)
class DividendYieldResult:
    value: float | None
    reason: DividendYieldReason | None
    window_start: str
    window_end: str
    price_currency: str | None
    distributions: list[dict[str, Any]]
    coverage: dict[str, Any] | None
    policy_version: str = DIVIDEND_YIELD_POLICY_VERSION
    interval: str = "(start,end]"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def is_known_currency(value: str | None) -> bool:
    # Do not case-fold denomination tags (GBp is not GBP), or admit
    # unknown/test currency markers as verified units.
    return bool(
        value
        and len(value) == 3
        and value.isascii()
        and value.isalpha()
        and value.isupper()
        and value not in {"XXX", "XTS"}
    )


def calculate_dividend_yield(
    as_of: date,
    close: float | None,
    price_currency: str | None,
    distributions: list[dict[str, Any]],
    coverage: dict[str, Any] | None,
) -> DividendYieldResult:
    start, end = trailing_dividend_window(as_of)
    relevant = [r for r in distributions if start.isoformat() < r["ex_date"] <= end.isoformat()]
    currency = price_currency.strip() if price_currency else None

    def result(value=None, reason=None):
        return DividendYieldResult(
            value, reason, start.isoformat(), end.isoformat(), currency, relevant, coverage
        )

    if not coverage or (coverage["window_start"], coverage["window_end"]) != (
        start.isoformat(),
        end.isoformat(),
    ):
        return result(reason=DividendYieldReason.COVERAGE_UNKNOWN)
    if coverage["status"] == "partial":
        return result(reason=DividendYieldReason.COVERAGE_PARTIAL)
    if (
        coverage["status"] != "complete"
        or not coverage.get("verified_at")
        or not coverage.get("assurance")
    ):
        return result(reason=DividendYieldReason.COVERAGE_UNKNOWN)
    if close is None or not isfinite(close) or close <= 0:
        return result(reason=DividendYieldReason.PRICE_UNAVAILABLE)
    if not is_known_currency(currency):
        return result(reason=DividendYieldReason.CURRENCY_UNKNOWN)
    for row in relevant:
        amount_currency = (row.get("currency") or "").strip()
        if not is_known_currency(amount_currency) or not row.get("currency_verified"):
            return result(reason=DividendYieldReason.CURRENCY_UNKNOWN)
        if amount_currency != currency:
            return result(reason=DividendYieldReason.CURRENCY_MISMATCH)
        if not isfinite(row["amount"]) or row["amount"] < 0:
            return result(reason=DividendYieldReason.AMOUNT_UNAVAILABLE)
    value = sum(float(row["amount"]) for row in relevant) / close * 100
    if not isfinite(value):
        return result(reason=DividendYieldReason.AMOUNT_UNAVAILABLE)
    return result(value)
