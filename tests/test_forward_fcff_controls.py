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


def cohort_schedule(weights, *, life=1, commissioned="2027-01-01"):
    req, f = request(profits=(100,) * 8)
    no_tax(req)
    cost = sum(amount for _, amount in weights)
    depreciation = dict(weights)
    commissioning_period = next(
        (
            row["index"]
            for row in req["operating_schedule"]["periods"]
            if row["start"] <= commissioned <= row["end"]
        ),
        None,
    )
    opening = cost if commissioned < req["operating_schedule"]["periods"][0]["start"] else 0
    for row in req["operating_schedule"]["periods"]:
        index = row["index"]
        addition = cost if index == commissioning_period else 0
        dep = depreciation.get(index, 0)
        closing = opening + addition - dep
        row["book_da"] = f.operand(dep)
        asset = row["asset_controls"][0]
        for field, amount in {
            "opening": opening,
            "commissioned_additions": addition,
            "depreciation": dep,
            "closing": closing,
        }.items():
            asset[field] = f.operand(amount)
        if addition:
            row["investment_items"] = [
                investment(f, "finite-cohort-purchase", cost, index, commissioned)
            ]
        opening = closing
    req["operating_schedule"]["cohorts"] = [
        {
            "id": "finite-cohort",
            "asset_class": "plant",
            "cost": f.operand(cost),
            "residual": f.operand(0),
            "life_years": f.operand(life),
            "in_service_date": commissioned,
            "method": "supplied_weights",
            "weights": [
                {"period_index": index, "depreciation": f.operand(amount)}
                for index, amount in weights
            ],
            "remaining_basis": "Explicit source-free finite cohort",
        }
    ]
    return req, commissioning_period


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


def test_zero_cohort_is_exempt_and_unfunded_forecast_cohort_refuses():
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
    for name in ("operating_cash_flows", "operating_value", "common_equity", "per_share"):
        assert result[name]["status"] == "invalid_input"
        assert result[name]["reason_codes"] == ["cohort_commissioned_cost_uncovered"]
        assert result[name]["missing_inputs"] == ["ghost"]
        assert result[name]["value"] is None


@pytest.mark.parametrize(
    "weights",
    (
        ((3, 10),),
        ((6, 10),),
        ((6, 5), (7, 5)),
        ((1, 0), (6, 10)),
    ),
)
def test_forecast_cohort_life_is_anchored_to_commissioning(weights):
    req, _ = cohort_schedule(weights)

    result = evaluate(req)

    for name in ("operating_cash_flows", "operating_value", "common_equity", "per_share"):
        assert result[name] == {
            "available": False,
            "status": "invalid_input",
            "reason_codes": ["cohort_weights_exceed_supplied_life"],
            "missing_inputs": [],
            "conditions": [],
            "limitations": [],
            "value": None,
        }


@pytest.mark.parametrize(
    ("weights", "life", "commissioned", "investment_period"),
    (
        (((1, 10),), 1, "2027-01-01", 1),
        (((1, 5), (2, 5)), 1, "2027-01-01", 1),
        (((2, 10),), 1, "2027-01-01", 1),
        (((6, 10),), 6, "2027-01-01", 1),
        (((6, 10),), 1, "2032-01-01", 6),
        (((1, 10), (6, 0)), 1, "2027-01-01", 1),
        (((1, 10),), 1, "2020-01-01", None),
    ),
)
def test_supported_cohort_life_counterparts_keep_cash(
    weights, life, commissioned, investment_period
):
    req, actual_investment_period = cohort_schedule(weights, life=life, commissioned=commissioned)

    result = evaluate(req)

    assert actual_investment_period == investment_period
    assert result["operating_cash_flows"]["available"], result["operating_cash_flows"]
    expected_cash = [100] * 8
    if investment_period is not None:
        expected_cash[investment_period - 1] = 90
    assert [
        row["fcff_operations"] for row in result["operating_cash_flows"]["value"]
    ] == expected_cash


