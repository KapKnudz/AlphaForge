"""Independent written equations and refusal controls for the supplied FCFF interface."""

from __future__ import annotations

import copy
import json
from decimal import Decimal as D

import pytest
from forward_fcff_fixtures import claim, complete_bridge, request, set_tail

from alphaforge.core.valuation.forward_fcff import evaluate
from alphaforge.core.valuation.forward_fcff_contract import ContractError, canonical, digest, freeze


def val(result, key):
    assert result[key]["available"], result[key]
    return result[key]["value"]


@pytest.mark.parametrize("currency", ["SEK", "EUR"])
def test_a6_exact_numerator_and_currency(currency):
    req, _ = request(currency)
    result = evaluate(freeze(req))
    rows = val(result, "operating_cash_flows")
    assert rows[1]["gross_tax_payment"] == pytest.approx(30)
    assert rows[1]["gross_tax_refund"] == 10
    assert rows[2]["gross_tax_payment"] == pytest.approx(31.5)
    assert rows[2]["gross_tax_refund"] == 10
    assert rows[2]["fcff_operations"] == pytest.approx(float(D(110) - D("31.5") + D(10)))
    assert result["terminal"]["next_year_fcff"] == 88.5
    assert result["terminal"]["value_at_horizon"] == 885
    expected = D(85) / D("1.2") + D(80) / D("1.2") ** 2 + D(885) / D("1.2") ** 2
    assert val(result, "operating_value") == pytest.approx(float(expected), rel=2e-14)
    assert not result["common_equity"]["available"] and not result["per_share"]["available"]
    assert result["denomination"]["currency"] == currency


def test_a6_two_year_refund_lag_refuses_then_corrected_horizon():
    req, f = request(lag=2)
    bad = evaluate(req)
    assert not bad["operating_value"]["available"]
    assert val(bad, "operating_cash_flows")[3]["fcff_operations"] == pytest.approx(96.35)
    set_tail(req, f, 3, 0.1, {"tax/tax/arising": 2, "tax/tax/entitlement": 2}, lag=2)
    result = evaluate(req)
    assert result["terminal"]["horizon"] == 3
    assert result["terminal"]["next_year_fcff"] == pytest.approx(96.35)
    assert result["terminal"]["value_at_horizon"] == pytest.approx(963.5)
    expected = (
        D(85) / D("1.2")
        + D(70) / D("1.2") ** 2
        + D("88.5") / D("1.2") ** 3
        + D("963.5") / D("1.2") ** 3
    )
    assert val(result, "operating_value") == pytest.approx(float(expected))


def test_a7_gross_cancellation_does_not_certify_tail():
    req, f = request(tau=0.1, rho=0.1, horizon=1)
    set_tail(req, f, 1, 0.1, {"tax/tax/arising": 2, "tax/tax/entitlement": 2})
    result = evaluate(req)
    rows = val(result, "operating_cash_flows")
    assert rows[1]["fcff_operations"] == 90
    assert rows[2]["fcff_operations"] == 99
    assert rows[2]["gross_tax_payment"] - 1.1 * rows[1]["gross_tax_payment"] == pytest.approx(-1)
    assert rows[2]["gross_tax_refund"] - 1.1 * rows[1]["gross_tax_refund"] == pytest.approx(-1)
    assert not result["operating_value"]["available"]


def test_known_future_terminal_regime_is_distinct_from_missing_regime():
    req, f = request()
    assert evaluate(req)["operating_value"]["available"]
    payment = next(
        template
        for template in req["terminal"]["templates"]
        if template["path"] == "tax/tax/payment"
    )
    payment["regime_start"] = 4
    refused = evaluate(req)
    assert refused["operating_cash_flows"]["available"]
    assert refused["operating_value"]["status"] == "unsupported"
    assert refused["operating_value"]["reason_codes"] == ["terminal_regime_not_stationary"]
    assert refused["operating_value"]["missing_inputs"] == ["tax/tax/payment"]
    diagnostic = {
        "code": "terminal_regime_not_stationary",
        "path": "tax/tax/payment",
        "regime_start": 4,
        "requested_tail_start": 3,
    }
    assert refused["terminal_regime_diagnostics"] == [diagnostic]
    assert refused["terminal"]["certificate"]["failures"] == [
        {"code": diagnostic["code"], "path": diagnostic["path"]}
    ]

    missing = copy.deepcopy(req)
    next(
        template
        for template in missing["terminal"]["templates"]
        if template["path"] == "tax/tax/payment"
    )["regime_start"] = None
    missing_result = evaluate(missing)
    assert missing_result["operating_cash_flows"]["available"]
    assert missing_result["operating_value"]["status"] == "insufficient_evidence"
    assert missing_result["operating_value"]["reason_codes"] == ["terminal_regime_missing"]
    assert missing_result["operating_value"]["missing_inputs"] == ["tax/tax/payment"]
    assert "terminal_regime_diagnostics" not in missing_result

    regimes = {
        template["path"]: template["regime_start"] for template in req["terminal"]["templates"]
    }
    set_tail(req, f, 3, 0.1, regimes)
    corrected = evaluate(req)
    assert corrected["operating_cash_flows"] == refused["operating_cash_flows"]
    assert corrected["operating_value"]["available"]
    assert corrected["terminal"]["horizon"] == 3


