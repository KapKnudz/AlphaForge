"""Executable synthetic acceptance of aggregate estimates and scoped reporting."""

import copy
import csv
import json
from decimal import Decimal as D

import pytest

from alphaforge.cli.main import build_parser
from alphaforge.core.valuation.estimated_fcff import evaluate
from alphaforge.core.valuation.estimated_inputs import LABEL, content_identity, revise
from tests.estimated_fixtures import block, pack, response


def base(p):
    return evaluate(p)["scenarios"]["base"]


def number(out, field):
    assert out["available"], out
    return D(out["value"][field])


def test_independent_constant_perpetuity_and_every_annual_leg():
    for p in (pack(), response()):
        result = base(p)
        assert number(result["operating_value"], "ev") == pytest.approx(D(140))
        assert number(result["common_equity"], "common_residual") == pytest.approx(D(155))
        assert number(result["per_share_reporting"], "price_estimate") == pytest.approx(D("15.5"))
        assert number(result["per_share_quote"], "price_estimate") == pytest.approx(D(31))
        for t, row in enumerate(result["annual_fcff"]["value"], 1):
            v = row["value"]
            assert D(v["ebit"]) == 20 and D(v["noncash_addbacks"]) == 5
            assert D(v["operating_cash_tax"]) == 4
            assert D(v["cash_investment"]) == 6 and D(v["lease_additions"]) == 1
            assert D(v["wc_open"]) == 10 and D(v["wc_close"]) == 10
            assert D(v["other_operating_cash"]) == 0 and D(v["fcff"]) == 14
            assert D(result["explicit_pv"]["value"]["annual_pv"][t - 1]) == pytest.approx(
                D(14) / D("1.1") ** t
            )
        assert result["annual_fcff"]["value"][3]["value"]["end"] == "2028-02-29"
        assert number(result["terminal"], "numerator") == 14
        assert result["reverse"]["status"] == "not_requested"


@pytest.mark.parametrize(
    "stage,survive,withheld",
    [
        ("operations", [], ["annual_fcff", "explicit_pv", "operating_value", "common_equity"]),
        ("discount", ["annual_fcff"], ["explicit_pv", "operating_value", "common_equity"]),
        ("terminal", ["annual_fcff", "explicit_pv"], ["operating_value", "common_equity"]),
        ("bridge", ["annual_fcff", "operating_value"], ["common_equity", "per_share_reporting"]),
        (
            "ownership",
            ["operating_value", "common_equity"],
            ["per_share_reporting", "per_share_quote"],
        ),
        ("fx", ["operating_value", "per_share_reporting"], ["per_share_quote"]),
    ],
)
def test_optional_missing_dependencies(stage, survive, withheld):
    p = pack()
    p["scenarios"]["base"][stage] = None
    r = base(p)
    for key in survive:
        assert r[key]["available"], (key, r[key])
    for key in withheld:
        assert not r[key]["available"], key
        assert r[key]["reasons"]


def test_operating_gap_retains_unaffected_years_and_tail_gap_preserves_explicit():
    p = pack()
    rows = p["scenarios"]["base"]["operations"]["rows"]
    rows[2]["operating_cash_tax"] = None
    r = base(p)
    assert not r["annual_fcff"]["available"]
    assert r["annual_fcff"]["value"][0]["available"]
    assert not r["annual_fcff"]["value"][2]["available"]
    rows[2]["operating_cash_tax"] = "4"
    rows[10]["operating_cash_tax"] = None
    r = base(p)
    assert r["explicit_pv"]["available"] and not r["operating_value"]["available"]


@pytest.mark.parametrize("bad", ["0", "-0.1"])
def test_invalid_rate_preserves_cash(bad):
    p = pack()
    p["scenarios"]["base"]["discount"]["inputs"]["wacc"] = bad
    r = base(p)
    assert r["annual_fcff"]["available"]
    assert r["wacc"]["status"] == "invalid_input"


