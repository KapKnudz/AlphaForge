"""Qualified own-company history, distinct from assumed future marginal returns.

Records enter through trusted analyst review, not automatic extraction. Validation
checks consistency/provenance, not the truth of an analyst's accounting choices.
"""

import json
from dataclasses import dataclass
from datetime import date, timedelta
from hashlib import sha256
from math import isclose, isfinite

CALIBRATION_VERSION = "own-company-average-roic-v1"
CALIBRATION_EVIDENCE_OPERANDS = (
    "capital_begin",
    "capital_end",
    "normalized_ebit",
    "tax_rate",
)
ECONOMIC_CONVENTION = "forward-funded-constant-margin-hurdle-convergence-v1"
LEGACY_CONVENTION = "legacy-capped-revenue-growth-v13"


def calibration_json(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)


def calibration_identity(record: dict) -> str:
    return sha256(calibration_json(record).encode()).hexdigest()


def calibration_operands_identity(record: dict) -> str:
    return calibration_identity(
        {key: value for key, value in record.items() if key != "reverse_growth_coverage"}
    )


@dataclass(frozen=True)
class ReinvestmentCalibration:
    identity: str
    historical_average_roic: float
    future_incremental_return: float
    record_json: str


def qualify_calibration(
    record: dict, *, as_of: date, currency: str, tax_rate: float
) -> ReinvestmentCalibration:
    """Fail closed on incomplete, mismatched or mislabeled accounting evidence."""
    if not isinstance(record, dict):
        raise ValueError("calibration must be a record")
    if type(record.get("company_id")) is not int or record["company_id"] <= 0:
        raise ValueError("calibration requires an own-company identity")
    if record.get("version") != CALIBRATION_VERSION:
        raise ValueError("unsupported calibration version")
    if record.get("method") != "own_company_average_roic":
        raise ValueError("only own-company average ROIC calibration is supported")
    if record.get("future_return_provenance") != "company_history_calibrated_assumption":
        raise ValueError("historical average ROIC is not observed future incremental ROIC")
    if record.get("currency") != currency or record.get("units") != "millions":
        raise ValueError("calibration denomination mismatch")
    if record.get("original_currency") != currency or record.get("conversion_mode") != "original":
        raise ValueError("this slice requires matched original-currency capital and earnings")
    start = date.fromisoformat(record["period_start"])
    end = date.fromisoformat(record["period_end"])
    if not 364 <= (end - start).days <= 371 or end > as_of:
        raise ValueError("calibration requires a completed annual fiscal period")
    amounts = tuple(
        record[key]
        for key in ("normalized_ebit", "tax_rate", "capital_begin", "capital_end", "future_return")
    )
    if any(
        isinstance(x, bool) or not isinstance(x, (int, float)) or not isfinite(x) for x in amounts
    ):
        raise ValueError("calibration amounts must be finite numbers")
    ebit, tax, beginning, ending, future = amounts
    if ebit <= 0 or beginning <= 0 or ending <= 0 or future <= 0 or not 0 <= tax < 1:
        raise ValueError("calibration requires positive profit and operating capital")
    if tax != tax_rate:
        raise ValueError("calibration and forecast tax basis mismatch")
    if record.get("capital_basis") != "operating_invested_capital":
        raise ValueError("ROCE, equity and total assets are not operating invested capital")
    if record.get("lease_basis") != "ifrs16_debt":
        raise ValueError("this slice only supports consistently reviewed IFRS16 debt basis")
    if record.get("earnings_basis") != "reported_ifrs16_ebit":
        raise ValueError("calibration earnings must match the reported IFRS16 forecast basis")
    for key in (
        "capital_reconciliation",
        "consolidation_perimeter",
        "cash_nonoperating_treatment",
        "goodwill_acquisition_treatment",
        "earnings_normalization",
        "tax_basis",
        "maintenance_capacity",
        "starting_capital_premise",
        "future_return_rationale",
        "approval_id",
    ):
        if not isinstance(record.get(key), str) or not record[key].strip():
            raise ValueError(f"calibration requires {key}")
    sources = record.get("sources")
    if not isinstance(sources, dict) or not set(CALIBRATION_EVIDENCE_OPERANDS) <= sources.keys():
        raise ValueError("calibration requires source provenance for every operand")
    for operand in CALIBRATION_EVIDENCE_OPERANDS:
        source = sources[operand]
        if not isinstance(source, dict):
            raise ValueError("invalid calibration source")
        for key in ("source_id", "url", "anchor"):
            if not isinstance(source.get(key), str) or not source[key].strip():
                raise ValueError(f"calibration source requires {key}")
        checksum = source.get("sha256", "")
        if (
            not isinstance(checksum, str)
            or len(checksum) != 64
            or any(c not in "0123456789abcdef" for c in checksum)
        ):
            raise ValueError("calibration requires a source content hash")
        published = date.fromisoformat(source["published_on"])
        observed = date.fromisoformat(source["observed_on"])
        if published > observed or observed > as_of:
            raise ValueError("calibration source unavailable at cutoff")
        operand_date = date.fromisoformat(source["accounting_date"])
        expected = start - timedelta(days=1) if operand == "capital_begin" else end
        if operand_date != expected or published < operand_date:
            raise ValueError("calibration operand fiscal date mismatch")
    average_return = ebit * (1 - tax) / ((beginning + ending) / 2)
    if not isfinite(average_return) or not isclose(future, average_return, rel_tol=1e-12):
        raise ValueError(
            "initial future return must be explicitly calibrated to historical average"
        )
    return ReinvestmentCalibration(
        calibration_identity(record), average_return, future, calibration_json(record)
    )