def test_a1_a2_a4_basic_arithmetic_gross_net_leases():
    req, f = request()
    req.pop("terminal")
    req.pop("discount")
    row = req["operating_schedule"]["periods"][0]
    # Retain only year one and replace gross tax by independently supplied 15/3.
    req["operating_schedule"]["periods"] = [row]
    req["operating_schedule"]["initial_wc"] = f.operand(10)
    row["wc_open"] = f.operand(10)
    row["wc_close"] = f.operand(16)
    row["book_da"] = f.operand(20)
    row["other_noncash_operating_charge"] = f.operand(5)
    ll = row["tax_ledgers"][0]
    for k, v in {
        "taxable_basis": 75,
        "tax_da": 20,
        "arising": 15,
        "payment": 15,
        "entitlement": 3,
        "refund": 3,
        "payable_open": 0,
        "payable_close": 0,
        "receivable_open": 0,
        "receivable_close": 0,
    }.items():
        ll[k] = f.operand(v)
    req["operating_schedule"]["tax_lots"] = []
    ll["allocations"] = []
    for kind, leg, amt in (("payment", "payable", 15), ("refund", "refund", 3)):
        req["operating_schedule"]["tax_lots"].append(
            {
                "id": leg,
                "ledger_id": "tax",
                "origin": "forecast_created",
                "leg": leg,
                "source_period": 1,
                "amount": f.operand(amt),
                "eligible_date": None,
                "prerequisites": [],
                "eligibility_basis": "Explicit hypothetical within-year eligibility",
                "accounting_date": row["end"],
            }
        )
        ll["allocations"].append(
            {
                "id": kind,
                "kind": kind,
                "lot_id": leg,
                "amount": f.operand(amt),
                "cash_date": None,
                "eligibility_order": {
                    "rank": 1 if kind == "payment" else 2,
                    "basis": "Explicit synthetic payment then refund order",
                },
            }
        )
    row["investment_items"] = [
        {
            "id": id,
            "asset_class": "plant",
            "kind": kind,
            "amount": f.operand(amount),
            "spend_period": 1,
            "in_service_date": row["start"],
            "classification": "renewal",
            "rationale": "Supplied hypothetical economic investment",
        }
        for id, kind, amount in (
            ("cash-buy", "cash_purchase", 30),
            ("lease-renewal", "economic_lease_addition", 4),
        )
    ]
    row["disposals"] = [
        {
            "id": "sale",
            "asset_class": "plant",
            "proceeds": f.operand(2),
            "carrying_reduction": f.operand(1),
            "gain_tax_basis": "Gain included in taxable basis premise",
        }
    ]
    a = row["asset_controls"][0]
    for k, v in {
        "opening": 50,
        "commissioned_additions": 34,
        "depreciation": 20,
        "impairment": 5,
        "disposal_reduction": 1,
        "closing": 58,
    }.items():
        a[k] = f.operand(v)
    row["settlements"] = [
        {
            "id": "settlement",
            "kind": "outflow",
            "amount": f.operand(1),
            "rationale": "Otherwise unembedded cash",
            "bridge_category": "legal_other",
            "period_index": 1,
        }
    ]
    row["financing_diagnostics"] = [
        {
            "id": "principal",
            "kind": "lease_principal",
            "amount": f.operand(9),
            "rationale": "Financing, not FCFF",
        },
        {
            "id": "interest",
            "kind": "lease_interest",
            "amount": f.operand(3),
            "rationale": "Financing, not FCFF",
        },
    ]
    result = evaluate(req)
    x = val(result, "operating_cash_flows")[0]
    assert x["ebit"] == 75
    assert x["fcff_operations"] == float(D(100) - D(12) - D(30) - D(4) + D(2) - D(6) - D(1)) == 49
    assert x["net_cash_reinvestment"] == 18
    assert x["net_book_investment"] == 8
    duplicate = copy.deepcopy(req)
    duplicate["operating_schedule"]["periods"][0]["net_reinvestment"] = f.operand(18)
    assert not evaluate(duplicate)["operating_cash_flows"]["available"]
    a["closing"]["value"] += 1
    assert evaluate(req)["operating_cash_flows"]["reason_codes"] == ["asset_rollforward_mismatch"]


def test_a9_finite_runoff_incomplete_never_ev():
    req, _ = request(profits=(20, 30, 30), tau=0, rho=0, growth=0, horizon=1, rate=0.1)
    req["operating_schedule"]["periods"] = req["operating_schedule"]["periods"][:2]
    req["operating_schedule"]["tax_lots"] = req["operating_schedule"]["tax_lots"][:4]
    req["terminal"] = {
        "kind": "finite_runoff",
        "horizon": 2,
        "residual_zero": True,
        "rationale": "Complete closure, no residual claims/operations",
    }
    result = evaluate(req)
    assert val(result, "operating_value") == pytest.approx(
        float(D(20) / D("1.1") + D(30) / D("1.1") ** 2)
    )
    req["terminal"]["residual_zero"] = False
    bad = evaluate(req)
    assert not bad["operating_value"]["available"] and bad["explicit_pv"] == result["explicit_pv"]


@pytest.mark.parametrize("key", ["discount", "terminal", "bridge", "ownership"])
def test_a16_output_independence(key):
    req, f = request()
    complete_bridge(req, f)
    good = evaluate(req)
    req.pop(key)
    result = evaluate(req)
    assert canonical(result["operating_cash_flows"]) == canonical(good["operating_cash_flows"])
    if key in {"bridge", "ownership"}:
        assert result["operating_value"] == good["operating_value"]
    if key == "ownership":
        assert result["common_equity"] == good["common_equity"]
    assert not result["per_share"]["available"]


@pytest.mark.parametrize(
    "change,reason",
    [
        ("risk_basis", "discount_basis_unsupported"),
        ("currency", "currency_mismatch"),
        ("compounding", "discount_basis_unsupported"),
    ],
)
def test_a8_wrong_discount_scoped(change, reason):
    req, _ = request()
    req["discount"][change] = "wrong"
    result = evaluate(req)
    val(result, "operating_cash_flows")
    assert result["operating_value"]["reason_codes"] == [reason]


@pytest.mark.parametrize("growth", [0.2, 0.21, -1])
def test_a8_no_gordon_pole_tolerance(growth):
    req, _ = request()
    req["terminal"]["growth"]["value"] = growth
    assert evaluate(req)["operating_value"]["reason_codes"] == ["terminal_growth_invalid"]


def test_a13_a15_bridge_shares_claims_and_negative_equity():
    req, f = request()
    items = [
        claim(req, f, "cash", "excess_distributable_cash", 100, "asset"),
        claim(req, f, "lease", "existing_lease_debt", 50),
        claim(req, f, "consideration", "acquisition_consideration", 30),
        claim(req, f, "options", "noncommon_claims", 20),
    ]
    complete_bridge(req, f, items)
    own = req["ownership"]
    own["rights"][2].update(status="valued", claim_event_ids=["options"])
    result = evaluate(req)
    assert val(result, "common_equity") == val(result, "operating_value")
    assert val(result, "per_share") == pytest.approx(val(result, "common_equity") / 100)
    own["rights"][3]["status"] = "unresolved"
    bad = evaluate(req)
    assert (
        bad["operating_value"] == result["operating_value"]
        and not bad["common_equity"]["available"]
    )
    own["rights"][3]["status"] = "not_applicable_supported"
    req["bridge"]["items"][1]["value"]["value"] = 2000
    negative = evaluate(req)
    assert (
        val(negative, "common_equity") < 0
        and "negative_equity_nontradable" in negative["common_equity"]["conditions"]
    )
    assert not negative["per_share"]["available"]