def test_post_horizon_positive_cohort_cost_requires_commissioning_coverage():
    req, commissioning_period = cohort_schedule(((1, 0),), commissioned="2035-01-01")
    item = req["operating_schedule"]["cohorts"][0]
    item["cost"]["value"] = 10
    item["residual"]["value"] = 10

    result = evaluate(req)

    assert commissioning_period is None
    for name in ("operating_cash_flows", "operating_value", "common_equity", "per_share"):
        assert result[name]["status"] == "invalid_input"
        assert result[name]["reason_codes"] == ["cohort_commissioned_cost_uncovered"]
        assert result[name]["missing_inputs"] == ["plant"]
        assert result[name]["value"] is None


def test_forecast_cohort_costs_aggregate_before_addition_coverage():
    req, _ = cohort_schedule(((1, 10),))
    first = req["operating_schedule"]["cohorts"][0]
    first["id"] = "first"
    first["cost"]["value"] = 6
    first["residual"]["value"] = 1
    first["weights"][0]["depreciation"]["value"] = 5
    second = copy.deepcopy(first)
    second["id"] = "second"
    for operand in (second["cost"], second["residual"], second["life_years"]):
        operand["provenance"]["id"] += "-second"
    second["weights"][0]["depreciation"]["provenance"]["id"] += "-second"
    req["operating_schedule"]["cohorts"] = [first, second]

    result = evaluate(req)

    assert result["operating_cash_flows"]["status"] == "invalid_input"
    assert result["operating_cash_flows"]["reason_codes"] == ["cohort_commissioned_cost_uncovered"]
    assert result["operating_cash_flows"]["missing_inputs"] == ["plant"]


def test_positive_residual_cohort_still_requires_commissioned_cost():
    req, f = request()
    no_tax(req)
    item = cohort(f, "plant", 0)
    item["cost"] = f.operand(10)
    item["residual"] = f.operand(10)
    req["operating_schedule"]["cohorts"] = [item]

    result = evaluate(req)

    assert result["operating_cash_flows"]["reason_codes"] == ["cohort_commissioned_cost_uncovered"]
    assert result["operating_cash_flows"]["missing_inputs"] == ["plant"]


def test_partial_cohort_cost_inventory_uses_full_asset_class_identity():
    req, _ = cohort_schedule(((1, 10),))
    rows = req["operating_schedule"]["periods"]
    for row in rows:
        asset = row["asset_controls"][0]
        asset["id"] = "group/plant"
    rows[0]["investment_items"][0]["asset_class"] = "group/plant"
    rows[0]["investment_items"][0]["amount"]["value"] = 20
    rows[0]["asset_controls"][0]["commissioned_additions"]["value"] = 20
    rows[0]["asset_controls"][0]["closing"]["value"] = 10
    for row in rows[1:]:
        row["asset_controls"][0]["opening"]["value"] = 10
        row["asset_controls"][0]["closing"]["value"] = 10
    req["operating_schedule"]["cohorts"][0]["asset_class"] = "group/plant"

    result = evaluate(req)

    assert result["operating_cash_flows"]["available"], result["operating_cash_flows"]
    first = result["operating_cash_flows"]["value"][0]
    assert first["fcff_operations"] == 80
    assert first["net_book_investment"] == 10


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


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        (("allocated_ebit",), "tax_allocation_group_incomplete"),
        (("allocated_book_da",), "tax_allocation_group_incomplete"),
        (("allocation_basis",), "tax_allocation_group_incomplete"),
        (("allocated_ebit", "allocated_book_da"), "tax_allocation_group_incomplete"),
        (("allocated_ebit", "allocation_basis"), "tax_allocation_group_incomplete"),
        (("allocated_book_da", "allocation_basis"), "tax_allocation_group_incomplete"),
        (("net_rate",), "net_tax_group_incomplete"),
        (("net_expense",), "net_tax_group_incomplete"),
    ],
)
def test_partial_optional_tax_groups_refuse_required_schedule(fields, reason):
    req, f = request()
    ledger = req["operating_schedule"]["periods"][0]["tax_ledgers"][0]
    values = {
        "allocated_ebit": f.operand(100),
        "allocated_book_da": f.operand(0),
        "allocation_basis": "Explicit complete tax allocation basis",
        "net_rate": f.operand(0.2),
        "net_expense": f.operand(20),
    }
    for field in fields:
        ledger[field] = values[field]

    result = evaluate(req)
    for output_name in ("operating_cash_flows", "operating_value", "common_equity", "per_share"):
        assert result[output_name]["reason_codes"] == [reason]
        assert result[output_name]["missing_inputs"] == ["tax"]


