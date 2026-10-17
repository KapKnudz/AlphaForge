"""Synthetic reverse cases at the shared evaluator, numerical seam and real CLI."""

import copy
import csv
import json
from decimal import Decimal as D
from decimal import localcontext

import pytest

from alphaforge.cli.estimated_forward import export
from alphaforge.cli.main import build_parser
from alphaforge.core.valuation import estimated_fcff as forward
from alphaforge.core.valuation.estimated_inputs import Unavailable, revise
from alphaforge.core.valuation.estimated_reverse import search
from tests.estimated_fixtures import block, response


def reverse_pack(target="32", lower="-.02", upper=".06"):
    p = response()
    p["scenarios"]["base"]["reverse"] = block(
        parameter="initial_nominal_revenue_growth",
        domain=block(
            lower=lower,
            upper=upper,
            selected_independently_of_target=True,
            selection_basis="Synthetic capacity permits modest contraction to expansion, selected before price.",
            full_interval_support="Synthetic response, tax and one-year capacity lead supported over these bounds.",
            fixed_claim_independence="Synthetic cash/lease debt fixed; performance claim responds to EBIT.",
            funding_and_distress_support="Synthetic retained liquidity funds this interval; no distress or optionality omitted.",
            thresholds_and_continuity="No known synthetic threshold; this statement is not a mathematical completeness proof.",
        ),
        target=block(
            price=target,
            currency="SEK",
            valuation_date="2024-02-29",
            close_date="2024-02-28",
            lag_days=1,
            share_basis="basic_common",
            exchange="SYNTHETIC",
            close_selection_basis="Last preceding synthetic close.",
            corporate_action_basis="No synthetic split or treasury change.",
            rights_comparability="Basic common target matches supplied rights allocation.",
        ),
    )
    return p


def independent_simple_price(x):
    """Independently written closed cash-budget arithmetic, no production imports.

    Constant 20% EBIT, 20% same-year tax, D&A5%, renewal6%, leases1%,
    next-year positive growth capital60%, WC10%, cash bridge15m, 10m shares, FX2.
    """
    with localcontext() as ctx:
        ctx.prec = 64
        sales = [D(100)]
        for k in range(10):
            sales.append(sales[-1] * (1 + x * D(9 - k) / 9))
        sales.append(sales[-1])
        cash = []
        wc = D(10)
        for k in range(1, 11):
            investment = max(D(0), sales[k + 1] - sales[k]) * D(".6")
            cash.append(sales[k] * D(".14") - investment - (sales[k] * D(".1") - wc))
            wc = sales[k] * D(".1")
        pv = sum((cf / D("1.1") ** k for k, cf in enumerate(cash, 1)), D(0))
        tail = sales[10] * D(".14") / D(".1") / D("1.1") ** 10
        return (pv + tail + 15) / 5


def result(p):
    return forward.evaluate(p)["scenarios"]["base"]["reverse"]


def test_recovered_path_reprices_independent_synthetic_budget_and_forward_is_unchanged():
    x = D(".013123")
    p = reverse_pack(str(independent_simple_price(x)))
    original = copy.deepcopy(p)
    before = forward.evaluate(response())["scenarios"]["base"]
    outputs = forward.evaluate(p)
    after = outputs["scenarios"]["base"]
    assert p == original
    assert {k: v for k, v in after.items() if k != "reverse"} == {
        k: v for k, v in before.items() if k != "reverse"
    }
    reverse = after["reverse"]
    assert reverse["status"] == "candidate_solution"
    value = reverse["value"]
    assert len(value["scan"]) == 201
    assert value["scan"][0]["x"] == "-0.02"
    assert value["scan"][-1]["x"] == "0.06"
    assert not value["coverage"]["certified_unique"]
    assert not value["coverage"]["exhaustive_roots"]
    (root,) = value["roots"]
    assert abs(D(root["initial_growth"]) - x) < D("1e-10")
    assert D(root["growth_localization"]) <= D("1e-10")
    assert abs(D(root["quote_residual"])) <= D("1e-6")
    assert len(root["growth_path"]) == len(root["fcff_path"]) == len(root["revenue_path"]) == 10
    assert D(root["growth_path"][-1]) == 0
    assert D(root["growth_path"][0]) == D(root["initial_growth"])
    recovered = response()
    recovered["scenarios"]["base"]["operations"]["inputs"]["initial_growth"] = root[
        "initial_growth"
    ]
    replay = forward.evaluate(recovered)["scenarios"]["base"]
    assert root["forward"] == {k: v for k, v in replay.items() if k != "reverse"}
    with localcontext() as ctx:
        ctx.prec = 64
        assert abs(
            (D(root["revenue_path"][-1]) / 100) ** (D(1) / 10) - 1 - D(root["horizon_cagr"])
        ) < D("1e-60")


