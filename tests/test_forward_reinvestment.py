"""Independent calculator locks and public qualified-calibration contracts."""

import json
import sqlite3
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from decimal import localcontext

import pytest
from dcf_calibration_fixtures import synthetic_calibration_fixture, synthetic_record
from test_method_date_growth_selection import (
    CUTOFF,
    annual,
    explicit_mature_dcf_route,
    packet,
    rank_exports,
    setup,
)

from alphaforge.cli.ranking_loader import load_results_for_company
from alphaforge.core.valuation.dcf_contract import FIXED_DEFAULT_ASSUMPTION_POLICY
from alphaforge.core.valuation.reinvestment import (
    ECONOMIC_CONVENTION,
    calibration_identity,
    calibration_json,
    qualify_calibration,
)
from alphaforge.core.valuation.reverse_dcf import (
    DcfAssumptions,
    ReverseDcfEngine,
    ReverseDcfInputs,
    UnsupportedEconomicPolicy,
    forward_investment,
)
from alphaforge.core.valuation.solve_eligibility import SOLVE_AXIS_REGISTRY, solve_axis_metadata
from alphaforge.db.numerical_runs import ReplayRefusal, canonical, replay_run, rules_bundle
from alphaforge.db.reinvestment import append_reinvestment_calibration
from alphaforge.db.repositories import upsert_prices, upsert_stock_splits


def hand_inputs(q=0.1, terminal=0.04, years=2):
    return ReverseDcfInputs(
        10,
        10,
        100,
        5,
        DcfAssumptions(
            years,
            0.04,
            1,
            0,
            0.1,
            terminal,
            reinvestment_return=q,
            revenue_growth_fade_to=terminal,
            ebit_margin_start=1,
            economic_convention=ECONOMIC_CONVENTION,
            calibration_identity="synthetic-hand-lock",
        ),
        eligibility_context_identity="synthetic-company:packet",
    )


def policy_solve_inputs():
    inputs = hand_inputs(years=5, terminal=0.02)
    return replace(inputs, assumptions=replace(inputs.assumptions, tax_rate=0.21))


def verified_growth_metadata(inputs):
    return solve_axis_metadata(
        "revenue_growth",
        assumptions=inputs.assumptions,
        assumption_provenance={
            name: {
                "origin": origin,
                "source": (
                    FIXED_DEFAULT_ASSUMPTION_POLICY[name][1]
                    if origin == "fixed_default"
                    else "synthetic test assumption"
                ),
                "evidence_references": (
                    ()
                    if origin == "fixed_default"
                    else ({"source_id": f"synthetic:{name}", "anchor": name},)
                ),
                "limitations": ("synthetic test assumption",),
            }
            for name, origin in {
                "projection_years": "fixed_default",
                "revenue_growth": "company_history",
                "ebit_margin": "report_evidence",
                "tax_rate": "fixed_default",
                "discount_rate": "market_evidence",
                "terminal_growth": "fixed_default",
                "net_reinvestment_rate": "fixed_default",
                "reinvestment_return": "qualified_calibration",
                "revenue_growth_fade_to": "fixed_default",
                "ebit_margin_start": "report_evidence",
                "economic_convention": "fixed_default",
                "calibration_identity": "qualified_calibration",
            }.items()
        },
        inputs=inputs,
        prerequisite_evidence={
            name: {
                "status": "met",
                "evidence_references": ()
                if name == "fixed_assumptions_with_provenance"
                else ({"source_id": f"evidence:{name}"},),
            }
            for name in SOLVE_AXIS_REGISTRY["revenue_growth"].evidence_prerequisites
        },
    )


@pytest.mark.parametrize("q,investment,fcff", [(0.2, 20, 80), (0.1, 40, 60)])
def test_hand_growth_spending(q, investment, fcff):
    assert forward_investment(100, 104, q) == investment
    assert 100 - investment == fcff