def test_complete_or_absent_optional_tax_groups_preserve_existing_arithmetic():
    absent, _ = request()
    absent_result = evaluate(absent)
    assert absent_result["operating_value"]["available"]

    complete, f = request()
    ledger = complete["operating_schedule"]["periods"][0]["tax_ledgers"][0]
    ledger.update(
        allocated_ebit=f.operand(100),
        allocated_book_da=f.operand(0),
        allocation_basis="Explicit complete tax allocation basis",
        net_rate=f.operand(0.2),
        net_expense=f.operand(20),
    )
    complete_result = evaluate(complete)
    assert complete_result["operating_value"]["value"] == absent_result["operating_value"]["value"]

    allocation_mismatch = copy.deepcopy(complete)
    allocation_mismatch["operating_schedule"]["periods"][0]["tax_ledgers"][0]["allocated_book_da"][
        "value"
    ] = 5
    allocation_result = evaluate(allocation_mismatch)
    assert allocation_result["operating_cash_flows"]["reason_codes"] == [
        "tax_basis_reconciliation_mismatch"
    ]

    net_mismatch = copy.deepcopy(complete)
    net_mismatch["operating_schedule"]["periods"][0]["tax_ledgers"][0]["net_expense"]["value"] = 99
    net_result = evaluate(net_mismatch)
    assert net_result["operating_cash_flows"]["reason_codes"] == ["net_tax_expense_mismatch"]


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


def outstanding_boundary_payable_request():
    """SYNTHETIC-only variant: opening payable survives the operating runoff."""
    req, f = boundary_request()
    for row in req["operating_schedule"]["periods"]:
        ledger = row["tax_ledgers"][0]
        ledger["allocations"] = [
            allocation
            for allocation in ledger["allocations"]
            if allocation["lot_id"] != "opening-payable"
        ]
        ledger["payable_open"] = f.operand(40)
        ledger["payable_close"] = f.operand(40)
        ledger["payment"] = f.operand(0)
    return req, f


