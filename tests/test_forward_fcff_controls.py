"""Independent aggregate vintage, WC, boundary/deferred cash and premise controls."""

from __future__ import annotations

import copy
from decimal import Decimal as D

import pytest
from forward_fcff_fixtures import claim, complete_bridge, request

from alphaforge.core.valuation.forward_fcff import evaluate
from alphaforge.core.valuation.forward_fcff_contract import canonical


def no_tax(req):
    req.pop("terminal", None)
    req["operating_schedule"]["tax_lots"] = []
    for row in req["operating_schedule"]["periods"]:
        row["tax_ledgers"] = []
        row["tax_not_applicable"] = "Explicit synthetic tax not applicable"


def control(f, opening, addition, dep, closing, **extra):
    a = {
        "id": "plant",
        "opening": f.operand(opening),
        "commissioned_additions": f.operand(addition),
        "construction_open": f.operand(0),
        "construction_close": f.operand(0),
        "transfers": f.operand(0),
        "depreciation": f.operand(dep),
        "impairment": f.operand(0),
        "disposal_reduction": f.operand(0),
        "other": [],
        "closing": f.operand(closing),
        "continuation_basis": "Independent supplied finite depreciation weights and renewal premise",
    }
    for key, value in extra.items():
        a[key] = f.operand(value)
    return a


def cohort(f, asset_class, amount, period=1):
    return {
        "id": "finite-cohort",
        "asset_class": asset_class,
        "cost": f.operand(amount),
        "residual": f.operand(0),
        "life_years": f.operand(1),
        "in_service_date": "2027-01-01",
        "method": "supplied_weights",
        "weights": [{"period_index": period, "depreciation": f.operand(amount)}],
        "remaining_basis": "Explicit source-free finite cohort",
    }


def investment(f, identity, amount, idx, commissioned, kind="cash_purchase"):
    return {
        "id": identity,
        "asset_class": "plant",
        "kind": kind,
        "amount": f.operand(amount),
        "spend_period": idx,
        "in_service_date": commissioned,
        "classification": "renewal",
        "rationale": "Independent supplied spend and commissioning, no universal life",
    }


def test_a2_nondefault_three_year_midpoint_vintage_and_optional_cohorts():
    req, f = request()
    no_tax(req)
    rows = req["operating_schedule"]["periods"][:4]
    req["operating_schedule"]["periods"] = rows
    deps = [4, 8, 8, 4]
    closings = [20, 12, 4, 0]
    opening = 0
    for idx, (row, dep, closing) in enumerate(zip(rows, deps, closings, strict=True), 1):
        row["book_da"] = f.operand(dep)
        row["asset_controls"] = [control(f, opening, 24 if idx == 1 else 0, dep, closing)]
        row["investment_items"] = (
            [investment(f, "vintage24", 24, 1, "2027-01-01")] if idx == 1 else []
        )
        opening = closing
    result = evaluate(req)
    assert result["operating_cash_flows"]["available"], result["operating_cash_flows"]
    expected = [float(D(100) - D(24)), 100, 110, 121]
    assert [r["fcff_operations"] for r in result["operating_cash_flows"]["value"]] == expected
    original = canonical(result["operating_cash_flows"])
    req["operating_schedule"]["cohorts"] = [
        {
            "id": "finite24",
            "asset_class": "plant",
            "cost": f.operand(24),
            "residual": f.operand(0),
            "life_years": f.operand(3),
            "in_service_date": "2027-01-01",
            "method": "supplied_weights",
            "weights": [
                {"period_index": idx, "depreciation": f.operand(dep)}
                for idx, dep in enumerate(deps, 1)
            ],
            "remaining_basis": "New 3-year half-year supplied finite vintage",
        }
    ]
    assert canonical(evaluate(req)["operating_cash_flows"]) == original
    req["operating_schedule"]["cohorts"][0]["residual"]["value"] = 3
    assert not evaluate(req)["operating_cash_flows"]["available"]