def test_growth_spending_is_not_capped_or_a_revenue_proxy():
    assert forward_investment(100, 130, 0.2) == 150
    assert forward_investment(10, 22, 0.2) == 60  # capital-driven hand case, not margin admission
    inputs = hand_inputs(q=0.01, years=3)
    with pytest.raises(UnsupportedEconomicPolicy, match="unsupported_financing"):
        ReverseDcfEngine().value(inputs)


@pytest.mark.parametrize(
    "q,first_investment,enterprise", [(0.1, 41.6, 1040), (0.2, 20.8, 1058.90909090909)]
)
def test_independently_signed_forward_terminal_bridge(q, first_investment, enterprise):
    value = ReverseDcfEngine().value(hand_inputs(q))
    first, last = value.projected_cash_flows
    assert first.reinvestment == pytest.approx(first_investment)
    assert first.fcff == pytest.approx(104 - first_investment)
    assert last.reinvestment == pytest.approx(43.264)
    assert last.fcff == pytest.approx(64.896)
    terminal = value.terminal_cash_flow
    assert terminal.nopat == pytest.approx(112.4864)
    assert terminal.reinvestment == pytest.approx(44.99456)
    assert terminal.fcff == pytest.approx(67.49184)
    assert last.incremental_return == terminal.incremental_return == 0.1
    assert value.terminal_value == pytest.approx(1124.864)
    assert value.enterprise_value == pytest.approx(enterprise)
    assert value.value_per_share == pytest.approx((enterprise - 5) / 10)


def test_five_year_independent_decimal_calculator_all_rows():
    inputs = replace(hand_inputs(q=0.2, years=5), current_revenue=123)
    inputs = replace(
        inputs,
        assumptions=replace(
            inputs.assumptions,
            revenue_growth=0.08,
            ebit_margin=0.2,
            ebit_margin_start=0.2,
            tax_rate=0.21,
        ),
    )
    value = ReverseDcfEngine().value(inputs)
    with localcontext() as ctx:
        ctx.prec = 45
        revenue = D(123)
        operating = []
        for index in range(7):
            growth = D(".08") + (D(".04") - D(".08")) * D(index) / 4 if index < 5 else D(".04")
            revenue *= 1 + growth
            operating.append((revenue, revenue * D(".2") * D(".79")))
        pv = D(0)
        for index, actual in enumerate((*value.projected_cash_flows, value.terminal_cash_flow)):
            revenue, profit = operating[index]
            future_profit = operating[index + 1][1]
            q = D(".2") + (D(".1") - D(".2")) * D(index) / 4 if index < 5 else D(".1")
            investment = (future_profit - profit) / q
            fcff = profit - investment
            for field, expected in {
                "revenue": revenue,
                "nopat": profit,
                "next_nopat": future_profit,
                "incremental_return": q,
                "reinvestment": investment,
                "fcff": fcff,
            }.items():
                assert getattr(actual, field) == pytest.approx(float(expected), abs=1e-10)
            if index < 5:
                pv += fcff / D("1.1") ** (index + 1)
            else:
                terminal_value = fcff / D(".06")
        enterprise = pv + terminal_value / D("1.1") ** 5
        assert value.enterprise_value == pytest.approx(float(enterprise), abs=1e-10)
        json.dumps(value.__dict__, default=lambda x: x.__dict__, allow_nan=False)


@pytest.mark.parametrize("terminal", [0, 0.01, 0.04, 0.09])
def test_terminal_growth_neutrality_at_hurdle_for_fixed_first_period_profit(terminal):
    # Forward-funding telescope: with all q=r, EV=N1/r, not g-dependent.
    value = ReverseDcfEngine().value(hand_inputs(terminal=terminal))
    assert value.enterprise_value == pytest.approx(1040)
    assert value.terminal_value == pytest.approx(value.terminal_cash_flow.nopat / 0.1)