def movement_created_boundary_claim_request(*, leg="payable", state="outstanding"):
    assert leg in {"payable", "refund"}
    assert state in {"inactive", "outstanding", "payment", "reversal", "writeoff"}
    assert state != "payment" or leg == "payable"
    assert state != "writeoff" or leg == "refund"
    req, f = boundary_request()
    lot_id = "opening-" + leg
    lot = next(lot for lot in req["operating_schedule"]["tax_lots"] if lot["id"] == lot_id)
    lot["amount"] = f.operand(0)
    first, second = req["operating_schedule"]["periods"]
    open_field = "payable_open" if leg == "payable" else "receivable_open"
    close_field = "payable_close" if leg == "payable" else "receivable_close"
    cash_field = "payment" if leg == "payable" else "refund"
    other_field = "payable_other" if leg == "payable" else "receivable_other"
    for row in (first, second):
        ledger = row["tax_ledgers"][0]
        ledger["allocations"] = [
            allocation for allocation in ledger["allocations"] if allocation["lot_id"] != lot_id
        ]
        ledger[open_field] = f.operand(0)
        ledger[close_field] = f.operand(0)
        ledger[cash_field] = f.operand(0)
        ledger[other_field] = []
    if leg == "refund":
        first["tax_ledgers"][0]["writeoff"] = f.operand(0)
    if state == "inactive":
        return req, f

    first_ledger = first["tax_ledgers"][0]
    second_ledger = second["tax_ledgers"][0]
    first_ledger[other_field] = [
        {
            "id": f"boundary-{leg}-recognition",
            "amount": f.operand(10),
            "rationale": "Explicit synthetic boundary movement",
        }
    ]
    first_ledger["lot_movements"] = [
        {
            "id": f"boundary-{leg}-lot-recognition",
            "lot_id": lot_id,
            "amount": f.operand(10),
            "rationale": "Allocate movement to the identified boundary lot",
        }
    ]
    first_ledger[close_field] = f.operand(10)
    second_ledger[open_field] = f.operand(10)
    second_ledger[close_field] = f.operand(10)
    if state == "payment":
        second_ledger[close_field] = f.operand(0)
        second_ledger[cash_field] = f.operand(10)
        second_ledger["allocations"].append(
            {
                "id": "movement-payment-2",
                "kind": "payment",
                "lot_id": lot_id,
                "amount": f.operand(10),
                "cash_date": None,
                "eligibility_order": {
                    "rank": 2,
                    "basis": "Explicit synthetic within-year sequence",
                },
            }
        )
    elif state == "reversal":
        second_ledger[close_field] = f.operand(0)
        second_ledger[other_field] = [
            {
                "id": f"boundary-{leg}-reversal",
                "amount": f.operand(-10),
                "rationale": "Explicit synthetic boundary movement reversal",
            }
        ]
        second_ledger["lot_movements"] = [
            {
                "id": f"boundary-{leg}-lot-reversal",
                "lot_id": lot_id,
                "amount": f.operand(-10),
                "rationale": "Allocate reversal to the identified boundary lot",
            }
        ]
    elif state == "writeoff":
        first_ledger[close_field] = f.operand(0)
        first_ledger["writeoff"] = f.operand(10)
        first_ledger["allocations"].append(
            {
                "id": "movement-writeoff-1",
                "kind": "writeoff",
                "lot_id": lot_id,
                "amount": f.operand(10),
                "cash_date": None,
                "eligibility_order": {
                    "rank": 2,
                    "basis": "Explicit synthetic within-year sequence",
                },
            }
        )
        second_ledger[open_field] = f.operand(0)
        second_ledger[close_field] = f.operand(0)
    return req, f


def boundary_refund_claim(req, f):
    return claim(
        req,
        f,
        "opening-refund",
        "opening_current_tax",
        float(D(20) / D("1.1") ** 2),
        "asset",
    )


def boundary_counterpart_claim(req, f, leg):
    if leg == "payable":
        return boundary_refund_claim(req, f)
    return claim(
        req,
        f,
        "opening-payable",
        "opening_current_tax",
        float(D(40) / D("1.1")),
    )


def test_synthetic_runoff_movement_created_claim_requires_bridge_coverage():
    req, f = movement_created_boundary_claim_request()
    complete_bridge(req, f, [boundary_refund_claim(req, f)])

    result = evaluate(req)

    assert result["operating_value"]["available"]
    assert result["common_equity"]["reason_codes"] == ["bridge_material_claim_unresolved"]
    assert result["common_equity"]["missing_inputs"] == ["opening-payable"]
    assert result["per_share"]["reason_codes"] == ["common_equity_unavailable"]


def test_synthetic_runoff_movement_created_claim_accepts_supplied_value():
    req, f = movement_created_boundary_claim_request()
    payable_pv = 4
    refund = boundary_refund_claim(req, f)
    complete_bridge(
        req,
        f,
        [claim(req, f, "opening-payable", "opening_current_tax", payable_pv), refund],
    )

    result = evaluate(req)

    assert result["operating_value"]["available"]
    assert result["common_equity"]["value"] == pytest.approx(
        result["operating_value"]["value"] - payable_pv + refund["value"]["value"]
    )
    assert result["per_share"]["available"]


