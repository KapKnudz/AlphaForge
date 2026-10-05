from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_forward_reinvestment import hand_inputs

from alphaforge.cli.ranking_loader import _dcf_solve_status, _solve_axis_result
from alphaforge.core.valuation.dcf_contract import DcfResultStatus
from alphaforge.core.valuation.dcf_policy import DcfAssumptionPolicy
from alphaforge.core.valuation.reverse_dcf import (
    ReverseDcfEngine,
    UnsupportedEconomicPolicy,
)
from alphaforge.core.valuation.solve_eligibility import (
    SOLVE_AXIS_REGISTRY,
    SOLVE_REGISTRY_VERSION,
    solve_axis_metadata,
)


def _supported_metadata(inputs):
    return solve_axis_metadata("revenue_growth", assumptions=inputs.assumptions)


def _verified_metadata(inputs):
    return solve_axis_metadata(
        "revenue_growth",
        assumptions=inputs.assumptions,
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


def test_registry_is_the_explicit_domain_and_reason_owner():
    assert DcfAssumptionPolicy.SOLVE_BOUNDS == {
        axis: (definition.lower_bound, definition.upper_bound)
        for axis, definition in SOLVE_AXIS_REGISTRY.items()
    }
    assert DcfAssumptionPolicy.SOLVE_BOUNDS["revenue_growth"] == (-0.10, 0.30)
    assert SOLVE_AXIS_REGISTRY["revenue_growth"].status == "supported"
    assert SOLVE_AXIS_REGISTRY["ebit_margin"].reason == "unavailable_constant_margin_only"
    assert SOLVE_AXIS_REGISTRY["terminal_growth"].status == "not_identifiable"
    assert SOLVE_REGISTRY_VERSION == "reverse-dcf-solve-registry-v1"


@pytest.mark.parametrize("operation", ["solve", "diagnose_solve_range"])
@pytest.mark.parametrize("axis", ["ebit_margin", "terminal_growth"])
def test_registry_disabled_axis_cannot_be_promoted(operation, axis):
    inputs = hand_inputs()
    definition = SOLVE_AXIS_REGISTRY[axis]
    metadata = solve_axis_metadata(axis, assumptions=inputs.assumptions)
    promoted = replace(metadata, status="supported")

    with pytest.raises(ValueError, match="decision does not match"):
        getattr(ReverseDcfEngine(), operation)(
            inputs,
            axis,
            definition.lower_bound,
            definition.upper_bound,
            eligibility=promoted,
        )


@pytest.mark.parametrize("operation", ["solve", "diagnose_solve_range"])
@pytest.mark.parametrize("axis", ["ebit_margin", "terminal_growth"])
@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("reason", "caller_override", "decision does not match"),
        ("root_interpretation", "caller override", "interpretation does not match"),
    ],
)
def test_registry_disabled_axis_explanation_cannot_be_overridden(
    operation, axis, field, value, message
):
    inputs = hand_inputs()
    definition = SOLVE_AXIS_REGISTRY[axis]
    metadata = replace(
        solve_axis_metadata(axis, assumptions=inputs.assumptions),
        **{field: value},
    )

    with pytest.raises(ValueError, match=message):
        getattr(ReverseDcfEngine(), operation)(
            inputs,
            axis,
            definition.lower_bound,
            definition.upper_bound,
            eligibility=metadata,
        )


@pytest.mark.parametrize(
    ("axis", "solution_status"),
    [("ebit_margin", "unavailable"), ("terminal_growth", "not_identifiable")],
)
def test_disabled_axis_result_exports_registry_decision(axis, solution_status):
    inputs = hand_inputs()
    definition = SOLVE_AXIS_REGISTRY[axis]
    metadata = replace(
        solve_axis_metadata(axis, assumptions=inputs.assumptions),
        status="supported",
        reason="caller_override",
        root_interpretation="caller override",
    )

    class SamplingTrap:
        def diagnose_solve_range(self, *args, **kwargs):
            raise AssertionError("disabled axis reached numerical sampling")

    result = _solve_axis_result(SamplingTrap(), inputs, axis, metadata)

    assert result["solution_status"] == solution_status
    assert result["reason"] == definition.reason
    assert result["qualification"] == definition.root_interpretation
    assert result["eligibility"]["status"] == definition.status
    assert result["eligibility"]["reason"] == definition.reason
    assert result["eligibility"]["root_interpretation"] == definition.root_interpretation