def test_gordon_strict_pole_and_not_final_row_shortcut():
    p = pack()
    p["scenarios"]["base"]["terminal"]["growth"] = ".1"
    r = base(p)
    assert r["explicit_pv"]["available"] and not r["operating_value"]["available"]
    p["scenarios"]["base"]["terminal"]["growth"] = "0"
    p["scenarios"]["base"]["operations"]["rows"][10]["finite_effects"]["opening"] = "1"
    assert not base(p)["operating_value"]["available"]
    p = pack()
    p["scenarios"]["base"]["operations"]["rows"][11]["cash_investment"] = "7"
    assert not base(p)["operating_value"]["available"]


def test_loss_has_no_automatic_refund_but_opening_recovery_survives():
    p = response()
    inputs = p["scenarios"]["base"]["operations"]["inputs"]
    inputs.update(
        margin_first="-.1",
        margin_terminal=".2",
        opening_recoverable="7",
        opening_recovery_fractions=["1"],
        legacy_remaining="12",
        legacy_annual="5",
    )
    r = base(p)
    rows = r["annual_fcff"]["value"]
    assert D(rows[0]["value"]["new_charge"]) == 0
    assert D(rows[0]["value"]["new_entitlement"]) == 0
    assert D(rows[0]["value"]["opening_recovery"]) == 7
    assert D(rows[0]["value"]["operating_cash_tax"]) == -7
    assert [D(x["value"]["legacy_da"]) for x in rows[:4]] == [5, 5, 2, 0]


def test_tax_recovery_lag_topup_and_opening_once():
    p = response()
    inputs = p["scenarios"]["base"]["operations"]["inputs"]
    inputs.update(
        refundable_profit_share="1",
        opening_payable="8",
        opening_recoverable="6",
        opening_deferred="5",
        opening_payment_fractions=[".5", ".5"],
        opening_recovery_fractions=["0", "1"],
        same_year_payment=".5",
    )
    r = base(p)
    rows = [x["value"] for x in r["annual_fcff"]["value"]]
    # EBIT20: charge8 + topup1, entitlement6. Half cash now/next;
    # no new refund until t3. Finite opening payment8-refund6+deferred5=7.
    assert D(rows[0]["new_charge"]) == 9
    assert D(rows[0]["new_entitlement"]) == 6
    assert D(rows[0]["new_payment"]) == D("4.5")
    assert D(rows[0]["operating_cash_tax"]) == D("9.5")
    assert D(rows[1]["operating_cash_tax"]) == 8
    assert D(rows[2]["new_recovery"]) == 6
    assert sum(D(x["opening_tax"]) for x in rows) == 7
    assert D(rows[10]["opening_tax"]) == 0
    inputs["recovery_fraction"] = ".5"
    low = base(p)["annual_fcff"]["value"][0]["value"]
    assert D(low["topup_charge"]) == 0
    assert D(low["new_entitlement"]) == 3


def test_lag_three_breaks_terminal_transition_not_explicit_value():
    p = response()
    inputs = p["scenarios"]["base"]["operations"]["inputs"]
    inputs.update(
        initial_growth=".1", terminal_growth=".02", refundable_profit_share="1", recovery_lag=3
    )
    p["scenarios"]["base"]["terminal"]["growth"] = ".02"
    r = base(p)
    assert r["explicit_pv"]["available"] and not r["operating_value"]["available"]


def test_post_witness_recoveries_and_settlements_are_rejected():
    p = response()
    inputs = p["scenarios"]["base"]["operations"]["inputs"]
    inputs.update(refundable_profit_share="1", recovery_lag=12)
    assert base(p)["annual_fcff"]["status"] == "invalid_input"

    p = response()
    p["scenarios"]["base"]["operations"]["inputs"]["settlements"] = ["0"] * 10 + ["1"]
    assert base(p)["annual_fcff"]["status"] == "invalid_input"


