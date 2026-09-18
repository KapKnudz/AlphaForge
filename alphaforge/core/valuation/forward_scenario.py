"""Reproducible three-case holding-period sensitivity analysis."""

from dataclasses import dataclass
from math import floor, isfinite
from typing import Literal

from alphaforge.core.statistics import cagr
from alphaforge.core.types import RankingModel
from alphaforge.core.valuation.policy_versions import (
    FORWARD_RANKING_POLICY_VERSION,
    FORWARD_SCENARIO_POLICY_VERSION,
)

ScenarioCase = Literal["bear", "base", "bull"]
EvidenceConfidence = Literal["low", "medium", "high"]
NetDebtChangeProvenance = Literal[
    "source_backed",
    "analyst_sensitivity",
    "not_applicable",
]
ForwardScenarioStatus = Literal[
    "available",
    "insufficient_evidence",
    "method_not_supported",
]
RankingTier = Literal["A", "B", "C", "IE", "RESEARCH"]


ForwardScenarioReadinessStatus = Literal[
    "required",
    "blocked",
    "method_not_supported",
]


@dataclass(frozen=True)
class ForwardScenarioReadiness:
    status: ForwardScenarioReadinessStatus
    missing_inputs: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def required(self) -> bool:
        return self.status == "required"


@dataclass(frozen=True)
class SourcedAssumption:
    value: float
    source_ids: tuple[str, ...]
    rationale: str
    mechanism: str | None = None


@dataclass(frozen=True)
class NetDebtChangeAssumption:
    value: float
    source_ids: tuple[str, ...]
    rationale: str
    mechanism: str
    provenance_type: NetDebtChangeProvenance


@dataclass(frozen=True)
class ScenarioBundle:
    case: ScenarioCase
    horizon_months: int
    revenue_cagr: SourcedAssumption
    ebit_margin: SourcedAssumption
    terminal_ev_ebit_low: SourcedAssumption
    terminal_ev_ebit_high: SourcedAssumption
    net_debt_change: NetDebtChangeAssumption
    share_count_growth: SourcedAssumption
    distributions_per_share: SourcedAssumption
    mechanism: str


@dataclass(frozen=True)
class ForwardScenarioInputs:
    current_price: float | None
    current_revenue: float | None
    current_shares: float | None
    current_net_debt: float | None
    historical_terminal_multiple_range: tuple[float | None, float | None]
    bundles: tuple[ScenarioBundle, ...]
    ranking_model: RankingModel = RankingModel.GENERAL
    price_currency: str | None = None
    financial_currency: str | None = None
    current_terminal_multiple: float | None = None
    base_terminal_multiple_ceiling: float | None = None
    bull_terminal_multiple_ceiling: float | None = None
    demonstrated_revenue_cagr: float | None = None
    demonstrated_ebit_margin: float | None = None
    case_horizon_months: int | None = None


@dataclass(frozen=True)
class ScenarioBandResult:
    case: ScenarioCase
    horizon_months: int
    low_price: float
    high_price: float
    low_holding_value: float
    high_holding_value: float
    low_annualized_return: float
    high_annualized_return: float

    @property
    def price_range(self) -> tuple[float, float]:
        return self.low_price, self.high_price

    @property
    def annualized_return_range(self) -> tuple[float, float]:
        return self.low_annualized_return, self.high_annualized_return


@dataclass(frozen=True)
class ForwardScenarioAnalysis:
    status: ForwardScenarioStatus
    policy_version: str
    bands: tuple[ScenarioBandResult, ...] = ()
    methodology_flags: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def results(self) -> tuple[ScenarioBandResult, ...]:
        """Compatibility-free plural accessor for the v2 bands."""
        return self.bands

    def band(self, case: ScenarioCase) -> ScenarioBandResult | None:
        return next((item for item in self.bands if item.case == case), None)

    def result(self, case: ScenarioCase) -> ScenarioBandResult | None:
        """Named accessor used by ranking and export consumers."""
        return self.band(case)


@dataclass(frozen=True)
class _CalculatedBandEndpoint:
    price: float
    holding_value: float
    annualized_return: float