def test_linked_transition_sensitivity_is_not_whole_forecast_neutrality():
    engine = ReverseDcfEngine()
    low = engine.value(hand_inputs(q=0.2, terminal=0.01))
    high = engine.value(hand_inputs(q=0.2, terminal=0.04))
    assert low.enterprise_value == pytest.approx(1044.72727272727)
    assert high.enterprise_value == pytest.approx(1058.90909090909)
    assert high.enterprise_value > low.enterprise_value
    for value in (low, high):
        assert value.terminal_value == pytest.approx(value.terminal_cash_flow.nopat / 0.1)


@pytest.mark.parametrize(
    "axis,reason",
    [("ebit_margin", "unavailable_constant_margin_only"), ("terminal_growth", "not_identifiable")],
)
def test_engine_never_solves_excluded_axes_even_at_base_price(axis, reason):
    engine = ReverseDcfEngine()
    inputs = hand_inputs()
    inputs = replace(inputs, current_price=engine.value(inputs).value_per_share)
    for operation in (engine.solve, engine.diagnose_solve_range):
        with pytest.raises(UnsupportedEconomicPolicy, match=reason):
            operation(inputs, axis, 0, 0.04)


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"terminal_growth": 0.1, "revenue_growth_fade_to": 0.1}, "exceed terminal_growth"),
        ({"terminal_growth": 0.11, "revenue_growth_fade_to": 0.11}, "exceed terminal_growth"),
        ({"discount_rate": 0}, "positive"),
        ({"discount_rate": float("nan")}, "finite"),
        ({"reinvestment_return": float("inf")}, "finite"),
        ({"reinvestment_return": 0}, "positive"),
        ({"reinvestment_return": -0.1}, "positive"),
        ({"ebit_margin": 0}, "nonpositive_nopat"),
        ({"ebit_margin_start": -0.1}, "varying_margin"),
        ({"ebit_margin_start": 0.9}, "varying_margin"),
        ({"revenue_growth": -0.01}, "unsupported_capital_release"),
        (
            {"terminal_growth": -0.01, "revenue_growth_fade_to": -0.01},
            "unsupported_capital_release",
        ),
    ],
)
def test_forward_economic_boundaries(changes, reason):
    inputs = hand_inputs()
    with pytest.raises(ValueError, match=reason):
        ReverseDcfEngine().value(
            replace(inputs, assumptions=replace(inputs.assumptions, **changes))
        )


def test_admissible_growth_range_root_and_no_solution_are_independent_of_legacy_caps():
    engine = ReverseDcfEngine()
    inputs = policy_solve_inputs()
    lower_price = engine.value(
        replace(inputs, assumptions=replace(inputs.assumptions, revenue_growth=0.0))
    ).value_per_share
    upper_price = engine.value(
        replace(inputs, assumptions=replace(inputs.assumptions, revenue_growth=0.08))
    ).value_per_share
    inputs = replace(inputs, current_price=engine.value(inputs).value_per_share)
    result = engine.solve(
        inputs,
        "revenue_growth",
        0,
        0.08,
        eligibility=verified_growth_metadata(inputs),
    )
    assert result.implied_assumption == pytest.approx(0.04)
    assert result.modeled_price == pytest.approx(inputs.current_price)
    no_match_inputs = replace(inputs, current_price=upper_price + 100)
    diagnostics, brackets, _ = engine.diagnose_solve_range(
        no_match_inputs,
        "revenue_growth",
        0,
        0.08,
        eligibility=verified_growth_metadata(no_match_inputs),
    )
    assert not brackets
    assert diagnostics["lower_endpoint_price"] == pytest.approx(lower_price)
    assert diagnostics["upper_endpoint_price"] == pytest.approx(upper_price)
    assert diagnostics["nearest_boundary_gap"] == pytest.approx(100)
    assert diagnostics["nearest_boundary_gap_pct_target"] == pytest.approx(
        100 / no_match_inputs.current_price * 100
    )
    assert "sampled range only" in diagnostics["range_qualification"]
    with pytest.raises(ValueError, match="not bracketed"):
        engine.solve(
            no_match_inputs,
            "revenue_growth",
            0,
            0.08,
            eligibility=verified_growth_metadata(no_match_inputs),
        )


