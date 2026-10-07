"""Approved funded-domain contract, including public capture/export/CLI replay."""

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import date
from fractions import Fraction
from pathlib import Path

import pytest
from dcf_calibration_fixtures import (
    reviewed_growth_inputs,
    synthetic_record,
    synthetic_reverse_coverage,
)
from test_forward_reinvestment import verified_growth_metadata
from test_method_date_growth_selection import (
    CUTOFF,
    annual,
    explicit_mature_dcf_route,
    packet,
    rank_exports,
    setup,
)

from alphaforge.cli.ranking_loader import _solve_axis_result, load_results_for_company
from alphaforge.core.valuation.growth_domain import (
    derive_growth_domain,
    growth_coverage_error,
    growth_history_identity,
)
from alphaforge.core.valuation.reverse_dcf import (
    DcfAssumptions,
    ReverseDcfEngine,
    ReverseDcfInputs,
    UnsupportedEconomicPolicy,
)
from alphaforge.core.valuation.solve_eligibility import solve_axis_metadata, solve_input_identity
from alphaforge.db.numerical_runs import ReplayRefusal, canonical, replay_run
from alphaforge.db.reinvestment import append_reinvestment_calibration


def mature_inputs(q=0.20, target=13, growth=0.10):
    return reviewed_growth_inputs(
        ReverseDcfInputs(
            target,
            10,
            121,
            5,
            DcfAssumptions(
                5,
                growth,
                0.20,
                0.21,
                0.15,
                0.02,
                reinvestment_return=q,
                revenue_growth_fade_to=0.02,
                ebit_margin_start=0.20,
            ),
            eligibility_context_identity="synthetic:qualified-mature-test",
        )
    )


def public_fixture(target=13, q=0.20, review=True):
    conn, cid = setup(periods=[annual(2024, 100), annual(2025, 110), annual(2026, 121)])
    packet(conn, cid)
    conn.execute("UPDATE prices SET close=?", (target,))
    record = synthetic_record(future_return=q)
    record["company_id"] = cid
    conn.execute("SAVEPOINT provisional_synthetic_review")
    append_reinvestment_calibration(conn, cid, record, as_of=date.fromisoformat(CUTOFF))
    if review:
        # Synthetic setup only: inspect the fixed basis BEFORE candidate evaluation.
        # This is an explicit test analyst assertion, not a production backfill.
        unreviewed = load_results_for_company(
            conn, cid, CUTOFF, dcf_routing=explicit_mature_dcf_route()
        )
        basis = unreviewed["reverse_dcf"]["implied"]["revenue_growth"]["eligibility"]["domain"][
            "fixed_basis"
        ]
        assert unreviewed["reverse_dcf"]["implied"]["revenue_growth"]["candidate_roots"] == []
        record["reverse_growth_coverage"] = synthetic_reverse_coverage(basis)
        # Roll back provisional setup before admitting the reviewed synthetic fixture.
        conn.execute("ROLLBACK TO provisional_synthetic_review")
        append_reinvestment_calibration(conn, cid, record, as_of=date.fromisoformat(CUTOFF))
    conn.execute("RELEASE provisional_synthetic_review")
    conn.commit()
    return conn, cid


