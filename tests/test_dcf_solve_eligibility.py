from dataclasses import replace
from types import SimpleNamespace

import pytest
from dcf_calibration_fixtures import reviewed_growth_inputs
from test_forward_reinvestment import hand_inputs

from alphaforge.cli.ranking_loader import _dcf_solve_status, _solve_axis_result
from alphaforge.core.valuation.dcf_contract import (
    FIXED_DEFAULT_ASSUMPTION_POLICY,
    DcfResultStatus,
)
from alphaforge.core.valuation.dcf_policy import DcfAssumptionPolicy
from alphaforge.core.valuation.reverse_dcf import (
    ReverseDcfEngine,
    UnsupportedEconomicPolicy,
)
from alphaforge.core.valuation.solve_eligibility import (
    SOLVE_AXIS_REGISTRY,
    SOLVE_REGISTRY_VERSION,
    fixed_assumption_provenance_complete,
    solve_axis_metadata,
)


def _test_assumption_provenance(inputs):
    origins = {
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
    }
    return {
        name: {
            "origin": origins[name],
            "source": (
                FIXED_DEFAULT_ASSUMPTION_POLICY[name][1]
                if origins[name] == "fixed_default"
                else "synthetic test assumption"
            ),
            "evidence_references": (
                ()
                if origins[name] == "fixed_default"
                else ({"source_id": f"synthetic:{name}", "anchor": name},)
            ),
            "limitations": ("synthetic test fixture assumption",),
        }
        for name in vars(inputs.assumptions)
    }


def _eligible_inputs():
    inputs = hand_inputs(years=5, terminal=0.02)
    return reviewed_growth_inputs(
        replace(inputs, assumptions=replace(inputs.assumptions, tax_rate=0.21))
    )


def _supported_metadata(inputs):
    return _verified_metadata(inputs)