@pytest.mark.parametrize("amount", [10, 0.0000000001])
def test_nonzero_cohort_depreciation_requires_matching_asset_control(amount):
    req, f = request()
    no_tax(req)
    req["operating_schedule"]["cohorts"] = [cohort(f, "ghost", amount)]

    result = evaluate(req)
    assert result["operating_cash_flows"]["reason_codes"] == ["cohort_class_da_mismatch"]
    assert not any(
        result[name]["available"]
        for name in ("operating_cash_flows", "operating_value", "common_equity", "per_share")
    )


@pytest.mark.parametrize("asset_class", ["", None, 7, {}, []])
def test_cohort_asset_class_must_be_nonempty_text(asset_class):
    req, f = request()
    no_tax(req)
    req["operating_schedule"]["cohorts"] = [cohort(f, asset_class, 10)]

    result = evaluate(req)
    assert result["operating_cash_flows"]["reason_codes"] == ["nonempty_text_required"]


def test_zero_cohort_is_exempt_and_aligned_cohort_reconciles():
    zero, zero_inputs = request()
    no_tax(zero)
    zero["operating_schedule"]["cohorts"] = [cohort(zero_inputs, "ghost", 0)]
    assert evaluate(zero)["operating_cash_flows"]["available"]

    aligned, aligned_inputs = request()
    no_tax(aligned)
    aligned["operating_schedule"]["cohorts"] = [cohort(aligned_inputs, "ghost", 10)]
    for index, row in enumerate(aligned["operating_schedule"]["periods"], 1):
        ghost = control(
            aligned_inputs,
            10 if index == 1 else 0,
            0,
            10 if index == 1 else 0,
            0,
        )
        ghost["id"] = "ghost"
        row["asset_controls"].append(ghost)
    aligned["operating_schedule"]["periods"][0]["book_da"] = aligned_inputs.operand(10)

    result = evaluate(aligned)
    assert result["operating_cash_flows"]["available"]
    assert result["operating_cash_flows"]["value"][0]["ebit"] == 90
    assert result["operating_cash_flows"]["value"][0]["fcff_operations"] == 100


def test_a2_cash_advance_spent_once_commissioned_next_year():
    req, f = request()
    no_tax(req)
    rows = req["operating_schedule"]["periods"][:2]
    req["operating_schedule"]["periods"] = rows
    rows[0]["investment_items"] = [investment(f, "advance30", 30, 1, "2028-01-01")]
    rows[0]["asset_controls"] = [control(f, 0, 0, 0, 0, construction_close=30)]
    rows[1]["book_da"] = f.operand(3)
    rows[1]["asset_controls"] = [control(f, 0, 30, 3, 27, construction_open=30, transfers=30)]
    rows[1]["asset_controls"][0]["transfer_events"] = [
        {
            "id": "commission-advance30",
            "in_service_date": "2028-01-01",
            "amount": f.operand(30),
            "rationale": "Advance enters service, no second economic spend",
        }
    ]
    result = evaluate(req)
    assert result["operating_cash_flows"]["available"], result["operating_cash_flows"]
    x = result["operating_cash_flows"]["value"]
    assert x[0]["fcff_operations"] == 70 and x[0]["book_da"] == 0
    assert x[1]["fcff_operations"] == 100 and x[1]["gross_economic_investment"] == 0
    rows[1]["asset_controls"][0]["transfer_events"] = []
    assert not evaluate(req)["operating_cash_flows"]["available"]