def test_loss_gap_recovery_queue_withholds_terminal_after_equal_witnesses():
    p = response()
    inputs = p["scenarios"]["base"]["operations"]["inputs"]
    inputs.update(
        margin_first="-.1",
        margin_terminal=".2",
        refundable_profit_share="1",
        recovery_lag=10,
    )
    r = base(p)
    witnesses = [row["value"] for row in r["annual_fcff"]["value"][10:]]
    assert all(D(row["new_recovery"]) == 0 for row in witnesses)
    assert all(D(row["finite_effects"]["recovery_transition"]) == 1 for row in witnesses)
    assert r["explicit_pv"]["available"] and not r["terminal"]["available"]
    assert r["terminal"]["reasons"][0]["code"] == "finite_effects_not_exhausted"


def test_direct_annual_period_endpoints_must_be_supplied_together():
    for supplied, missing in (("start", "end"), ("end", "start")):
        p = pack()
        row = p["scenarios"]["base"]["operations"]["rows"][0]
        row[supplied] = "2024-03-01" if supplied == "start" else "2025-02-28"
        row.pop(missing, None)
        result = base(p)["annual_fcff"]
        assert not result["available"]
        assert result["value"][0]["reasons"] == [
            {"code": "annual_period_pair_required", "path": "1"}
        ]


def test_no_duplicate_lease_tax_compensation_claims():
    p = pack()
    p["scenarios"]["base"]["bridge"]["items"][0]["allocation_id"] = "operating_tax"
    r = base(p)
    assert r["operating_value"]["available"] and not r["common_equity"]["available"]
    p = pack()
    p["scenarios"]["base"]["operations"]["basis"] = "add_back_ordinary_compensation"
    assert base(p)["annual_fcff"]["status"] == "unsupported"


def test_every_registered_allocation_is_consumed_once_by_its_destination():
    for destination, unavailable in (
        ("operations", "annual_fcff"),
        ("bridge", "bridge"),
        ("rights", "common_equity"),
    ):
        p = pack()
        p["allocations"].append({"id": "orphan", "destination": destination})
        result = base(p)
        assert result[unavailable]["status"] == "invalid_input"

    p = pack()
    p["scenarios"]["base"]["operations"]["allocation_ids"].append("renewal")
    assert base(p)["annual_fcff"]["status"] == "invalid_input"

    p = pack()
    p["allocations"].append({"id": "documented-exclusion", "destination": "excluded_pair"})
    assert base(p)["common_equity"]["available"]


def test_negative_residual_is_not_tradable_or_floored():
    p = pack()
    item = next(
        x for x in p["scenarios"]["base"]["bridge"]["items"] if x["category"] == "legal_other"
    )
    item["inputs"]["amount"] = "-200"
    r = base(p)
    assert number(r["common_equity"], "common_residual") == pytest.approx(D(-45))
    assert number(r["per_share_reporting"], "signed_quotient") == pytest.approx(D("-4.5"))
    assert r["per_share_reporting"]["value"]["price_estimate"] is None
    assert not r["per_share_quote"]["value"]["tradable_price"]


def test_endogenous_calls_independently_known_atm_value():
    p = pack()
    p["valuation_date"] = "2023-02-28"
    p["allocations"].append({"id": "call", "destination": "rights"})
    s = p["scenarios"]["base"]
    for key in ("discount", "bridge", "ownership", "rights", "fx"):
        s[key]["valuation_date"] = p["valuation_date"]
    # One-year ATM call at S=100, sigma=.2, r=q=0:
    # C=100*(2*Phi(.1)-1)=7.965567455405796...
    call = D("7.965567455405796")
    target = D(1000) + call
    item = next(x for x in s["bridge"]["items"] if x["category"] == "accessible_cash")
    item["inputs"]["amount"] = str(target - 140 + 5)
    s["rights"]["items"] = [
        block(
            id="synthetic-call",
            allocation_id="call",
            count="1000000",
            strike="100",
            volatility=".2",
            risk_free_continuous="0",
            dividend_yield="0",
            expiry="2024-02-28",
        )
    ]
    r = base(p)
    assert float(number(r["per_share_reporting"], "price_estimate")) == pytest.approx(
        100, abs=1e-10
    )
    assert float(number(r["common_equity"], "rights_value")) == pytest.approx(
        float(call), abs=1e-10
    )
    c = r["common_equity"]["value"]
    assert abs(D(c["allocation_residual"])) <= D("1e-24")
    s["bridge"]["items"][0]["inputs"]["amount"] = "2000"
    assert number(base(p)["common_equity"], "rights_value") > call