class ForwardScenarioEngine:
    """Validate three coherent bundles and calculate their price/return bands."""

    POLICY_VERSION = FORWARD_SCENARIO_POLICY_VERSION
    HORIZONS = (24, 36, 48)
    CASES: tuple[ScenarioCase, ...] = ("bear", "base", "bull")
    PLAUSIBILITY_TOLERANCE = 0.005

    @classmethod
    def assess_readiness(cls, inputs: ForwardScenarioInputs) -> ForwardScenarioReadiness:
        if inputs.ranking_model != RankingModel.GENERAL:
            return ForwardScenarioReadiness(
                status="method_not_supported",
                warnings=(f"{inputs.ranking_model} requires a dedicated forward valuation method",),
            )

        missing = []
        for name, value in (
            ("current_price", inputs.current_price),
            ("current_revenue", inputs.current_revenue),
            ("current_shares", inputs.current_shares),
        ):
            if not cls._positive(value):
                missing.append(name)
        if not cls._finite(inputs.current_net_debt):
            missing.append("current_net_debt")
        price_currency = cls._currency(inputs.price_currency)
        financial_currency = cls._currency(inputs.financial_currency)
        if price_currency is None:
            missing.append("price_currency")
        if financial_currency is None:
            missing.append("financial_currency")
        if price_currency and financial_currency and price_currency != financial_currency:
            missing.append("matching_currency")
        if not cls._positive(inputs.current_terminal_multiple):
            missing.append("current_ev_ebit")

        warnings = []
        historical_low, historical_high = inputs.historical_terminal_multiple_range
        if historical_low is None or historical_high is None:
            warnings.append(
                "historical terminal multiple range is unavailable; "
                "current EV/EBIT is the only multiple anchor"
            )
        if missing:
            return ForwardScenarioReadiness(
                status="blocked",
                missing_inputs=tuple(dict.fromkeys(missing)),
                warnings=tuple(warnings),
            )
        return ForwardScenarioReadiness(
            status="required",
            warnings=tuple(warnings),
        )

    def analyze(self, inputs: ForwardScenarioInputs) -> ForwardScenarioAnalysis:
        if inputs.ranking_model != RankingModel.GENERAL:
            return ForwardScenarioAnalysis(
                status="method_not_supported",
                policy_version=self.POLICY_VERSION,
                methodology_flags=(
                    f"{inputs.ranking_model} requires a dedicated forward valuation method",
                ),
            )

        flags, warnings = self._validate(inputs)
        if flags:
            return ForwardScenarioAnalysis(
                status="insufficient_evidence",
                policy_version=self.POLICY_VERSION,
                methodology_flags=tuple(flags),
                warnings=tuple(warnings),
            )

        bands = tuple(self._calculate_band(inputs, bundle) for bundle in inputs.bundles)
        output_flags = self._validate_outputs(bands)
        if output_flags:
            return ForwardScenarioAnalysis(
                status="insufficient_evidence",
                policy_version=self.POLICY_VERSION,
                bands=bands,
                methodology_flags=tuple(output_flags),
                warnings=tuple(warnings),
            )
        return ForwardScenarioAnalysis(
            status="available",
            policy_version=self.POLICY_VERSION,
            bands=bands,
            warnings=tuple(warnings),
        )

    def _validate(self, inputs: ForwardScenarioInputs) -> tuple[list[str], list[str]]:
        flags: list[str] = []
        warnings: list[str] = []
        price_currency = self._currency(inputs.price_currency)
        financial_currency = self._currency(inputs.financial_currency)
        if price_currency is None or financial_currency is None:
            flags.append("price and financial currencies are required for forward valuation")
        elif price_currency != financial_currency:
            flags.append(
                "forward valuation currency mismatch: "
                f"price is {price_currency}, financial inputs are {financial_currency}"
            )

        for name, value in (
            ("current_price", inputs.current_price),
            ("current_revenue", inputs.current_revenue),
            ("current_shares", inputs.current_shares),
        ):
            if not self._positive(value):
                flags.append(f"{name} must be a finite positive number")
        if not self._finite(inputs.current_net_debt):
            flags.append("current_net_debt must be finite")

        historical_low, historical_high = inputs.historical_terminal_multiple_range
        if historical_low is None or historical_high is None:
            warnings.append(
                "historical terminal multiple range is unavailable; "
                "bear multiples are not floored by history"
            )
        elif not (
            self._positive(historical_low)
            and self._positive(historical_high)
            and historical_low <= historical_high
        ):
            flags.append("historical terminal multiple range must be finite, positive, and ordered")

        by_case = {bundle.case: bundle for bundle in inputs.bundles}
        if len(by_case) != len(inputs.bundles):
            flags.append("scenario bundles must contain exactly one bundle for each case")
        missing = sorted(set(self.CASES) - set(by_case))
        unexpected = sorted(set(by_case) - set(self.CASES))
        if missing:
            flags.append("missing scenario bundles: " + ", ".join(missing))
        if unexpected:
            flags.append("unsupported scenario bundles: " + ", ".join(unexpected))

        horizons = {bundle.horizon_months for bundle in inputs.bundles}
        if len(horizons) != 1 or not horizons.issubset(self.HORIZONS):
            flags.append("all scenario bundles must use the same 24, 36, or 48 month horizon")
        if (
            inputs.case_horizon_months is not None
            and len(horizons) == 1
            and inputs.case_horizon_months not in horizons
        ):
            flags.append("scenario bundle horizon must match case_horizon_months")

        for bundle in inputs.bundles:
            self._validate_bundle(
                inputs,
                bundle,
                flags,
                warnings,
            )

        if set(self.CASES).issubset(by_case):
            self._validate_case_order(by_case, flags)
            self._validate_plausibility(inputs, by_case["base"], flags)
            self._validate_plausibility(inputs, by_case["bull"], flags)
        return flags, warnings

    @staticmethod
    def _currency(value: str | None) -> str | None:
        normalized = (value or "").strip().upper()
        return normalized or None

    @classmethod
    def _validate_bundle(
        cls,
        inputs: ForwardScenarioInputs,
        bundle: ScenarioBundle,
        flags: list[str],
        warnings: list[str],
    ) -> None:
        assumptions = {
            "revenue_cagr": bundle.revenue_cagr,
            "ebit_margin": bundle.ebit_margin,
            "terminal_ev_ebit_low": bundle.terminal_ev_ebit_low,
            "terminal_ev_ebit_high": bundle.terminal_ev_ebit_high,
            "net_debt_change": bundle.net_debt_change,
            "share_count_growth": bundle.share_count_growth,
            "distributions_per_share": bundle.distributions_per_share,
        }
        prefix = bundle.case
        for name, assumption in assumptions.items():
            if not cls._finite(getattr(assumption, "value", None)):
                flags.append(f"{prefix}.{name} must be finite")
            if not getattr(assumption, "source_ids", ()):
                flags.append(f"{prefix}.{name} requires at least one source ID")
            if not getattr(assumption, "rationale", "").strip():
                flags.append(f"{prefix}.{name} requires a rationale")

        if not isinstance(bundle.mechanism, str) or not bundle.mechanism.strip():
            flags.append(f"{prefix}.mechanism requires a rationale")

        change = bundle.net_debt_change
        provenance_type = getattr(change, "provenance_type", None)
        if provenance_type not in (
            "source_backed",
            "analyst_sensitivity",
            "not_applicable",
        ):
            flags.append(f"{prefix}.net_debt_change has an invalid provenance type")
        if not isinstance(change.mechanism, str) or not change.mechanism.strip():
            flags.append(f"{prefix}.net_debt_change requires a mechanism")
        if change.value == 0 and provenance_type != "not_applicable":
            flags.append(f"{prefix}.zero net_debt_change must use not_applicable provenance")
        if change.value != 0:
            if provenance_type == "not_applicable":
                flags.append(f"{prefix}.non-zero net_debt_change requires provenance")
            if provenance_type == "analyst_sensitivity":
                warnings.append(
                    f"{prefix}.net_debt_change is an analyst sensitivity; "
                    "its mechanism is not source-backed"
                )

        for name, assumption in (
            ("share_count_growth", bundle.share_count_growth),
            ("distributions_per_share", bundle.distributions_per_share),
        ):
            if assumption.value != 0 and not (assumption.mechanism or "").strip():
                flags.append(f"{prefix}.{name} requires a mechanism")
            rationale = assumption.rationale.lower()
            if assumption.value == 0 and any(
                phrase in rationale
                for phrase in ("missing", "unavailable", "not provided", "unknown")
            ):
                warnings.append(
                    f"{prefix}.{name} is zero because supporting data is missing; "
                    "treat it as an analyst sensitivity"
                )

        if bundle.revenue_cagr.value <= -1.0:
            flags.append(f"{prefix}.revenue_cagr must be greater than -100%")
        if bundle.ebit_margin.value <= 0:
            flags.append(f"{prefix}.ebit_margin must be positive for EV/EBIT")
        if bundle.share_count_growth.value <= -1.0:
            flags.append(f"{prefix}.share_count_growth must be greater than -100%")
        if bundle.distributions_per_share.value < 0:
            flags.append(f"{prefix}.distributions_per_share cannot be negative")

        low = bundle.terminal_ev_ebit_low
        high = bundle.terminal_ev_ebit_high
        if low.value <= 0 or high.value <= 0:
            flags.append(f"{prefix}.terminal multiple range must be positive")
        if low.value > high.value:
            flags.append(f"{prefix}.terminal multiple range must be ordered")
        if not cls._finite(inputs.current_net_debt):
            return
        if not cls._finite(inputs.current_net_debt + change.value):
            flags.append(f"{prefix}.future net debt must be finite")

    @classmethod
    def _validate_case_order(
        cls,
        by_case: dict[str, ScenarioBundle],
        flags: list[str],
    ) -> None:
        bear = by_case["bear"]
        base = by_case["base"]
        bull = by_case["bull"]
        bear_comparisons = (
            bear.revenue_cagr.value <= base.revenue_cagr.value,
            bear.ebit_margin.value <= base.ebit_margin.value,
            bear.net_debt_change.value >= base.net_debt_change.value,
            bear.share_count_growth.value >= base.share_count_growth.value,
            bear.distributions_per_share.value <= base.distributions_per_share.value,
            bear.terminal_ev_ebit_low.value <= base.terminal_ev_ebit_low.value,
            bear.terminal_ev_ebit_high.value <= base.terminal_ev_ebit_high.value,
        )
        bull_comparisons = (
            bull.revenue_cagr.value >= base.revenue_cagr.value,
            bull.ebit_margin.value >= base.ebit_margin.value,
            bull.net_debt_change.value <= base.net_debt_change.value,
            bull.share_count_growth.value <= base.share_count_growth.value,
            bull.distributions_per_share.value >= base.distributions_per_share.value,
            bull.terminal_ev_ebit_low.value >= base.terminal_ev_ebit_low.value,
            bull.terminal_ev_ebit_high.value >= base.terminal_ev_ebit_high.value,
        )
        if not all(bear_comparisons):
            flags.append("bear cannot improve a driver versus base")
        if not all(bull_comparisons):
            flags.append("bull cannot worsen a driver versus base")

    @classmethod
    def _validate_plausibility(
        cls,
        inputs: ForwardScenarioInputs,
        bundle: ScenarioBundle,
        flags: list[str],
    ) -> None:
        multiple_ceiling = (
            inputs.base_terminal_multiple_ceiling
            if bundle.case == "base"
            else inputs.bull_terminal_multiple_ceiling
        )
        beyond: list[str] = []
        if cls._finite(inputs.demonstrated_revenue_cagr) and (
            bundle.revenue_cagr.value
            > inputs.demonstrated_revenue_cagr + cls.PLAUSIBILITY_TOLERANCE
        ):
            beyond.append("growth")
        if cls._finite(inputs.demonstrated_ebit_margin) and (
            bundle.ebit_margin.value > inputs.demonstrated_ebit_margin + cls.PLAUSIBILITY_TOLERANCE
        ):
            beyond.append("margin")
        if cls._positive(multiple_ceiling) and (
            bundle.terminal_ev_ebit_high.value > multiple_ceiling
        ):
            beyond.append("multiple")

        if bundle.case == "base" and beyond:
            flags.append("base uses assumptions beyond demonstrated bounds: " + ", ".join(beyond))
        elif bundle.case == "bull":
            if "multiple" in beyond:
                flags.append("bull terminal multiple exceeds the supported ceiling")
            if len(beyond) > 1:
                flags.append(f"bull combines {len(beyond)} unprecedented operating levers")

    @classmethod
    def _calculate_band(
        cls,
        inputs: ForwardScenarioInputs,
        bundle: ScenarioBundle,
    ) -> ScenarioBandResult:
        low = cls._calculate_endpoint(inputs, bundle, bundle.terminal_ev_ebit_low.value)
        high = cls._calculate_endpoint(inputs, bundle, bundle.terminal_ev_ebit_high.value)
        return ScenarioBandResult(
            case=bundle.case,
            horizon_months=bundle.horizon_months,
            low_price=low.price,
            high_price=high.price,
            low_holding_value=low.holding_value,
            high_holding_value=high.holding_value,
            low_annualized_return=low.annualized_return,
            high_annualized_return=high.annualized_return,
        )

    @staticmethod
    def _calculate_endpoint(
        inputs: ForwardScenarioInputs,
        bundle: ScenarioBundle,
        terminal_multiple: float,
    ) -> _CalculatedBandEndpoint:
        if (
            inputs.current_price is None
            or inputs.current_revenue is None
            or inputs.current_shares is None
            or inputs.current_net_debt is None
        ):
            raise ValueError("validated forward inputs unexpectedly contain missing values")
        years = bundle.horizon_months / 12.0
        revenue = inputs.current_revenue * (1.0 + bundle.revenue_cagr.value) ** years
        ebit = revenue * bundle.ebit_margin.value
        enterprise_value = ebit * terminal_multiple
        equity_value = enterprise_value - (inputs.current_net_debt + bundle.net_debt_change.value)
        diluted_shares = inputs.current_shares * (1.0 + bundle.share_count_growth.value)
        price = equity_value / diluted_shares
        holding_value = price + bundle.distributions_per_share.value
        annualized_return = cagr(inputs.current_price, holding_value, years)
        if annualized_return is None:
            annualized_return = float("nan")
        return _CalculatedBandEndpoint(price, holding_value, annualized_return)

    @classmethod
    def _validate_outputs(cls, bands: tuple[ScenarioBandResult, ...]) -> list[str]:
        flags: list[str] = []
        by_case = {band.case: band for band in bands}
        for band in bands:
            values = (
                band.low_price,
                band.high_price,
                band.low_holding_value,
                band.high_holding_value,
                band.low_annualized_return,
                band.high_annualized_return,
            )
            if not all(cls._finite(value) for value in values):
                flags.append(f"{band.case} produces a non-finite output")
            if band.low_price <= 0 or band.high_price <= 0:
                flags.append(f"{band.case} produces a non-positive price")
            if band.low_holding_value <= 0 or band.high_holding_value <= 0:
                flags.append(f"{band.case} produces a non-positive holding value")
            if band.low_price > band.high_price:
                flags.append(f"{band.case} low price must not exceed high price")

        if set(cls.CASES).issubset(by_case):
            bear = by_case["bear"]
            base = by_case["base"]
            bull = by_case["bull"]
            if bear.high_price > base.low_price:
                flags.append("bear_high must be less than or equal to base_low")
            if base.high_price > bull.low_price:
                flags.append("base_high must be less than or equal to bull_low")
            if base.high_annualized_return - base.low_annualized_return > 0.15:
                flags.append("base annualized-return range exceeds 15 percentage points")
        return flags

    @staticmethod
    def _positive(value: float | None) -> bool:
        return ForwardScenarioEngine._finite(value) and value > 0

    @staticmethod
    def _finite(value: float | None) -> bool:
        return isinstance(value, (int, float)) and isfinite(value)