def test_a4_capitalized_lease_70_no_principal_interest_shield():
    req, f = request()
    no_tax(req)
    row = req["operating_schedule"]["periods"][0]
    req["operating_schedule"]["periods"] = [row]
    row["book_da"] = f.operand(10)
    row["asset_controls"] = [control(f, 40, 12, 10, 42)]
    row["investment_items"] = [
        investment(f, "lease12", 12, 1, "2027-01-01", "economic_lease_addition")
    ]
    row["tax_not_applicable"] = None
    req["operating_schedule"]["tax_lots"] = [
        {
            "id": "current-tax18",
            "ledger_id": "tax",
            "origin": "forecast_created",
            "leg": "payable",
            "source_period": 1,
            "amount": f.operand(18),
            "eligible_date": None,
            "prerequisites": [],
            "eligibility_basis": "Explicit same-year gross operating payment",
            "accounting_date": row["end"],
        }
    ]
    ledger = {
        "id": "tax",
        "scope": "operating_unlevered",
        "loss_basis": None,
        "deferred_basis": "Explicit no old/new differences",
        "payable_other": [],
        "receivable_other": [],
        "allocations": [
            {
                "id": "pay18",
                "kind": "payment",
                "lot_id": "current-tax18",
                "amount": f.operand(18),
                "cash_date": None,
                "eligibility_order": {
                    "rank": 1,
                    "basis": "Explicit synthetic single cash event order",
                },
            }
        ],
    }
    for key, value in {
        "taxable_basis": 90,
        "tax_da": 10,
        "nondeductible": 0,
        "loss_use": 0,
        "permanent_difference": 0,
        "payable_open": 0,
        "payable_close": 0,
        "arising": 18,
        "payment": 18,
        "receivable_open": 0,
        "receivable_close": 0,
        "entitlement": 0,
        "refund": 0,
        "writeoff": 0,
    }.items():
        ledger[key] = f.operand(value)
    row["tax_ledgers"] = [ledger]
    row["financing_diagnostics"] = [
        {
            "id": "principal9",
            "kind": "lease_principal",
            "amount": f.operand(9),
            "rationale": "Excluded financing",
        },
        {
            "id": "interest3",
            "kind": "lease_interest",
            "amount": f.operand(3),
            "rationale": "Excluded financing",
        },
    ]
    result = evaluate(req)
    assert result["operating_cash_flows"]["available"]
    assert result["operating_cash_flows"]["value"][0]["fcff_operations"] == 70
    row["financing_diagnostics"].append(
        {
            "id": "shield",
            "kind": "interest_shield",
            "amount": f.operand(0.6),
            "rationale": "Not allowed",
        }
    )
    assert not evaluate(req)["operating_cash_flows"]["available"]