def test_dependent_taxes_investment_wc_claims_and_rights_recompute():
    p = reverse_pack()
    s = p["scenarios"]["base"]
    inputs = s["operations"]["inputs"]
    inputs.update(
        refundable_profit_share=".5",
        recovery_lag=1,
        same_year_payment=".5",
        opening_payable="2",
        opening_recoverable="1",
    )
    claim = next(item for item in s["bridge"]["items"] if item["category"] == "consideration")
    claim.update(
        method="earnings_linked_pv",
        inputs={"recurring_baseline_ebit": "20", "current_pv_anchor": "1", "later_pv_anchor": "2"},
    )
    p["allocations"].append({"id": "award", "destination": "rights"})
    s["rights"]["items"] = [
        block(
            id="award",
            allocation_id="award",
            count="10000",
            strike="1",
            volatility="0",
            risk_free_continuous="0",
            dividend_yield="0",
            expiry="2025-02-28",
        )
    ]
    # Construct target at a declared endpoint to exercise the complete real forward path.
    s["operations"]["inputs"]["initial_growth"] = ".06"
    s["reverse"]["target"]["price"] = forward.evaluate(
        {**p, "scenarios": {"base": {k: v for k, v in s.items() if k != "reverse"}}}
    )["scenarios"]["base"]["per_share_quote"]["value"]["price_estimate"]
    r = result(p)
    assert r["status"] == "candidate_solution"
    recovered = r["value"]["roots"][0]["forward"]
    flat = copy.deepcopy(p)
    flat["scenarios"]["base"].pop("reverse")
    flat["scenarios"]["base"]["operations"]["inputs"]["initial_growth"] = "0"
    flat = forward.evaluate(flat)["scenarios"]["base"]
    for key in (
        "ebit",
        "ongoing_da",
        "new_charge",
        "new_entitlement",
        "new_payment",
        "new_recovery",
        "new_payable_remaining",
        "new_recoverable_remaining",
        "cash_investment",
        "lease_additions",
        "wc_close",
    ):
        assert (
            recovered["annual_fcff"]["value"][2]["value"][key]
            != flat["annual_fcff"]["value"][2]["value"][key]
        ), key
    assert recovered["bridge"]["value"] != flat["bridge"]["value"]
    assert (
        recovered["common_equity"]["value"]["rights_value"]
        != flat["common_equity"]["value"]["rights_value"]
    )
    assert recovered["wacc"] == flat["wacc"]
    for row in recovered["annual_fcff"]["value"]:
        assert row["available"]
    assert recovered["terminal"]["available"]


@pytest.mark.parametrize(
    "mutation, status",
    [
        (lambda s: s["reverse"].pop("domain"), "insufficient_evidence"),
        (lambda s: s["reverse"].pop("target"), "insufficient_evidence"),
        (lambda s: s["reverse"]["domain"].pop("full_interval_support"), "insufficient_evidence"),
        (lambda s: s["reverse"]["domain"].update(lower="-1"), "invalid_input"),
        (
            lambda s: s["reverse"]["domain"].update(selected_independently_of_target=False),
            "insufficient_evidence",
        ),
        (lambda s: s["reverse"]["target"].update(currency="EUR"), "invalid_input"),
        (lambda s: s["reverse"]["target"].update(lag_days=0), "invalid_input"),
        (lambda s: s["reverse"]["target"].update(price="0"), "invalid_input"),
        (lambda s: s["reverse"].update(parameter="cagr"), "unsupported"),
    ],
)
def test_reverse_prerequisites_only_withhold_reverse(mutation, status):
    p = reverse_pack()
    mutation(p["scenarios"]["base"])
    s = forward.evaluate(p)["scenarios"]["base"]
    assert s["reverse"]["status"] == status
    assert s["per_share_quote"]["available"]


