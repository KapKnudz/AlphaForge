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
                "eligibility_order": "Explicit assumed ordering",
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
            "material": True,
        }
    ]
    result = evaluate(req)
    assert result["operating_cash_flows"]["reason_codes"] == ["invalid_limitation_output"]


def test_a13_real_material_claim_cannot_be_zeroed():
    req, f = request()
    complete_bridge(req, f)
    req["limitations"] = [
        {
            "id": "actual-cash-unknown",
            "text": "Material actual issuer cash/claims not classified",
            "affected_outputs": ["common_equity", "per_share"],
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
    targets[module]["provenance"]["id"] = operating_id
    result = evaluate(req)
    assert result["operating_cash_flows"]["available"]
    dependent = "operating_value" if module in {"discount", "terminal"} else "common_equity"
    if dependent == "common_equity":
        assert result["operating_value"]["available"]
    assert result[dependent]["reason_codes"] == ["duplicate_operand_id"]


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


def test_zero_gross_legs_and_no_tax_division():
    req, _ = request(tau=0, rho=0)
    val(evaluate(req), "operating_value")