def test_cross_ledger_prerequisites_follow_cash_chronology_not_ids():
    req, f = request()
    req.pop("terminal")
    for lot in req["operating_schedule"]["tax_lots"]:
        lot["ledger_id"] = "b" if lot["leg"] == "payable" else "a"
        if lot["leg"] == "refund":
            lot["prerequisites"] = [f"payable-{lot['source_period']}"]

    def suffix_operands(tree, suffix):
        if isinstance(tree, dict):
            if set(tree) == {"value", "provenance"}:
                tree["provenance"]["id"] += suffix
            else:
                for value in tree.values():
                    suffix_operands(value, suffix)
        elif isinstance(tree, list):
            for value in tree:
                suffix_operands(value, suffix)

    for row in req["operating_schedule"]["periods"]:
        original = row["tax_ledgers"][0]
        payment = copy.deepcopy(original)
        refund = copy.deepcopy(original)
        payment["id"] = "b"
        refund["id"] = "a"
        suffix_operands(payment, "-b")
        suffix_operands(refund, "-a")
        for key in (
            "receivable_open",
            "receivable_close",
            "entitlement",
            "refund",
            "writeoff",
        ):
            payment[key]["value"] = 0
        for key in (
            "taxable_basis",
            "payable_open",
            "payable_close",
            "arising",
            "payment",
        ):
            refund[key]["value"] = 0
        for ledger, allocated in (
            (payment, row["cash_operating_profit_before_da"]["value"]),
            (refund, 0),
        ):
            ledger["allocated_ebit"] = f.operand(allocated)
            ledger["allocated_book_da"] = f.operand(0)
            ledger["allocation_basis"] = "Explicit synthetic ledger allocation"
        payment["allocations"] = [
            allocation for allocation in payment["allocations"] if allocation["kind"] == "payment"
        ]
        refund["allocations"] = [
            allocation for allocation in refund["allocations"] if allocation["kind"] == "refund"
        ]
        year = row["start"][:4]
        for allocation in payment["allocations"]:
            allocation["cash_date"] = f"{year}-07-01"
        for allocation in refund["allocations"]:
            allocation["cash_date"] = f"{year}-08-01"
        row["tax_ledgers"] = [refund, payment]

    first = evaluate(req)
    assert first["operating_cash_flows"]["available"], first["operating_cash_flows"]
    renamed = copy.deepcopy(req)
    for lot in renamed["operating_schedule"]["tax_lots"]:
        if lot["ledger_id"] == "a":
            lot["ledger_id"] = "z"
    for row in renamed["operating_schedule"]["periods"]:
        for ledger in row["tax_ledgers"]:
            if ledger["id"] == "a":
                ledger["id"] = "z"
    second = evaluate(renamed)
    assert second["operating_cash_flows"]["available"]
    cash_fields = (
        "gross_tax_payment",
        "gross_tax_refund",
        "boundary_tax_payment",
        "boundary_tax_refund",
        "fcff_operations",
    )
    assert [
        tuple(row[field] for field in cash_fields)
        for row in second["operating_cash_flows"]["value"]
    ] == [
        tuple(row[field] for field in cash_fields) for row in first["operating_cash_flows"]["value"]
    ]

    structured = copy.deepcopy(renamed)
    for row in structured["operating_schedule"]["periods"]:
        allocations = [
            allocation for ledger in row["tax_ledgers"] for allocation in ledger["allocations"]
        ]
        for rank, allocation in enumerate(
            sorted(
                allocations,
                key=lambda allocation: (
                    allocation["kind"] != "payment",
                    allocation["lot_id"],
                ),
            ),
            1,
        ):
            allocation["cash_date"] = None
            allocation["eligibility_order"] = {
                "rank": rank,
                "basis": "Explicit synthetic cross-ledger sequence",
            }
    structured_result = evaluate(structured)
    assert structured_result["operating_cash_flows"]["available"]
    permuted = copy.deepcopy(structured)
    for row in permuted["operating_schedule"]["periods"]:
        row["tax_ledgers"].reverse()
        for ledger in row["tax_ledgers"]:
            ledger["allocations"].reverse()
    assert canonical(evaluate(permuted)) == canonical(structured_result)

    refund_first = copy.deepcopy(structured)
    second_row = refund_first["operating_schedule"]["periods"][1]
    second_allocations = [
        allocation for ledger in second_row["tax_ledgers"] for allocation in ledger["allocations"]
    ]
    next(allocation for allocation in second_allocations if allocation["kind"] == "refund")[
        "eligibility_order"
    ]["rank"] = 1
    next(allocation for allocation in second_allocations if allocation["lot_id"] == "payable-1")[
        "eligibility_order"
    ]["rank"] = 3
    assert evaluate(refund_first)["operating_cash_flows"]["reason_codes"] == [
        "refund_eligibility_unestablished"
    ]

    tied = copy.deepcopy(structured)
    second_row = tied["operating_schedule"]["periods"][1]
    second_allocations = [
        allocation for ledger in second_row["tax_ledgers"] for allocation in ledger["allocations"]
    ]
    second_allocations[1]["eligibility_order"]["rank"] = second_allocations[0]["eligibility_order"][
        "rank"
    ]
    assert evaluate(tied)["operating_cash_flows"]["reason_codes"] == ["tax_event_order_invalid"]

    late = copy.deepcopy(renamed)
    for row in late["operating_schedule"]["periods"]:
        year = row["start"][:4]
        for ledger in row["tax_ledgers"]:
            if ledger["id"] == "b":
                for allocation in ledger["allocations"]:
                    allocation["cash_date"] = f"{year}-09-01"
    assert evaluate(late)["operating_cash_flows"]["reason_codes"] == [
        "refund_eligibility_unestablished"
    ]


def test_a5_wc_signed_releases_and_deficit_funding_premise():
    req, f = request()
    no_tax(req)
    rows = req["operating_schedule"]["periods"][:2]
    req["operating_schedule"]["periods"] = rows
    req["operating_schedule"]["initial_wc"] = f.operand(10)
    for row, opening, closing in zip(rows, [10, 14], [14, 11], strict=True):
        row["wc_open"] = f.operand(opening)
        row["wc_close"] = f.operand(closing)
    result = evaluate(req)
    x = result["operating_cash_flows"]["value"]
    assert [r["delta_wc"] for r in x] == [4, -3]
    assert sum(r["delta_wc"] for r in x) == 1
    rows[0]["wc_close"]["value"] = 1000
    rows[1]["wc_open"]["value"] = 1000
    result = evaluate(req)
    assert result["operating_cash_flows"]["value"][0]["fcff_operations"] < 0
    assert not result["operating_value"]["available"]  # no complete tail, never clipped to zero