def test_evidence_preflight_happens_before_any_numerical_sampling():
    inputs = hand_inputs()
    metadata = solve_axis_metadata(
        "revenue_growth",
        assumptions=inputs.assumptions,
        prerequisite_evidence={
            "explicit_mature_operating_route": {
                "status": "met",
                "evidence_references": [{"source_id": "route:1"}],
            },
            "qualified_consecutive_annual_history": {
                "status": "unmet",
                "reason": "quality",
                "evidence_references": [{"source_id": "annual:1"}],
            },
            "qualified_reinvestment_calibration": {
                "status": "met",
                "evidence_references": [{"source_id": "calibration:1"}],
            },
            "fixed_assumptions_with_provenance": {"status": "met"},
        },
    )

    class SamplingTrap(ReverseDcfEngine):
        sampled = False

        def _value_with(self, *args):
            self.sampled = True
            raise AssertionError("numerical search ran before evidence preflight")

    engine = SamplingTrap()
    assert metadata.status == "insufficient_evidence"
    for operation in (engine.solve, engine.diagnose_solve_range):
        with pytest.raises(UnsupportedEconomicPolicy, match="quality"):
            operation(inputs, "revenue_growth", 0, 0.08, eligibility=metadata)
        assert not engine.sampled
    unverified = solve_axis_metadata("revenue_growth", assumptions=inputs.assumptions)
    for operation in (engine.solve, engine.diagnose_solve_range):
        with pytest.raises(UnsupportedEconomicPolicy, match="solve_evidence_unavailable"):
            operation(inputs, "revenue_growth", 0, 0.08)
        assert not engine.sampled
        with pytest.raises(UnsupportedEconomicPolicy, match="solve_evidence_unavailable"):
            operation(inputs, "revenue_growth", 0, 0.08, eligibility=unverified)
        assert not engine.sampled


@pytest.mark.parametrize("operation", ["solve", "diagnose_solve_range"])
def test_supplied_eligibility_must_match_fixed_input_assumptions(operation):
    inputs = hand_inputs()
    metadata = _verified_metadata(inputs)
    changed_inputs = replace(
        inputs,
        assumptions=replace(inputs.assumptions, discount_rate=0.15),
    )

    with pytest.raises(ValueError, match="fixed assumptions do not match inputs"):
        getattr(ReverseDcfEngine(), operation)(
            changed_inputs,
            "revenue_growth",
            0,
            0.08,
            eligibility=metadata,
        )


@pytest.mark.parametrize("operation", ["solve", "diagnose_solve_range"])
@pytest.mark.parametrize(
    "invalid_metadata", ["empty_prerequisites", "missing_reference", "missing_fixed"]
)
def test_supplied_eligibility_must_be_complete(operation, invalid_metadata):
    inputs = hand_inputs()
    metadata = _verified_metadata(inputs)
    if invalid_metadata == "empty_prerequisites":
        metadata = replace(metadata, evidence_prerequisites=())
        expected_exception = ValueError
        expected_message = "prerequisites do not match"
    elif invalid_metadata == "missing_reference":
        prerequisites = tuple(dict(item) for item in metadata.evidence_prerequisites)
        prerequisites[0]["evidence_references"] = ()
        metadata = replace(metadata, evidence_prerequisites=prerequisites)
        expected_exception = UnsupportedEconomicPolicy
        expected_message = "solve_evidence_unavailable"
    else:
        fixed = dict(metadata.fixed_assumptions)
        fixed.pop("discount_rate")
        metadata = replace(metadata, fixed_assumptions=fixed)
        expected_exception = ValueError
        expected_message = "fixed assumptions are incomplete"

    with pytest.raises(expected_exception, match=expected_message):
        getattr(ReverseDcfEngine(), operation)(
            inputs,
            "revenue_growth",
            0,
            0.08,
            eligibility=metadata,
        )