def _verified_metadata(inputs, provenance=None):
    return solve_axis_metadata(
        "revenue_growth",
        assumptions=inputs.assumptions,
        assumption_provenance=provenance or _test_assumption_provenance(inputs),
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


def test_registry_is_the_explicit_domain_and_reason_owner():
    assert DcfAssumptionPolicy.SOLVE_BOUNDS == {
        axis: (definition.lower_bound, definition.upper_bound)
        for axis, definition in SOLVE_AXIS_REGISTRY.items()
    }
    assert DcfAssumptionPolicy.SOLVE_BOUNDS["revenue_growth"] == (0, 0.30)
    assert SOLVE_AXIS_REGISTRY["revenue_growth"].status == "supported"
    assert SOLVE_AXIS_REGISTRY["ebit_margin"].reason == "unavailable_constant_margin_only"
    assert SOLVE_AXIS_REGISTRY["terminal_growth"].status == "not_identifiable"
    assert SOLVE_REGISTRY_VERSION == "reverse-dcf-solve-registry-v3-admissible-growth"


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
        assumption_provenance=_test_assumption_provenance(inputs),
        inputs=inputs,
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
    inputs = _eligible_inputs()
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
def test_solve_eligibility_is_bound_to_company_and_frozen_inputs(operation):
    inputs = _eligible_inputs()
    metadata = _verified_metadata(inputs)
    engine = ReverseDcfEngine()
    stale_inputs = replace(inputs, current_price=inputs.current_price + 1)
    other_company = replace(inputs, eligibility_context_identity="company-b:packet-b")

    for changed_inputs in (stale_inputs, other_company):
        with pytest.raises(ValueError, match="input identity"):
            getattr(engine, operation)(
                changed_inputs,
                "revenue_growth",
                0,
                0.08,
                eligibility=metadata,
            )
    with pytest.raises(UnsupportedEconomicPolicy, match="eligibility_context_identity"):
        getattr(engine, operation)(
            replace(inputs, eligibility_context_identity=None),
            "revenue_growth",
            0,
            0.08,
            eligibility=metadata,
        )


@pytest.mark.parametrize("operation", ["solve", "diagnose_solve_range"])
@pytest.mark.parametrize(
    "provenance_changes",
    [
        {"origin": None},
        {"source": ""},
        {"origin": "market_evidence", "evidence_references": ()},
        {"origin": "fixed_default", "evidence_references": (), "limitations": ()},
        {
            "origin": "fixed_default",
            "evidence_references": (),
            "limitations": ("synthetic approved-default wording",),
        },
    ],
)
def test_fixed_assumption_provenance_must_follow_existing_contract(operation, provenance_changes):
    inputs = _eligible_inputs()
    metadata = _verified_metadata(inputs)
    fixed = {name: dict(record) for name, record in metadata.fixed_assumptions.items()}
    fixed["discount_rate"].update(provenance_changes)
    metadata = replace(metadata, fixed_assumptions=fixed)

    with pytest.raises(UnsupportedEconomicPolicy, match="fixed_assumptions_with_provenance"):
        getattr(ReverseDcfEngine(), operation)(
            inputs,
            "revenue_growth",
            0,
            0.08,
            eligibility=metadata,
        )


@pytest.mark.parametrize("operation", ["solve", "diagnose_solve_range"])
@pytest.mark.parametrize(
    ("field", "unapproved_value"),
    [
        ("projection_years", 2),
        ("tax_rate", 0.0),
        ("terminal_growth", 0.04),
        ("net_reinvestment_rate", 0.01),
        ("revenue_growth_fade_to", 0.04),
        ("economic_convention", "legacy-capped-revenue-growth-v13"),
    ],
)
def test_solve_rejects_unapproved_fixed_default_values(operation, field, unapproved_value):
    inputs = _eligible_inputs()
    inputs = replace(
        inputs,
        assumptions=replace(inputs.assumptions, **{field: unapproved_value}),
    )
    metadata = _verified_metadata(inputs)

    with pytest.raises(
        UnsupportedEconomicPolicy,
        match="fixed_assumption_provenance_incomplete",
    ):
        getattr(ReverseDcfEngine(), operation)(
            inputs,
            "revenue_growth",
            0,
            0.08,
            eligibility=metadata,
        )


@pytest.mark.parametrize("operation", ["solve", "diagnose_solve_range"])
def test_solve_rejects_unapproved_fixed_default_source(operation):
    inputs = _eligible_inputs()
    provenance = _test_assumption_provenance(inputs)
    provenance["tax_rate"] = {
        **provenance["tax_rate"],
        "source": "synthetic replacement default",
    }
    metadata = _verified_metadata(inputs, provenance)

    with pytest.raises(
        UnsupportedEconomicPolicy,
        match="fixed_assumption_provenance_incomplete",
    ):
        getattr(ReverseDcfEngine(), operation)(
            inputs,
            "revenue_growth",
            0,
            0.08,
            eligibility=metadata,
        )


@pytest.mark.parametrize("field", FIXED_DEFAULT_ASSUMPTION_POLICY)
def test_every_fixed_default_is_bound_to_policy_value_and_source(field):
    value, source = FIXED_DEFAULT_ASSUMPTION_POLICY[field]
    provenance = {
        field: {
            "origin": "fixed_default",
            "source": source,
            "evidence_references": (),
            "limitations": ("policy default test fixture",),
        }
    }
    assert fixed_assumption_provenance_complete({field: value}, None, provenance)

    other_value = f"{value}-other" if isinstance(value, str) else value + 1
    assert not fixed_assumption_provenance_complete({field: other_value}, None, provenance)
    provenance[field]["source"] = "replacement default source"
    assert not fixed_assumption_provenance_complete({field: value}, None, provenance)


@pytest.mark.parametrize("operation", ["solve", "diagnose_solve_range"])
@pytest.mark.parametrize(
    "domain_change",
    [
        {"bounds_inclusive": False},
        {"candidate_policy": "caller-selected candidate policy"},
        {"caller_extension": True},
    ],
)
def test_complete_exported_eligibility_record_is_identity_bound(operation, domain_change):
    inputs = _eligible_inputs()
    metadata = _verified_metadata(inputs)
    metadata = replace(metadata, domain={**metadata.domain, **domain_change})

    with pytest.raises(ValueError, match="input identity"):
        getattr(ReverseDcfEngine(), operation)(
            inputs,
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
    inputs = _eligible_inputs()
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
    inputs = _eligible_inputs()
    inputs = replace(inputs, current_price=engine.value(inputs).value_per_share)
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
    assert faded.valuation.projected_cash_flows[-1].revenue_growth == pytest.approx(
        target_inputs.assumptions.revenue_growth_fade_to
    )


def test_unbracketed_endpoint_match_is_not_promoted_to_a_root():
    inputs = _eligible_inputs()
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
    inputs = _eligible_inputs()
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
    inputs = _eligible_inputs()
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
            "candidate_solutions",
        ),
    ],
)
def test_exported_sampled_match_count_tracks_filtered_points(
    sampled_match_regions, solution_status
):
    inputs = _eligible_inputs()
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
    assert result["sampled_match_regions"] == sampled_match_regions
    if sampled_match_regions:
        assert "uniqueness or completeness" in result["solution_qualification"]


def test_no_crossing_nonconvergence_and_invalid_candidate_remain_distinct():
    inputs = _eligible_inputs()
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