def test_noncommon_rights_must_exactly_partition_bridge_claims():
    req, f = request()
    items = [
        claim(req, f, "option-a", "noncommon_claims", 12),
        claim(req, f, "option-b", "noncommon_claims", 8),
    ]
    complete_bridge(req, f, items)
    missing = evaluate(req)
    assert missing["operating_value"]["available"]
    assert missing["common_equity"]["reason_codes"] == ["ownership_claim_unvalued"]
    assert missing["common_equity"]["status"] == "insufficient_evidence"

    req["ownership"]["rights"][2].update(status="valued", claim_event_ids=["option-a", "option-b"])
    valid = evaluate(req)
    assert valid["common_equity"]["available"]
    req["ownership"]["rights"][3].update(status="valued", claim_event_ids=["option-a"])
    duplicate = evaluate(req)
    assert duplicate["operating_value"]["available"]
    assert duplicate["common_equity"]["reason_codes"] == ["ownership_claim_duplicated"]
    assert duplicate["common_equity"]["status"] == "invalid_input"


def test_nested_bridge_and_right_sets_have_canonical_identity():
    req, f = request()
    items = [
        claim(req, f, "preferred-a", "noncommon_claims", 2),
        claim(req, f, "preferred-b", "noncommon_claims", 3),
    ]
    complete_bridge(req, f, items)
    preferred = next(right for right in req["ownership"]["rights"] if right["id"] == "preferred")
    preferred.update(
        status="valued",
        claim_event_ids=["preferred-a", "preferred-b"],
    )
    expected_request = freeze(req).body
    expected_output = canonical(evaluate(req))

    inventory_reversed = copy.deepcopy(req)
    inventory_reversed["bridge"]["completeness"]["inventoried_categories"].reverse()
    claims_reversed = copy.deepcopy(req)
    next(right for right in claims_reversed["ownership"]["rights"] if right["id"] == "preferred")[
        "claim_event_ids"
    ].reverse()
    both_reversed = copy.deepcopy(inventory_reversed)
    next(right for right in both_reversed["ownership"]["rights"] if right["id"] == "preferred")[
        "claim_event_ids"
    ].reverse()
    outer_reversed = copy.deepcopy(req)
    for key in ("categories", "items"):
        outer_reversed["bridge"][key].reverse()
    outer_reversed["ownership"]["rights"].reverse()

    for variant in (
        inventory_reversed,
        claims_reversed,
        both_reversed,
        outer_reversed,
    ):
        assert freeze(variant).body == expected_request
        assert canonical(evaluate(variant)) == expected_output

    duplicate_inventory = copy.deepcopy(req)
    inventory = duplicate_inventory["bridge"]["completeness"]["inventoried_categories"]
    inventory.append(inventory[0])
    duplicate_inventory_result = evaluate(duplicate_inventory)
    assert duplicate_inventory_result["operating_value"]["available"]
    assert duplicate_inventory_result["common_equity"]["reason_codes"] == ["duplicate_id"]

    duplicate_claim = copy.deepcopy(req)
    duplicate_preferred = next(
        right for right in duplicate_claim["ownership"]["rights"] if right["id"] == "preferred"
    )
    duplicate_preferred["claim_event_ids"].append("preferred-a")
    duplicate_claim_result = evaluate(duplicate_claim)
    assert duplicate_claim_result["operating_value"]["available"]
    assert duplicate_claim_result["common_equity"]["reason_codes"] == ["ownership_claim_duplicated"]


@pytest.mark.parametrize("location", ["inventory", "claims"])
def test_malformed_nested_bridge_sets_refuse_at_owner(location):
    req, f = request()
    items = [claim(req, f, "preferred", "noncommon_claims", 2)]
    complete_bridge(req, f, items)
    preferred = next(right for right in req["ownership"]["rights"] if right["id"] == "preferred")
    preferred.update(status="valued", claim_event_ids=["preferred"])
    if location == "inventory":
        req["bridge"]["completeness"]["inventoried_categories"].append({})
    else:
        preferred["claim_event_ids"].append({})

    result = evaluate(req)
    assert result["operating_cash_flows"]["available"]
    assert result["operating_value"]["available"]
    assert result["common_equity"]["reason_codes"] == ["nonempty_text_required"]


def test_consumed_unresolved_operands_are_stage_scoped():
    cases = (
        ("schedule", (False, False, False, False)),
        ("rate", (True, False, False, False)),
        ("terminal", (True, False, False, False)),
        ("bridge", (True, True, False, False)),
        ("shares", (True, True, True, False)),
    )
    for location, expected in cases:
        req, f = request()
        item = claim(req, f, "debt", "nonlease_debt", 10)
        complete_bridge(req, f, [item])
        operands = {
            "schedule": req["operating_schedule"]["periods"][0]["cash_operating_profit_before_da"],
            "rate": req["discount"]["rate"],
            "terminal": req["terminal"]["growth"],
            "bridge": item["value"],
            "shares": req["ownership"]["ordinary_shares"],
        }
        operands[location]["provenance"]["uncertainty"] = "unresolved"
        result = evaluate(req)
        assert (
            tuple(
                result[key]["available"]
                for key in (
                    "operating_cash_flows",
                    "operating_value",
                    "common_equity",
                    "per_share",
                )
            )
            == expected
        )
        first_unavailable = expected.index(False)
        key = ("operating_cash_flows", "operating_value", "common_equity", "per_share")[
            first_unavailable
        ]
        assert result[key]["reason_codes"] == ["required_operand_unresolved"]


def test_zero_mature_tax_sources_do_not_require_phantom_queue_lots():
    profits = (100, 100, 0, 0, 0, 0, 0, 0)
    explicit_zero, explicit_inputs = request(profits=profits, horizon=4, growth=0)
    set_tail(
        explicit_zero,
        explicit_inputs,
        4,
        0,
        {"tax/tax/arising": 3, "tax/tax/entitlement": 3},
    )
    explicit_result = evaluate(explicit_zero)
    assert explicit_result["operating_value"]["available"]

    no_phantom = copy.deepcopy(explicit_zero)
    no_phantom["operating_schedule"]["tax_lots"] = [
        lot for lot in no_phantom["operating_schedule"]["tax_lots"] if lot["amount"]["value"] != 0
    ]
    result = evaluate(no_phantom)
    assert [
        row["fcff_operations"] for row in result["operating_cash_flows"]["value"]
    ] == pytest.approx(
        [
            85,
            80,
            -5,
            0,
            0,
            0,
            0,
            0,
        ]
    )
    assert result["terminal"]["next_year_fcff"] == 0
    assert result["terminal"]["present_value"] == 0
    assert result["operating_value"]["value"] == pytest.approx(123.49537037037037)
    assert result["operating_value"] == explicit_result["operating_value"]