def test_supported_growth_solves_initial_growth_and_keeps_mature_fade():
    engine = ReverseDcfEngine()
    inputs = replace(hand_inputs(), current_price=103.5)
    result = engine.solve(
        inputs,
        "revenue_growth",
        0,
        0.08,
        eligibility=_verified_metadata(inputs),
    )
    assert result.implied_assumption == pytest.approx(0.04)

    target_inputs = replace(
        inputs,
        current_price=engine.value(
            replace(inputs, assumptions=replace(inputs.assumptions, revenue_growth=0.08))
        ).value_per_share,
    )
    faded = engine.solve(
        target_inputs,
        "revenue_growth",
        0,
        0.12,
        eligibility=_verified_metadata(target_inputs),
    )
    assert faded.implied_assumption == pytest.approx(0.08)
    assert faded.valuation.projected_cash_flows[0].revenue_growth == pytest.approx(0.08)
    assert faded.valuation.projected_cash_flows[-1].revenue_growth == pytest.approx(0.04)


def test_unbracketed_endpoint_match_is_not_promoted_to_a_root():
    inputs = hand_inputs()
    metadata = _supported_metadata(inputs)
    endpoint = {
        "classification": "sampled_endpoint_match",
        "location": "lower_endpoint",
        "assumption": 0.0,
        "associated_sign_change_bracket_count": 0,
        "qualification": "sampled endpoint only",
    }
    diagnostics = {
        "sampled_match_regions": [],
        "sampled_match_points": [endpoint],
        "no_solution_direction": "not_established",
        "range_qualification": "sampled range only",
    }

    class EndpointEngine:
        def diagnose_solve_range(self, *args, **kwargs):
            return diagnostics, (), (endpoint,)

    result = _solve_axis_result(EndpointEngine(), inputs, "revenue_growth", metadata)
    assert result["solution_status"] == "sampled_match"
    assert result["candidate_roots"] == []
    assert result["sampled_match_points"][0]["classification"] == "sampled_endpoint_match"
    assert "implied_assumption" not in result
    assert _dcf_solve_status(result["solution_status"], None, None) is DcfResultStatus.SAMPLED_MATCH


def test_sampled_tangency_and_flat_tolerance_region_are_not_roots():
    inputs = hand_inputs()
    metadata = _supported_metadata(inputs)
    tangent = {
        "classification": "sampled_no_sign_change_match",
        "location": "interior",
        "assumption": 0.04,
        "associated_sign_change_bracket_count": 0,
        "qualification": "tangency or analytical exactness is not established",
    }
    diagnostics = {
        "sampled_match_regions": [],
        "sampled_match_points": [tangent],
        "no_solution_direction": "not_established",
        "range_qualification": "sampled range only",
    }

    class TangentEngine:
        def diagnose_solve_range(self, *args, **kwargs):
            return diagnostics, (), (tangent,)

    sampled_match = _solve_axis_result(TangentEngine(), inputs, "revenue_growth", metadata)
    assert sampled_match["solution_status"] == "sampled_match"
    assert sampled_match["candidate_roots"] == []
    assert "tangency" in sampled_match["sampled_match_points"][0]["qualification"]

    flat_diagnostics = {
        **diagnostics,
        "sampled_match_points": [],
        "sampled_match_regions": [
            {
                "classification": "contiguous_samples_within_tolerance",
                "qualification": "a continuous equivalence interval is not established",
            }
        ],
    }

    class FlatEngine:
        def diagnose_solve_range(self, *args, **kwargs):
            return flat_diagnostics, (), ()

    region = _solve_axis_result(FlatEngine(), inputs, "revenue_growth", metadata)
    assert region["solution_status"] == "sampled_match_region"
    assert region["candidate_roots"] == []
    assert "complete root set are not established" in region["solution_evidence"]


def test_candidate_roots_are_not_collapsed_to_a_unique_answer():
    inputs = hand_inputs()
    metadata = _supported_metadata(inputs)
    diagnostics = {
        "sampled_match_regions": [],
        "sampled_match_points": [],
        "no_solution_direction": "not_established",
        "range_qualification": "sampled range only",
    }
    value = ReverseDcfEngine().value(inputs)

    class MultipleEngine:
        def diagnose_solve_range(self, *args, **kwargs):
            return diagnostics, ((0.01, 0.02), (0.06, 0.07)), ()

        def solve(self, _inputs, _axis, lower, upper, **kwargs):
            assumption = (lower + upper) / 2
            return SimpleNamespace(
                implied_assumption=assumption,
                modeled_price=inputs.current_price,
                price_difference=0.0,
                iterations=1,
                valuation=value,
            )

    result = _solve_axis_result(MultipleEngine(), inputs, "revenue_growth", metadata)
    assert result["solution_status"] == "candidate_solutions"
    assert result["candidate_solution_count"] == 2
    assert len(result["candidate_roots"]) == 2
    assert "implied_assumption" not in result
    assert "value_per_share" not in result
    assert "does not establish uniqueness or completeness" in result["solution_qualification"]
    assert (
        _dcf_solve_status(result["solution_status"], None, None)
        is DcfResultStatus.CANDIDATE_SOLUTIONS
    )