def test_synthetic_runoff_settled_movement_claim_retains_cash_history():
    req, f = movement_created_boundary_claim_request(state="payment")
    refund = boundary_refund_claim(req, f)
    complete_bridge(req, f, [refund])

    missing = evaluate(req)

    assert missing["operating_value"]["available"]
    assert missing["common_equity"]["reason_codes"] == ["bridge_material_claim_unresolved"]
    scheduled = claim(req, f, "movement-payable-cash", "opening_current_tax", 10)
    scheduled["location"] = "boundary_schedule"
    scheduled["period_index"] = 2
    complete_bridge(req, f, [refund, scheduled])
    req["bridge"]["boundary_cash"] = [
        {
            "id": "movement-payable-cash",
            "lot_id": "opening-payable",
            "period_index": 2,
            "direction": "obligation",
            "amount": f.operand(10),
            "claim_rate": f.operand(0.1),
            "rationale": "Explicit synthetic movement-created payable cash",
        }
    ]

    result = evaluate(req)
    payable_pv = float(D(10) / D("1.1") ** 2)
    assert result["common_equity"]["value"] == pytest.approx(
        result["operating_value"]["value"] - payable_pv + refund["value"]["value"]
    )
    assert result["per_share"]["available"]


@pytest.mark.parametrize(("leg", "state"), (("payable", "reversal"), ("refund", "writeoff")))
def test_synthetic_runoff_deactivated_movement_claim_requires_bridge_coverage(leg, state):
    req, f = movement_created_boundary_claim_request(leg=leg, state=state)
    complete_bridge(req, f, [boundary_counterpart_claim(req, f, leg)])

    result = evaluate(req)

    assert result["operating_cash_flows"]["available"]
    assert result["operating_value"]["available"]
    assert result["common_equity"]["status"] == "insufficient_evidence"
    assert result["common_equity"]["reason_codes"] == ["bridge_material_claim_unresolved"]
    assert result["common_equity"]["missing_inputs"] == ["opening-" + leg]
    assert result["common_equity"]["value"] is None
    assert result["per_share"]["status"] == "insufficient_evidence"
    assert result["per_share"]["reason_codes"] == ["common_equity_unavailable"]
    assert result["per_share"]["value"] is None


@pytest.mark.parametrize(("leg", "state"), (("payable", "reversal"), ("refund", "writeoff")))
def test_synthetic_runoff_deactivated_movement_claim_accepts_qualified_zero(leg, state):
    req, f = movement_created_boundary_claim_request(leg=leg, state=state)
    counterpart = boundary_counterpart_claim(req, f, leg)
    direction = "obligation" if leg == "payable" else "asset"
    qualified_zero = claim(req, f, "opening-" + leg, "opening_current_tax", 0, direction)
    complete_bridge(req, f, [counterpart, qualified_zero])

    result = evaluate(req)

    adjustment = counterpart["value"]["value"] * (1 if counterpart["direction"] == "asset" else -1)
    expected_equity = result["operating_value"]["value"] + adjustment
    assert result["operating_cash_flows"]["available"]
    assert result["operating_value"]["available"]
    assert result["common_equity"]["value"] == pytest.approx(expected_equity)
    assert result["per_share"]["value"] == pytest.approx(expected_equity / 100)


@pytest.mark.parametrize("leg", ("payable", "refund"))
def test_synthetic_runoff_inactive_zero_boundary_claim_needs_no_bridge_item(leg):
    req, f = movement_created_boundary_claim_request(leg=leg, state="inactive")
    counterpart = boundary_counterpart_claim(req, f, leg)
    complete_bridge(req, f, [counterpart])

    result = evaluate(req)

    adjustment = counterpart["value"]["value"] * (1 if counterpart["direction"] == "asset" else -1)
    expected_equity = result["operating_value"]["value"] + adjustment
    assert result["operating_cash_flows"]["available"]
    assert result["operating_value"]["available"]
    assert result["common_equity"]["value"] == pytest.approx(expected_equity)
    assert result["per_share"]["value"] == pytest.approx(expected_equity / 100)


