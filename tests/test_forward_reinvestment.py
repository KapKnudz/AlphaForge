"""Independent calculator locks and public qualified-calibration contracts."""

import json
import sqlite3
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from decimal import localcontext

import pytest
from dcf_calibration_fixtures import synthetic_calibration_fixture, synthetic_record
from test_method_date_growth_selection import CUTOFF, annual, packet, rank_exports, setup

from alphaforge.cli.ranking_loader import load_results_for_company
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
from alphaforge.db.numerical_runs import ReplayRefusal, canonical, replay_run, rules_bundle
from alphaforge.db.reinvestment import append_reinvestment_calibration


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
    inputs = replace(hand_inputs(), current_price=103.5)
    result = engine.solve(inputs, "revenue_growth", 0, 0.08)
    assert result.implied_assumption == pytest.approx(0.04)
    assert result.modeled_price == pytest.approx(103.5)
    diagnostics, brackets, _ = engine.diagnose_solve_range(
        replace(inputs, current_price=200),
        "revenue_growth",
        0,
        0.08,
    )
    assert not brackets
    assert diagnostics["lower_endpoint_price"] == pytest.approx(99.5)
    assert diagnostics["upper_endpoint_price"] == pytest.approx(107.5)
    assert diagnostics["nearest_boundary_gap"] == pytest.approx(92.5)
    assert diagnostics["nearest_boundary_gap_pct_target"] == pytest.approx(46.25)
    assert "sampled range only" in diagnostics["range_qualification"]
    with pytest.raises(ValueError, match="not bracketed"):
        engine.solve(replace(inputs, current_price=200), "revenue_growth", 0, 0.08)


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
    identity = synthetic_calibration_fixture(conn, cid)
    live = load_results_for_company(conn, cid, CUTOFF)["reverse_dcf"]
    assert live["dcf"]["available"]
    _, _, exported = rank_exports(conn, monkeypatch, tmp_path)
    assert exported[str(cid)]["dcf"] == live["dcf"]
    value = exported[str(cid)]["dcf"]
    assert value["calibration"]["identity"] == identity
    assert value["assumptions"]["calibration_identity"] == identity
    assert value["required_return"]["basis"] == "discount_rate_proxy_for_cost_of_capital"
    assert (
        value["projected_cash_flows"][-1]["incremental_return"]
        == value["assumptions"]["discount_rate"]
    )
    assert (
        value["terminal_cash_flow"]["incremental_return"] == value["assumptions"]["discount_rate"]
    )
    assert value["status"] == "available"
    assert value["reason"] is None
    assert value["version"] == "reverse-dcf-v15-typed-result-contract"
    provenance = value["assumption_provenance"]
    assert set(provenance) == set(value["assumptions"])
    assert provenance["revenue_growth"]["origin"] == "company_history"
    assert provenance["ebit_margin"]["origin"] == "report_evidence"
    assert provenance["tax_rate"]["origin"] == "fixed_default"
    calibration_refs = provenance["reinvestment_return"]["evidence_references"]
    record_sources = synthetic_record()["sources"]
    assert {ref["source_id"] for ref in calibration_refs} == {
        source["source_id"] for source in record_sources.values()
    }
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
    assert body["tables"]["reinvestment_calibrations"][0]["identity"] == identity
    assert {ref["source_id"] for ref in calibration_refs} == {
        source["source_id"] for source in retained_record["sources"].values()
    }
    assert rules["economic_convention"] == ECONOMIC_CONVENTION
    assert rules["reinvestment_calibration"] == synthetic_record()["version"]
    assert rules["dcf_result_contract"] == "dcf-result-contract-v1"
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
    assert dcf["dcf"]["version"] == "reverse-dcf-v15-typed-result-contract"
    if case in {"basis", "future", "hash", "company"}:
        candidates = result["selection"]["reinvestment_calibration"]["candidates"]
        assert candidates[0]["rejection_reason"]
    if case == "margin":
        assert dcf["dcf"]["missing_information"] == ["varying_margin_capital_evidence_unavailable"]
    if case == "loss":
        assert dcf["dcf"]["missing_information"] == ["negative_nopat_unsupported_reinvestment"]