@pytest.mark.parametrize(
    "mutation, status, code, path",
    [
        (
            lambda s: s.__setitem__("fx", None),
            "not_requested",
            "not_supplied",
            "quote_fx",
        ),
        (
            lambda s: s.__setitem__("ownership", None),
            "not_requested",
            "not_supplied",
            "ownership",
        ),
        (
            lambda s: s.__setitem__("terminal", None),
            "not_requested",
            "not_supplied",
            "terminal",
        ),
        (
            lambda s: s.__setitem__("bridge", None),
            "not_requested",
            "not_supplied",
            "bridge",
        ),
        (
            lambda s: s.__setitem__("rights", None),
            "not_requested",
            "not_supplied",
            "rights",
        ),
        (
            lambda s: s.__setitem__("discount", None),
            "not_requested",
            "not_supplied",
            "discount",
        ),
        (
            lambda s: s["operations"]["inputs"].pop("margin_first"),
            "insufficient_evidence",
            "missing_operand",
            "margin_first",
        ),
        (
            lambda s: s["fx"].update(record_ids=["missing-fx"]),
            "insufficient_evidence",
            "record_not_catalogued",
            "missing-fx",
        ),
    ],
)
def test_fixed_forward_refusal_is_preserved_by_requested_reverse(
    mutation, status, code, path
):
    p = reverse_pack()
    mutation(p["scenarios"]["base"])
    forward_only = copy.deepcopy(p)
    forward_only["scenarios"]["base"].pop("reverse")
    expected = forward.evaluate(forward_only)["scenarios"]["base"]["per_share_quote"]
    actual = forward.evaluate(p)["scenarios"]["base"]["reverse"]
    assert expected["status"] == status
    assert expected["reasons"] == [{"code": code, "path": path}]
    assert actual["status"] == expected["status"]
    assert actual["reasons"] == expected["reasons"]
    assert not actual["available"]


@pytest.mark.parametrize(
    "variant, status, reasons",
    [
        ("missing", "not_requested", [{"code": "not_supplied", "path": "rights"}]),
        ("malformed", "invalid_input", [{"code": "invalid_right_items", "path": ""}]),
        (
            "unqualified",
            "unsupported",
            [{"code": "unqualified_record", "path": "rights-bad"}],
        ),
        ("supported", "not_requested", [{"code": "not_supplied", "path": "ownership"}]),
    ],
)
def test_rights_qualification_precedes_standalone_ownership(variant, status, reasons):
    p = reverse_pack()
    scenario = p["scenarios"]["base"]
    scenario["ownership"] = None
    if variant == "missing":
        scenario["rights"] = None
    elif variant == "malformed":
        scenario["rights"]["items"] = "bad"
    elif variant == "unqualified":
        p["records"]["rights-bad"] = copy.deepcopy(p["records"]["budget"])
        p["records"]["rights-bad"]["status"] = "unsupported"
        scenario["rights"]["record_ids"] = ["rights-bad"]
    forward_only = copy.deepcopy(p)
    forward_only["scenarios"]["base"].pop("reverse")
    expected = forward.evaluate(forward_only)["scenarios"]["base"]["per_share_quote"]
    actual = result(p)
    assert expected["status"] == actual["status"] == status
    assert expected["reasons"] == actual["reasons"] == reasons


def test_fixed_terminal_refusal_precedes_candidate_domain_classification():
    p = reverse_pack()
    p["scenarios"]["base"]["operations"]["inputs"].update(
        legacy_remaining="100",
        legacy_annual="1",
        refundable_profit_share=".5",
        recovery_lag=3,
    )
    forward_only = copy.deepcopy(p)
    forward_only["scenarios"]["base"].pop("reverse")
    expected = forward.evaluate(forward_only)["scenarios"]["base"]["per_share_quote"]
    actual = result(p)
    assert expected["status"] == "invalid_input"
    assert expected["reasons"] == [{"code": "finite_effects_not_exhausted", "path": ""}]
    assert actual["status"] == expected["status"]
    assert actual["reasons"] == expected["reasons"]