def test_nonfinite_projection_and_investment_never_escape():
    with pytest.raises(ValueError, match="finite"):
        forward_investment(1, 1e308, 1e-308)
    with pytest.raises(ValueError, match="finite"):
        ReverseDcfEngine().value(replace(hand_inputs(), current_revenue=1.79e308))


@pytest.mark.parametrize("q", [0.3, 1e16])
def test_last_funding_endpoint_is_exact_even_with_return_cancellation(q):
    inputs = hand_inputs(q=q, years=5)
    inputs = replace(
        inputs, assumptions=replace(inputs.assumptions, discount_rate=0.115, revenue_growth=0.09)
    )
    value = ReverseDcfEngine().value(inputs)
    assert value.projected_cash_flows[-1].incremental_return == 0.115
    assert value.terminal_cash_flow.incremental_return == 0.115
    assert value.projected_cash_flows[-1].revenue_growth == 0.04


def test_one_year_horizon_converges_in_only_funding_interval():
    inputs = hand_inputs(q=0.2, years=1)
    inputs = replace(inputs, assumptions=replace(inputs.assumptions, revenue_growth=0.08))
    value = ReverseDcfEngine().value(inputs)
    assert value.projected_cash_flows[0].revenue_growth == 0.04
    assert value.projected_cash_flows[0].incremental_return == 0.1
    assert value.terminal_cash_flow.incremental_return == 0.1


@pytest.mark.parametrize(
    "key,value",
    [
        ("future_return_provenance", "observed_future_roic"),
        ("capital_basis", "issuer_roce"),
        ("capital_begin", 0),
        ("capital_end", -1),
        ("normalized_ebit", -1),
        ("future_return", 0),
        ("future_return", float("nan")),
        ("future_return", 0.5),
        ("currency", "EUR"),
        ("original_currency", "EUR"),
        ("conversion_mode", "sek"),
        ("earnings_basis", "rent_ebit"),
        ("company_id", 0),
        ("consolidation_perimeter", ""),
        ("units", "thousands"),
        ("lease_basis", "rent"),
        ("maintenance_capacity", ""),
        ("starting_capital_premise", ""),
        ("tax_rate", 0.2),
    ],
)
def test_invalid_calibration_refused(key, value):
    record = synthetic_record()
    record[key] = value
    with pytest.raises((ValueError, TypeError)):
        qualify_calibration(record, as_of=date.fromisoformat(CUTOFF), currency="SEK", tax_rate=0.21)


@pytest.mark.parametrize(
    "key,value",
    [
        ("published_on", "2026-06-02"),
        ("observed_on", "2026-06-02"),
        ("accounting_date", "2026-03-30"),
        ("anchor", ""),
        ("sha256", "bad"),
    ],
)
def test_calibration_operand_provenance_refused(key, value):
    record = synthetic_record()
    record["sources"]["capital_end"][key] = value
    with pytest.raises(ValueError):
        qualify_calibration(record, as_of=date.fromisoformat(CUTOFF), currency="SEK", tax_rate=0.21)


def test_matched_historical_nopat_over_average_capital_hand_lock():
    record = synthetic_record()
    record.update(
        normalized_ebit=25, tax_rate=0.2, capital_begin=80, capital_end=120, future_return=0.2
    )
    record["tax_basis"] = "synthetic normalized 20% tax"
    qualified = qualify_calibration(
        record, as_of=date.fromisoformat(CUTOFF), currency="SEK", tax_rate=0.2
    )
    assert qualified.historical_average_roic == 0.2  # 25 * .8 / ((80 + 120) / 2)


def test_average_history_and_future_assumption_are_distinct():
    record = synthetic_record()
    qualified = qualify_calibration(
        record, as_of=date.fromisoformat(CUTOFF), currency="SEK", tax_rate=0.21
    )
    assert qualified.historical_average_roic == pytest.approx(0.2)
    assert qualified.future_incremental_return == pytest.approx(0.2)
    assert (
        json.loads(qualified.record_json)["future_return_provenance"]
        == "company_history_calibrated_assumption"
    )