def test_terminal_queue_uses_accepted_horizon_settlement_state():
    from tools.check_forward_fcff_acceptance import _queue_state

    req, inputs = request(
        profits=(1, 0, 0, 0),
        tau=0.3,
        rho=0,
        lag=1,
        horizon=2,
        growth=0,
    )
    first_ledger = req["operating_schedule"]["periods"][0]["tax_ledgers"][0]
    first_allocation = first_ledger["allocations"][0]
    first_allocation["amount"] = inputs.operand(0.1)
    second_allocation = copy.deepcopy(first_allocation)
    second_allocation["id"] += "-second"
    second_allocation["amount"] = inputs.operand(0.2)
    first_ledger["allocations"].append(second_allocation)
    first_ledger["payment"]["value"] = 0.3
    first_ledger["payable_close"]["value"] = 0

    second_ledger = req["operating_schedule"]["periods"][1]["tax_ledgers"][0]
    second_ledger["allocations"] = []
    second_ledger["payable_open"]["value"] = 0
    second_ledger["payment"]["value"] = 0
    second_ledger["payable_close"]["value"] = 0
    set_tail(
        req,
        inputs,
        2,
        0,
        {"tax/tax/arising": 2, "tax/tax/entitlement": 2},
        1,
    )

    payment_kernel = next(
        kernel for kernel in req["terminal"]["tax_kernels"] if kernel["leg"] == "payment"
    )
    assert "payable-1" not in {queue["lot_id"] for queue in payment_kernel["queues"]}
    assert "payable-1" not in _queue_state(req, 2, "tax", "payment")
    result = evaluate(req)
    assert result["operating_value"]["value"] == pytest.approx(0.7 / 1.2)

    grouped = copy.deepcopy(req)
    grouped_allocation = grouped["operating_schedule"]["periods"][0]["tax_ledgers"][0][
        "allocations"
    ][0]
    grouped_allocation["amount"]["value"] = 0.3
    grouped["operating_schedule"]["periods"][0]["tax_ledgers"][0]["allocations"] = [
        grouped_allocation
    ]
    assert evaluate(grouped)["operating_value"]["value"] == pytest.approx(
        result["operating_value"]["value"]
    )


def test_terminal_queue_inventory_includes_tiny_distant_nonzero_lot():
    req, inputs = request(
        profits=(100,) * 12,
        tau=0.2,
        rho=0.1,
        lag=5,
        horizon=6,
        growth=0,
    )
    lot = next(lot for lot in req["operating_schedule"]["tax_lots"] if lot["id"] == "refund-5")
    lot["amount"] = inputs.operand(9.999999995)
    tiny_lot = copy.deepcopy(lot)
    tiny_lot["id"] = "refund-5-tiny"
    tiny_lot["amount"] = inputs.operand(0.000000005)
    req["operating_schedule"]["tax_lots"].append(tiny_lot)
    allocation = next(
        allocation
        for allocation in req["operating_schedule"]["periods"][9]["tax_ledgers"][0]["allocations"]
        if allocation["lot_id"] == "refund-5"
    )
    allocation["amount"] = inputs.operand(9.999999995)
    tiny_allocation = copy.deepcopy(allocation)
    tiny_allocation["id"] = "cash-refund-10-5-tiny"
    tiny_allocation["lot_id"] = tiny_lot["id"]
    tiny_allocation["amount"] = inputs.operand(0.000000005)
    req["operating_schedule"]["periods"][9]["tax_ledgers"][0]["allocations"].append(tiny_allocation)
    set_tail(
        req,
        inputs,
        6,
        0,
        {"tax/tax/arising": 1, "tax/tax/entitlement": 1},
        5,
    )

    complete = evaluate(req)
    assert complete["operating_value"]["value"] == pytest.approx(382.59387860082313)
    refund_kernel = next(
        kernel for kernel in req["terminal"]["tax_kernels"] if kernel["leg"] == "refund"
    )
    refund_kernel["queues"] = [
        queue for queue in refund_kernel["queues"] if queue["lot_id"] != tiny_lot["id"]
    ]
    omitted = evaluate(req)
    assert omitted["operating_cash_flows"]["available"]
    assert omitted["operating_value"]["reason_codes"] == ["terminal_mature_queue_missing"]
    assert omitted["operating_value"]["missing_inputs"] == [
        "terminal.tax_kernels.tax.refund.queues"
    ]


def test_terminal_queue_covers_all_remaining_lots_beyond_witness_years():
    req, _ = request(
        profits=(100,) * 12,
        tau=0.1,
        rho=0.1,
        lag=5,
        horizon=5,
        growth=0,
    )
    entitlement = next(
        template
        for template in req["terminal"]["templates"]
        if template["path"] == "tax/tax/entitlement"
    )
    entitlement["regime_start"] = 1
    assert evaluate(req)["operating_value"]["available"]
    req["terminal"]["tax_kernels"][1]["queues"] = [
        queue
        for queue in req["terminal"]["tax_kernels"][1]["queues"]
        if queue["source_period"] != 3
    ]
    omitted = evaluate(req)
    assert omitted["operating_cash_flows"]["available"]
    assert omitted["operating_value"]["reason_codes"] == ["terminal_mature_queue_missing"]
    assert omitted["operating_value"]["missing_inputs"] == [
        "terminal.tax_kernels.tax.refund.queues"
    ]


def test_exhausted_state_ids_are_unique_without_collapsing_distinct_states():
    req, _ = request()
    req["terminal"]["exhausted_states"] = [
        {
            "id": "plant-closing",
            "path": "asset/plant/closing",
            "exhausted_by": 1,
            "rationale": "Synthetic zero state",
        },
        {
            "id": "plant-construction",
            "path": "asset/plant/construction_close",
            "exhausted_by": 1,
            "rationale": "Synthetic distinct zero state",
        },
    ]
    permuted = copy.deepcopy(req)
    permuted["terminal"]["exhausted_states"].reverse()
    assert freeze(req).body == freeze(permuted).body
    assert canonical(evaluate(req)) == canonical(evaluate(permuted))
    req["terminal"]["exhausted_states"][1]["id"] = "plant-closing"
    duplicate = evaluate(req)
    assert duplicate["operating_cash_flows"]["available"]
    assert duplicate["operating_value"]["reason_codes"] == ["duplicate_id"]
    assert duplicate["operating_value"]["missing_inputs"] == ["terminal.exhausted_states"]