@pytest.mark.parametrize(
    "field, path",
    [("fx", "quote_fx"), ("bridge", "bridge"), ("ownership", "ownership")],
)
def test_fixed_refusal_precedes_mixed_candidate_domain_failures(field, path):
    p = reverse_pack()
    scenario = p["scenarios"]["base"]
    scenario["operations"]["inputs"].update(
        initial_growth=".06", refundable_profit_share=".5", recovery_lag=3
    )
    scenario[field] = None
    actual = result(p)
    assert actual["status"] == "not_requested"
    assert actual["reasons"] == [{"code": "not_supplied", "path": path}]


def test_reverse_ignores_replaced_original_growth_and_preserves_forward_outputs():
    x = D(".013123")
    p = reverse_pack(str(independent_simple_price(x)))
    p["scenarios"]["base"]["operations"]["inputs"]["initial_growth"] = "-1"
    forward_only = copy.deepcopy(p)
    forward_only["scenarios"]["base"].pop("reverse")
    expected_forward = forward.evaluate(forward_only)["scenarios"]["base"]
    outputs = forward.evaluate(p)["scenarios"]["base"]
    assert {key: value for key, value in outputs.items() if key != "reverse"} == {
        key: value for key, value in expected_forward.items() if key != "reverse"
    }
    assert expected_forward["reverse"]["status"] == "not_requested"
    assert expected_forward["reverse"]["reasons"] == [
        {"code": "reverse_not_requested", "path": "reverse"}
    ]
    assert outputs["annual_fcff"]["status"] == "invalid_input"
    assert outputs["annual_fcff"]["reasons"] == [{"code": "invalid_growth", "path": ""}]
    assert outputs["reverse"]["status"] == "candidate_solution"
    assert abs(D(outputs["reverse"]["value"]["roots"][0]["initial_growth"]) - x) < D("1e-10")


def test_fixed_discount_and_built_rate_dependencies_keep_forward_ordering():
    p = reverse_pack()
    scenario = p["scenarios"]["base"]
    scenario["discount"] = None
    scenario["ownership"] = None
    assert result(p)["reasons"] == [{"code": "not_supplied", "path": "discount"}]

    p = reverse_pack()
    scenario = p["scenarios"]["base"]
    scenario["discount"]["inputs"] = {
        "risk_free_continuous": ".03",
        "beta": "1.2",
        "equity_risk_premium": ".05",
        "market_close": "20",
        "market_close_date": p["valuation_date"],
        "market_close_currency": "SEK",
        "debt": "25",
        "debt_date": p["valuation_date"],
        "debt_currency": "EUR",
        "debt_cost": ".06",
        "debt_cost_date": p["valuation_date"],
        "debt_cost_basis": "nominal_annual_effective_pre_tax",
        "shield_rate": ".2",
        "shield_rate_date": p["valuation_date"],
    }
    scenario["ownership"] = None
    assert result(p)["reasons"] == [{"code": "not_supplied", "path": "ownership"}]


def test_missing_and_invalid_fixed_projection_operands_remain_distinct():
    missing = reverse_pack()
    missing["scenarios"]["base"]["operations"]["inputs"].pop("margin_first")
    assert result(missing)["reasons"] == [{"code": "missing_operand", "path": "margin_first"}]
    invalid = reverse_pack()
    invalid["scenarios"]["base"]["operations"]["inputs"]["margin_first"] = "bad"
    assert result(invalid)["reasons"] == [{"code": "invalid_number", "path": "margin_first"}]


def test_direct_budget_survives_unsupported_reverse_response():
    from tests.estimated_fixtures import pack

    p = pack()
    p["scenarios"]["base"]["reverse"] = reverse_pack()["scenarios"]["base"]["reverse"]
    assert result(p)["status"] == "unsupported"
    assert forward.evaluate(p)["scenarios"]["base"]["per_share_quote"]["available"]


def test_sampled_absence_is_not_certified_no_solution_and_negative_residual_is_signed():
    p = reverse_pack(target="100000")
    r = result(p)
    assert r["status"] == "no_crossing_observed"
    assert not r["available"]
    assert not r["search_diagnostics"]["coverage"]["absence_demonstrated"]
    s = p["scenarios"]["base"]
    for item in s["bridge"]["items"]:
        if item["category"] == "nonlease_debt":
            item["inputs"]["amount"] = "-1000"
    r = result(p)
    assert all(
        D(sample["price"]) < 0 and not sample["tradable_price"]
        for sample in r["search_diagnostics"]["scan"]
    )