def test_public_qualified_run_exports_disabled_axes_and_exact_replay(monkeypatch, tmp_path):
    conn, cid = setup(periods=[annual(2024, 100), annual(2025, 110), annual(2026, 121)])
    packet(conn, cid)
    record = synthetic_record()
    record["company_id"] = cid
    record["sources"]["unused"] = {
        "source_id": "synthetic:unqualified-extra",
        "url": "https://example.invalid/unqualified-extra",
    }
    identity = append_reinvestment_calibration(
        conn,
        cid,
        record,
        as_of=date.fromisoformat(CUTOFF),
    )
    live = load_results_for_company(conn, cid, CUTOFF, dcf_routing=explicit_mature_dcf_route())[
        "reverse_dcf"
    ]
    assert live["dcf"]["available"]
    _, _, exported = rank_exports(
        conn,
        monkeypatch,
        tmp_path,
        dcf_routing={cid: explicit_mature_dcf_route()},
    )
    assert exported[str(cid)]["dcf"] == live["dcf"]
    value = exported[str(cid)]["dcf"]
    assert value["calibration"]["identity"] == identity
    assert value["assumptions"]["calibration_identity"] == identity
    assert value["required_return"] == {
        "policy_version": "required-return-v2-market-cap-buckets",
        "market_cap": 100_000_000,
        "size_bucket": "below_sek_1bn",
        "required_return": 0.15,
        "source_date": CUTOFF,
        "basis": "discount_rate_proxy_for_cost_of_capital",
    }
    assert (
        value["projected_cash_flows"][-1]["incremental_return"]
        == value["assumptions"]["discount_rate"]
    )
    assert (
        value["terminal_cash_flow"]["incremental_return"] == value["assumptions"]["discount_rate"]
    )
    assert value["status"] == "available"
    assert value["reason"] is None
    assert value["version"] == "reverse-dcf-v16-explicit-input-quality"
    growth_solve = exported[str(cid)]["implied"]["revenue_growth"]
    eligibility = growth_solve["eligibility"]
    assert eligibility["registry_version"] == "reverse-dcf-solve-registry-v2"
    assert eligibility["status"] == "supported"
    assert eligibility["domain"] == {
        "lower_bound": -0.10,
        "upper_bound": 0.30,
        "bounds_inclusive": True,
        "candidate_policy": "sample the full declared range; any invalid sampled candidate refuses the axis",
    }
    assert "initial revenue growth fades linearly" in eligibility["root_interpretation"]
    assert eligibility["fixed_assumptions"]["discount_rate"]["evidence_references"]
    assert {item["status"] for item in eligibility["evidence_prerequisites"]} == {"met"}
    provenance = value["assumption_provenance"]
    assert set(provenance) == set(value["assumptions"])
    assert provenance["revenue_growth"]["origin"] == "company_history"
    assert provenance["ebit_margin"]["origin"] == "report_evidence"
    assert provenance["tax_rate"]["origin"] == "fixed_default"
    discount = provenance["discount_rate"]
    assert discount["origin"] == "market_evidence"
    discount_refs = {ref["anchor"]: ref for ref in discount["evidence_references"]}
    assert discount_refs["close market-cap operand"] == {
        "source_id": f"stock-price:company-{cid}:date-{CUTOFF}",
        "source_url": None,
        "published_on": None,
        "observed_on": CUTOFF,
        "anchor": "close market-cap operand",
        "sha256": None,
    }
    assert discount_refs["shares outstanding market-cap operand"]["source_id"] == (
        f"financial-period:company-{cid}:type-year:end-2026-03-31"
    )
    calibration_refs = provenance["reinvestment_return"]["evidence_references"]
    record_sources = synthetic_record()["sources"]
    assert {ref["source_id"] for ref in calibration_refs} == {
        source["source_id"] for source in record_sources.values()
    }
    assert provenance["calibration_identity"]["evidence_references"] == calibration_refs
    assert "synthetic:unqualified-extra" not in {ref["source_id"] for ref in calibration_refs}
    assert provenance["reinvestment_return"]["limitations"]
    json.dumps(value, allow_nan=False)
    for axis, status in (("ebit_margin", "unavailable"), ("terminal_growth", "not_identifiable")):
        result = exported[str(cid)]["implied"][axis]
        assert result["solution_status"] == status
        assert result["status"] == ("unsupported" if axis == "ebit_margin" else "not_identifiable")
        assert result["candidate_roots"] == []
        assert not {"implied_assumption", "lower_endpoint_price", "value_per_share"} & result.keys()
    executed = conn.execute("SELECT * FROM executed_numerical_runs").fetchone()
    snapshot = conn.execute("SELECT body,rules FROM numerical_input_bodies").fetchone()
    body, rules = json.loads(snapshot["body"]), json.loads(snapshot["rules"])
    retained_record = json.loads(body["tables"]["reinvestment_calibrations"][0]["record_json"])
    retained_prices = {
        (row["company_id"], row["price_date"]): row for row in body["tables"]["prices"]
    }
    retained_reports = {
        (row["company_id"], row["period_type"], row["period_end"]): row
        for row in body["tables"]["financial_periods"]
    }
    assert retained_prices[(cid, CUTOFF)]["close"] == 10
    assert retained_reports[(cid, "year", "2026-03-31")]["shares_outstanding"] == 10
    assert body["tables"]["reinvestment_calibrations"][0]["identity"] == identity
    assert {ref["source_id"] for ref in calibration_refs} == {
        retained_record["sources"][operand]["source_id"] for operand in record_sources
    }
    assert rules["economic_convention"] == ECONOMIC_CONVENTION
    assert rules["reinvestment_calibration"] == synthetic_record()["version"]
    assert rules["dcf_result_contract"] == "dcf-result-contract-v2"
    assert rules["dcf_solve_registry"] == "reverse-dcf-solve-registry-v2"
    original = json.loads(executed["outputs"])
    replayed = replay_run(conn, executed["run_id"])
    assert replayed["outputs"] == original
    assert replayed["outputs"]["dcf"][str(cid)]["dcf"] == value
    # A conflicting new review changes live availability, never the frozen run.
    record = synthetic_record()
    record["approval_id"] = "synthetic-alternative-review"
    append_reinvestment_calibration(conn, cid, record, as_of=date.fromisoformat(CUTOFF))
    assert load_results_for_company(conn, cid, CUTOFF)["reverse_dcf"]["status"] == "unavailable"
    conn.execute("DELETE FROM prices")
    conn.commit()
    assert replay_run(conn, executed["run_id"])["outputs"] == original
    assert canonical(json.loads(executed["outputs"])) == canonical(original)
    old_rules = dict(rules)
    old_rules.pop("dcf_result_contract")
    monkeypatch.setattr("alphaforge.db.numerical_runs.rules_bundle", lambda: old_rules)
    with pytest.raises(ReplayRefusal, match="unsupported_rules_or_code"):
        replay_run(conn, executed["run_id"])
    monkeypatch.setattr(
        "alphaforge.db.numerical_runs.rules_bundle",
        lambda: {**rules_bundle(), "economic_convention": "incompatible"},
    )
    with pytest.raises(ReplayRefusal, match="unsupported_rules_or_code"):
        replay_run(conn, executed["run_id"])