def test_acquisitions_and_rights_have_stable_unique_canonical_identities():
    req, f = request()
    complete_bridge(req, f)
    req["object"]["acquisitions"] = [
        {
            "id": "earlier",
            "description": "Synthetic earlier acquisition",
            "included": True,
            "control_date": "2025-01-01",
        },
        {
            "id": "later",
            "description": "Synthetic later acquisition",
            "included": True,
            "control_date": "2026-01-01",
        },
    ]
    permuted = copy.deepcopy(req)
    permuted["object"]["acquisitions"].reverse()
    permuted["ownership"]["rights"].reverse()
    assert freeze(req).body == freeze(permuted).body
    assert canonical(evaluate(req)) == canonical(evaluate(permuted))

    duplicate_acquisition = copy.deepcopy(req)
    duplicate_acquisition["object"]["acquisitions"][1]["id"] = "earlier"
    acquisition_result = evaluate(duplicate_acquisition)
    assert acquisition_result["operating_cash_flows"]["reason_codes"] == ["duplicate_id"]
    assert acquisition_result["operating_cash_flows"]["status"] == "invalid_input"

    duplicate_right = copy.deepcopy(req)
    duplicate_right["ownership"]["rights"][1]["id"] = duplicate_right["ownership"]["rights"][0][
        "id"
    ]
    right_result = evaluate(duplicate_right)
    assert right_result["operating_cash_flows"]["available"]
    assert right_result["operating_value"]["available"]
    assert right_result["common_equity"]["reason_codes"] == ["duplicate_id"]
    assert right_result["common_equity"]["status"] == "invalid_input"


@pytest.mark.parametrize("part", ["templates", "tax_kernels", "queues", "weights"])
def test_duplicate_terminal_identities_only_refuse_value(part):
    req, _ = request()
    if part in {"templates", "tax_kernels"}:
        values = req["terminal"][part]
    else:
        values = req["terminal"]["tax_kernels"][0][part]
    values.append(copy.deepcopy(values[0]))

    result = evaluate(req)
    assert result["operating_cash_flows"]["available"]
    assert result["operating_value"]["reason_codes"] == ["duplicate_id"]
    assert result["operating_value"]["status"] == "invalid_input"


@pytest.mark.parametrize("leg", ["payment", "refund"])
def test_duplicate_terminal_lot_references_keep_empty_diagnostic_path(leg):
    req, _ = request()
    kernel = next(kernel for kernel in req["terminal"]["tax_kernels"] if kernel["leg"] == leg)
    duplicate = copy.deepcopy(kernel["queues"][0])
    duplicate["id"] += "-duplicate"
    kernel["queues"].append(duplicate)

    result = evaluate(req)
    assert result["operating_cash_flows"]["available"]
    assert result["operating_value"]["reason_codes"] == ["duplicate_id"]
    assert result["operating_value"]["missing_inputs"] == []


@pytest.mark.parametrize("identity", ["", 7, None, {}])
@pytest.mark.parametrize("owner", ["terminal", "bridge", "ownership"])
def test_malformed_optional_identities_only_refuse_owner(owner, identity):
    req, inputs = request()
    complete_bridge(
        req,
        inputs,
        [claim(req, inputs, "cash", "excess_distributable_cash", 10, "asset")],
    )
    if owner == "terminal":
        req["terminal"]["templates"][0]["id"] = identity
    elif owner == "bridge":
        req["bridge"]["items"][0]["id"] = identity
    else:
        req["ownership"]["rights"][0]["id"] = identity

    result = evaluate(req)
    assert result["operating_cash_flows"]["available"]
    if owner == "terminal":
        assert result["operating_value"]["reason_codes"] == ["nonempty_text_required"]
    else:
        assert result["operating_value"]["available"]
        assert result["common_equity"]["reason_codes"] == ["nonempty_text_required"]


@pytest.mark.parametrize("offset", ["", 0.5, None, True])
def test_malformed_terminal_weight_offsets_only_refuse_value(offset):
    req, _ = request()
    req["terminal"]["tax_kernels"][0]["weights"][0]["offset_years"] = offset

    result = evaluate(req)
    assert result["operating_cash_flows"]["available"]
    assert result["operating_value"]["reason_codes"] == ["invalid_integer"]


@pytest.mark.parametrize("part", ["items", "categories"])
def test_duplicate_bridge_identities_only_refuse_equity(part):
    req, inputs = request()
    complete_bridge(
        req,
        inputs,
        [claim(req, inputs, "cash", "excess_distributable_cash", 10, "asset")],
    )
    req["bridge"][part].append(copy.deepcopy(req["bridge"][part][0]))

    result = evaluate(req)
    assert result["operating_cash_flows"]["available"]
    assert result["operating_value"]["available"]
    assert result["common_equity"]["reason_codes"] == ["duplicate_id"]
    assert result["common_equity"]["status"] == "invalid_input"


def test_acquisition_control_dates_gate_existing_operations_perimeter():
    req, _ = request()
    acquisition = {
        "id": "acquisition",
        "description": "Synthetic perimeter declaration",
        "included": True,
        "control_date": req["valuation_date"],
    }
    req["object"]["acquisitions"] = [acquisition]
    assert evaluate(req)["operating_cash_flows"]["available"]

    acquisition["control_date"] = "2027-01-01"
    future = evaluate(req)
    assert future["operating_cash_flows"]["reason_codes"] == [
        "future_acquisition_control_unsupported"
    ]
    assert future["operating_cash_flows"]["missing_inputs"] == [
        "object.acquisitions.acquisition.control_date"
    ]

    acquisition["control_date"] = None
    unknown = evaluate(req)
    assert unknown["operating_cash_flows"]["reason_codes"] == ["acquisition_control_date_missing"]
    acquisition["included"] = False
    assert evaluate(req)["operating_cash_flows"]["available"]


def test_a11_share_units():
    req, f = request(profits=(0.15, 0.15, 0.15), tau=0, rho=0, horizon=1, growth=0, rate=0.1)
    complete_bridge(req, f, shares=300000)
    result = evaluate(req)
    assert val(result, "common_equity") == pytest.approx(1.5)
    assert val(result, "per_share") == pytest.approx(5)


def test_limitation_targets_must_name_supported_outputs():
    req, _ = request()
    req["limitations"] = [
        {
            "id": "misspelled-equity-target",
            "text": "Material equity claim remains unresolved",
            "affected_outputs": ["common_equitty"],
            "conditional_outputs": [],
            "material": True,
        }
    ]
    result = evaluate(req)
    assert result["operating_cash_flows"]["reason_codes"] == ["invalid_limitation_output"]


def test_material_limitations_withhold_each_target_and_dependents():
    for target in (
        "operating_cash_flows",
        "operating_value",
        "common_equity",
        "per_share",
    ):
        req, f = request()
        complete_bridge(req, f)
        req["limitations"] = [
            {
                "id": f"unresolved-{target}",
                "text": "Material input remains unresolved",
                "affected_outputs": [target],
                "conditional_outputs": [],
                "material": True,
            }
        ]
        result = evaluate(req)
        if target == "operating_cash_flows":
            assert result[target]["reason_codes"] == [
                "operating_cash_flow_material_input_unresolved"
            ]
            assert not result["operating_value"]["available"]
        elif target == "operating_value":
            assert result["operating_cash_flows"]["available"]
            assert result[target]["reason_codes"] == ["operating_value_material_input_unresolved"]
            assert result["common_equity"]["reason_codes"] == ["operating_value_unavailable"]
        elif target == "common_equity":
            assert result["operating_value"]["available"]
            assert result[target]["reason_codes"] == ["bridge_material_claim_unresolved"]
            assert result["per_share"]["reason_codes"] == ["common_equity_unavailable"]
        else:
            assert result["common_equity"]["available"]
            assert result[target]["reason_codes"] == ["ownership_material_input_unresolved"]