def test_algebraic_constant_response_demonstrates_no_solution_or_nonidentification():
    p = reverse_pack(target="4")
    inputs = p["scenarios"]["base"]["operations"]["inputs"]
    for key in (
        "margin_first",
        "margin_terminal",
        "da_first",
        "da_terminal",
        "cash_renewal_rate",
        "cash_growth_capital",
        "lease_renewal_rate",
        "lease_growth_capital",
        "wc_ratio",
    ):
        inputs[key] = "0"
    inputs["opening_wc"] = "0"
    r = result(p)
    assert r["status"] == "no_solution"
    assert r["value"]["coverage"]["absence_demonstrated"]
    p["scenarios"]["base"]["reverse"]["target"]["price"] = "3"
    r = result(p)
    assert r["status"] == "not_identifiable"
    assert r["value"]["roots"] == []
    assert r["value"]["coverage"]["exhaustive_roots"]


def test_reverse_only_limitation_preserves_forward():
    p = reverse_pack()
    p["limitations"].append(
        {
            "reason": "Synthetic unsupported funding threshold",
            "affected_outputs": ["reverse"],
            "status": "unsupported",
        }
    )
    assert result(p)["status"] == "unsupported"
    assert forward.evaluate(p)["scenarios"]["base"]["per_share_quote"]["available"]


def point_evaluator(function):
    """Controlled response functions exercise bounded search, never financial oracles."""

    def point(x):
        price = function(x)
        rows = [forward.outcome({"revenue": "100", "growth": "0", "fcff": "1"}) for _ in range(12)]
        return forward.outcome(
            {
                "price": price,
                "tradable_price": price > 0,
                "baseline_revenue": "100",
                "forward": {"annual_fcff": forward.outcome(rows)},
            }
        )

    return point


def test_search_discovers_multiplicity_without_claiming_completeness():
    r = search(
        point_evaluator(lambda x: 10 + (x - D(".013123")) * (x - D(".074321"))),
        D(0),
        D(".1"),
        D(10),
    )
    assert r["outcome"] == "multiple_solutions"
    assert len(r["roots"]) == len(r["candidate_brackets"]) == 2
    assert not r["coverage"]["exhaustive_roots"]
    assert all(root["growth_localization"] <= D("1e-10") for root in r["roots"])


def test_search_retains_match_regions_without_spurious_roots():
    r = search(point_evaluator(lambda x: D(10)), D(0), D(".1"), D(10))
    assert r["outcome"] == "match_region"
    assert r["roots"] == []
    assert r["match_regions"][0]["sample_count"] == 201
    assert not r["coverage"]["exhaustive_roots"]


def test_tangency_and_discontinuity_do_not_manufacture_solutions():
    tangent = search(point_evaluator(lambda x: 10 + (x - D(".013123")) ** 2), D(0), D(".1"), D(10))
    assert tangent["outcome"] in {"no_crossing_observed", "sampled_match", "match_region"}
    assert not tangent["coverage"]["absence_demonstrated"]
    discontinuity = search(
        point_evaluator(lambda x: D(9) if x < D(".013123") else D(11)), D(0), D(".1"), D(10)
    )
    assert discontinuity["outcome"] == "numerical_failure"
    assert discontinuity["roots"] == []
    assert discontinuity["candidate_brackets"][0]["iterations"] == 200


@pytest.mark.parametrize(
    "hole, status", [(".05", "unsupported_domain"), (".013125", "unsupported_domain")]
)
def test_domain_holes_are_not_skipped_in_scan_or_refinement(monkeypatch, hole, status):
    original = forward._scenario

    def hole_evaluator(pack, scenario, valuation, **kwargs):
        x = D(scenario["operations"]["inputs"]["initial_growth"])
        if x == D(hole):
            raise Unavailable("synthetic_funding_hole", str(x), "unsupported")
        return original(pack, scenario, valuation, **kwargs)

    monkeypatch.setattr(forward, "_scenario", hole_evaluator)
    p = reverse_pack(str(independent_simple_price(D(".013123"))))
    r = result(p)
    assert r["status"] == status
    assert r["value"] is None
    diagnostic = r["search_diagnostics"]
    assert len(diagnostic["scan"]) == 201
    assert diagnostic["failed_evaluations"][0]["reasons"][0]["code"] == "synthetic_funding_hole"
    assert forward.evaluate(p)["scenarios"]["base"]["per_share_quote"]["available"]


