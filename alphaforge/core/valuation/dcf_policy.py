"""Explicit, deterministic assumptions for the first reverse-DCF policy."""

from dataclasses import dataclass, replace
from datetime import date
from math import isfinite
from statistics import mean, pstdev
from typing import Literal

from alphaforge.core.statistics import cagr
from alphaforge.core.types import Report
from alphaforge.core.valuation.required_return import (
    RequiredReturnDecision,
    RequiredReturnPolicy,
)
from alphaforge.core.valuation.reverse_dcf import DcfAssumptions


@dataclass(frozen=True)
class NormalizationWindow:
    years: int
    start_year: int | None
    end_year: int | None
    ebit_margin: float
    reported_fcf_margin: float | None
    operating_cash_flow_margin: float | None


@dataclass(frozen=True)
class NormalizationDiagnostics:
    confidence: Literal["low", "medium", "high"]
    selected_window_years: int
    three_year: NormalizationWindow | None
    five_year: NormalizationWindow | None
    annual_fcf_margin_stddev: float | None
    annual_fcf_margin_range: float | None
    negative_fcf_years: int
    fcf_sign_changes: int
    highly_volatile_fcf: bool
    material_window_disagreement: bool
    material_aggregate_investing: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class HistoricalOperatingYear:
    year: int | None
    revenue_growth: float | None
    ebit_margin: float


@dataclass(frozen=True)
class HistoricalOperatingBenchmarks:
    annuals: tuple[HistoricalOperatingYear, ...]
    observed_period_count: int
    three_year_revenue_cagr: float | None
    five_year_revenue_cagr: float | None
    three_year_average_ebit_margin: float | None
    five_year_average_ebit_margin: float | None
    periods_below_three_year_margin: int | None
    periods_below_five_year_margin: int | None
    peak_ebit_margin: float
    peak_ebit_margin_year: int | None
    trough_ebit_margin: float
    trough_ebit_margin_year: int | None