@pytest.mark.parametrize(
    "target,status,position",
    [
        (13, "candidate_solutions", "within_sampled_range"),
        (10, "no_crossing", "below_sampled_range"),
        (30, "no_crossing", "above_sampled_range"),
    ],
)
def test_public_full_funded_domain_and_frozen_replay(
    monkeypatch, tmp_path, target, status, position
):
    conn, cid = public_fixture(target)
    _, _, exported = rank_exports(
        conn, monkeypatch, tmp_path, dcf_routing={cid: explicit_mature_dcf_route()}
    )
    value = exported[str(cid)]
    assert value["dcf"]["value_per_share"] == pytest.approx(13.97606581792473)
    g = value["implied"]["revenue_growth"]
    assert g["status"] == status
    assert g["target_position"] == position
    assert g["diagnostic_grid_points"] == 201
    d = g["eligibility"]["domain"]
    assert d["lower_bound"] == 0
    assert d["upper_bound"] == 0.26
    assert d["requested_bounds"] == [0, 0.26]
    assert d["binding_constraints"] == ["funding_year_1"]
    assert d["interval_validation"] == "analytic_real_economics"
    assert d["coverage_approval"]["approval_id"] == "synthetic-reverse-test-only"
    if target == 13:
        assert g["candidate_solution_count"] == 1
        root = g["candidate_roots"][0]
        assert root["implied_assumption"] == pytest.approx(0.04215994, abs=1e-7)
        assert abs(root["price_difference"]) <= 1e-6
        assert "does not establish uniqueness or completeness" in g["solution_qualification"]
    else:
        assert g["candidate_roots"] == []
    assert "implied_assumption" not in g
    row = conn.execute("SELECT * FROM executed_numerical_runs").fetchone()
    original = json.loads(row["outputs"])
    assert original["dcf"][str(cid)] == value
    conn.execute("UPDATE prices SET close=7")
    new_review = synthetic_record()
    new_review["company_id"] = cid
    new_review["approval_id"] = "different-live-synthetic-review"
    append_reinvestment_calibration(conn, cid, new_review, as_of=date.fromisoformat(CUTOFF))
    conn.commit()
    assert replay_run(conn, row["run_id"])["outputs"] == original
    # Actual CLI, from a different cwd; this fixture has no retained PDF objects.
    db = tmp_path / "frozen.db"
    with sqlite3.connect(db) as dest:
        conn.backup(dest)
    root_dir = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "alphaforge.cli.main",
            "--dsn",
            f"sqlite:///{db}",
            "replay",
            "--run-id",
            str(row["run_id"]),
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(root_dir)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    cli = json.loads(result.stdout)
    assert cli["audit_only"]
    assert cli["outputs"] == original
    # Simulate external corruption in this test DB after bypassing its immutable trigger.
    conn.execute("DROP TRIGGER numerical_run_no_update")
    conn.execute("UPDATE executed_numerical_runs SET outputs='{}'")
    with pytest.raises(ReplayRefusal, match="corrupt_retained_body"):
        replay_run(conn, row["run_id"])


def test_price_and_forward_estimate_do_not_select_domain(monkeypatch, tmp_path):
    conn, cid = public_fixture()
    identities = []
    for price in (13, 10, 30):
        conn.execute("UPDATE prices SET close=?", (price,))
        g = load_results_for_company(conn, cid, CUTOFF, dcf_routing=explicit_mature_dcf_route())[
            "reverse_dcf"
        ]["implied"]["revenue_growth"]
        d = g["eligibility"]["domain"]
        identities.append((d["certificate_identity"], g["eligibility"]["input_identity"]))
        assert d["requested_bounds"] == [0, 0.26]
    assert len({item[0] for item in identities}) == 1
    assert len({item[1] for item in identities}) == 3
    inputs = mature_inputs()
    changed = replace(inputs, assumptions=replace(inputs.assumptions, revenue_growth=0.04))
    assert derive_growth_domain(inputs) == derive_growth_domain(changed)
    assert (
        verified_growth_metadata(inputs).input_identity
        != verified_growth_metadata(changed).input_identity
    )
    with pytest.raises(ValueError, match="input identity"):
        ReverseDcfEngine().solve(
            replace(inputs, current_price=10),
            "revenue_growth",
            0,
            0.26,
            eligibility=verified_growth_metadata(inputs),
        )


def test_price_crossing_hurdle_bucket_requires_a_new_coverage_review():
    conn, cid = public_fixture()
    original = load_results_for_company(conn, cid, CUTOFF, dcf_routing=explicit_mature_dcf_route())[
        "reverse_dcf"
    ]
    conn.execute("UPDATE prices SET close=110")
    changed = load_results_for_company(conn, cid, CUTOFF, dcf_routing=explicit_mature_dcf_route())[
        "reverse_dcf"
    ]
    assert changed["dcf"]["status"] == "available"
    assert changed["dcf"]["assumptions"]["discount_rate"] == 0.135
    growth = changed["implied"]["revenue_growth"]
    assert growth["status"] == "insufficient_evidence"
    assert growth["reason"] == "reverse_growth_coverage_basis_mismatch"
    assert growth["candidate_roots"] == []
    assert (
        growth["eligibility"]["domain"]["certificate_identity"]
        != original["implied"]["revenue_growth"]["eligibility"]["domain"]["certificate_identity"]
    )