def boundary_request():
    req, f = request(profits=(20, 30, 30), tau=0, rho=0, horizon=1, growth=0, rate=0.1)
    no_tax(req)
    rows = req["operating_schedule"]["periods"][:2]
    req["operating_schedule"]["periods"] = rows
    req["terminal"] = {
        "kind": "finite_runoff",
        "horizon": 2,
        "residual_zero": True,
        "rationale": "Complete finite operating runoff, boundary claims valued independently",
    }
    lots = []
    for leg, amount in (("payable", 40), ("refund", 25)):
        lots.append(
            {
                "id": "opening-" + leg,
                "ledger_id": "boundary",
                "origin": "boundary_existing",
                "leg": leg,
                "source_period": 0,
                "amount": f.operand(amount),
                "eligible_date": None,
                "prerequisites": [],
                "eligibility_basis": "Hypothetical boundary claim timing, not factual zero",
                "accounting_date": req["valuation_date"],
            }
        )
    req["operating_schedule"]["tax_lots"] = lots
    for idx, row in enumerate(rows, 1):
        allocations = []
        for kind, leg, amount in (
            ("payment", "payable", 40 if idx == 1 else 0),
            ("refund", "refund", 20 if idx == 2 else 0),
            ("writeoff", "refund", 5 if idx == 1 else 0),
        ):
            if amount:
                allocations.append(
                    {
                        "id": f"{kind}-{idx}",
                        "kind": kind,
                        "lot_id": "opening-" + leg,
                        "amount": f.operand(amount),
                        "cash_date": None,
                        "eligibility_order": {
                            "rank": len(allocations) + 1,
                            "basis": "Explicit synthetic within-year sequence",
                        },
                    }
                )
        ll = {
            "id": "boundary",
            "scope": "operating_unlevered",
            "taxable_basis": f.operand(row["cash_operating_profit_before_da"]["value"]),
            "tax_da": f.operand(0),
            "nondeductible": f.operand(0),
            "loss_use": f.operand(0),
            "permanent_difference": f.operand(0),
            "loss_basis": None,
            "deferred_basis": "No new differences; cash effects are separately identified",
            "payable_open": f.operand(40 if idx == 1 else 0),
            "payable_close": f.operand(0),
            "arising": f.operand(0),
            "payment": f.operand(40 if idx == 1 else 0),
            "payable_other": [],
            "receivable_open": f.operand(25 if idx == 1 else 20),
            "receivable_close": f.operand(20 if idx == 1 else 0),
            "entitlement": f.operand(0),
            "refund": f.operand(20 if idx == 2 else 0),
            "writeoff": f.operand(5 if idx == 1 else 0),
            "receivable_other": [],
            "allocations": allocations,
        }
        row["tax_ledgers"] = [ll]
        row["tax_not_applicable"] = None
    return req, f


def test_tax_lot_origin_period_and_boundary_date_are_bidirectional():
    forecast, _ = request()
    assert evaluate(forecast)["operating_cash_flows"]["available"]
    forecast["operating_schedule"]["tax_lots"][0]["source_period"] = 0
    assert evaluate(forecast)["operating_cash_flows"]["reason_codes"] == [
        "tax_lot_origin_period_mismatch"
    ]

    boundary, _ = boundary_request()
    assert evaluate(boundary)["operating_cash_flows"]["available"]
    wrong_period = copy.deepcopy(boundary)
    wrong_period["operating_schedule"]["tax_lots"][0]["source_period"] = 1
    assert evaluate(wrong_period)["operating_cash_flows"]["reason_codes"] == [
        "tax_lot_origin_period_mismatch"
    ]
    boundary["operating_schedule"]["tax_lots"][0]["accounting_date"] = "2026-06-29"
    assert evaluate(boundary)["operating_cash_flows"]["reason_codes"] == [
        "boundary_lot_date_mismatch"
    ]