def test_real_tax_transition_hole_withholds_reverse_not_forward():
    p = reverse_pack()
    s = p["scenarios"]["base"]
    s["operations"]["inputs"].update(refundable_profit_share=".5", recovery_lag=3)
    outputs = forward.evaluate(p)["scenarios"]["base"]
    assert outputs["per_share_quote"]["available"]
    r = outputs["reverse"]
    assert r["status"] == "unsupported_domain"
    failed = r["search_diagnostics"]["failed_evaluations"]
    assert failed and failed[0]["reasons"][0]["code"] == "finite_effects_not_exhausted"
    assert len(r["search_diagnostics"]["scan"]) == 201
    assert r["search_diagnostics"]["roots"] == []
    different_original = copy.deepcopy(p)
    different_original["scenarios"]["base"]["operations"]["inputs"]["initial_growth"] = ".06"
    assert result(different_original) == r


def test_recovered_loss_has_no_new_refund_but_preserves_opening_recovery():
    p = reverse_pack()
    s = p["scenarios"]["base"]
    s["operations"]["inputs"].update(
        margin_first="-.1",
        refundable_profit_share=".5",
        opening_recoverable="1",
        initial_growth=".06",
    )
    just_forward = copy.deepcopy(p)
    just_forward["scenarios"]["base"].pop("reverse")
    s["reverse"]["target"]["price"] = forward.evaluate(just_forward)["scenarios"]["base"][
        "per_share_quote"
    ]["value"]["price_estimate"]
    r = result(p)
    assert r["status"] == "candidate_solution"
    row = r["value"]["roots"][0]["forward"]["annual_fcff"]["value"][0]["value"]
    assert D(row["ebit"]) < 0
    assert D(row["new_charge"]) == D(row["new_entitlement"]) == D(row["new_recovery"]) == 0
    assert D(row["opening_recovery"]) == 1
    assert D(row["operating_cash_tax"]) == -1


def test_malformed_requested_scenario_reports_reverse_invalid_input():
    p = reverse_pack()
    p["allocations"] = [{}]
    r = result(p)
    assert r["status"] == "invalid_input"
    assert r["reasons"][0]["code"] == "malformed_scenario_input"


def test_forward_override_also_recomputes_and_invalidates_requested_reverse():
    p = reverse_pack(str(independent_simple_price(D(".013123"))))
    scenarios = copy.deepcopy(p["scenarios"])
    scenarios["base"]["operations"]["inputs"]["wc_ratio"] = ".12"
    child = revise(
        p,
        version="synthetic-r2",
        author="Synthetic override",
        prepared_on="2024-03-16",
        reason="Working capital consumes more growth funding",
        changes={"scenarios": scenarios},
    )
    assert "reverse" in child["revision"]["affected_outputs"]
    assert "reverse" in child["revision"]["result_differences"]["scenarios"]["base"]
    assert child["parent"]["pack_version"] == p["pack_version"]
    assert (
        child["scenarios"]["base"]["reverse"]["domain"]
        == p["scenarios"]["base"]["reverse"]["domain"]
    )
    assert (
        result(child)["value"]["fixed_assumptions_identity"]
        != result(p)["value"]["fixed_assumptions_identity"]
    )