def test_history_identity_ignores_only_quality_surrogate_ids():
    quality = {
        "selected_periods": [],
        "excluded_periods": [{"evidence_id": "99", "reason": "bad prefix"}],
        "evidenced_anomalies": [],
        "valuation_period": None,
    }
    other = {**quality, "excluded_periods": [{"evidence_id": "1", "reason": "bad prefix"}]}
    assert growth_history_identity(quality, [{"revenue": 121}]) == growth_history_identity(
        other, [{"revenue": 121}]
    )
    assert growth_history_identity(quality, [{"revenue": 121}]) != growth_history_identity(
        other, [{"revenue": 122}]
    )
    other["excluded_periods"][0]["reason"] = "different financial exclusion"
    assert growth_history_identity(quality, []) != growth_history_identity(other, [])


def test_missing_review_preserves_forward_and_does_not_sample(monkeypatch):
    conn, cid = public_fixture(review=False)

    def trap(*args, **kwargs):
        raise AssertionError("missing analyst review reached candidate prices")

    monkeypatch.setattr(ReverseDcfEngine, "_value_with", trap)
    result = load_results_for_company(conn, cid, CUTOFF, dcf_routing=explicit_mature_dcf_route())[
        "reverse_dcf"
    ]
    assert result["dcf"]["status"] == "available"
    assert result["dcf"]["value_per_share"] == pytest.approx(13.97606581792473)
    g = result["implied"]["revenue_growth"]
    assert g["status"] == "insufficient_evidence"
    assert g["reason"] == "full_interval_starting_capital_coverage_unavailable"
    assert not {"lower_endpoint_price", "upper_endpoint_price", "implied_assumption"} & g.keys()
    assert g["candidate_roots"] == []


@pytest.mark.parametrize(
    "lo,hi,reason",
    [
        (-0.01, 0.20, "unsupported_capital_release"),
        (0, 0.30, "unsupported_financing"),
        (0, 0.261, "unsupported_financing"),
    ],
)
def test_requested_interval_is_refused_not_clipped(lo, hi, reason):
    inputs = mature_inputs()
    metadata = verified_growth_metadata(inputs, (lo, hi))

    class SamplingTrap(ReverseDcfEngine):
        def _value_with(self, *args):
            raise AssertionError("invalid interval was sampled")

    engine = SamplingTrap()
    for operation in (engine.solve, engine.diagnose_solve_range):
        with pytest.raises(UnsupportedEconomicPolicy, match=reason):
            operation(inputs, "revenue_growth", lo, hi, eligibility=metadata)
    with pytest.raises(UnsupportedEconomicPolicy, match=reason):
        ReverseDcfEngine().value(
            replace(
                inputs, assumptions=replace(inputs.assumptions, revenue_growth=lo if lo < 0 else hi)
            )
        )


@pytest.mark.parametrize(
    "q,reason", [(0.004, "empty_admissible_domain"), (0.005, "degenerate_admissible_domain")]
)
def test_empty_and_singleton_domains_do_not_become_searches(q, reason):
    inputs = mature_inputs(q=q, growth=0)
    d = derive_growth_domain(inputs)
    assert d["reason"] == reason
    g = _solve_axis_result(
        ReverseDcfEngine(), inputs, "revenue_growth", verified_growth_metadata(inputs)
    )
    assert g["reason"] == reason
    assert g["candidate_roots"] == []
    assert "lower_endpoint_price" not in g
    if q == 0.005:
        assert d["economic_bounds"][0] == d["economic_bounds"][1]
        assert ReverseDcfEngine().value(inputs).value_per_share == pytest.approx(10.215752526360529)


