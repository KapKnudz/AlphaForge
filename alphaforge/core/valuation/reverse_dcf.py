"""Deterministic reverse DCF for ordinary operating companies.

The engine owns the arithmetic. An agent or human may propose and critique
assumptions, but identical inputs always produce identical output here.
"""

from dataclasses import dataclass, replace
from math import isfinite
from typing import Literal

ImpliedAssumption = Literal["revenue_growth", "ebit_margin", "terminal_growth"]
_SUPPORTED_ASSUMPTIONS = {"revenue_growth", "ebit_margin", "terminal_growth"}
_BANK_BRANCH_IDS = {68, 69, 70}
_PROPERTY_BRANCH_ID = 75


class UnsupportedValuationModel(ValueError):
    """Raised when FCFF is inappropriate for the supplied company sector."""


@dataclass(frozen=True)
class DcfAssumptions:
    projection_years: int
    revenue_growth: float
    ebit_margin: float
    tax_rate: float
    discount_rate: float
    terminal_growth: float
    net_reinvestment_rate: float = 0.0
    reinvestment_return: float | None = None
    revenue_growth_fade_to: float | None = None
    ebit_margin_start: float | None = None


@dataclass(frozen=True)
class ReverseDcfInputs:
    current_price: float
    shares_outstanding: float
    current_revenue: float
    net_debt: float
    assumptions: DcfAssumptions
    branch_id: int | None = None


@dataclass(frozen=True)
class ProjectedCashFlow:
    year: int
    revenue_growth: float
    revenue: float
    ebit_margin: float
    ebit: float
    nopat: float
    fcff: float
    discounted_fcff: float


@dataclass(frozen=True)
class DcfValue:
    enterprise_value: float
    equity_value: float
    value_per_share: float
    terminal_value: float
    discounted_terminal_value: float
    projected_cash_flows: tuple[ProjectedCashFlow, ...]


@dataclass(frozen=True)
class ReverseDcfResult:
    solved_assumption: ImpliedAssumption
    implied_assumption: float
    lower_bound: float
    upper_bound: float
    target_price: float
    modeled_price: float
    price_difference: float
    iterations: int
    valuation: DcfValue