def test_material_limitation_requires_a_target_but_nonmaterial_note_may_be_untargeted():
    req, f = request()
    complete_bridge(req, f)
    req["limitations"] = [
        {
            "id": "unresolved-without-target",
            "text": "Material input has no declared output",
            "affected_outputs": [],
            "conditional_outputs": [],
            "material": True,
        }
    ]
    assert evaluate(req)["operating_cash_flows"]["reason_codes"] == ["invalid_limitation_output"]
    req["limitations"][0].update(
        affected_outputs=["operating_value"],
        conditional_outputs=["operating_value"],
    )
    assert evaluate(req)["operating_value"]["available"]
    req["limitations"][0].update(
        affected_outputs=[],
        conditional_outputs=[],
        material=False,
    )
    assert evaluate(req)["per_share"]["available"]


def test_a13_real_material_claim_cannot_be_zeroed():
    req, f = request()
    complete_bridge(req, f)
    req["limitations"] = [
        {
            "id": "actual-cash-unknown",
            "text": "Material actual issuer cash/claims not classified",
            "affected_outputs": ["common_equity", "per_share"],
            "conditional_outputs": [],
            "material": True,
        }
    ]
    result = evaluate(req)
    val(result, "operating_value")
    assert result["common_equity"]["reason_codes"] == ["bridge_material_claim_unresolved"]


def test_a14_double_counted_event_scoped_bridge():
    req, f = request()
    complete_bridge(req, f, [claim(req, f, "cash-payment-1-1", "legal_other", 5)])
    result = evaluate(req)
    val(result, "operating_value")
    assert result["common_equity"]["reason_codes"] == ["double_counted_event"]


@pytest.mark.parametrize(
    "category", ["pension_employee", "acquisition_consideration", "legal_other"]
)
def test_settlement_bridge_links_reconcile_category_direction_period_and_amount(category):
    req, f = request()
    req["operating_schedule"]["periods"][0]["settlements"] = [
        {
            "id": "scheduled-claim",
            "kind": "outflow",
            "amount": f.operand(5),
            "rationale": "Explicit scheduled claim cash",
            "bridge_category": category,
            "period_index": 1,
        }
    ]
    item = claim(req, f, "scheduled-claim", category, 5)
    item.update(location="operating_schedule", period_index=1)
    complete_bridge(req, f, [item])
    result = evaluate(req)
    assert result["operating_cash_flows"]["available"]
    assert result["operating_value"]["available"]
    assert result["common_equity"]["value"] == result["operating_value"]["value"]

    wrong_direction = copy.deepcopy(req)
    wrong_direction["operating_schedule"]["periods"][0]["settlements"][0]["kind"] = "recovery"
    assert evaluate(wrong_direction)["operating_cash_flows"]["reason_codes"] == [
        "settlement_bridge_direction_mismatch"
    ]
    wrong_period = copy.deepcopy(req)
    wrong_period["operating_schedule"]["periods"][0]["settlements"][0]["period_index"] = 2
    assert evaluate(wrong_period)["operating_cash_flows"]["reason_codes"] == [
        "settlement_period_mismatch"
    ]
    wrong_link_period = copy.deepcopy(req)
    wrong_link_period["bridge"]["items"][0]["period_index"] = 2
    wrong_link = evaluate(wrong_link_period)
    assert wrong_link["operating_value"]["available"]
    assert wrong_link["common_equity"]["reason_codes"] == ["claim_operating_event_mismatch"]
    wrong_amount = copy.deepcopy(req)
    wrong_amount["bridge"]["items"][0]["value"]["value"] = 4
    assert evaluate(wrong_amount)["common_equity"]["reason_codes"] == [
        "claim_operating_event_mismatch"
    ]


def test_bridge_operating_link_requires_matching_event_type_and_amount():
    req, f = request()
    allocation = req["operating_schedule"]["periods"][0]["tax_ledgers"][0]["allocations"][0]
    item = claim(
        req,
        f,
        allocation["id"],
        "nonlease_debt",
        allocation["amount"]["value"],
    )
    item["location"] = "operating_schedule"
    item["period_index"] = 1
    complete_bridge(req, f, [item])
    result = evaluate(req)
    val(result, "operating_value")
    assert result["common_equity"]["reason_codes"] == ["claim_operating_event_mismatch"]


@pytest.mark.parametrize("module", ["discount", "terminal", "bridge", "ownership"])
def test_optional_operand_duplicates_only_refuse_dependent_outputs(module):
    req, f = request()
    operating_id = req["operating_schedule"]["initial_wc"]["provenance"]["id"]
    if module in {"bridge", "ownership"}:
        items = [claim(req, f, "cash", "excess_distributable_cash", 10, "asset")]
        complete_bridge(req, f, items)
    targets = {
        "discount": req["discount"]["rate"],
        "terminal": req["terminal"]["growth"],
        "bridge": req.get("bridge", {}).get("items", [{}])[0].get("value"),
        "ownership": req.get("ownership", {}).get("ordinary_shares"),
    }
    valid = evaluate(req)
    assert valid["operating_value"]["available"]
    if module in {"bridge", "ownership"}:
        assert valid["common_equity"]["available"]
    if module == "ownership":
        assert valid["per_share"]["available"]
    targets[module]["provenance"]["id"] = operating_id
    result = evaluate(req)
    assert result["operating_cash_flows"]["available"]
    if module in {"discount", "terminal"}:
        assert result["operating_value"]["reason_codes"] == ["duplicate_operand_id"]
    elif module == "bridge":
        assert result["operating_value"]["available"]
        assert result["common_equity"]["reason_codes"] == ["duplicate_operand_id"]
    else:
        assert result["operating_value"]["available"]
        assert result["common_equity"]["available"]
        assert result["per_share"]["reason_codes"] == ["duplicate_operand_id"]


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), None])
def test_a12_invalid_required_operand(value):
    req, _ = request()
    req["operating_schedule"]["periods"][0]["book_da"]["value"] = value
    assert not evaluate(req)["operating_cash_flows"]["available"]


