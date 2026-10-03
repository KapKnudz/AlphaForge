from dataclasses import replace

import pytest

from alphaforge.core.valuation.reverse_dcf import (
    DcfAssumptions,
    ReverseDcfEngine,
    ReverseDcfInputs,
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


def test_solve_rejects_negative_ebit_margin_candidates():
    with pytest.raises(UnsupportedValuationModel, match="negative NOPAT"):
        ReverseDcfEngine().solve(_inputs(), "ebit_margin", -0.10, 0.30)


def test_range_diagnostics_reject_negative_ebit_margin_candidates():
    with pytest.raises(UnsupportedValuationModel, match="negative NOPAT"):
        ReverseDcfEngine().diagnose_solve_range(
            _inputs(), "ebit_margin", -0.10, 0.30, sample_intervals=4
        )


def _target_at(inputs, assumption, value):
    engine = ReverseDcfEngine()
    candidate_inputs = replace(
        inputs,
        assumptions=replace(inputs.assumptions, **{assumption: value}),
    )
    target_price = engine.value(candidate_inputs).value_per_share
    return replace(inputs, current_price=target_price)


def test_range_diagnostics_classify_endpoint_match_without_crossing():
    diagnostics, brackets, matches = ReverseDcfEngine().diagnose_solve_range(
        _target_at(_inputs(), "terminal_growth", -0.01),
        "terminal_growth",
        -0.01,
        0.04,
    )

    assert brackets == ()
    assert len(matches) == 1
    assert matches[0]["classification"] == "sampled_endpoint_match"
    assert matches[0]["location"] == "lower_endpoint"
    assert "analytical exactness is not established" in matches[0]["qualification"]
    assert diagnostics["sign_change_bracket_count"] == 0
    assert diagnostics["sampled_match_regions"] == []


def test_range_diagnostics_classify_no_sign_change_match_without_crossing():
    inputs = _inputs(revenue_growth=0.0, discount_rate=0.15, reinvestment_return=0.02)
    diagnostics, brackets, matches = ReverseDcfEngine().diagnose_solve_range(
        _target_at(inputs, "terminal_growth", 0.0),
        "terminal_growth",
        -0.01,
        0.04,
    )

    assert brackets == ()
    assert len(matches) == 1
    assert matches[0]["classification"] == "sampled_no_sign_change_match"
    assert "tangency or analytical exactness is not established" in matches[0][
        "qualification"
    ]
    assert diagnostics["sign_change_bracket_count"] == 0


def test_range_diagnostics_convert_sampled_straddle_to_crossing_bracket():
    diagnostics, brackets, matches = ReverseDcfEngine().diagnose_solve_range(
        _target_at(_inputs(), "terminal_growth", 0.01),
        "terminal_growth",
        -0.01,
        0.04,
    )

    assert len(brackets) == 1
    assert brackets[0][0] < 0.01 < brackets[0][1]
    assert matches == ()
    assert diagnostics["sign_change_bracket_count"] == 1
    assert diagnostics["sampled_match_regions"] == []


def test_range_diagnostics_classify_contiguous_plateau_samples_as_region():
    inputs = _inputs(revenue_growth=0.0, discount_rate=0.15, reinvestment_return=0.02)
    diagnostics, brackets, matches = ReverseDcfEngine().diagnose_solve_range(
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
    assert "continuous equivalence interval is not established" in region["qualification"]
