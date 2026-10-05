from dataclasses import replace

import pytest

from alphaforge.core.valuation.reinvestment import ECONOMIC_CONVENTION, LEGACY_CONVENTION
from alphaforge.core.valuation.reverse_dcf import (
    DcfAssumptions,
    ReverseDcfEngine,
    ReverseDcfInputs,
    UnsupportedEconomicPolicy,
    UnsupportedValuationModel,
)


def _inputs(
    *,
    ebit_margin: float = 0.20,
    ebit_margin_start: float | None = None,
    revenue_growth: float = 0.05,
    discount_rate: float = 0.10,
    reinvestment_return: float = 0.20,
):
    return ReverseDcfInputs(
        current_price=10.0,
        shares_outstanding=10.0,
        current_revenue=100.0,
        net_debt=5.0,
        assumptions=DcfAssumptions(
            economic_convention=LEGACY_CONVENTION,
            projection_years=5,
            revenue_growth=revenue_growth,
            ebit_margin=ebit_margin,
            tax_rate=0.21,
            discount_rate=discount_rate,
            terminal_growth=0.02,
            reinvestment_return=reinvestment_return,
            revenue_growth_fade_to=0.02,
            ebit_margin_start=ebit_margin_start,
        ),
    )


@pytest.mark.parametrize(
    ("ebit_margin", "ebit_margin_start"),
    [(-0.10, None), (0.20, -0.10)],
)
def test_value_rejects_negative_nopat_roic_reinvestment(ebit_margin, ebit_margin_start):
    with pytest.raises(UnsupportedValuationModel, match="negative NOPAT"):
        ReverseDcfEngine().value(
            _inputs(ebit_margin=ebit_margin, ebit_margin_start=ebit_margin_start)
        )


@pytest.mark.parametrize("convention", [ECONOMIC_CONVENTION, LEGACY_CONVENTION])
@pytest.mark.parametrize(
    ("assumption", "reason"),
    [
        ("ebit_margin", "unavailable_constant_margin_only"),
        ("terminal_growth", "not_identifiable"),
    ],
)
@pytest.mark.parametrize("operation", ["solve", "diagnose_solve_range"])
def test_solve_policy_applies_to_every_convention_and_operation(
    convention, assumption, reason, operation
):
    inputs = _inputs()
    inputs = replace(
        inputs,
        assumptions=replace(inputs.assumptions, economic_convention=convention),
    )

    with pytest.raises(UnsupportedEconomicPolicy, match=reason):
        getattr(ReverseDcfEngine(), operation)(inputs, assumption, -0.10, 0.30)


class _DiagnosticEngine(ReverseDcfEngine):
    @staticmethod
    def _solve_preflight(inputs, assumption, lower_bound, upper_bound, eligibility):
        # Exercise the numerical diagnostic independently of terminal-axis policy.
        if not lower_bound < upper_bound:
            raise ValueError("range bounds must be increasing")


def _target_at(inputs, assumption, value):
    engine = ReverseDcfEngine()
    candidate_inputs = replace(
        inputs,
        assumptions=replace(inputs.assumptions, **{assumption: value}),
    )
    target_price = engine.value(candidate_inputs).value_per_share
    return replace(inputs, current_price=target_price)


def test_range_diagnostics_classify_endpoint_match_without_crossing():
    diagnostics, brackets, matches = _DiagnosticEngine().diagnose_solve_range(
        _target_at(_inputs(), "terminal_growth", -0.01),
        "terminal_growth",
        -0.01,
        0.04,
    )

    assert brackets == ()
    assert len(matches) == 1
    assert matches[0]["classification"] == "sampled_endpoint_match"
    assert matches[0]["location"] == "lower_endpoint"
    assert matches[0]["associated_sign_change_bracket_count"] == 0
    assert "analytical exactness is not established" in matches[0]["qualification"]
    assert diagnostics["sign_change_bracket_count"] == 0
    assert diagnostics["sampled_match_regions"] == []


def test_range_diagnostics_classify_no_sign_change_match_without_crossing():
    inputs = _inputs(revenue_growth=0.0, discount_rate=0.15, reinvestment_return=0.02)
    diagnostics, brackets, matches = _DiagnosticEngine().diagnose_solve_range(
        _target_at(inputs, "terminal_growth", 0.0),
        "terminal_growth",
        -0.01,
        0.04,
    )

    assert brackets == ()
    assert len(matches) == 1
    assert matches[0]["classification"] == "sampled_no_sign_change_match"
    assert matches[0]["associated_sign_change_bracket_count"] == 0
    assert "tangency or analytical exactness is not established" in matches[0]["qualification"]
    assert diagnostics["sign_change_bracket_count"] == 0