@pytest.mark.parametrize(
    "change",
    [
        "duplicate_key",
        "unknown_field",
        "duplicate_event",
        "date",
        "bool_index",
        "period_gap",
        "units",
    ],
)
def test_a11_a12_closed_shape(change):
    req, _ = request()
    if change == "duplicate_key":
        with pytest.raises(ContractError):
            freeze('{"schema_version":"a","schema_version":"b"}')
        return
    if change == "unknown_field":
        req["unrequested_fx"] = 1
    if change == "duplicate_event":
        req["operating_schedule"]["tax_lots"].append(
            copy.deepcopy(req["operating_schedule"]["tax_lots"][0])
        )
    if change == "date":
        req["valuation_date"] = "2026-02-30"
    if change == "bool_index":
        req["operating_schedule"]["periods"][0]["index"] = True
    if change == "period_gap":
        req["operating_schedule"]["periods"][1]["start"] = "2027-07-02"
    if change == "units":
        req["denomination"]["amount_units"] = "absolute"
    assert not evaluate(req)["operating_cash_flows"]["available"]


def test_leap_day_stays_in_full_annual_period_without_shifting_endpoints():
    req, _ = request()
    req["valuation_date"] = "2024-02-28"
    req["evidence_cutoff"] = "2024-02-28"
    req["prepared_on"] = "2024-02-28"
    expected = [
        ("2024-02-29", "2025-02-28"),
        ("2025-03-01", "2026-02-28"),
        ("2026-03-01", "2027-02-28"),
        ("2027-03-01", "2028-02-28"),
        ("2028-02-29", "2029-02-28"),
        ("2029-03-01", "2030-02-28"),
    ]
    for row, (start, end) in zip(req["operating_schedule"]["periods"], expected, strict=True):
        row["start"] = start
        row["end"] = end
    result = evaluate(req)
    assert result["operating_cash_flows"]["available"]
    assert [
        (row["start"], row["end"]) for row in result["operating_cash_flows"]["value"]
    ] == expected

    shortened = copy.deepcopy(req)
    shortened["operating_schedule"]["periods"][0]["end"] = "2025-02-27"
    assert evaluate(shortened)["operating_cash_flows"]["reason_codes"] == ["unsupported_period"]


def test_a10_fact_source_cutoff_and_real_review():
    req, _ = request()
    operand = req["operating_schedule"]["periods"][0]["cash_operating_profit_before_da"]
    p = operand["provenance"]
    p.update(
        {
            "class": "reported_fact",
            "uncertainty": "source_observed",
            "accounting_date": "2026-06-30",
            "source_refs": [{"source_id": "source", "anchor": "p1"}],
        }
    )
    result = evaluate(req)
    assert result["operating_cash_flows"]["reason_codes"] == ["source_reference_unresolved"]
    catalog = {
        "sources": [
            {
                "id": "source",
                "url": "https://example.test/source",
                "sha256": "a" * 64,
                "publication_date": "2026-07-01",
                "observation_date": "2026-06-30",
                "ingested_at": "2026-07-02T00:00:00Z",
                "anchors": {"p1": "Synthetic retained fact, no private evidence"},
                "limitations": [],
            }
        ]
    }
    req["prepared_on"] = "2026-10-01"
    req["evidence_context"] = {"catalog": catalog, "catalog_hash": digest(catalog)}
    assert evaluate(req)["operating_cash_flows"]["reason_codes"] == ["fact_not_cutoff_qualified"]
    req["evidence_cutoff"] = "2026-07-01"
    req["knowledge_basis"] = "retrospective_research"
    result = evaluate(req)
    val(result, "operating_cash_flows")
    assert result["review_status"] == "unreviewed"
    req["reviews"] = [
        {
            "id": "review",
            "reviewer": "Actual test human event",
            "reviewer_kind": "human",
            "reviewed_on": "2026-10-02",
            "scope_operand_ids": [p["id"]],
            "basis_identity": "actual basis",
            "conclusion": "conditional",
            "limitations": [],
        }
    ]
    assert evaluate(req)["operating_cash_flows"]["reason_codes"] == ["invalid_review_date_or_kind"]


def test_prepublication_review_is_retained_but_cannot_qualify_output():
    req, _ = request()
    operand = req["discount"]["rate"]
    provenance = operand["provenance"]
    provenance["source_refs"] = [{"source_id": "source", "anchor": "p1"}]
    catalog = {
        "sources": [
            {
                "id": "source",
                "url": "https://example.test/source",
                "sha256": "a" * 64,
                "publication_date": "2026-01-01",
                "observation_date": "2025-12-31",
                "ingested_at": "2026-01-02T00:00:00Z",
                "anchors": {"p1": "Synthetic retained fact"},
                "limitations": [],
            }
        ]
    }
    req["evidence_context"] = {"catalog": catalog, "catalog_hash": digest(catalog)}
    used = set(evaluate(req)["equations"]["operand_ids"])

    def link(tree):
        if isinstance(tree, dict):
            linked = tree.get("provenance")
            if isinstance(linked, dict) and linked.get("id") in used:
                linked["review_ids"] = ["review"]
            for value in tree.values():
                link(value)
        elif isinstance(tree, list):
            for value in tree:
                link(value)

    link(req)
    req["reviews"] = [
        {
            "id": "review",
            "reviewer": "Actual test human event",
            "reviewer_kind": "human",
            "reviewed_on": "2020-01-01",
            "scope_operand_ids": sorted(used),
            "basis_identity": "actual complete basis",
            "conclusion": "conditional",
            "limitations": [],
        }
    ]
    early = evaluate(req)
    assert early["operating_value"]["available"]
    assert early["review_status"] == "unreviewed"
    assert early["review_diagnostics"] == [
        {
            "code": "review_precedes_source_publication",
            "operand_id": provenance["id"],
            "review_id": "review",
            "source_id": "source",
        }
    ]
    req["reviews"][0]["reviewed_on"] = "2026-02-01"
    eligible = evaluate(req)
    assert eligible["operating_value"]["value"] == early["operating_value"]["value"]
    assert eligible["review_status"] == "human_reviewed"
    assert "review_diagnostics" not in eligible

    catalog["sources"][0]["publication_date"] = None
    catalog["sources"][0]["limitations"] = ["publication_unknown"]
    req["evidence_context"] = {"catalog": catalog, "catalog_hash": digest(catalog)}
    unknown = evaluate(req)
    assert unknown["operating_value"]["value"] == early["operating_value"]["value"]
    assert unknown["review_status"] == "unreviewed"
    assert unknown["review_diagnostics"] == [
        {
            "code": "review_source_publication_unknown",
            "operand_id": provenance["id"],
            "review_id": "review",
            "source_id": "source",
        }
    ]