def test_missing_shares_do_not_parse_or_cite_unconsumed_split_dates():
    conn, cid = setup(periods=[annual(2026, number_Of_Shares=None)])
    upsert_stock_splits(
        conn,
        [{"insId": 991, "splitDate": "unknown", "splitType": "S", "ratio": "5:1"}],
    )

    result = load_results_for_company(conn, cid, CUTOFF)["reverse_dcf"]

    assert result["current_shares"] is None
    assert result["dcf"]["status"] == "insufficient_evidence"
    assert result["dcf"]["missing_information"] == [
        "positive market capitalization unavailable for required-return hurdle"
    ]
    assert result["dcf"]["assumption_provenance"] == {}
    assert conn.execute("SELECT split_date FROM stock_splits").fetchone()[0] == "unknown"


def test_split_adjusted_discount_provenance_is_retained_and_replayed(monkeypatch, tmp_path):
    periods = [
        annual(2024, 100, number_Of_Shares=90),
        annual(2025, 110, number_Of_Shares=90),
        annual(2026, 121, number_Of_Shares=90),
        annual(
            2026,
            30,
            period_type="quarter",
            period_end="2026-05-31",
            report_Date=CUTOFF,
            period=1,
            number_Of_Shares=450,
        ),
    ]
    conn, cid = setup(periods=periods)
    upsert_prices(conn, cid, [{"d": CUTOFF, "c": 3}], currency="SEK")
    upsert_stock_splits(
        conn,
        [{"insId": 991, "splitDate": "2026-04-15", "splitType": "S", "ratio": "5:1"}],
    )
    packet(conn, cid)
    synthetic_calibration_fixture(conn, cid)

    _, _, exported = rank_exports(
        conn,
        monkeypatch,
        tmp_path,
        dcf_routing={cid: explicit_mature_dcf_route()},
    )
    result = exported[str(cid)]
    value = result["dcf"]
    assert result["current_shares"] == 450
    assert value["required_return"]["market_cap"] == 1_350_000_000
    assert value["required_return"]["size_bucket"] == "sek_1bn_to_below_5bn"
    assert value["assumptions"]["discount_rate"] == 0.135
    discount_refs = value["assumption_provenance"]["discount_rate"]["evidence_references"]
    assert {ref["source_id"] for ref in discount_refs} == {
        f"financial-period:company-{cid}:type-year:end-2026-03-31",
        f"stock-price:company-{cid}:date-{CUTOFF}",
        "stock-split:borsdata-991:date-2026-04-15",
    }

    executed = conn.execute("SELECT * FROM executed_numerical_runs").fetchone()
    body = json.loads(conn.execute("SELECT body FROM numerical_input_bodies").fetchone()[0])
    assert body["tables"]["stock_splits"] == [
        {
            "id": 1,
            "company_id": cid,
            "borsdata_id": 991,
            "split_type": "S",
            "ratio": "5:1",
            "split_date": "2026-04-15",
        }
    ]
    replayed = replay_run(conn, executed["run_id"])["outputs"]
    assert replayed["dcf"][str(cid)]["dcf"] == value