@pytest.mark.parametrize("q,upper", [(0.02, 0.02), (0.15, 0.1933333333333333)])
def test_strict_numerical_boundary_failure_refuses_whole_interval(q, upper):
    inputs = mature_inputs(q=q, growth=0)
    meta = verified_growth_metadata(inputs)
    assert meta.domain["upper_bound"] == upper
    result = _solve_axis_result(ReverseDcfEngine(), inputs, "revenue_growth", meta)
    assert result["reason"] == "invalid_candidate_economics"
    assert result["error"] == "unsupported_financing"
    assert result["candidate_assumption"] == upper
    assert result["boundary_evaluation_failure"]
    assert result["candidate_roots"] == []
    assert "lower_endpoint_price" not in result


def test_small_interval_can_have_decreasing_prices_and_scope_ceiling_can_bind():
    inputs = mature_inputs(q=0.006, growth=0)
    meta = verified_growth_metadata(inputs)
    assert meta.domain["upper_bound"] == pytest.approx(0.0013333333333333333)
    diagnostics, _, _ = ReverseDcfEngine().diagnose_solve_range(
        inputs, "revenue_growth", 0, meta.domain["upper_bound"], eligibility=meta
    )
    assert diagnostics["monotonicity"] == "sampled_decreasing"
    inputs = mature_inputs(q=0.25)
    meta = verified_growth_metadata(inputs)
    assert meta.domain["upper_bound"] == 0.30
    assert meta.domain["binding_constraints"] == ["scope_ceiling"]
    diagnostic, _, _ = ReverseDcfEngine().diagnose_solve_range(
        inputs, "revenue_growth", 0, 0.30, eligibility=meta
    )
    assert diagnostic["diagnostic_grid_points"] == 201


@pytest.mark.parametrize("endpoint", [0, 0.26])
def test_endpoint_only_matches_are_not_promoted(endpoint):
    inputs = mature_inputs()
    engine = ReverseDcfEngine()
    price = engine.value(
        replace(inputs, assumptions=replace(inputs.assumptions, revenue_growth=endpoint))
    ).value_per_share
    inputs = replace(inputs, current_price=price)
    result = _solve_axis_result(engine, inputs, "revenue_growth", verified_growth_metadata(inputs))
    assert result["solution_status"] == "sampled_match"
    assert result["candidate_roots"] == []
    assert result["sampled_match_points"][0]["classification"] == "sampled_endpoint_match"


def test_restricted_request_and_brackets_are_bound_to_declared_coverage():
    inputs = mature_inputs(target=10)
    meta = verified_growth_metadata(inputs, (0, 0.08))
    g = _solve_axis_result(ReverseDcfEngine(), inputs, "revenue_growth", meta)
    assert g["solution_status"] == "no_candidate_solution"
    assert g["eligibility"]["domain"]["upper_bound"] == 0.26
    assert g["eligibility"]["domain"]["requested_bounds"] == [0, 0.08]
    with pytest.raises(ValueError, match="requested interval"):
        ReverseDcfEngine().solve(inputs, "revenue_growth", 0.08, 0.09, eligibility=meta)
    with pytest.raises(ValueError, match="full requested interval"):
        ReverseDcfEngine().diagnose_solve_range(inputs, "revenue_growth", 0, 0.04, eligibility=meta)


@pytest.mark.parametrize("field", ["current_revenue", "discount_rate", "packet_hash", "approval"])
def test_stale_coverage_and_tampered_certificate_are_rejected(field):
    inputs = mature_inputs()
    if field == "current_revenue":
        inputs = replace(inputs, current_revenue=122)
    elif field == "discount_rate":
        inputs = replace(inputs, assumptions=replace(inputs.assumptions, discount_rate=0.135))
    elif field == "packet_hash":
        inputs = replace(
            inputs, growth_domain_context={**inputs.growth_domain_context, "packet_hash": "other"}
        )
    else:
        record = json.loads(canonical(inputs.growth_domain_context["calibration_record"]))
        record["reverse_growth_coverage"]["approved_on"] = "2026-06-02"
        from alphaforge.core.valuation.reinvestment import calibration_identity

        inputs = replace(
            inputs,
            growth_domain_context={**inputs.growth_domain_context, "calibration_record": record},
            assumptions=replace(
                inputs.assumptions, calibration_identity=calibration_identity(record)
            ),
        )
    assert growth_coverage_error(inputs)
    assert verified_growth_metadata(inputs).status == "insufficient_evidence"
    original = mature_inputs()
    meta = verified_growth_metadata(original)
    domain = {**meta.domain, "upper_bound": 0.30}
    tampered = replace(meta, domain=domain)
    tampered = replace(tampered, input_identity=solve_input_identity(original, tampered))
    with pytest.raises(ValueError, match="derived certificate"):
        ReverseDcfEngine().solve(original, "revenue_growth", 0, 0.30, eligibility=tampered)


