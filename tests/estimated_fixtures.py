"""Entirely synthetic aggregate budgets and evidence, never issuer golden data."""

import copy
import hashlib

from alphaforge.core.valuation.estimated_fcff import BRIDGE_CATEGORIES
from alphaforge.core.valuation.estimated_inputs import CONTRACT, STANDARD


def block(**values):
    return {"record_ids": ["budget"], **values}


def pack():
    row = block(
        ebit="20",
        noncash_addbacks="5",
        other_noncash="0",
        operating_cash_tax="4",
        cash_investment="6",
        lease_additions="1",
        disposal_cash="0",
        wc_open="10",
        wc_close="10",
        other_operating_cash="0",
        finite_effects={"opening": "0"},
    )
    bridge = [
        block(
            id=k,
            category=k,
            allocation_id=k,
            method="supplied_pv",
            inputs={
                "amount": "20" if k == "accessible_cash" else "-5" if k == "lease_debt" else "0"
            },
        )
        for k in sorted(BRIDGE_CATEGORIES)
    ]
    scenario = {
        "operations": block(
            mode="annual",
            rows=[copy.deepcopy(row) for _ in range(12)],
            basis="recurring_ebit_after_da_no_compensation_addback",
            tax_basis="unlevered_including_opening_operating_once",
            continuing_capacity="Synthetic stable capacity; renewal funded",
            funding_access="Synthetic available cash and collections",
            noncash_compatibility="D&A reduced EBIT; no compensation addback",
            allocation_ids=["operating_tax", "renewal"],
        ),
        "discount": block(inputs={"wacc": "0.1"}),
        "terminal": block(
            mode="gordon",
            growth="0",
            sustainable_mechanism="Synthetic fully funded stationary economics",
        ),
        "bridge": block(
            valuation_date="2024-02-29", categories=sorted(BRIDGE_CATEGORIES), items=bridge
        ),
        "ownership": block(basic_shares="10000000"),
        "rights": block(
            valuation_date="2024-02-29",
            currency="EUR",
            method="endogenous_european_calls_basic_shares",
            items=[],
        ),
        "fx": block(valuation_date="2024-02-29", quote_currency="SEK", quote_per_reporting="2"),
    }
    return {
        "standard": STANDARD,
        "contract": CONTRACT,
        "pack_version": "synthetic-r1",
        "parent": None,
        "entity": "Synthetic Services",
        "perimeter": "Synthetic controlled operations only",
        "object": "controlled_nonfinancial_operations",
        "valuation_date": "2024-02-29",
        "knowledge_cutoff": "2024-03-15T12:00:00Z",
        "prepared_on": "2024-03-16",
        "prepared_by": "Synthetic preparer",
        "knowledge_basis": "retrospective_research",
        "currency": "EUR",
        "quote_currency": "SEK",
        "money_units": "millions",
        "share_units": "absolute",
        "lease_basis": "capitalized_debt_like",
        "conditions": ["Synthetic conditional budgets, not forecasts"],
        "limitations": [],
        "reviews": [],
        "sources": [
            {
                "id": "synthetic",
                "content_sha256": hashlib.sha256(b"synthetic evidence").hexdigest(),
                "published_on": "2024-03-01",
                "observed_on": "2024-02-29",
                "ingested_at": "2024-03-15T10:00:00Z",
                "anchors": ["budget-table"],
            }
        ],
        "records": {
            "budget": {
                "kind": "estimate",
                "status": "conditional",
                "anchors": [{"source_id": "synthetic", "anchor": "budget-table"}],
                "rationale": "Synthetic steady activity",
                "mechanism": "Funded renewal and cash tax",
                "counterevidence": "Synthetic demand can decline",
                "response": "Adverse scenario disclosed",
                "alternatives": "Higher cash tax and investment",
                "effective_period": "Twelve rolling years",
                "prepared_by": "Synthetic preparer",
                "prepared_on": "2024-03-16",
            }
        },
        "allocations": [{"id": k, "destination": "bridge"} for k in sorted(BRIDGE_CATEGORIES)]
        + [
            {"id": "operating_tax", "destination": "operations"},
            {"id": "renewal", "destination": "operations"},
        ],
        "scenarios": {"base": scenario},
    }


def response():
    p = pack()
    ops = p["scenarios"]["base"]["operations"]
    ops.pop("rows")
    ops["mode"] = "aggregate_response"
    ops["inputs"] = {
        "revenue": "100",
        "initial_growth": "0",
        "terminal_growth": "0",
        "mature_year": 8,
        "margin_first": ".2",
        "margin_terminal": ".2",
        "da_first": ".05",
        "da_terminal": ".05",
        "cash_renewal_rate": ".06",
        "cash_growth_capital": ".5",
        "lease_renewal_rate": ".01",
        "lease_growth_capital": ".1",
        "wc_ratio": ".1",
        "opening_wc": "10",
        "legacy_remaining": "0",
        "legacy_annual": "0",
        "opening_payable": "0",
        "opening_recoverable": "0",
        "opening_deferred": "0",
        "opening_deferred_years": 5,
        "opening_payment_fractions": ["1"],
        "opening_recovery_fractions": ["1"],
        "opening_recovery_fraction": "1",
        "refundable_profit_share": "0",
        "recovery_fraction": "1",
        "gross_charge_rate": ".4",
        "refund_rate": ".3",
        "minimum_rate": ".15",
        "ordinary_tax_rate": ".2",
        "same_year_payment": "1",
        "recovery_lag": 2,
        "settlements": [],
        "initial_cash_diagnostic": "20",
    }
    return p