def test_report_provenance_uses_exact_consumed_windows():
    periods = [annual(year, 100 * 1.05 ** (year - 2017)) for year in range(2017, 2027)]
    conn, cid = setup(periods=periods)
    packet(conn, cid)
    synthetic_calibration_fixture(conn, cid)

    value = load_results_for_company(conn, cid, CUTOFF, dcf_routing=explicit_mature_dcf_route())[
        "reverse_dcf"
    ]["dcf"]
    provenance = value["assumption_provenance"]

    def expected(years):
        return {f"financial-period:company-{cid}:type-year:end-{year}-03-31" for year in years}

    growth_refs = provenance["revenue_growth"]["evidence_references"]
    margin_refs = provenance["ebit_margin"]["evidence_references"]
    assert {ref["source_id"] for ref in growth_refs} == expected(range(2023, 2027))
    assert {ref["source_id"] for ref in margin_refs} == expected(range(2022, 2027))
    assert provenance["ebit_margin_start"]["evidence_references"] == margin_refs


def test_report_provenance_identity_distinguishes_period_types():
    periods = [
        annual(2025, 110, operating_Income=22),
        annual(2026, 121, operating_Income=24.2),
        annual(2026, 121, period_type="r12", operating_Income=24.2),
    ]
    conn, cid = setup(periods=periods)
    packet(conn, cid)
    synthetic_calibration_fixture(conn, cid)

    value = load_results_for_company(conn, cid, CUTOFF, dcf_routing=explicit_mature_dcf_route())[
        "reverse_dcf"
    ]["dcf"]
    provenance = value["assumption_provenance"]
    growth_ids = {ref["source_id"] for ref in provenance["revenue_growth"]["evidence_references"]}
    margin_ids = {ref["source_id"] for ref in provenance["ebit_margin"]["evidence_references"]}
    discount_ids = {ref["source_id"] for ref in provenance["discount_rate"]["evidence_references"]}
    annual_ids = {
        f"financial-period:company-{cid}:type-year:end-{year}-03-31" for year in (2025, 2026)
    }
    latest_annual_id = f"financial-period:company-{cid}:type-year:end-2026-03-31"
    r12_id = f"financial-period:company-{cid}:type-r12:end-2026-03-31"
    assert growth_ids == annual_ids
    assert margin_ids == {latest_annual_id}
    assert r12_id in discount_ids
    assert latest_annual_id not in discount_ids