def test_provenance_changes_certificate_identity_without_changing_funding_bounds():
    inputs = mature_inputs()
    original = verified_growth_metadata(inputs)
    provenance = {key: dict(value) for key, value in original.fixed_assumptions.items()}
    provenance["discount_rate"]["source"] = "alternative synthetic market-source review"
    changed = solve_axis_metadata(
        "revenue_growth",
        inputs=inputs,
        assumption_provenance=provenance,
        prerequisite_evidence={item["name"]: item for item in original.evidence_prerequisites},
    )
    assert changed.status == "supported"
    assert changed.domain["upper_bound"] == original.domain["upper_bound"]
    assert changed.domain["certificate_identity"] != original.domain["certificate_identity"]
    assert changed.input_identity != original.input_identity


def test_scope_only_refusal_does_not_claim_unfunded_economics():
    inputs = mature_inputs(q=0.25)
    metadata = verified_growth_metadata(inputs, (0, 0.31))
    with pytest.raises(UnsupportedEconomicPolicy, match="outside_declared_growth_scope"):
        ReverseDcfEngine().diagnose_solve_range(
            inputs, "revenue_growth", 0, 0.31, eligibility=metadata
        )
    # The scope ceiling is not a financial maximum.
    assert ReverseDcfEngine().value(
        replace(inputs, assumptions=replace(inputs.assumptions, revenue_growth=0.31))
    )


def test_public_scope_ceiling_can_bind_without_financing_relaxation(monkeypatch, tmp_path):
    conn, cid = public_fixture(q=0.25)
    _, _, exported = rank_exports(
        conn, monkeypatch, tmp_path, dcf_routing={cid: explicit_mature_dcf_route()}
    )
    g = exported[str(cid)]["implied"]["revenue_growth"]
    assert g["eligibility"]["domain"]["requested_bounds"] == [0, 0.30]
    assert g["eligibility"]["domain"]["binding_constraints"] == ["scope_ceiling"]
    assert g["diagnostic_grid_points"] == 201
    row = conn.execute("SELECT run_id,outputs FROM executed_numerical_runs").fetchone()
    assert replay_run(conn, row["run_id"])["outputs"] == json.loads(row["outputs"])


def test_interval_certificate_is_exact_and_contains_all_six_funding_rows():
    inputs = mature_inputs()
    d = derive_growth_domain(inputs)
    assert [c["funding_year"] for c in d["constraints"]] == list(range(1, 7))
    bounds = d["economic_bounds"]
    exact = Fraction(int(bounds[1]["numerator"]), int(bounds[1]["denominator"]))
    assert Fraction(d["upper_bound"]) <= exact
    assert d["constraints"][-1]["satisfied"]


def test_invalid_interior_candidate_discards_earlier_candidates():
    inputs = mature_inputs()

    class FailingEngine(ReverseDcfEngine):
        calls = 0

        def solve(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise UnsupportedEconomicPolicy("unsupported_financing")
            return super().solve(*args, **kwargs)

        def diagnose_solve_range(self, *args, **kwargs):
            d, b, m = super().diagnose_solve_range(*args, **kwargs)
            return d, (*b, *b), m

    result = _solve_axis_result(
        FailingEngine(), inputs, "revenue_growth", verified_growth_metadata(inputs)
    )
    assert result["reason"] == "invalid_candidate_economics"
    assert result["candidate_roots"] == []
    assert "value_per_share" not in result