class ReverseDcfEngine:
    """Value FCFF and solve one market-implied assumption with bisection."""

    def value(self, inputs: ReverseDcfInputs) -> DcfValue:
        self._validate(inputs)
        assumptions = inputs.assumptions
        revenue = inputs.current_revenue
        projected: list[ProjectedCashFlow] = []
        present_value = 0.0

        for year in range(1, assumptions.projection_years + 1):
            revenue_growth = self._fade(
                assumptions.revenue_growth,
                assumptions.revenue_growth_fade_to,
                year,
                assumptions.projection_years,
            )
            ebit_margin = self._fade(
                assumptions.ebit_margin_start,
                assumptions.ebit_margin,
                year,
                assumptions.projection_years,
            )
            revenue *= 1.0 + revenue_growth
            ebit = revenue * ebit_margin
            nopat = ebit * (1.0 - assumptions.tax_rate)
            fcff = self._fcff(revenue, nopat, assumptions, revenue_growth)
            discounted_fcff = fcff / (1.0 + assumptions.discount_rate) ** year
            present_value += discounted_fcff
            projected.append(
                ProjectedCashFlow(
                    year=year,
                    revenue_growth=revenue_growth,
                    revenue=revenue,
                    ebit_margin=ebit_margin,
                    ebit=ebit,
                    nopat=nopat,
                    fcff=fcff,
                    discounted_fcff=discounted_fcff,
                )
            )

        terminal_revenue = revenue * (1.0 + assumptions.terminal_growth)
        terminal_ebit = terminal_revenue * assumptions.ebit_margin
        terminal_nopat = terminal_ebit * (1.0 - assumptions.tax_rate)
        terminal_fcff = self._terminal_fcff(
            terminal_revenue,
            terminal_nopat,
            assumptions,
        )
        terminal_value = terminal_fcff / (assumptions.discount_rate - assumptions.terminal_growth)
        discounted_terminal_value = (
            terminal_value / (1.0 + assumptions.discount_rate) ** assumptions.projection_years
        )
        enterprise_value = present_value + discounted_terminal_value
        equity_value = enterprise_value - inputs.net_debt

        return DcfValue(
            enterprise_value=enterprise_value,
            equity_value=equity_value,
            value_per_share=equity_value / inputs.shares_outstanding,
            terminal_value=terminal_value,
            discounted_terminal_value=discounted_terminal_value,
            projected_cash_flows=tuple(projected),
        )

    def solve(
        self,
        inputs: ReverseDcfInputs,
        assumption: ImpliedAssumption,
        lower_bound: float,
        upper_bound: float,
        *,
        price_tolerance: float = 1e-6,
        assumption_tolerance: float = 1e-10,
        max_iterations: int = 200,
    ) -> ReverseDcfResult:
        if assumption not in _SUPPORTED_ASSUMPTIONS:
            raise ValueError(f"unsupported implied assumption: {assumption}")
        if not lower_bound < upper_bound:
            raise ValueError("lower_bound must be less than upper_bound")
        if price_tolerance <= 0 or assumption_tolerance <= 0 or max_iterations <= 0:
            raise ValueError("solver tolerances and max_iterations must be positive")

        supplied_lower_bound = lower_bound
        supplied_upper_bound = upper_bound
        lower_value = self._value_with(inputs, assumption, lower_bound)
        upper_value = self._value_with(inputs, assumption, upper_bound)
        lower_difference = lower_value.value_per_share - inputs.current_price
        upper_difference = upper_value.value_per_share - inputs.current_price

        if lower_difference == 0.0:
            return self._result(
                inputs, assumption, lower_bound, lower_bound, upper_bound, 0, lower_value
            )
        if upper_difference == 0.0:
            return self._result(
                inputs, assumption, upper_bound, lower_bound, upper_bound, 0, upper_value
            )
        if lower_difference * upper_difference > 0:
            if abs(lower_difference) <= price_tolerance:
                return self._result(
                    inputs, assumption, lower_bound, lower_bound, upper_bound, 0, lower_value
                )
            if abs(upper_difference) <= price_tolerance:
                return self._result(
                    inputs, assumption, upper_bound, lower_bound, upper_bound, 0, upper_value
                )
            raise ValueError(
                "current price is not bracketed by modeled prices at the supplied bounds"
            )

        valuation = lower_value
        implied = lower_bound
        for iteration in range(1, max_iterations + 1):
            implied = (lower_bound + upper_bound) / 2.0
            valuation = self._value_with(inputs, assumption, implied)
            difference = valuation.value_per_share - inputs.current_price
            if (
                abs(difference) <= price_tolerance
                or upper_bound - lower_bound <= assumption_tolerance
            ):
                return self._result(
                    inputs,
                    assumption,
                    implied,
                    supplied_lower_bound,
                    supplied_upper_bound,
                    iteration,
                    valuation,
                )
            if lower_difference * difference <= 0:
                upper_bound = implied
            else:
                lower_bound = implied
                lower_difference = difference

        raise RuntimeError("reverse DCF solver did not converge")

    @staticmethod
    def _fade(
        start: float | None,
        end: float | None,
        year: int,
        projection_years: int,
    ) -> float:
        if start is None:
            if end is None:
                raise ValueError("fade path requires at least one endpoint")
            return end
        if end is None or projection_years == 1:
            return start
        progress = (year - 1) / (projection_years - 1)
        return start + (end - start) * progress

    @staticmethod
    def _fcff(
        revenue: float,
        nopat: float,
        assumptions: DcfAssumptions,
        revenue_growth: float | None = None,
    ) -> float:
        if assumptions.reinvestment_return is not None:
            reinvestment_share_of_nopat = min(
                max(
                    assumptions.revenue_growth if revenue_growth is None else revenue_growth,
                    0.0,
                )
                / assumptions.reinvestment_return,
                1.0,
            )
            return nopat * (1.0 - reinvestment_share_of_nopat)
        return nopat - revenue * assumptions.net_reinvestment_rate

    @staticmethod
    def _terminal_fcff(
        revenue: float,
        nopat: float,
        assumptions: DcfAssumptions,
    ) -> float:
        if assumptions.reinvestment_return is None:
            return ReverseDcfEngine._fcff(revenue, nopat, assumptions)
        reinvestment_share_of_nopat = min(
            max(assumptions.terminal_growth, 0.0) / assumptions.reinvestment_return,
            1.0,
        )
        return nopat * (1.0 - reinvestment_share_of_nopat)

    def diagnose_solve_range(
        self,
        inputs: ReverseDcfInputs,
        assumption: ImpliedAssumption,
        lower_bound: float,
        upper_bound: float,
        *,
        sample_intervals: int = 200,
        price_tolerance: float = 1e-6,
    ) -> tuple[dict, tuple[tuple[float, float], ...], tuple[dict, ...]]:
        """Report endpoint and sampled range diagnostics without assuming monotonicity."""
        if assumption not in _SUPPORTED_ASSUMPTIONS:
            raise ValueError(f"unsupported implied assumption: {assumption}")
        if not lower_bound < upper_bound:
            raise ValueError("lower_bound must be less than upper_bound")
        if sample_intervals <= 0 or price_tolerance <= 0:
            raise ValueError("range diagnostic settings must be positive")

        points = tuple(
            lower_bound + (upper_bound - lower_bound) * index / sample_intervals
            for index in range(sample_intervals + 1)
        )
        prices = tuple(
            self._value_with(inputs, assumption, point).value_per_share for point in points
        )
        if not all(isfinite(price) for price in prices):
            raise ValueError("reverse DCF range diagnostic produced a non-finite price")
        differences = tuple(price - inputs.current_price for price in prices)
        matches = tuple(abs(difference) <= price_tolerance for difference in differences)
        signed_residual_indexes = [
            index
            for index, difference in enumerate(differences)
            if abs(difference) > 1e-12 * max(1.0, abs(prices[index]), abs(inputs.current_price))
        ]
        brackets = [
            (points[left], points[right])
            for left, right in zip(
                signed_residual_indexes,
                signed_residual_indexes[1:],
                strict=False,
            )
            if differences[left] * differences[right] < 0
        ]
        sampled_match_points = []
        sampled_match_regions = []
        index = 0
        while index <= sample_intervals:
            if not matches[index]:
                index += 1
                continue
            start = index
            while index < sample_intervals and matches[index + 1]:
                index += 1
            end = index
            if start != end:
                sampled_match_regions.append(
                    {
                        "classification": "contiguous_samples_within_tolerance",
                        "lower_sample_assumption": points[start],
                        "upper_sample_assumption": points[end],
                        "sample_count": end - start + 1,
                        "maximum_absolute_price_difference": max(
                            abs(differences[match_index]) for match_index in range(start, end + 1)
                        ),
                        "qualification": (
                            "contiguous grid samples match within price tolerance; "
                            "a continuous equivalence interval is not established"
                        ),
                    }
                )
            elif start == 0 or start == sample_intervals:
                sampled_match_points.append(
                    {
                        "classification": "sampled_endpoint_match",
                        "location": "lower_endpoint" if start == 0 else "upper_endpoint",
                        "assumption": points[start],
                        "modeled_price": prices[start],
                        "price_difference": differences[start],
                        "qualification": (
                            "endpoint sample matches within price tolerance; "
                            "analytical exactness is not established"
                        ),
                    }
                )
            else:
                sign_change = any(lower <= points[start] <= upper for lower, upper in brackets)
                sampled_match_points.append(
                    {
                        "classification": (
                            "sampled_match_with_sign_change"
                            if sign_change
                            else "sampled_no_sign_change_match"
                        ),
                        "location": "interior",
                        "assumption": points[start],
                        "modeled_price": prices[start],
                        "price_difference": differences[start],
                        "qualification": (
                            "sample matches within price tolerance alongside sampled sign-change "
                            "evidence; analytical exactness at the sample is not established"
                            if sign_change
                            else "isolated sample matches within price tolerance without a sampled "
                            "sign change; tangency or analytical exactness is not established"
                        ),
                    }
                )
            index += 1

        brackets.sort()
        for match in sampled_match_points:
            match["associated_sign_change_bracket_count"] = sum(
                lower <= match["assumption"] <= upper for lower, upper in brackets
            )
        for region in sampled_match_regions:
            region["associated_sign_change_bracket_count"] = sum(
                lower <= region["upper_sample_assumption"]
                and upper >= region["lower_sample_assumption"]
                for lower, upper in brackets
            )

        changes = []
        for left, right in zip(prices, prices[1:], strict=False):
            tolerance = 1e-12 * max(1.0, abs(left), abs(right))
            changes.append(1 if right - left > tolerance else -1 if left - right > tolerance else 0)
        observed_changes = [change for change in changes if change]
        if not observed_changes:
            monotonicity = "sampled_flat"
        elif all(change > 0 for change in observed_changes):
            monotonicity = "sampled_increasing"
        elif all(change < 0 for change in observed_changes):
            monotonicity = "sampled_decreasing"
        else:
            monotonicity = "sampled_non_monotonic"

        extrema = []
        for index in range(1, sample_intervals):
            if prices[index] > prices[index - 1] and prices[index] > prices[index + 1]:
                extrema.append(
                    {"type": "local_maximum", "assumption": points[index], "price": prices[index]}
                )
            elif prices[index] < prices[index - 1] and prices[index] < prices[index + 1]:
                extrema.append(
                    {"type": "local_minimum", "assumption": points[index], "price": prices[index]}
                )

        minimum_index = min(range(len(prices)), key=prices.__getitem__)
        maximum_index = max(range(len(prices)), key=prices.__getitem__)
        minimum_price, maximum_price = prices[minimum_index], prices[maximum_index]
        target = inputs.current_price
        if target < minimum_price - price_tolerance:
            target_position = "below_sampled_range"
            no_solution_direction = "below"
            nearest_boundary = "sampled_minimum"
            nearest_boundary_price = minimum_price
        elif target > maximum_price + price_tolerance:
            target_position = "above_sampled_range"
            no_solution_direction = "above"
            nearest_boundary = "sampled_maximum"
            nearest_boundary_price = maximum_price
        else:
            target_position = "within_sampled_range"
            no_solution_direction = "not_established"
            lower_gap = target - minimum_price
            upper_gap = maximum_price - target
            if lower_gap <= upper_gap:
                nearest_boundary = "sampled_minimum"
                nearest_boundary_price = minimum_price
            else:
                nearest_boundary = "sampled_maximum"
                nearest_boundary_price = maximum_price
        boundary_gap = abs(target - nearest_boundary_price)
        diagnostics = {
            "lower_bound": lower_bound,
            "upper_bound": upper_bound,
            "lower_endpoint_price": prices[0],
            "upper_endpoint_price": prices[-1],
            "target_price": target,
            "target_position": target_position,
            "no_solution_direction": no_solution_direction,
            "sampled_minimum_price": minimum_price,
            "sampled_minimum_assumption": points[minimum_index],
            "sampled_maximum_price": maximum_price,
            "sampled_maximum_assumption": points[maximum_index],
            "nearest_boundary": nearest_boundary,
            "nearest_boundary_price": nearest_boundary_price,
            "nearest_boundary_gap": boundary_gap,
            "nearest_boundary_gap_denominator": "target_price",
            "nearest_boundary_gap_denominator_value": target,
            "nearest_boundary_gap_pct_target": boundary_gap / target * 100.0,
            "monotonicity": monotonicity,
            "interior_extrema": extrema,
            "diagnostic_grid_points": sample_intervals + 1,
            "price_tolerance": price_tolerance,
            "sign_change_bracket_count": len(brackets),
            "sampled_match_point_count": len(sampled_match_points),
            "sampled_match_points": sampled_match_points,
            "sampled_match_regions": sampled_match_regions,
            "range_qualification": (
                "sampled range only; extrema or roots between grid points are not excluded"
            ),
        }
        return diagnostics, tuple(brackets), tuple(sampled_match_points)

    def _value_with(
        self,
        inputs: ReverseDcfInputs,
        assumption: ImpliedAssumption,
        value: float,
    ) -> DcfValue:
        assumptions = replace(inputs.assumptions, **{assumption: value})
        return self.value(replace(inputs, assumptions=assumptions))

    @staticmethod
    def _result(
        inputs: ReverseDcfInputs,
        assumption: ImpliedAssumption,
        implied: float,
        lower_bound: float,
        upper_bound: float,
        iterations: int,
        valuation: DcfValue,
    ) -> ReverseDcfResult:
        return ReverseDcfResult(
            solved_assumption=assumption,
            implied_assumption=implied,
            lower_bound=lower_bound,
            upper_bound=upper_bound,
            target_price=inputs.current_price,
            modeled_price=valuation.value_per_share,
            price_difference=valuation.value_per_share - inputs.current_price,
            iterations=iterations,
            valuation=valuation,
        )

    @staticmethod
    def _validate(inputs: ReverseDcfInputs) -> None:
        if inputs.branch_id in _BANK_BRANCH_IDS:
            raise UnsupportedValuationModel(
                "banks require a residual-income or dividend model, not FCFF"
            )
        if inputs.branch_id == _PROPERTY_BRANCH_ID:
            raise UnsupportedValuationModel(
                "property companies require a NAV/FFO-oriented model, not FCFF"
            )

        numeric_inputs = (
            inputs.current_price,
            inputs.shares_outstanding,
            inputs.current_revenue,
            inputs.net_debt,
        )
        assumptions = inputs.assumptions
        numeric_assumptions = (
            assumptions.revenue_growth,
            assumptions.ebit_margin,
            assumptions.tax_rate,
            assumptions.discount_rate,
            assumptions.terminal_growth,
            assumptions.net_reinvestment_rate,
        )
        if assumptions.reinvestment_return is not None:
            numeric_assumptions += (assumptions.reinvestment_return,)
        if assumptions.revenue_growth_fade_to is not None:
            numeric_assumptions += (assumptions.revenue_growth_fade_to,)
        if assumptions.ebit_margin_start is not None:
            numeric_assumptions += (assumptions.ebit_margin_start,)
        if not all(isfinite(value) for value in numeric_inputs + numeric_assumptions):
            raise ValueError("all DCF inputs must be finite")
        if inputs.current_price <= 0:
            raise ValueError("current_price must be positive")
        if inputs.shares_outstanding <= 0:
            raise ValueError("shares_outstanding must be positive")
        if inputs.current_revenue <= 0:
            raise ValueError("current_revenue must be positive")
        if not isinstance(assumptions.projection_years, int) or assumptions.projection_years <= 0:
            raise ValueError("projection_years must be a positive integer")
        if assumptions.revenue_growth <= -1.0:
            raise ValueError("revenue_growth must exceed -1")
        if (
            assumptions.revenue_growth_fade_to is not None
            and assumptions.revenue_growth_fade_to <= -1.0
        ):
            raise ValueError("revenue_growth_fade_to must exceed -1")
        if not 0.0 <= assumptions.tax_rate <= 1.0:
            raise ValueError("tax_rate must be between 0 and 1")
        if assumptions.discount_rate <= assumptions.terminal_growth:
            raise ValueError("discount_rate must exceed terminal_growth")
        if assumptions.discount_rate <= -1.0:
            raise ValueError("discount_rate must exceed -1")
        if assumptions.terminal_growth <= -1.0:
            raise ValueError("terminal_growth must exceed -1")
        if assumptions.reinvestment_return is not None and assumptions.reinvestment_return <= 0.0:
            raise ValueError("reinvestment_return must be positive when supplied")
        if assumptions.reinvestment_return is not None and (
            assumptions.ebit_margin < 0.0
            or (assumptions.ebit_margin_start is not None and assumptions.ebit_margin_start < 0.0)
        ):
            raise UnsupportedValuationModel(
                "ROIC-based reinvestment is unsupported for negative NOPAT"
            )