@dataclass(frozen=True)
class RankedForwardCase:
    company_id: int
    ticker: str
    analysis: ForwardScenarioAnalysis
    required_return: float
    evidence_confidence: EvidenceConfidence
    unresolved_high_severity_challenge: bool = False


@dataclass(frozen=True)
class ForwardRank:
    company_id: int
    ticker: str
    economic_tier: RankingTier
    tier: RankingTier
    rank: int
    tied: bool
    actionable: bool
    bear_lower_bound: float | None
    base_band: tuple[float, float] | None
    bull_lower_bound: float | None
    evidence_confidence: EvidenceConfidence
    flags: tuple[str, ...]


class ForwardScenarioRanker:
    """Apply the versioned hurdle-first, downside-first ranking policy."""

    POLICY_VERSION = FORWARD_RANKING_POLICY_VERSION
    COMPARISON_TOLERANCE = 0.02
    _CONFIDENCE = {"low": 0, "medium": 1, "high": 2}
    _TIER_ORDER = {"A": 0, "B": 1, "C": 2, "IE": 3, "RESEARCH": 4}

    def rank(
        self,
        cases: list[RankedForwardCase],
        economic_tier_overrides: dict[int, RankingTier] | None = None,
    ) -> tuple[ForwardRank, ...]:
        economic_tier_overrides = economic_tier_overrides or {}
        prepared = [
            self._prepare(case, economic_tier_overrides.get(case.company_id)) for case in cases
        ]
        prepared.sort(key=lambda item: (*item[1], item[0]["company_id"]))
        output: list[ForwardRank] = []
        previous_key = None
        current_rank = 0
        for position, (draft, tie_key) in enumerate(prepared, 1):
            if tie_key != previous_key:
                current_rank = position
            output.append(ForwardRank(**draft, rank=current_rank, tied=False))
            previous_key = tie_key

        counts: dict[int, int] = {}
        for item in output:
            counts[item.rank] = counts.get(item.rank, 0) + 1
        return tuple(
            ForwardRank(**{**item.__dict__, "tied": counts[item.rank] > 1}) for item in output
        )

    def _prepare(
        self,
        case: RankedForwardCase,
        economic_tier_override: RankingTier | None = None,
    ) -> tuple[dict, tuple]:
        flags = list(case.analysis.methodology_flags)
        if case.analysis.status != "available":
            economic_tier: RankingTier = "IE"
        else:
            base_low = self._return(case.analysis, "base", "low")
            base_high = self._return(case.analysis, "base", "high")
            if base_low >= case.required_return:
                economic_tier = "A"
            elif base_high >= case.required_return:
                economic_tier = "B"
            else:
                economic_tier = "C"
        if economic_tier_override is not None and economic_tier in ("A", "B", "C"):
            economic_tier = economic_tier_override
            flags.append("economic tier held by two-snapshot hysteresis")

        if case.unresolved_high_severity_challenge:
            tier: RankingTier = "RESEARCH"
            actionable = False
            flags.append("unresolved high-severity critic challenge")
        elif economic_tier == "IE":
            tier = "IE"
            actionable = False
            flags.append(case.analysis.status)
        else:
            tier = economic_tier
            if case.evidence_confidence == "low":
                tier = {"A": "B", "B": "C", "C": "C"}[tier]
                flags.append("low evidence confidence lowered tier")
            actionable = tier in ("A", "B")

        if case.analysis.status == "available":
            bear_lower = self._return(case.analysis, "bear", "low")
            base_band = (
                self._return(case.analysis, "base", "low"),
                self._return(case.analysis, "base", "high"),
            )
            bull_lower = self._return(case.analysis, "bull", "low")
            width = base_band[1] - base_band[0]
        else:
            bear_lower = None
            base_band = None
            bull_lower = None
            width = None

        q = self._quantize
        tie_key = (
            self._TIER_ORDER[tier],
            -q(bear_lower),
            -q(base_band[0] if base_band else None),
            -self._CONFIDENCE[case.evidence_confidence],
            q(width),
            -q(bull_lower),
        )
        draft = {
            "company_id": case.company_id,
            "ticker": case.ticker,
            "economic_tier": economic_tier,
            "tier": tier,
            "actionable": actionable,
            "bear_lower_bound": bear_lower,
            "base_band": base_band,
            "bull_lower_bound": bull_lower,
            "evidence_confidence": case.evidence_confidence,
            "flags": tuple(flags),
        }
        return draft, tie_key

    @classmethod
    def _quantize(cls, value: float | None) -> int:
        if value is None:
            return -(10**9)
        return floor(value / cls.COMPARISON_TOLERANCE + 0.5)

    @staticmethod
    def _return(
        analysis: ForwardScenarioAnalysis,
        case: ScenarioCase,
        side: Literal["low", "high"],
    ) -> float:
        band = analysis.band(case)
        if band is None:
            raise ValueError(f"available analysis is missing {case} band")
        return band.low_annualized_return if side == "low" else band.high_annualized_return


def forward_analysis_from_dict(payload: dict) -> ForwardScenarioAnalysis:
    """Rebuild a persisted v2 deterministic result for comparative ranking."""
    return ForwardScenarioAnalysis(
        status=payload["status"],
        policy_version=payload["policy_version"],
        bands=tuple(ScenarioBandResult(**item) for item in payload.get("bands", [])),
        methodology_flags=tuple(payload.get("methodology_flags", [])),
        warnings=tuple(payload.get("warnings", [])),
    )
