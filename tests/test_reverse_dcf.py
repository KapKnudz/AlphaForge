import pytest

from alphaforge.core.valuation.reverse_dcf import (
    DcfAssumptions,
    ReverseDcfEngine,
    ReverseDcfInputs,
    UnsupportedValuationModel,
)


def _inputs(*, ebit_margin: float = 0.20, ebit_margin_start: float | None = None):
    return ReverseDcfInputs(
        current_price=10.0,
        shares_outstanding=10.0,
        current_revenue=100.0,
        net_debt=5.0,
        assumptions=DcfAssumptions(
            projection_years=5,
            revenue_growth=0.05,
            ebit_margin=ebit_margin,
            tax_rate=0.21,
            discount_rate=0.10,
            terminal_growth=0.02,
            reinvestment_return=0.20,
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