def test_v18_upgrade_adds_empty_calibration_lane_without_backfill():
    from alphaforge.db.migrations import migrate

    conn, _ = setup()
    conn.execute("DROP TABLE reinvestment_calibrations")
    conn.execute("PRAGMA user_version=18")
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 19
    assert conn.execute("SELECT COUNT(*) FROM reinvestment_calibrations").fetchone()[0] == 0


def test_calibration_rows_are_immutable_including_replace():
    conn, cid = setup()
    identity = synthetic_calibration_fixture(conn, cid)
    for sql in (
        "UPDATE reinvestment_calibrations SET record_json='{}'",
        "DELETE FROM reinvestment_calibrations",
        "INSERT OR REPLACE INTO reinvestment_calibrations SELECT * FROM reinvestment_calibrations",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(sql)
    assert conn.execute("SELECT identity FROM reinvestment_calibrations").fetchone()[0] == identity


@pytest.mark.parametrize(
    "case", ["missing", "basis", "future", "hash", "company", "margin", "loss"]
)
def test_public_missing_invalid_or_unsupported_inputs_never_leak_values(case):
    report = annual(2026)
    if case == "margin":
        reports = [annual(2024, operating_Income=10), annual(2025, operating_Income=10), report]
    elif case == "loss":
        reports = [annual(2026, operating_Income=-10)]
    else:
        reports = [report]
    conn, cid = setup(periods=reports)
    if case != "missing":
        record = synthetic_record()
        if case == "basis":
            record["capital_basis"] = "issuer_roce"
        if case == "company":
            record["company_id"] = cid + 1
        if case == "future":
            record["sources"]["capital_end"]["observed_on"] = "2026-06-02"
        identity = calibration_identity(record) if case != "hash" else "b" * 64
        conn.execute(
            "INSERT INTO reinvestment_calibrations(company_id,identity,record_json) VALUES (?,?,?)",
            (cid, identity, calibration_json(record)),
        )
    result = load_results_for_company(conn, cid, CUTOFF)
    dcf = result["reverse_dcf"]
    assert dcf["status"] == "unavailable"
    assert not dcf.get("implied")
    assert (
        not {"value_per_share", "enterprise_value", "projected_cash_flows", "terminal_cash_flow"}
        & dcf["dcf"].keys()
    )
    if case in {"missing", "basis", "future", "hash", "company"}:
        assert dcf["dcf"]["status"] == "insufficient_evidence"
    else:
        assert dcf["dcf"]["status"] == "domain_unavailable"
    assert dcf["dcf"]["version"] == "reverse-dcf-v16-explicit-input-quality"
    if case in {"basis", "future", "hash", "company"}:
        candidates = result["selection"]["reinvestment_calibration"]["candidates"]
        assert candidates[0]["rejection_reason"]
    if case == "margin":
        assert dcf["dcf"]["missing_information"] == ["varying_margin_capital_evidence_unavailable"]
    if case == "loss":
        assert dcf["dcf"]["missing_information"] == ["negative_nopat_unsupported_reinvestment"]