def test_a3_boundary_lots_writeoff_not_cash_and_bridge_once():
    req, f = boundary_request()
    result = evaluate(req)
    assert result["operating_value"]["available"], result["operating_value"]
    rows = result["operating_cash_flows"]["value"]
    assert [r["fcff_operations"] for r in rows] == [20, 30]
    assert [r["fcff_with_boundary_tax"] for r in rows] == [-20, 50]
    expected = D(40) / D("1.1") - D(20) / D("1.1") ** 2
    assert result["boundary_tax_pv"] == pytest.approx(float(expected))
    complete_bridge(req, f)  # Cannot declare actual known positive lots not applicable.
    assert not evaluate(req)["common_equity"]["available"]
    items = [
        claim(req, f, "opening-payable", "opening_current_tax", float(D(40) / D("1.1"))),
        claim(
            req, f, "opening-refund", "opening_current_tax", float(D(20) / D("1.1") ** 2), "asset"
        ),
    ]
    complete_bridge(req, f, items)
    result = evaluate(req)
    assert result["common_equity"]["available"], result["common_equity"]
    assert result["common_equity"]["value"] == pytest.approx(
        result["operating_value"]["value"] - float(expected)
    )
    req["bridge"]["boundary_cash"] = [
        {
            "id": "double-tax",
            "lot_id": "opening-payable",
            "period_index": 1,
            "direction": "obligation",
            "amount": f.operand(40),
            "claim_rate": f.operand(0.1),
            "rationale": "Duplicate claim should refuse",
        }
    ]
    scheduled = claim(req, f, "double-tax", "opening_current_tax", 40)
    scheduled["location"] = "boundary_schedule"
    scheduled["period_index"] = 1
    req["bridge"]["items"].append(scheduled)
    assert evaluate(req)["common_equity"]["reason_codes"] == ["double_counted_event"]


def test_boundary_cash_requires_complete_value_reconciled_inventory_links():
    req, f = boundary_request()
    items = [
        claim(req, f, "scheduled-payable", "opening_current_tax", 40),
        claim(req, f, "scheduled-refund", "opening_current_tax", 20, "asset"),
    ]
    for period_index, item in enumerate(items, 1):
        item["location"] = "boundary_schedule"
        item["period_index"] = period_index
    complete_bridge(req, f, items)
    req["bridge"]["boundary_cash"] = [
        {
            "id": "scheduled-payable",
            "lot_id": "opening-payable",
            "period_index": 1,
            "direction": "obligation",
            "amount": f.operand(40),
            "claim_rate": f.operand(0.1),
            "rationale": "Explicit opening payable cash",
        },
        {
            "id": "scheduled-refund",
            "lot_id": "opening-refund",
            "period_index": 2,
            "direction": "asset",
            "amount": f.operand(20),
            "claim_rate": f.operand(0.1),
            "rationale": "Explicit opening refund cash",
        },
    ]
    result = evaluate(req)
    expected = D(40) / D("1.1") - D(20) / D("1.1") ** 2
    assert result["common_equity"]["value"] == pytest.approx(
        result["operating_value"]["value"] - float(expected)
    )
    req["bridge"]["items"][1]["value"]["value"] = 19
    assert evaluate(req)["common_equity"]["reason_codes"] == ["claim_boundary_event_mismatch"]
    req["bridge"]["items"][1]["value"]["value"] = 20
    req["bridge"]["items"].pop()
    assert evaluate(req)["common_equity"]["reason_codes"] == ["claim_boundary_event_missing"]


def test_a10_adjustment_source_is_validated_and_never_overwritten():
    req, f = request()
    row = req["operating_schedule"]["periods"][0]
    base = f.operand(107)
    adjusted = f.operand(100)
    adjusted["provenance"].update(
        {
            "class": "analyst_adjustment",
            "derived_from": [base["provenance"]["id"]],
            "adjustment": {
                "source_operand": base["provenance"]["id"],
                "delta": -7,
                "operation": "add",
                "result": 100,
            },
        }
    )
    req["operating_schedule"]["supporting_operands"] = [base]
    row["cash_operating_profit_before_da"] = adjusted
    result = evaluate(req)
    assert result["operating_cash_flows"]["available"]
    assert base["value"] == 107 and adjusted["value"] == 100
    corrupt = copy.deepcopy(req)
    corrupt["operating_schedule"]["supporting_operands"][0]["provenance"].update(
        {
            "class": "reported_fact",
            "uncertainty": "source_observed",
            "accounting_date": "2026-06-30",
            "source_refs": [{"source_id": "missing", "anchor": "p1"}],
        }
    )
    assert evaluate(corrupt)["operating_cash_flows"]["reason_codes"] == [
        "source_reference_unresolved"
    ]