def test_parented_override_recalculates_and_invalidates_review_without_mutation():
    p = pack()
    p["reviews"] = [
        {"reviewer": "Synthetic human", "scope": "reference_only", "reviewed_on": "2024-03-16"}
    ]
    original = copy.deepcopy(p)
    scenarios = copy.deepcopy(p["scenarios"])
    scenarios["base"]["fx"]["quote_per_reporting"] = "3"
    child = revise(
        p,
        version="synthetic-r2",
        author="Synthetic override",
        prepared_on="2024-03-17",
        reason="Synthetic FX alternative",
        changes={"scenarios": scenarios},
    )
    assert p == original
    assert child["parent"]["content_identity"] == content_identity(p)
    assert child["reviews"] == [] and child["revision"]["invalidated_reviews"] == p["reviews"]
    assert child["revision"]["changed_inputs"][0]["path"].endswith("quote_per_reporting")
    assert child["revision"]["affected_outputs"] == [
        "common_equity",
        "explicit_pv",
        "operating_value",
        "per_share_quote",
        "per_share_reporting",
        "conclusion",
    ]
    assert number(base(child)["per_share_quote"], "price_estimate") == pytest.approx(D("46.5"))
    assert number(base(p)["per_share_quote"], "price_estimate") == pytest.approx(D(31))


def test_source_anchor_and_limitation_scope():
    p = pack()
    p["limitations"].append(
        {
            "reason": "Synthetic unestimable legal claim",
            "status": "insufficient_evidence",
            "affected_outputs": ["common_equity"],
        }
    )
    r = base(p)
    assert r["operating_value"]["available"] and not r["common_equity"]["available"]
    p["limitations"][0]["affected_outputs"] = ["reverse"]
    assert base(p)["per_share_quote"]["available"]
    p["records"]["budget"]["anchors"][0]["anchor"] = "not-owned"
    assert not base(p)["annual_fcff"]["available"]


def test_scenario_disagreement_and_scaling():
    p = pack()
    p["scenarios"]["downside"] = copy.deepcopy(p["scenarios"]["base"])
    for row in p["scenarios"]["downside"]["operations"]["rows"]:
        row["ebit"] = "10"
    r = evaluate(p)
    assert "material_scenario_disagreement" in r["firm_investment_conclusion"]["reasons"]
    p = pack()
    p["scenarios"]["base"]["ownership"]["basic_shares"] = "20000000"
    assert number(base(p)["per_share_reporting"], "price_estimate") == pytest.approx(D("7.75"))


def test_equal_zero_scenarios_are_not_materially_different():
    p = pack()
    bridge = p["scenarios"]["base"]["bridge"]["items"]
    next(x for x in bridge if x["category"] == "accessible_cash")["inputs"]["amount"] = "0"
    next(x for x in bridge if x["category"] == "legal_other")["inputs"]["amount"] = "-135"
    p["scenarios"]["same"] = copy.deepcopy(p["scenarios"]["base"])
    result = evaluate(p)
    assert number(result["scenarios"]["base"]["common_equity"], "common_residual") == 0
    assert "material_scenario_disagreement" not in result["firm_investment_conclusion"]["reasons"]


