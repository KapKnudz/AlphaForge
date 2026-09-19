"""Deterministic market-cap hurdle policy for the reverse DCF."""

from dataclasses import dataclass
from datetime import date
from math import isfinite


@dataclass(frozen=True)
class RequiredReturnDecision:
    available: bool
    policy_version: str
    market_cap: float | None
    size_bucket: str | None
    required_return: float | None
    source_date: str | None
    missing_information: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class RequiredReturnPolicy:
    """Build a dated required return without beta or company-specific WACC."""

    VERSION = "required-return-v2-market-cap-buckets"
    SUPPORTED_CURRENCY = "SEK"
    SIZE_BUCKETS = (
        (1_000_000_000.0, "below_sek_1bn", 0.15),
        (5_000_000_000.0, "sek_1bn_to_below_5bn", 0.135),
        (30_000_000_000.0, "sek_5bn_to_below_30bn", 0.115),
        (float("inf"), "sek_30bn_and_above", 0.10),
    )

    def build(
        self,
        *,
        as_of: date,
        currency: str | None,
        market_cap: float | None,
    ) -> RequiredReturnDecision:
        normalized_currency = (currency or self.SUPPORTED_CURRENCY).upper()
        if normalized_currency != self.SUPPORTED_CURRENCY:
            return RequiredReturnDecision(
                available=False,
                policy_version=self.VERSION,
                market_cap=market_cap,
                size_bucket=None,
                required_return=None,
                source_date=as_of.isoformat(),
                missing_information=(
                    f"market-cap hurdle policy is defined for {self.SUPPORTED_CURRENCY}; "
                    f"received {normalized_currency}",
                ),
            )
        if market_cap is None or not isfinite(market_cap) or market_cap <= 0:
            return RequiredReturnDecision(
                available=False,
                policy_version=self.VERSION,
                market_cap=market_cap,
                size_bucket=None,
                required_return=None,
                source_date=as_of.isoformat(),
                missing_information=(
                    "positive market capitalization unavailable for required-return hurdle",
                ),
            )
        size_bucket, required_return = self._bucket(market_cap)
        return RequiredReturnDecision(
            available=True,
            policy_version=self.VERSION,
            market_cap=market_cap,
            size_bucket=size_bucket,
            required_return=required_return,
            source_date=as_of.isoformat(),
        )

    @classmethod
    def _bucket(
        cls,
        market_cap: float,
    ) -> tuple[str, float]:
        for upper_bound, bucket, required_return in cls.SIZE_BUCKETS:
            if market_cap < upper_bound:
                return bucket, required_return
        raise RuntimeError("size bucket policy is incomplete")