def test_cli_and_api_preserve_fixed_refusal_reason_parity(tmp_path):
    p = reverse_pack()
    scenario = p["scenarios"]["base"]
    scenario["operations"]["inputs"].update(
        initial_growth=".06", refundable_profit_share=".5", recovery_lag=3
    )
    scenario["fx"] = None
    outputs = forward.evaluate(p)
    api_target = export(tmp_path / "api", p, outputs)
    api_reverse = json.loads((api_target / "base-reverse.json").read_text())
    input_file = tmp_path / "pack.json"
    input_file.write_text(json.dumps(p))
    args = build_parser().parse_args(
        [
            "estimated-forward",
            "--input-json",
            str(input_file),
            "--exports-dir",
            str(tmp_path / "cli"),
        ]
    )
    assert args.func(args) == 0
    cli_target = next((tmp_path / "cli" / "estimated-forward").iterdir())
    cli_reverse = json.loads((cli_target / "base-reverse.json").read_text())
    assert cli_reverse["status"] == api_reverse["status"] == "not_requested"
    assert cli_reverse["reasons"] == api_reverse["reasons"] == [
        {"code": "not_supplied", "path": "quote_fx"}
    ]


def test_exports_and_overrides_retain_reverse_identity_and_recompute(tmp_path):
    p = reverse_pack(str(independent_simple_price(D(".013123"))))
    p["reviews"] = [{"scope": "reference_only", "status": "accepted", "reviewed_on": "2024-03-16"}]
    original = copy.deepcopy(p)
    scenarios = copy.deepcopy(p["scenarios"])
    scenarios["base"]["reverse"]["target"]["price"] = str(independent_simple_price(D(".025123")))
    child = revise(
        p,
        version="synthetic-r2",
        author="Synthetic override",
        prepared_on="2024-03-16",
        reason="Different dated synthetic target",
        changes={"scenarios": scenarios},
    )
    assert p == original
    assert child["reviews"] == []
    assert child["revision"]["affected_outputs"] == ["reverse", "conclusion"]
    differences = child["revision"]["result_differences"]["scenarios"]["base"]
    assert set(differences) == {"reverse"}
    outputs = forward.evaluate(child)
    target = export(tmp_path, child, outputs, parent_pack=p)
    detached = json.loads((target / "base-reverse.json").read_text())
    assert detached["identity"]["parent"] == child["parent"]
    assert detached["identity"]["human_status"] == "not_implementation_accepted"
    assert detached["value"]["target"]["close_date"] == "2024-02-28"
    assert (
        detached["identity"]["reverse_search_policy"]
        == detached["value"]["search_policy"]
        == "estimated-reverse-v1; linear-fade10; scan201; bisection200"
    )
    assert detached["identity"]["reverse_numerical_policy"] == (
        "growth A=1e-10; repricing A=1e-6,R=1e-8"
    )
    assert detached["value"]["tolerances"] == {
        "growth_absolute": "1E-10",
        "quote_absolute": "0.000001",
        "quote_relative": "1E-8",
    }
    assert "estimated_reverse.py" in " ".join(detached["execution"]["code_sources"])
    limit = csv.field_size_limit()
    try:
        csv.field_size_limit(10_000_000)
        with (target / "reverse.csv").open() as stream:
            rows = list(csv.DictReader(stream))
    finally:
        csv.field_size_limit(limit)
    assert sum(row["record_type"] == "scan" for row in rows) == 201
    root = next(json.loads(row["record"]) for row in rows if row["record_type"] == "root")
    assert abs(D(root["initial_growth"]) - D(".025123")) < D("1e-10")
    assert json.loads(rows[0]["context"])["parent_assumption_pack"] == p
    assert "candidate_solution" in (target / "report.md").read_text()
    assert export(tmp_path, child, outputs, parent_pack=p) == target
    input_file = tmp_path / "pack.json"
    input_file.write_text(json.dumps(p))
    override_file = tmp_path / "override.json"
    override_file.write_text(
        json.dumps(
            {
                "version": "synthetic-r2",
                "author": "Synthetic override",
                "prepared_on": "2024-03-16",
                "reason": "Different dated synthetic target",
                "changes": {"scenarios": scenarios},
            }
        )
    )
    args = build_parser().parse_args(
        [
            "estimated-forward",
            "--input-json",
            str(input_file),
            "--override-json",
            str(override_file),
            "--exports-dir",
            str(tmp_path / "cli"),
        ]
    )
    assert args.func(args) == 0
    exported = next((tmp_path / "cli" / "estimated-forward").iterdir())
    cli_reverse = json.loads((exported / "base-reverse.json").read_text())
    assert cli_reverse["value"] == detached["value"]
    assert cli_reverse["parent_assumption_pack"] == p
