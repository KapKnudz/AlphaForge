"""Synthetic numerical disclosures only; never issuer or sector evidence."""

from datetime import date, timedelta

from alphaforge.core.valuation.reinvestment import CALIBRATION_VERSION
from alphaforge.db.reinvestment import append_reinvestment_calibration


def synthetic_record(
    *, future_return=0.20, end="2026-03-31", published="2026-05-01", observed="2026-06-01"
):
    ending = date.fromisoformat(end)
    beginning = ending.replace(year=ending.year - 1)
    start = beginning + timedelta(days=1)
    sources = {
        operand: {
            "source_id": "synthetic:capital-fixture",
            "url": "https://example.invalid/synthetic-capital-fixture",
            "sha256": "a" * 64,
            "anchor": f"synthetic:capital-fixture#{operand}",
            "accounting_date": beginning.isoformat() if operand == "capital_begin" else end,
            "published_on": published,
            "observed_on": observed,
        }
        for operand in ("normalized_ebit", "tax_rate", "capital_begin", "capital_end")
    }
    return {
        "company_id": 1,
        "version": CALIBRATION_VERSION,
        "method": "own_company_average_roic",
        "currency": "SEK",
        "original_currency": "SEK",
        "conversion_mode": "original",
        "units": "millions",
        "period_start": start.isoformat(),
        "period_end": end,
        "normalized_ebit": 25.0,
        "tax_rate": 0.21,
        "capital_begin": 19.75 / future_return,
        "capital_end": 19.75 / future_return,
        "future_return": future_return,
        "future_return_provenance": "company_history_calibrated_assumption",
        "capital_basis": "operating_invested_capital",
        "lease_basis": "ifrs16_debt",
        "earnings_basis": "reported_ifrs16_ebit",
        "consolidation_perimeter": "synthetic unchanged consolidated operating perimeter",
        "capital_reconciliation": "synthetic matched operating capital, includes operating NWC",
        "cash_nonoperating_treatment": "synthetic no excess cash/nonoperating assets",
        "goodwill_acquisition_treatment": "synthetic no acquisition or goodwill changes",
        "earnings_normalization": "synthetic recurring IFRS16 EBIT",
        "tax_basis": "synthetic normalized 21% cash operating tax assumption",
        "maintenance_capacity": "synthetic replacement spending maintains zero-growth capacity",
        "starting_capital_premise": "synthetic year-one capital already installed at valuation boundary",
        "future_return_rationale": "synthetic future marginal assumption equals historical average",
        "approval_id": "synthetic-test-only",
        "sources": sources,
    }


def synthetic_calibration_fixture(conn, cid, **kwargs):
    record = synthetic_record(**kwargs)
    record["company_id"] = cid
    return append_reinvestment_calibration(
        conn, cid, record, as_of=date.fromisoformat(record["sources"]["capital_end"]["observed_on"])
    )