def test_a14_restricted_pairs_complete_or_refuse():
    req, f = request()
    items = [claim(req, f, "excess", "excess_distributable_cash", 90, "asset")]
    for id, category, direction in [
        ("restricted", "nonoperating_assets", "asset"),
        ("jackpot", "legal_other", "obligation"),
    ]:
        item = claim(req, f, id, category, 0, direction)
        item.update(
            location="excluded_matched_pair",
            matched_pair_id="matched10",
            excluded_amount=f.operand(10),
        )
        items.append(item)
    complete_bridge(req, f, items)
    result = evaluate(req)
    assert result["common_equity"]["available"], result["common_equity"]
    assert result["common_equity"]["value"] == result["operating_value"]["value"] + 90
    req["bridge"]["items"].pop()
    result = evaluate(req)
    assert not result["common_equity"]["available"] and result["operating_value"]["available"]


def test_tax_lot_other_movement_requires_exact_allocation():
    req, f = boundary_request()
    first, second = req["operating_schedule"]["periods"]
    ledger = first["tax_ledgers"][0]
    ledger["receivable_other"] = [
        {
            "id": "reclass-recognition",
            "amount": f.operand(1),
            "rationale": "Explicit signed receivable reclassification, not cash",
        }
    ]
    ledger["lot_movements"] = [
        {
            "id": "reclass-lot",
            "lot_id": "opening-refund",
            "amount": f.operand(1),
            "rationale": "Allocate the recognition to actual opening lot",
        }
    ]
    ledger["receivable_close"]["value"] = 21
    following = second["tax_ledgers"][0]
    following["receivable_open"]["value"] = 21
    following["refund"]["value"] = 21
    following["allocations"][0]["amount"]["value"] = 21
    result = evaluate(req)
    assert result["operating_cash_flows"]["available"], result["operating_cash_flows"]
    assert result["operating_cash_flows"]["value"][0]["fcff_operations"] == 20
    ledger["lot_movements"] = []
    assert evaluate(req)["operating_cash_flows"]["reason_codes"] == [
        "tax_refund_lot_movements_mismatch"
    ]


def test_missing_basic_count_limitation_does_not_withhold_equity():
    req, f = request()
    complete_bridge(req, f)
    req["ownership"]["ordinary_shares"]["value"] = None
    req["limitations"] = [
        {
            "id": "basic-count-unknown",
            "text": "Only the basic count is unknown",
            "affected_outputs": ["per_share"],
            "conditional_outputs": [],
            "material": True,
        }
    ]
    result = evaluate(req)
    assert result["common_equity"]["available"] and not result["per_share"]["available"]


def test_supplied_loss_entitlement_missing_never_negative_rate_refund():
    req, f = request()
    req.pop("terminal")
    req["operating_schedule"]["periods"][0]["cash_operating_profit_before_da"] = f.operand(-100)
    req["operating_schedule"]["periods"][0]["tax_ledgers"][0]["taxable_basis"] = f.operand(-100)
    assert not evaluate(req)["operating_cash_flows"]["available"]


def test_optional_packet_hash_alone_does_not_supply_evidence():
    req, _ = request()
    req["evidence_context"]["packet_hash"] = "a" * 64
    assert evaluate(req)["operating_cash_flows"]["reason_codes"] == ["invalid_evidence_packet"]


def test_negative_fcff_requires_funding_access_for_value_only():
    req, f = request(profits=(-10, 30, 30), tau=0, rho=0, horizon=1, growth=0)
    no_tax(req)
    req["terminal"] = {
        "kind": "finite_runoff",
        "horizon": 6,
        "residual_zero": True,
        "rationale": "Complete hypothetical runoff",
    }
    req["terminal"]["horizon"] = len(req["operating_schedule"]["periods"])
    req["object"]["funding_access"] = None
    result = evaluate(req)
    assert result["operating_cash_flows"]["available"]
    assert result["operating_cash_flows"]["value"][0]["fcff_operations"] == -10
    assert result["operating_value"]["reason_codes"] == ["funding_access_missing"]