@pytest.mark.parametrize(
    ("sampled_match_regions", "solution_status"),
    [
        ([], "candidate_solutions"),
        (
            [
                {
                    "classification": "contiguous_samples_within_tolerance",
                    "qualification": "a continuous equivalence interval is not established",
                }
            ],
            "sampled_match_region",
        ),
    ],
)
def test_exported_sampled_match_count_tracks_filtered_points(
    sampled_match_regions, solution_status
):
    inputs = hand_inputs()
    metadata = _supported_metadata(inputs)
    bracketed_match = {
        "classification": "sampled_match_with_sign_change",
        "assumption": 0.04,
        "associated_sign_change_bracket_count": 1,
    }
    diagnostics = {
        "sampled_match_regions": sampled_match_regions,
        "sampled_match_point_count": 1,
        "sampled_match_points": [bracketed_match],
        "no_solution_direction": "not_established",
    }
    value = ReverseDcfEngine().value(inputs)

    class BracketedMatchEngine:
        def diagnose_solve_range(self, *args, **kwargs):
            return diagnostics, ((0.03, 0.05),), (bracketed_match,)

        def solve(self, *args, **kwargs):
            return SimpleNamespace(
                implied_assumption=0.04,
                modeled_price=inputs.current_price,
                price_difference=0.0,
                iterations=1,
                valuation=value,
            )

    result = _solve_axis_result(BracketedMatchEngine(), inputs, "revenue_growth", metadata)

    assert result["solution_status"] == solution_status
    assert result["candidate_solution_count"] == 1
    assert result["sampled_match_points"] == []
    assert result["sampled_match_point_count"] == len(result["sampled_match_points"])


def test_no_crossing_nonconvergence_and_invalid_candidate_remain_distinct():
    inputs = hand_inputs()
    metadata = _supported_metadata(inputs)
    no_crossing_diagnostics = {
        "sampled_match_regions": [],
        "sampled_match_points": [],
        "no_solution_direction": "above",
        "range_qualification": "sampled range only",
    }

    class NoCrossingEngine:
        def diagnose_solve_range(self, *args, **kwargs):
            return no_crossing_diagnostics, (), ()

    no_crossing = _solve_axis_result(NoCrossingEngine(), inputs, "revenue_growth", metadata)
    assert (
        _dcf_solve_status(
            no_crossing["solution_status"], no_crossing.get("reason"), no_crossing.get("error")
        )
        is DcfResultStatus.NO_CROSSING
    )

    class NonconvergentEngine:
        def diagnose_solve_range(self, *args, **kwargs):
            return no_crossing_diagnostics, ((0.01, 0.02),), ()

        def solve(self, *args, **kwargs):
            raise RuntimeError("reverse DCF solver did not converge")

    nonconvergence = _solve_axis_result(NonconvergentEngine(), inputs, "revenue_growth", metadata)
    assert (
        _dcf_solve_status(
            nonconvergence["solution_status"],
            nonconvergence.get("reason"),
            nonconvergence.get("error"),
        )
        is DcfResultStatus.NONCONVERGENCE
    )
    assert nonconvergence["candidate_roots"] == []

    class InvalidCandidateEngine:
        def diagnose_solve_range(self, *args, **kwargs):
            raise UnsupportedEconomicPolicy("unsupported_capital_release")

    invalid = _solve_axis_result(InvalidCandidateEngine(), inputs, "revenue_growth", metadata)
    assert invalid["reason"] == "invalid_candidate_economics"
    assert (
        _dcf_solve_status(invalid["solution_status"], invalid.get("reason"), invalid.get("error"))
        is DcfResultStatus.DOMAIN_UNAVAILABLE
    )
    assert invalid["candidate_roots"] == []