@dataclass(frozen=True)
class DcfPolicyDecision:
    available: bool
    policy_version: str
    assumptions: DcfAssumptions | None
    solve_bounds: dict[str, tuple[float, float]]
    assumption_sources: dict[str, str]
    normalized_fcf_margin: float | None = None
    normalization: NormalizationDiagnostics | None = None
    reinvestment_roic: float | None = None
    required_return: RequiredReturnDecision | None = None
    missing_information: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class DcfAssumptionPolicy:
    """Build auditable FCFF assumptions only from stored company evidence."""

    VERSION = "reverse-dcf-v11-market-cap-hurdle"
    PROJECTION_YEARS = 5
    TAX_RATE = 0.21
    TERMINAL_GROWTH = 0.02
    GROWTH_RANGE = (-0.05, 0.15)
    NET_REINVESTMENT_RANGE = (-0.05, 0.15)
    FCF_MARGIN_VOLATILITY = 0.10
    FCF_MARGIN_RANGE = 0.25
    WINDOW_DISAGREEMENT = 0.05
    MATERIAL_INVESTING_MARGIN = 0.15
    EXTREME_INVESTING_YEAR = 0.30
    SOLVE_BOUNDS = {
        "revenue_growth": (-0.10, 0.30),
        "ebit_margin": (0.0, 0.50),
        "terminal_growth": (-0.01, 0.04),
    }

    def __init__(self, required_return_policy: RequiredReturnPolicy | None = None):
        self.required_return_policy = required_return_policy or RequiredReturnPolicy()

    def build(
        self,
        current_report: Report | None,
        latest_annual_report: Report | None,
        historical_annual_reports: list[Report],
        *,
        as_of: date | None = None,
        currency: str | None = "SEK",
        market_cap: float | None = None,
        roic: float | None = None,
    ) -> DcfPolicyDecision:
        missing = self._missing_operating_inputs(current_report)
        if missing:
            return DcfPolicyDecision(
                available=False,
                policy_version=self.VERSION,
                assumptions=None,
                solve_bounds=dict(self.SOLVE_BOUNDS),
                assumption_sources={},
                missing_information=tuple(missing),
            )

        required_return = self.required_return_policy.build(
            as_of=as_of or date.today(),
            currency=currency,
            market_cap=market_cap,
        )
        if not required_return.available:
            return DcfPolicyDecision(
                available=False,
                policy_version=self.VERSION,
                assumptions=None,
                solve_bounds=dict(self.SOLVE_BOUNDS),
                assumption_sources={},
                required_return=required_return,
                missing_information=required_return.missing_information,
            )

        growth = self._historical_revenue_growth(
            latest_annual_report,
            historical_annual_reports,
        )
        warnings: list[str] = list(required_return.warnings)
        if growth is None:
            growth = 0.0
            growth_source = "zero-growth fallback; historical revenue growth unavailable"
            warnings.append("historical revenue growth unavailable")
        else:
            raw_growth = growth
            growth = self._clamp(growth, *self.GROWTH_RANGE)
            growth_source = "annual revenue CAGR over up to three years"
            if growth != raw_growth:
                warnings.append(f"revenue growth clamped from {raw_growth:.4f} to {growth:.4f}")

        economics = self._normalized_operating_economics(
            current_report,
            latest_annual_report,
            historical_annual_reports,
        )
        if economics is None:
            return DcfPolicyDecision(
                available=False,
                policy_version=self.VERSION,
                assumptions=None,
                solve_bounds=dict(self.SOLVE_BOUNDS),
                assumption_sources={},
                required_return=required_return,
                missing_information=("normalized EBIT history unavailable",),
            )
        ebit_margin, fcf_margin, normalization, economics_source, economics_warnings = economics
        warnings.extend(economics_warnings)
        roic_fraction = self._roic_fraction(roic)
        if roic_fraction is None:
            raw_reinvestment = 0.0
            warnings.append(
                "positive ROIC unavailable; net reinvestment set to 0% and confidence lowered"
            )
        else:
            reinvestment_share_of_nopat = self._clamp(
                growth / roic_fraction if growth > 0 else 0.0,
                0.0,
                1.0,
            )
            raw_reinvestment = ebit_margin * (1.0 - self.TAX_RATE) * reinvestment_share_of_nopat
        reinvestment = self._clamp(raw_reinvestment, *self.NET_REINVESTMENT_RANGE)
        if reinvestment != raw_reinvestment:
            warnings.append(
                f"net reinvestment rate clamped from {raw_reinvestment:.4f} to {reinvestment:.4f}"
            )

        assumptions = DcfAssumptions(
            projection_years=self.PROJECTION_YEARS,
            revenue_growth=growth,
            ebit_margin=ebit_margin,
            tax_rate=self.TAX_RATE,
            discount_rate=required_return.required_return,
            terminal_growth=self.TERMINAL_GROWTH,
            net_reinvestment_rate=reinvestment,
            reinvestment_return=roic_fraction,
            revenue_growth_fade_to=self.TERMINAL_GROWTH,
            ebit_margin_start=(
                current_report.ebit / current_report.revenue
                if current_report.ebit is not None
                else None
            ),
        )
        return DcfPolicyDecision(
            available=True,
            policy_version=self.VERSION,
            assumptions=assumptions,
            solve_bounds=dict(self.SOLVE_BOUNDS),
            assumption_sources={
                "projection_years": "fixed policy horizon",
                "revenue_growth": growth_source,
                "ebit_margin": economics_source,
                "tax_rate": "fixed normalized Nordic modeling rate",
                "discount_rate": (
                    "deterministic required-return hurdle selected by market-cap bucket"
                ),
                "terminal_growth": "fixed mature nominal growth policy",
                "net_reinvestment_rate": (
                    "normalized NOPAT margin multiplied by historical revenue growth / "
                    "Börsdata ROIC, capped at 100% of NOPAT and the policy range; "
                    "reported FCF is diagnostic only because it includes aggregate investing cash flow"
                ),
                "reinvestment_return": (
                    "positive Börsdata ROIC used to recompute mature-state "
                    "reinvestment from terminal growth"
                ),
                "revenue_growth_fade_to": (
                    "year-one revenue growth fades linearly to fixed mature "
                    "terminal growth by the final explicit year"
                ),
                "ebit_margin_start": (
                    "current R12 or latest annual EBIT margin; fades linearly "
                    "to the modeled final-year EBIT margin"
                ),
            },
            normalized_fcf_margin=fcf_margin,
            normalization=self._with_reinvestment_confidence(
                normalization,
                roic_fraction,
            ),
            reinvestment_roic=roic_fraction,
            required_return=required_return,
            warnings=tuple(warnings),
        )

    @classmethod
    def build_operating_history(
        cls,
        latest_annual_report: Report | None,
        historical_annual_reports: list[Report],
    ) -> HistoricalOperatingBenchmarks | None:
        reports = [*historical_annual_reports, latest_annual_report]
        valid = [report for report in reports if cls._has_valid_operating_economics(report)]
        dated = {report.year: report for report in valid if report.year is not None}
        annuals = [dated[year] for year in sorted(dated)] if dated else valid
        if not annuals:
            return None

        points = []
        previous = None
        for report in annuals:
            growth = cls._annualized_growth(previous, report) if previous else None
            points.append(
                HistoricalOperatingYear(
                    year=report.year,
                    revenue_growth=growth,
                    ebit_margin=report.ebit / report.revenue,
                )
            )
            previous = report

        latest_five = annuals[-5:]
        peak = max(annuals, key=lambda report: report.ebit / report.revenue)
        trough = min(annuals, key=lambda report: report.ebit / report.revenue)
        three_year_margin = cls._window(annuals[-3:]).ebit_margin if len(annuals) >= 3 else None
        five_year_margin = cls._window(latest_five).ebit_margin if len(annuals) >= 5 else None

        def count_below(baseline: float | None) -> int | None:
            if baseline is None:
                return None
            return sum(report.ebit / report.revenue < baseline for report in annuals)

        return HistoricalOperatingBenchmarks(
            annuals=tuple(points[-5:]),
            observed_period_count=len(points),
            three_year_revenue_cagr=cls._exact_revenue_cagr(annuals, 3),
            five_year_revenue_cagr=cls._exact_revenue_cagr(annuals, 5),
            three_year_average_ebit_margin=three_year_margin,
            five_year_average_ebit_margin=five_year_margin,
            periods_below_three_year_margin=count_below(three_year_margin),
            periods_below_five_year_margin=count_below(five_year_margin),
            peak_ebit_margin=peak.ebit / peak.revenue,
            peak_ebit_margin_year=peak.year,
            trough_ebit_margin=trough.ebit / trough.revenue,
            trough_ebit_margin_year=trough.year,
        )

    @staticmethod
    def _annualized_growth(previous: Report, current: Report) -> float | None:
        periods = 1
        if previous.year is not None and current.year is not None:
            periods = current.year - previous.year
            if periods <= 0:
                return None
        return cagr(previous.revenue, current.revenue, periods)

    @staticmethod
    def _exact_revenue_cagr(reports: list[Report], years: int) -> float | None:
        latest = reports[-1]
        if latest.year is None:
            return None
        baseline = next(
            (report for report in reports if report.year == latest.year - years),
            None,
        )
        if baseline is None:
            return None
        return cagr(baseline.revenue, latest.revenue, years)

    @staticmethod
    def _missing_operating_inputs(report: Report | None) -> list[str]:
        if report is None:
            return ["current R12 or annual report unavailable"]
        missing: list[str] = []
        if report.revenue is None or report.revenue <= 0:
            missing.append("positive current revenue unavailable")
        return missing

    @classmethod
    def _normalized_operating_economics(
        cls,
        current: Report,
        latest_annual: Report | None,
        history: list[Report],
    ) -> (
        tuple[
            float,
            float | None,
            NormalizationDiagnostics,
            str,
            tuple[str, ...],
        ]
        | None
    ):
        valid_annuals = [
            report
            for report in [*history, latest_annual]
            if cls._has_valid_operating_economics(report)
        ]
        dated = {report.year: report for report in valid_annuals if report.year is not None}
        annuals = [dated[year] for year in sorted(dated)] if dated else valid_annuals

        current_fallback = False
        if len(annuals) >= 5:
            selected = annuals[-5:]
            warning = ()
        elif len(annuals) >= 3:
            selected = annuals[-3:]
            warning = ("five-year operating-margin history unavailable; using three-year average",)
        elif cls._has_valid_operating_economics(latest_annual):
            selected = [latest_annual]
            warning = (
                "three-year operating-margin history unavailable; using latest annual margin",
            )
        else:
            if not cls._has_valid_operating_economics(current):
                return None
            selected = [current]
            current_fallback = True
            warning = ("annual operating economics unavailable; using current R12 margin",)

        three_year = cls._window(annuals[-3:]) if len(annuals) >= 3 else None
        five_year = cls._window(annuals[-5:]) if len(annuals) >= 5 else None
        selected_window = cls._window(selected)
        diagnostics = cls._normalization_diagnostics(
            annuals,
            selected_window,
            three_year,
            five_year,
        )
        years = [str(report.year) for report in selected if report.year is not None]
        if current_fallback:
            source = "current R12 EBIT margin fallback"
        elif len(years) == len(selected) == 1:
            period = f"annual year {years[0]}"
            source = f"revenue-weighted EBIT margin over {period}"
        elif len(years) == len(selected):
            period = f"annual years {years[0]}-{years[-1]}"
            source = f"revenue-weighted EBIT margin over {period}"
        else:
            period = f"latest {len(selected)} valid reports"
            source = f"revenue-weighted EBIT margin over {period}"
        return (
            selected_window.ebit_margin,
            selected_window.reported_fcf_margin,
            diagnostics,
            source,
            warning
            + ("reported FCF includes aggregate investing cash flow and is diagnostic only",)
            + diagnostics.reasons,
        )

    @staticmethod
    def _window(reports: list[Report]) -> NormalizationWindow:
        revenue = sum(report.revenue for report in reports)
        years = [report.year for report in reports if report.year is not None]
        fcf_reports = [report for report in reports if report.free_cash_flow is not None]
        ocf_reports = [report for report in reports if report.operating_cash_flow is not None]
        return NormalizationWindow(
            years=len(reports),
            start_year=min(years) if years else None,
            end_year=max(years) if years else None,
            ebit_margin=sum(report.ebit for report in reports) / revenue,
            reported_fcf_margin=(
                sum(report.free_cash_flow for report in fcf_reports)
                / sum(report.revenue for report in fcf_reports)
                if fcf_reports
                else None
            ),
            operating_cash_flow_margin=(
                sum(report.operating_cash_flow for report in ocf_reports)
                / sum(report.revenue for report in ocf_reports)
                if ocf_reports
                else None
            ),
        )

    @classmethod
    def _normalization_diagnostics(
        cls,
        annuals: list[Report],
        selected: NormalizationWindow,
        three_year: NormalizationWindow | None,
        five_year: NormalizationWindow | None,
    ) -> NormalizationDiagnostics:
        fcf = cls._fcf_diagnostics(annuals)
        disagreement = cls._window_disagreement(three_year, five_year)
        material_investing = cls._investing_diagnostics(annuals)
        reasons: list[str] = []
        if selected.years < 3:
            reasons.append("fewer than three valid annual observations")
        if fcf["negative_years"]:
            reasons.append(
                f"reported FCF is non-positive in {fcf['negative_years']} observed year(s)"
            )
        if fcf["highly_volatile"]:
            reasons.append("annual reported FCF margins are highly volatile")
        if disagreement:
            reasons.append("three- and five-year normalized margins materially disagree")
        if material_investing:
            reasons.append(
                "aggregate investing cash flow is material and may include acquisitions or disposals"
            )
        confidence = "low" if reasons else "medium"
        return NormalizationDiagnostics(
            confidence=confidence,
            selected_window_years=selected.years,
            three_year=three_year,
            five_year=five_year,
            annual_fcf_margin_stddev=fcf["stddev"],
            annual_fcf_margin_range=fcf["margin_range"],
            negative_fcf_years=fcf["negative_years"],
            fcf_sign_changes=fcf["sign_changes"],
            highly_volatile_fcf=fcf["highly_volatile"],
            material_window_disagreement=disagreement,
            material_aggregate_investing=material_investing,
            reasons=tuple(reasons),
        )

    @classmethod
    def _fcf_diagnostics(cls, annuals: list[Report]) -> dict:
        margins = [
            report.free_cash_flow / report.revenue
            for report in annuals[-5:]
            if report.free_cash_flow is not None
        ]
        negative_years = sum(value <= 0 for value in margins)
        sign_changes = sum(
            (margins[index] > 0) != (margins[index - 1] > 0) for index in range(1, len(margins))
        )
        stddev = pstdev(margins) if len(margins) >= 2 else None
        margin_range = max(margins) - min(margins) if margins else None
        average = mean(margins) if margins else None
        highly_volatile = len(margins) >= 3 and (
            stddev >= cls.FCF_MARGIN_VOLATILITY
            or margin_range >= cls.FCF_MARGIN_RANGE
            or sign_changes >= 2
            or (average is not None and abs(average) >= 0.01 and stddev / abs(average) >= 1.0)
        )
        return {
            "negative_years": negative_years,
            "sign_changes": sign_changes,
            "stddev": stddev,
            "margin_range": margin_range,
            "highly_volatile": highly_volatile,
        }

    @classmethod
    def _window_disagreement(cls, three_year, five_year) -> bool:
        if three_year is None or five_year is None:
            return False
        return abs(three_year.ebit_margin - five_year.ebit_margin) >= cls.WINDOW_DISAGREEMENT or (
            three_year.reported_fcf_margin is not None
            and five_year.reported_fcf_margin is not None
            and abs(three_year.reported_fcf_margin - five_year.reported_fcf_margin)
            >= cls.WINDOW_DISAGREEMENT
        )

    @classmethod
    def _investing_diagnostics(cls, annuals: list[Report]) -> bool:
        margins = [
            cls._investing_cash_flow(report) / report.revenue
            for report in annuals[-5:]
            if cls._investing_cash_flow(report) is not None
        ]
        return bool(margins) and (
            abs(mean(margins)) >= cls.MATERIAL_INVESTING_MARGIN
            or max(abs(value) for value in margins) >= cls.EXTREME_INVESTING_YEAR
        )

    @staticmethod
    def _investing_cash_flow(report: Report) -> float | None:
        if report.investing_cash_flow is not None:
            return report.investing_cash_flow
        return (report.raw_payload or {}).get("cash_Flow_From_Investing_Activities")

    @staticmethod
    def _with_reinvestment_confidence(
        diagnostics: NormalizationDiagnostics,
        roic: float | None,
    ) -> NormalizationDiagnostics:
        if roic is not None:
            return diagnostics
        reason = "positive Börsdata ROIC unavailable for reinvestment calibration"
        return replace(
            diagnostics,
            confidence="low",
            reasons=diagnostics.reasons + (reason,),
        )

    @staticmethod
    def _roic_fraction(roic: float | None) -> float | None:
        if roic is None or not isfinite(roic) or roic <= 0:
            return None
        return roic / 100.0

    @staticmethod
    def _has_valid_operating_economics(report: Report | None) -> bool:
        return (
            report is not None
            and report.revenue is not None
            and report.revenue > 0
            and report.ebit is not None
        )

    @classmethod
    def _historical_revenue_growth(
        cls,
        latest: Report | None,
        history: list[Report],
    ) -> float | None:
        if latest is None or latest.revenue is None or latest.revenue <= 0:
            return None
        candidates = [
            report for report in history if report.revenue is not None and report.revenue > 0
        ]
        if not candidates:
            return None

        has_dated_history = latest.year is not None and any(
            report.year is not None for report in candidates
        )
        if has_dated_history:
            dated = [
                report
                for report in candidates
                if report.year is not None and 0 < latest.year - report.year <= 3
            ]
            if not dated:
                return None
            baseline = min(dated, key=lambda report: report.year)
            periods = latest.year - baseline.year
        else:
            periods = min(3, len(candidates))
            baseline = candidates[-periods]
        return cagr(baseline.revenue, latest.revenue, periods)

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        return min(max(value, lower), upper)