def test_real_cli_detached_exports_are_self_describing_and_write_once(tmp_path, capsys):
    input_path = tmp_path / "synthetic.json"
    input_path.write_text(json.dumps(pack()))
    parser = build_parser()
    args = parser.parse_args(
        ["estimated-forward", "--input-json", str(input_path), "--exports-dir", str(tmp_path)]
    )
    assert args.func(args) == 0
    directory = json.loads(capsys.readouterr().out)["export_directory"]
    from pathlib import Path

    root = Path(directory)
    detached = json.loads((root / "base-per_share_quote.json").read_text())
    assert detached["identity"]["label"] == LABEL
    assert detached["assumption_pack"] == pack()
    assert detached["identity"]["human_status"] == "not_implementation_accepted"
    csv_rows = list(csv.DictReader((root / "annual.csv").open()))
    assert len(csv_rows) == 12
    assert json.loads(csv_rows[0]["context"])["assumption_pack"]["pack_version"] == "synthetic-r1"
    before = {x.name: x.read_bytes() for x in root.iterdir()}
    assert args.func(args) == 0
    assert before == {x.name: x.read_bytes() for x in root.iterdir()}


def test_supported_wacc_build_effective_compounding_and_shield_once():
    p = pack()
    inputs = {
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
    p["scenarios"]["base"]["discount"]["inputs"] = inputs
    r = base(p)
    # Market E=100m and D=25m, independently of the modeled enterprise value.
    expected = D(".8") * (D(".03").exp() - 1 + D(".06")) + D(".2") * D(".06") * D(".8")
    assert number(r["wacc"], "market_equity") == 100
    assert number(r["wacc"], "debt_weight") == D(".2")
    assert number(r["wacc"], "wacc") == pytest.approx(expected)
    assert all(D(x["value"]["operating_cash_tax"]) == 4 for x in r["annual_fcff"]["value"])
    inputs["basic_shares"] = "5000000"
    assert base(p)["wacc"]["status"] == "invalid_input"
    inputs.pop("basic_shares")
    p["scenarios"]["base"]["ownership"]["basic_shares"] = "20000000"
    shared = base(p)
    assert number(shared["wacc"], "market_equity") == 200
    assert D(shared["basic_shares"]["value"]) == 20000000


def test_built_wacc_revision_dependencies_cover_downstream_outputs():
    p = pack()
    p["scenarios"]["base"]["discount"]["inputs"] = {
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
    scenarios = copy.deepcopy(p["scenarios"])
    scenarios["base"]["ownership"]["basic_shares"] = "20000000"
    child = revise(
        p,
        version="synthetic-built-r2",
        author="Synthetic override",
        prepared_on="2024-03-17",
        reason="Synthetic ownership alternative",
        changes={"scenarios": scenarios},
    )
    assert child["revision"]["affected_outputs"] == [
        "common_equity",
        "explicit_pv",
        "operating_value",
        "per_share_quote",
        "per_share_reporting",
        "conclusion",
    ]
    differences = child["revision"]["result_differences"]["base"]
    assert {"wacc", "explicit_pv", "operating_value", "common_equity"} <= set(differences)


def test_wacc_and_ownership_bases_are_dated_and_shared():
    p = pack()
    p["scenarios"]["base"]["discount"]["valuation_date"] = "2024-02-28"
    assert base(p)["wacc"]["status"] == "invalid_input"
    p = pack()
    p["scenarios"]["base"]["ownership"]["valuation_date"] = "2024-02-28"
    r = base(p)
    assert r["wacc"]["available"] and r["basic_shares"]["status"] == "invalid_input"


def test_foreign_claims_and_rights_fx_are_scoped_and_dated():
    p = pack()
    s = p["scenarios"]["base"]
    claim = next(x for x in s["bridge"]["items"] if x["category"] == "legal_other")
    claim.update(
        method="foreign_pv",
        inputs={
            "amount": "-12",
            "currency": "USD",
            "reporting_currency": "EUR",
            "foreign_per_reporting": "1.2",
            "fx_date": p["valuation_date"],
        },
    )
    assert number(base(p)["common_equity"], "common_residual") == pytest.approx(D(145))
    claim["inputs"]["foreign_per_reporting"] = None
    r = base(p)
    assert r["operating_value"]["available"] and not r["common_equity"]["available"]
    claim["inputs"]["foreign_per_reporting"] = "1.2"
    p["allocations"].append({"id": "call", "destination": "rights"})
    s["rights"].update(
        currency="SEK",
        reporting_currency="EUR",
        fx_date=p["valuation_date"],
        quote_per_reporting="2",
    )
    s["rights"]["items"] = [
        block(
            id="synthetic-call",
            allocation_id="call",
            count="1000000",
            strike="20",
            volatility=".2",
            risk_free_continuous="0",
            dividend_yield="0",
            expiry="2025-02-28",
        )
    ]
    with_fx = base(p)
    s["fx"] = None
    no_quote = base(p)
    assert no_quote["common_equity"] == with_fx["common_equity"]
    assert (
        no_quote["per_share_reporting"]["available"]
        and not no_quote["per_share_quote"]["available"]
    )
    s["rights"]["quote_per_reporting"] = None
    assert not base(p)["common_equity"]["available"]


def test_fx_pairs_reject_same_currency_conversion_and_missing_pair_side():
    p = pack()
    p["quote_currency"] = "EUR"
    fx = p["scenarios"]["base"]["fx"]
    fx.update(quote_currency="EUR", quote_per_reporting="2")
    assert base(p)["per_share_quote"]["status"] == "invalid_input"
    fx["quote_per_reporting"] = "1"
    assert number(base(p)["per_share_quote"], "price_estimate") == D("15.5")

    p = pack()
    del p["scenarios"]["base"]["fx"]["reporting_currency"]
    assert base(p)["per_share_quote"]["status"] == "invalid_input"

    p = pack()
    p["scenarios"]["base"]["rights"].update(
        reporting_currency="EUR",
        fx_date=p["valuation_date"],
        quote_per_reporting="2",
    )
    assert base(p)["common_equity"]["status"] == "invalid_input"


def test_bridge_category_controls_method_and_sign():
    p = pack()
    debt = next(
        x for x in p["scenarios"]["base"]["bridge"]["items"] if x["category"] == "nonlease_debt"
    )
    debt["inputs"]["amount"] = "10"
    assert base(p)["common_equity"]["status"] == "invalid_input"
    debt.update(
        method="foreign_pv",
        inputs={
            "amount": "10",
            "currency": "USD",
            "reporting_currency": "EUR",
            "foreign_per_reporting": "1.2",
            "fx_date": p["valuation_date"],
        },
    )
    assert base(p)["common_equity"]["status"] == "invalid_input"

    p = pack()
    asset = next(
        x
        for x in p["scenarios"]["base"]["bridge"]["items"]
        if x["category"] == "nonoperating_assets"
    )
    asset.update(
        method="earnings_linked_pv",
        inputs={
            "recurring_baseline_ebit": "20",
            "current_pv_anchor": "1",
            "later_pv_anchor": "0",
        },
    )
    assert base(p)["common_equity"]["status"] == "invalid_input"


@pytest.mark.parametrize("value", ["0", "0.0000000000000000000001", "-1"])
def test_no_sign_tolerance_on_shares(value):
    p = pack()
    p["scenarios"]["base"]["ownership"]["basic_shares"] = value
    r = base(p)
    assert r["common_equity"]["available"] and not r["per_share_reporting"]["available"]


def test_selected_zero_fcff_and_negative_cash_are_not_missing():
    p = response()
    inputs = p["scenarios"]["base"]["operations"]["inputs"]
    inputs["cash_renewal_rate"] = ".2"
    r = base(p)
    assert all(D(x["value"]["fcff"]) == 0 for x in r["annual_fcff"]["value"])
    assert number(r["operating_value"], "ev") == 0
    inputs["initial_cash_diagnostic"] = "-10"
    inputs["cash_renewal_rate"] = ".21"
    r = base(p)
    assert number(r["operating_value"], "ev") == pytest.approx(D(-10))
    assert all(
        D(x["value"]["undistributed_cash_diagnostic"]) < 0 for x in r["annual_fcff"]["value"]
    )


def test_publication_upper_bound_and_unknown_source_scope():
    p = pack()
    source = p["sources"][0]
    source["published_on"] = None
    assert not base(p)["annual_fcff"]["available"]
    source.update(
        public_by="2024-03-10",
        publication_limitation="Exact first posting unknown; public by retained capture",
    )
    assert base(p)["annual_fcff"]["available"]
    source["public_by"] = "2024-03-17"
    assert base(p)["annual_fcff"]["status"] == "invalid_input"


def test_override_cli_retains_both_versions_and_actual_differences(tmp_path, capsys):
    p = pack()
    (tmp_path / "pack.json").write_text(json.dumps(p))
    scenarios = copy.deepcopy(p["scenarios"])
    scenarios["base"]["fx"]["quote_per_reporting"] = "3"
    change = {
        "version": "synthetic-r2",
        "author": "Synthetic override",
        "prepared_on": "2024-03-17",
        "reason": "Alternative FX",
        "changes": {"scenarios": scenarios},
    }
    (tmp_path / "override.json").write_text(json.dumps(change))
    args = build_parser().parse_args(
        [
            "estimated-forward",
            "--input-json",
            str(tmp_path / "pack.json"),
            "--override-json",
            str(tmp_path / "override.json"),
            "--exports-dir",
            str(tmp_path),
        ]
    )
    assert args.func(args) == 0
    from pathlib import Path

    directory = Path(json.loads(capsys.readouterr().out)["export_directory"])
    exported = json.loads((directory / "outputs.json").read_text())
    assert exported["parent_assumption_pack"] == p
    revision = exported["assumption_pack"]["revision"]
    assert revision["affected_outputs"] == [
        "common_equity",
        "explicit_pv",
        "operating_value",
        "per_share_quote",
        "per_share_reporting",
        "conclusion",
    ]
    assert set(revision["result_differences"]["base"]) == {"per_share_quote"}
    assert json.loads((tmp_path / "pack.json").read_text()) == p


def test_casefolded_scenario_export_names_are_refused(tmp_path, capsys):
    p = pack()
    p["scenarios"]["BASE"] = copy.deepcopy(p["scenarios"]["base"])
    input_path = tmp_path / "casefold-collision.json"
    input_path.write_text(json.dumps(p))
    args = build_parser().parse_args(
        ["estimated-forward", "--input-json", str(input_path), "--exports-dir", str(tmp_path)]
    )
    assert args.func(args) == 1
    refusal = json.loads(capsys.readouterr().err)
    assert refusal["status"] == "estimated_research_refused"
    assert "duplicate_scenario_export_id" in refusal["reason"]
    assert not (tmp_path / "estimated-forward").exists()


def test_malformed_mapping_shapes_have_structured_refusals(tmp_path, capsys):
    malformed_records = pack()
    malformed_records["records"] = []
    for index, payload in enumerate(([], malformed_records)):
        input_path = tmp_path / f"malformed-{index}.json"
        input_path.write_text(json.dumps(payload))
        args = build_parser().parse_args(
            ["estimated-forward", "--input-json", str(input_path), "--exports-dir", str(tmp_path)]
        )
        assert args.func(args) == 1
        refusal = json.loads(capsys.readouterr().err)
        assert refusal["status"] == "estimated_research_refused"

    malformed_scenario = pack()
    malformed_scenario["scenarios"]["base"] = []
    scenario = evaluate(malformed_scenario)["scenarios"]["base"]
    assert scenario["annual_fcff"]["status"] == "invalid_input"

    malformed_projection = response()
    malformed_projection["scenarios"]["base"]["operations"]["inputs"] = []
    assert base(malformed_projection)["annual_fcff"]["status"] == "invalid_input"


def test_unavailable_export_keeps_full_context_and_propagated_reasons(tmp_path, capsys):
    p = pack()
    p["scenarios"]["base"]["discount"] = None
    (tmp_path / "pack.json").write_text(json.dumps(p))
    args = build_parser().parse_args(
        [
            "estimated-forward",
            "--input-json",
            str(tmp_path / "pack.json"),
            "--exports-dir",
            str(tmp_path),
        ]
    )
    assert args.func(args) == 0
    from pathlib import Path

    directory = Path(json.loads(capsys.readouterr().out)["export_directory"])
    output = json.loads((directory / "base-operating_value.json").read_text())
    assert output["value"] is None and output["reasons"]
    assert output["identity"]["label"] == LABEL and output["assumption_pack"] == p


def test_precise_cutoff_does_not_treat_later_same_day_as_eligible():
    p = pack()
    source = p["sources"][0]
    source["published_on"] = "2024-03-15"
    assert not base(p)["annual_fcff"]["available"]
    source["published_at"] = "2024-03-15T13:00:00Z"
    assert base(p)["annual_fcff"]["status"] == "invalid_input"
    source["published_at"] = "2024-03-15T11:00:00Z"
    assert base(p)["annual_fcff"]["available"]


def test_malformed_optional_bridge_does_not_poison_operations():
    p = pack()
    p["scenarios"]["base"]["bridge"]["items"] = "not-an-array"
    r = base(p)
    assert r["operating_value"]["available"]
    assert r["common_equity"]["status"] == "invalid_input"


def test_reference_comparison_preserves_component_tolerances_and_exact_metadata():
    from tools.check_estimated_forward_acceptance import AcceptanceError, compare_outputs

    expected = {
        "identity": {"valuation_date": "2024-02-29", "currency": "EUR"},
        "scenarios": {
            "base": {
                "annual_fcff": {
                    "value": [
                        {
                            "value": {
                                "fcff": "14.000000000",
                                "record_ids": ["1"],
                                "extension_code": "1",
                            }
                        }
                    ],
                },
                "bridge": {
                    "value": [{"id": "1", "allocation_id": "1", "amount": "5.00000000"}]
                },
                "operating_value": {"value": {"ev": "140.00000000"}},
                "per_share_reporting": {"value": {"price_estimate": "15.50000000"}},
                "basic_shares": {"value": "10000000"},
            }
        },
    }
    actual = copy.deepcopy(expected)
    actual["scenarios"]["base"]["annual_fcff"]["value"][0]["value"]["fcff"] = (
        "14.000000019"
    )
    actual["scenarios"]["base"]["bridge"]["value"][0]["amount"] = "5.00000009"
    actual["scenarios"]["base"]["operating_value"]["value"]["ev"] = "140.00000009"
    actual["scenarios"]["base"]["per_share_reporting"]["value"]["price_estimate"] = (
        "15.500000009"
    )
    compare_outputs(actual, expected)

    outside = copy.deepcopy(expected)
    outside["scenarios"]["base"]["annual_fcff"]["value"][0]["value"]["fcff"] = (
        "14.000000021"
    )
    with pytest.raises(AcceptanceError, match="annual comparison mismatch"):
        compare_outputs(outside, expected)

    for mutate in (
        lambda value: value["scenarios"]["base"]["annual_fcff"]["value"][0]["value"].update(
            record_ids=["1.000000001"]
        ),
        lambda value: value["scenarios"]["base"]["annual_fcff"]["value"][0]["value"].update(
            extension_code="1.000000001"
        ),
        lambda value: value["scenarios"]["base"]["bridge"]["value"][0].update(
            allocation_id="1.000000001"
        ),
    ):
        semantic = copy.deepcopy(expected)
        mutate(semantic)
        with pytest.raises(AcceptanceError, match="exact comparison mismatch"):
            compare_outputs(semantic, expected)

    actual["identity"]["valuation_date"] = "2024-03-01"
    with pytest.raises(AcceptanceError, match="exact comparison mismatch"):
        compare_outputs(actual, expected)
    actual = copy.deepcopy(expected)
    actual["scenarios"]["base"]["basic_shares"]["value"] = "10000001"
    with pytest.raises(AcceptanceError, match="exact comparison mismatch"):
        compare_outputs(actual, expected)