def test_human_review_status_requires_transitive_source_chronology():
    req, _ = request()
    child = req["discount"]["rate"]["provenance"]
    parent = req["operating_schedule"]["initial_wc"]["provenance"]
    child["derived_from"] = [parent["id"]]
    child["source_refs"] = [{"source_id": "old-source", "anchor": "p1"}]
    parent["source_refs"] = [{"source_id": "new-source", "anchor": "p1"}]
    catalog = {
        "sources": [
            {
                "id": "old-source",
                "url": "https://example.test/old",
                "sha256": "a" * 64,
                "publication_date": "2019-01-01",
                "observation_date": "2018-12-31",
                "ingested_at": "2026-01-02T00:00:00Z",
                "anchors": {"p1": "Synthetic old premise"},
                "limitations": [],
            },
            {
                "id": "new-source",
                "url": "https://example.test/new",
                "sha256": "b" * 64,
                "publication_date": "2026-01-01",
                "observation_date": "2025-12-31",
                "ingested_at": "2026-01-02T00:00:00Z",
                "anchors": {"p1": "Synthetic parent premise"},
                "limitations": [],
            },
        ]
    }
    req["evidence_context"] = {"catalog": catalog, "catalog_hash": digest(catalog)}
    used = set(evaluate(req)["equations"]["operand_ids"])

    def link(tree):
        if isinstance(tree, dict):
            provenance = tree.get("provenance")
            if isinstance(provenance, dict) and provenance.get("id") in used:
                provenance["review_ids"] = ["review"]
            for value in tree.values():
                link(value)
        elif isinstance(tree, list):
            for value in tree:
                link(value)

    link(req)
    parent["review_ids"] = ["parent-review"]
    req["reviews"] = [
        {
            "id": "review",
            "reviewer": "Actual test human event",
            "reviewer_kind": "human",
            "reviewed_on": "2020-01-01",
            "scope_operand_ids": sorted(used),
            "basis_identity": "complete child scope",
            "conclusion": "conditional",
            "limitations": [],
        },
        {
            "id": "parent-review",
            "reviewer": "Actual parent review",
            "reviewer_kind": "human",
            "reviewed_on": "2026-02-01",
            "scope_operand_ids": [parent["id"]],
            "basis_identity": "eligible parent scope",
            "conclusion": "conditional",
            "limitations": [],
        },
    ]
    result = evaluate(req)
    assert result["operating_value"]["available"]
    assert result["review_status"] == "unreviewed"
    assert {
        diagnostic["operand_id"]
        for diagnostic in result["review_diagnostics"]
        if diagnostic["code"] == "review_precedes_source_publication"
    } == {child["id"]}


def test_human_review_status_requires_operand_backlinks():
    req, _ = request()
    used = set(evaluate(req)["equations"]["operand_ids"])

    def link(tree):
        if isinstance(tree, dict):
            provenance = tree.get("provenance")
            if isinstance(provenance, dict) and provenance.get("id") in used:
                provenance["review_ids"] = ["review"]
            for value in tree.values():
                link(value)
        elif isinstance(tree, list):
            for value in tree:
                link(value)

    link(req)
    req["reviews"] = [
        {
            "id": "review",
            "reviewer": "Actual test human event",
            "reviewer_kind": "human",
            "reviewed_on": "2026-06-30",
            "scope_operand_ids": sorted(used),
            "basis_identity": "actual complete basis",
            "conclusion": "conditional",
            "limitations": [],
        }
    ]
    assert evaluate(req)["review_status"] == "human_reviewed"

    def unlink(tree, identity):
        if isinstance(tree, dict):
            provenance = tree.get("provenance")
            if isinstance(provenance, dict) and provenance.get("id") == identity:
                provenance["review_ids"] = []
            for value in tree.values():
                unlink(value, identity)
        elif isinstance(tree, list):
            for value in tree:
                unlink(value, identity)

    unlink(req, next(iter(used)))
    assert evaluate(req)["review_status"] == "unreviewed"


def test_a17_canonical_input_event_order_not_paths():
    req, _ = request()
    same = copy.deepcopy(req)
    same["operating_schedule"]["tax_lots"].reverse()
    for row in same["operating_schedule"]["periods"]:
        row["tax_ledgers"][0]["allocations"].reverse()
    assert freeze(req).body == freeze(json.dumps(same, sort_keys=True)).body
    assert canonical(evaluate(req)) == canonical(evaluate(same))
    same["discount"]["rate"]["value"] += 0.01
    assert freeze(req).body != freeze(same).body


def test_a3_eligibility_cash_before_known_date():
    req, _ = request()
    lot = req["operating_schedule"]["tax_lots"][0]
    lot["eligible_date"] = "2030-01-01"
    result = evaluate(req)
    assert result["operating_cash_flows"]["reason_codes"] == ["refund_eligibility_unestablished"]


def test_structured_order_bounds_reject_only_definitely_ineligible_refund():
    req, _ = request(lag=0)
    row = req["operating_schedule"]["periods"][0]
    allocations = row["tax_ledgers"][0]["allocations"]
    refund = next(allocation for allocation in allocations if allocation["kind"] == "refund")
    payment = next(allocation for allocation in allocations if allocation["kind"] == "payment")
    refund["cash_date"] = None
    refund["eligibility_order"] = {"rank": 1, "basis": "Refund before dated payment"}
    payment["cash_date"] = row["start"]
    payment["eligibility_order"] = {"rank": 2, "basis": "Dated payment after refund"}
    refund_lot = next(
        lot for lot in req["operating_schedule"]["tax_lots"] if lot["id"] == refund["lot_id"]
    )
    refund_lot["eligible_date"] = row["end"]

    refused = evaluate(req)
    for output_name in ("operating_cash_flows", "operating_value", "common_equity", "per_share"):
        assert refused[output_name]["reason_codes"] == ["refund_eligibility_unestablished"]
        assert not refused[output_name]["available"]

    payment["cash_date"] = row["end"]
    valid = evaluate(req)
    assert valid["operating_cash_flows"]["value"][0]["fcff_operations"] == 95
    assert valid["operating_value"]["value"] == pytest.approx(756.25)


def test_arbitrary_dateless_tax_order_text_is_not_interpreted():
    req, _ = request()
    assert evaluate(req)["operating_cash_flows"]["available"]
    allocation = req["operating_schedule"]["periods"][1]["tax_ledgers"][0]["allocations"][-1]
    allocation["eligibility_order"] = "Refund BEFORE its prerequisite payment"
    result = evaluate(req)
    assert result["operating_cash_flows"]["reason_codes"] == ["tax_event_order_invalid"]
    assert result["operating_cash_flows"]["status"] == "invalid_input"
    assert not result["operating_value"]["available"]


def test_zero_gross_legs_and_no_tax_division():
    req, _ = request(tau=0, rho=0)
    val(evaluate(req), "operating_value")