def test_synthetic_runoff_outstanding_prevalued_opening_payable_bridges_once():
    req, f = outstanding_boundary_payable_request()
    payable_pv = 12  # Independently supplied hypothetical same-date PV, not carrying value.
    refund_pv = float(D(20) / D("1.1") ** 2)
    complete_bridge(
        req,
        f,
        [
            claim(req, f, "opening-payable", "opening_current_tax", payable_pv),
            claim(req, f, "opening-refund", "opening_current_tax", refund_pv, "asset"),
        ],
    )

    result = evaluate(req)
    expected_operations = float(D(20) / D("1.1") + D(30) / D("1.1") ** 2)
    rows = result["operating_cash_flows"]["value"]
    assert [row["fcff_operations"] for row in rows] == [20, 30]
    assert [row["fcff_with_boundary_tax"] for row in rows] == [20, 50]
    assert result["explicit_pv"] == pytest.approx(expected_operations)
    assert result["operating_value"]["value"] == pytest.approx(expected_operations)
    expected_equity = expected_operations - payable_pv + refund_pv
    assert result["common_equity"]["value"] == pytest.approx(expected_equity)
    assert result["per_share"]["value"] == pytest.approx(expected_equity / 100)


def test_synthetic_runoff_uncovered_opening_claim_withholds_only_equity():
    req, f = outstanding_boundary_payable_request()
    complete_bridge(
        req,
        f,
        [
            claim(
                req,
                f,
                "opening-refund",
                "opening_current_tax",
                float(D(20) / D("1.1") ** 2),
                "asset",
            )
        ],
    )

    result = evaluate(req)
    expected_operations = float(D(20) / D("1.1") + D(30) / D("1.1") ** 2)
    assert [row["fcff_operations"] for row in result["operating_cash_flows"]["value"]] == [20, 30]
    assert result["explicit_pv"] == pytest.approx(expected_operations)
    assert result["operating_value"]["value"] == pytest.approx(expected_operations)
    assert result["common_equity"]["reason_codes"] == ["bridge_material_claim_unresolved"]
    assert result["common_equity"]["value"] is None
    assert result["per_share"]["reason_codes"] == ["common_equity_unavailable"]
    assert result["per_share"]["value"] is None


def test_synthetic_runoff_outstanding_forecast_created_lot_still_refuses():
    req, f = outstanding_boundary_payable_request()
    first = req["operating_schedule"]["periods"][0]
    lot = req["operating_schedule"]["tax_lots"][0]
    lot["origin"] = "forecast_created"
    lot["source_period"] = 1
    lot["accounting_date"] = first["end"]
    first["tax_ledgers"][0]["payable_open"] = f.operand(0)
    first["tax_ledgers"][0]["arising"] = f.operand(40)

    result = evaluate(req)
    assert [row["fcff_operations"] for row in result["operating_cash_flows"]["value"]] == [20, 30]
    assert result["explicit_pv"] == pytest.approx(float(D(20) / D("1.1") + D(30) / D("1.1") ** 2))
    assert result["operating_value"]["reason_codes"] == ["finite_runoff_incomplete"]
    assert result["operating_value"]["value"] is None
    assert result["common_equity"]["reason_codes"] == ["operating_value_unavailable"]
    assert result["per_share"]["reason_codes"] == ["operating_value_unavailable"]


def test_synthetic_runoff_fully_settled_boundary_counterpart_preserves_value():
    req, f = boundary_request()
    payable_pv = float(D(40) / D("1.1"))
    refund_pv = float(D(20) / D("1.1") ** 2)
    complete_bridge(
        req,
        f,
        [
            claim(req, f, "opening-payable", "opening_current_tax", payable_pv),
            claim(req, f, "opening-refund", "opening_current_tax", refund_pv, "asset"),
        ],
    )

    result = evaluate(req)
    expected_operations = float(D(20) / D("1.1") + D(30) / D("1.1") ** 2)
    rows = result["operating_cash_flows"]["value"]
    assert [row["fcff_operations"] for row in rows] == [20, 30]
    assert [row["fcff_with_boundary_tax"] for row in rows] == [-20, 50]
    assert result["explicit_pv"] == pytest.approx(expected_operations)
    assert result["operating_value"]["value"] == pytest.approx(expected_operations)
    assert result["boundary_tax_pv"] == pytest.approx(payable_pv - refund_pv)
    expected_equity = expected_operations - payable_pv + refund_pv
    assert result["common_equity"]["value"] == pytest.approx(expected_equity)
    assert result["per_share"]["value"] == pytest.approx(expected_equity / 100)


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