def test_range_diagnostics_convert_sampled_straddle_to_crossing_bracket():
    diagnostics, brackets, matches = _DiagnosticEngine().diagnose_solve_range(
        _target_at(_inputs(), "terminal_growth", 0.01),
        "terminal_growth",
        -0.01,
        0.04,
    )

    assert len(brackets) == 1
    assert brackets[0][0] < 0.01 < brackets[0][1]
    assert len(matches) == 1
    assert matches[0]["classification"] == "sampled_match_with_sign_change"
    assert matches[0]["associated_sign_change_bracket_count"] == 1
    assert diagnostics["sign_change_bracket_count"] == 1
    assert diagnostics["sampled_match_regions"] == []


def test_range_diagnostics_preserve_crossing_across_tolerance_match_region():
    inputs = _target_at(_inputs(), "terminal_growth", 0.01)
    diagnostics, brackets, matches = _DiagnosticEngine().diagnose_solve_range(
        inputs,
        "terminal_growth",
        -0.01,
        0.04,
        price_tolerance=0.03,
    )

    assert len(brackets) == 1
    assert brackets[0][0] < 0.01 < brackets[0][1]
    assert matches == ()
    assert len(diagnostics["sampled_match_regions"]) == 1
    region = diagnostics["sampled_match_regions"][0]
    assert region["lower_sample_assumption"] < 0.01 < region["upper_sample_assumption"]
    assert region["associated_sign_change_bracket_count"] == 1
    result = _DiagnosticEngine().solve(inputs, "terminal_growth", *brackets[0])
    assert result.implied_assumption == pytest.approx(0.01)


def test_range_diagnostics_preserve_two_crossings_around_tolerance_match():
    inputs = _inputs(revenue_growth=0.0, discount_rate=0.15, reinvestment_return=0.02)
    center_target = _target_at(inputs, "terminal_growth", 0.0)
    inputs = replace(center_target, current_price=center_target.current_price - 0.5e-6)

    diagnostics, brackets, matches = _DiagnosticEngine().diagnose_solve_range(
        inputs,
        "terminal_growth",
        -0.01,
        0.04,
    )

    assert len(brackets) == 2
    assert brackets[0][1] == pytest.approx(0.0)
    assert brackets[1][0] == pytest.approx(0.0)
    assert len(matches) == 1
    assert matches[0]["classification"] == "sampled_match_with_sign_change"
    assert matches[0]["associated_sign_change_bracket_count"] == 2
    assert diagnostics["sign_change_bracket_count"] == 2
    assert diagnostics["sampled_match_point_count"] == 1
    left_result = _DiagnosticEngine().solve(inputs, "terminal_growth", *brackets[0])
    right_result = _DiagnosticEngine().solve(inputs, "terminal_growth", *brackets[1])
    assert left_result.implied_assumption < 0.0 < right_result.implied_assumption


def test_range_diagnostics_classify_contiguous_plateau_samples_as_region():
    inputs = _inputs(revenue_growth=0.0, discount_rate=0.15, reinvestment_return=0.02)
    diagnostics, brackets, matches = _DiagnosticEngine().diagnose_solve_range(
        _target_at(inputs, "terminal_growth", 0.02),
        "terminal_growth",
        -0.01,
        0.04,
    )

    assert brackets == ()
    assert matches == ()
    assert len(diagnostics["sampled_match_regions"]) == 1
    region = diagnostics["sampled_match_regions"][0]
    assert region["lower_sample_assumption"] == pytest.approx(0.02)
    assert region["upper_sample_assumption"] == pytest.approx(0.04)
    assert region["sample_count"] > 1
    assert region["associated_sign_change_bracket_count"] == 0
    assert "continuous equivalence interval is not established" in region["qualification"]


def test_range_diagnostics_do_not_turn_plateau_roundoff_into_a_crossing():
    assumptions = replace(
        _inputs(revenue_growth=0.0, discount_rate=0.15, reinvestment_return=0.02).assumptions,
        ebit_margin=24.2 / 121.0,
        ebit_margin_start=24.2 / 121.0,
    )
    inputs = replace(
        _inputs(revenue_growth=0.0, discount_rate=0.15, reinvestment_return=0.02),
        current_price=3.171574253715503,
        current_revenue=121.0,
        assumptions=assumptions,
    )

    diagnostics, brackets, matches = _DiagnosticEngine().diagnose_solve_range(
        inputs,
        "terminal_growth",
        -0.01,
        0.04,
    )

    assert brackets == ()
    assert matches == ()
    assert len(diagnostics["sampled_match_regions"]) == 1
    assert diagnostics["sampled_match_regions"][0]["associated_sign_change_bracket_count"] == 0
