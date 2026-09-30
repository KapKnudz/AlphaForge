from __future__ import annotations

import math
from typing import TYPE_CHECKING

from alphaforge.core.kpi_taxonomy import KpiIds
from alphaforge.core.ranking.metrics import Metric

if TYPE_CHECKING:
    from alphaforge.core.financial.types import FinancialResult
    from alphaforge.core.valuation.types import ValuationResult


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _linear_score(value: float | None, floor: float, ceiling: float) -> float | None:
    """Score value linearly: 0 at floor, 100 at ceiling. None → None."""
    if value is None:
        return None
    if value <= floor:
        return 0.0
    if value >= ceiling:
        return 100.0
    return (value - floor) / (ceiling - floor) * 100.0


def _inverted_linear_score(value: float | None, floor: float, ceiling: float) -> float | None:
    """Inverted linear: 100 at floor (best), 0 at ceiling (worst). None → None."""
    if value is None:
        return None
    if value <= floor:
        return 100.0
    if value >= ceiling:
        return 0.0
    return (ceiling - value) / (ceiling - floor) * 100.0


def _growth_score(value: float | None, scale: float) -> float | None:
    """Smoothly reward positive growth without a hard 100-point ceiling."""
    if value is None:
        return None
    if value <= 0:
        return 0.0
    return min(99.9, 100.0 * (1.0 - math.exp(-value / scale)))


# ---------------------------------------------------------------------------
# Margin of safety
# ---------------------------------------------------------------------------

# Calibrated for the Swedish market. Adjust if applying to other markets.
RISK_FREE_RATE = 0.03  # ~Swedish 10Y government bond
EQUITY_RISK_PREMIUM = 0.05  # standard ERP


def compute_margin_of_safety(
    fcf_yield: float | None,
    debt_to_equity: float | None = None,
    quality_score: float = 50.0,
    growth_score: float = 50.0,
    risk_free_rate: float = RISK_FREE_RATE,
    equity_risk_premium: float = EQUITY_RISK_PREMIUM,
) -> float | None:
    """Required-return spread: actual FCF yield minus required yield.

    A large positive margin suggests undervaluation; a negative margin
    means the stock is expensive unless quality or growth is exceptional.

    required_yield = risk_free_rate + equity_risk_premium
                     + leverage_penalty - quality_bonus - growth_bonus
    """
    if fcf_yield is None:
        return None

    # Leverage penalty: 0% at D/E=0, scaling linearly to ~5% at D/E=2.0
    if debt_to_equity is not None and debt_to_equity > 0:
        leverage_penalty = min(debt_to_equity * 0.025, 0.05)
    else:
        leverage_penalty = 0.0

    # Quality bonus: high-quality companies deserve a lower required return.
    # Scales from 0% at quality=50 (neutral) to 3% at quality=100.
    quality_bonus = max(0.0, (quality_score - 50.0) / 50.0 * 0.03)

    # Growth bonus: fast growers deserve a lower required return.
    # Scales from 0% at growth=50 (neutral) to 2% at growth=100.
    growth_bonus = max(0.0, (growth_score - 50.0) / 50.0 * 0.02)

    required_yield = (
        risk_free_rate + equity_risk_premium + leverage_penalty - quality_bonus - growth_bonus
    )

    return fcf_yield - required_yield


# ---------------------------------------------------------------------------
# Category scoring
# ---------------------------------------------------------------------------


def _quality_metrics_definitions(financial, fundamental_kpis) -> list[Metric]:
    fundamental_kpis = fundamental_kpis or {}

    def _roe_desc(v):
        p, n = [], []
        if v >= 0.20:
            p.append(f"ROE {v:.0%} — strong")
        elif v <= 0.05:
            n.append(f"ROE {v:.0%} — weak")
        return p, n, []

    def _roa_desc(v):
        p, n = [], []
        if v >= 0.10:
            p.append(f"ROA {v:.0%} — strong")
        elif v <= 0.02:
            n.append(f"ROA {v:.0%} — weak")
        return p, n, []

    def _net_margin_desc(v):
        p, n = [], []
        if v >= 0.15:
            p.append(f"Net margin {v:.0%} — strong")
        elif v <= 0.03:
            n.append(f"Net margin {v:.0%} — low")
        return p, n, []

    def _fcf_margin_desc(v):
        p, n = [], []
        if v >= 0.15:
            p.append(f"FCF margin {v:.0%} — strong")
        elif v <= 0.03:
            n.append(f"FCF margin {v:.0%} — low")
        return p, n, []

    def _roic_desc(v):
        p, n = [], []
        if v >= 0.15:
            p.append(f"ROIC {v:.0%} — strong")
        elif v <= 0.05:
            n.append(f"ROIC {v:.0%} — weak")
        return p, n, []

    def _cash_conv_desc(v):
        p, n = [], []
        if v >= 1.0:
            p.append(f"Operating cash conversion {v:.1f}x — strong")
        elif v < 0.5:
            n.append(f"Operating cash conversion {v:.1f}x — weak")
        return p, n, []

    def _margin_vol_desc(v):
        p, n = [], []
        if v <= 0.03:
            p.append("Operating margin has been stable")
        elif v >= 0.10:
            n.append("Operating margin has been volatile")
        return p, n, []

    def _fcf_cons_desc(v):
        p, n = [], []
        if v == 1.0:
            p.append("Free cash flow positive in every observed year")
        elif v < 0.5:
            n.append("Free cash flow positive in fewer than half of observed years")
        return p, n, []

    roic_raw = fundamental_kpis.get(KpiIds.ROIC)
    roic_val = roic_raw / 100 if roic_raw is not None else None

    return [
        Metric(
            "roe",
            lambda: financial.roe,
            lambda v: _linear_score(v, 0.0, 0.25),
            transformation="linear[0,0.25]",
            dependencies=("net_income", "equity"),
            describe=_roe_desc,
        ),
        Metric(
            "roa",
            lambda: financial.roa,
            lambda v: _linear_score(v, 0.0, 0.15),
            transformation="linear[0,0.15]",
            dependencies=("net_income", "total_assets"),
            describe=_roa_desc,
        ),
        Metric(
            "net_margin",
            lambda: financial.net_margin,
            lambda v: _linear_score(v, 0.0, 0.20),
            transformation="linear[0,0.20]",
            dependencies=("net_income", "revenue"),
            describe=_net_margin_desc,
        ),
        Metric(
            "fcf_margin",
            lambda: financial.fcf_margin,
            lambda v: _linear_score(v, 0.0, 0.20),
            transformation="linear[0,0.20]",
            dependencies=("free_cash_flow", "revenue"),
            describe=_fcf_margin_desc,
        ),
        Metric(
            "roic",
            lambda: roic_val,
            lambda v: _linear_score(v, 0.0, 0.20),
            transformation="linear[0,0.20]",
            dependencies=("roic_kpi",),
            describe=_roic_desc,
        ),
        Metric(
            "cash_conversion",
            lambda: getattr(financial, "cash_conversion", None),
            lambda v: _linear_score(v, 0.0, 1.0),
            transformation="linear[0,1]",
            dependencies=("operating_cash_flow", "operating_profit"),
            describe=_cash_conv_desc,
        ),
        Metric(
            "operating_margin_stability",
            lambda: getattr(financial, "operating_margin_volatility", None),
            lambda v: _inverted_linear_score(v, 0.0, 0.10),
            transformation="inverted_linear[0,0.10]",
            dependencies=("historical_operating_margins",),
            describe=_margin_vol_desc,
        ),
        Metric(
            "fcf_consistency",
            lambda: getattr(financial, "positive_fcf_ratio", None),
            lambda v: _linear_score(v, 0.0, 1.0),
            transformation="linear[0,1]",
            dependencies=("historical_free_cash_flow",),
            describe=_fcf_cons_desc,
        ),
    ]


def score_quality(
    financial: FinancialResult | None,
    fundamental_kpis: dict[int, float | None] | None = None,
) -> dict:
    if financial is None:
        return {
            "score": 0.0,
            "positives": [],
            "negatives": [],
            "missing": ["financial data not available"],
            "available": False,
        }

    metric_defs = _quality_metrics_definitions(financial, fundamental_kpis)
    results = [m.evaluate() for m in metric_defs]

    scores = [r.score for r in results if r.score is not None]
    positives = [p for r in results for p in r.positives]
    negatives = [n for r in results for n in r.negatives]
    missing = [m for r in results for m in r.missing]

    score = sum(scores) / len(scores) if scores else 0.0
    return {
        "score": score,
        "positives": positives,
        "negatives": negatives,
        "missing": missing,
        "available": bool(scores),
    }


def _growth_metrics_definitions(financial) -> list[Metric]:
    revenue_per_share_growth = getattr(financial, "revenue_per_share_growth", None)
    ebit_per_share_growth = getattr(financial, "ebit_per_share_growth", None)
    net_income_per_share_growth = getattr(financial, "net_income_per_share_growth", None)
    revenue_per_share_years = getattr(
        financial, "revenue_per_share_growth_years", getattr(financial, "per_share_growth_years", 1)
    )
    ebit_per_share_years = getattr(
        financial, "ebit_per_share_growth_years", getattr(financial, "per_share_growth_years", 1)
    )
    net_income_per_share_years = getattr(
        financial,
        "net_income_per_share_growth_years",
        getattr(financial, "per_share_growth_years", 1),
    )
    share_count_years = getattr(financial, "share_count_growth_years", 1)
    revenue_growth = (
        revenue_per_share_growth
        if revenue_per_share_growth is not None
        else financial.revenue_growth
    )
    ebit_growth = (
        ebit_per_share_growth if ebit_per_share_growth is not None else financial.ebit_growth
    )
    net_income_growth = (
        net_income_per_share_growth
        if net_income_per_share_growth is not None
        else financial.net_income_growth
    )
    per_share_basis = revenue_per_share_growth is not None
    revenue_turnaround = getattr(financial, "revenue_turnaround", False)
    revenue_deterioration = getattr(financial, "revenue_deterioration", False)
    revenue_years = getattr(financial, "revenue_growth_years", 1)
    ebit_turnaround = getattr(financial, "ebit_turnaround", False)
    ebit_deterioration = getattr(financial, "ebit_deterioration", False)
    ebit_years = getattr(financial, "ebit_growth_years", 1)
    net_income_turnaround = getattr(financial, "net_income_turnaround", False)
    net_income_deterioration = getattr(financial, "net_income_deterioration", False)
    net_income_years = getattr(financial, "net_income_growth_years", 1)
    one_off_risk = getattr(financial, "earnings_growth_one_off_risk", False)
    recent_revenue_growth = getattr(financial, "recent_revenue_growth", None)

    def _revenue_raw():
        return revenue_growth

    def _revenue_score(v):
        if revenue_turnaround:
            return 50.0
        if revenue_deterioration:
            return 0.0
        return _growth_score(v, 0.20)

    def _revenue_desc(v):
        p, n = [], []
        if revenue_turnaround:
            p.append("Revenue returned to positive territory")
        elif revenue_deterioration:
            n.append("Revenue turned non-positive")
        if v is not None:
            years = revenue_per_share_years if per_share_basis else revenue_years
            period = f"{years}y CAGR" if years > 1 else "YoY"
            label = "Revenue/share growth" if per_share_basis else "Revenue growth"
            if v >= 0.15:
                p.append(f"{label} {v:.0%} ({period}) — strong")
            elif v <= -0.05:
                n.append(f"{label} {v:.0%} ({period}) — declining")
        return p, n, []

    def _ebit_raw():
        return ebit_growth

    def _ebit_score(v):
        if ebit_turnaround:
            return 50.0
        if ebit_deterioration:
            return 0.0
        s = _growth_score(v, 0.30)
        if s is not None and one_off_risk:
            s = min(s, 60.0)
        return s

    def _ebit_desc(v):
        p, n = [], []
        if ebit_turnaround:
            p.append("EBIT returned to profit; percentage growth is not meaningful")
        elif ebit_deterioration:
            n.append("EBIT turned negative")
        if v is not None:
            years = ebit_per_share_years if ebit_per_share_growth is not None else ebit_years
            period = f"{years}y CAGR" if years > 1 else "YoY"
            label = "EBIT/share growth" if ebit_per_share_growth is not None else "EBIT growth"
            if v >= 0.15:
                p.append(f"{label} {v:.0%} ({period}) — strong")
            elif v <= -0.05:
                n.append(f"{label} {v:.0%} ({period}) — declining")
        return p, n, []

    def _net_income_raw():
        return net_income_growth

    def _net_income_score(v):
        if net_income_turnaround:
            return 50.0
        if net_income_deterioration:
            return 0.0
        s = _growth_score(v, 0.30)
        if s is not None and one_off_risk:
            s = min(s, 60.0)
        return s

    def _net_income_desc(v):
        p, n = [], []
        if net_income_turnaround:
            p.append("Net income returned to profit; percentage growth is not meaningful")
        elif net_income_deterioration:
            n.append("Net income turned negative")
        if v is not None:
            years = (
                net_income_per_share_years
                if net_income_per_share_growth is not None
                else net_income_years
            )
            period = f"{years}y CAGR" if years > 1 else "YoY"
            label = (
                "Net income/share growth"
                if net_income_per_share_growth is not None
                else "Net income growth"
            )
            if v >= 0.15:
                p.append(f"{label} {v:.0%} ({period}) — strong")
            elif v <= -0.05:
                n.append(f"{label} {v:.0%} ({period}) — declining")
        return p, n, []

    def _recent_rev_desc(v):
        p, n, f = [], [], []
        if getattr(financial, "recent_growth_acceleration", False):
            p.append(f"Latest-quarter revenue growth {v:.0%} — accelerating")
        elif getattr(financial, "recent_growth_slowdown", False):
            n.append(f"Latest-quarter revenue growth {v:.0%} — slowing")
            f.append("recent_growth_slowdown")
        return p, n, f

    def _dilution_desc(_):
        n, f = [], []
        if getattr(financial, "share_dilution", False):
            period = f"{share_count_years}y CAGR" if share_count_years > 1 else "YoY"
            n.append(
                f"Share count growth {getattr(financial, 'share_count_growth', 0.0):.0%} "
                f"({period}) — dilution"
            )
            f.append("share_dilution")
        return [], n, f

    def _one_off_desc(_):
        n = []
        if one_off_risk:
            n.append("Exceptional earnings jump versus revenue — possible one-off or base effect")
        return [], n, []

    return [
        Metric(
            "revenue_growth",
            _revenue_raw,
            _revenue_score,
            transformation="growth[scale=0.20]",
            dependencies=("revenue",),
            describe=_revenue_desc,
        ),
        Metric(
            "ebit_growth",
            _ebit_raw,
            _ebit_score,
            transformation="growth[scale=0.30]",
            dependencies=("ebit",),
            describe=_ebit_desc,
        ),
        Metric(
            "net_income_growth",
            _net_income_raw,
            _net_income_score,
            transformation="growth[scale=0.30]",
            dependencies=("net_income",),
            describe=_net_income_desc,
        ),
        Metric(
            "recent_revenue_growth",
            lambda: recent_revenue_growth,
            lambda v: _growth_score(v, 0.20) if v is not None else None,
            transformation="growth[scale=0.20]",
            dependencies=("recent_revenue",),
            describe=_recent_rev_desc,
        ),
        Metric(
            "share_dilution",
            lambda: 1.0 if getattr(financial, "share_dilution", False) else 0.0,
            lambda _: None,
            weight=0.0,
            transformation="flag_only",
            describe=_dilution_desc,
        ),
        Metric(
            "earnings_one_off_risk",
            lambda: 1.0 if one_off_risk else 0.0,
            lambda _: None,
            weight=0.0,
            transformation="flag_only",
            describe=_one_off_desc,
        ),
    ]


def score_growth(financial: FinancialResult | None) -> dict:
    if financial is None:
        return {
            "score": 0.0,
            "positives": [],
            "negatives": [],
            "missing": ["financial data not available"],
            "available": False,
        }

    metric_defs = _growth_metrics_definitions(financial)
    results = [m.evaluate() for m in metric_defs]

    scores = [r.score for r in results if r.score is not None]
    positives = [p for r in results for p in r.positives]
    negatives = [n for r in results for n in r.negatives]
    missing = [m for r in results for m in r.missing if r.weight > 0]
    flags = [f for r in results for f in r.flags]

    score = sum(scores) / len(scores) if scores else 0.0
    return {
        "score": score,
        "positives": positives,
        "negatives": negatives,
        "missing": missing,
        "flags": flags,
        "available": bool(scores),
    }


def _valuation_metrics_definitions(
    valuation, debt_to_equity, quality_score, growth_score
) -> list[Metric]:
    raw_earnings_yield = getattr(valuation, "raw_earnings_yield", None)
    raw_fcf_yield = getattr(valuation, "raw_fcf_yield", None)
    earnings_yield = (
        raw_earnings_yield if raw_earnings_yield is not None else valuation.earnings_yield
    )
    fcf_yield = raw_fcf_yield if raw_fcf_yield is not None else valuation.free_cash_flow_yield

    def _fcf_yield_desc(v):
        p, n = [], []
        if v >= 0.08:
            p.append(f"FCF yield {v:.1%} — attractive")
        elif v < 0:
            n.append(f"Free cash flow is negative (yield {v:.1%})")
        elif v <= 0.02:
            n.append(f"FCF yield {v:.1%} — expensive")
        return p, n, []

    def _earn_yield_desc(v):
        p, n = [], []
        if v >= 0.08:
            p.append(f"Earnings yield {v:.1%} — attractive")
        elif v <= 0.02:
            n.append(f"Earnings yield {v:.1%} — expensive")
        return p, n, []

    def _ev_ebit_desc(v):
        p, n = [], []
        if v <= 20:
            p.append(f"EV/EBIT at {v:.0f}th percentile — cheap vs history")
        elif v >= 80:
            n.append(f"EV/EBIT at {v:.0f}th percentile — expensive vs history")
        return p, n, []

    def _pe_desc(v):
        p, n = [], []
        if v <= 20:
            p.append(f"PE at {v:.0f}th percentile — cheap vs history")
        elif v >= 80:
            n.append(f"PE at {v:.0f}th percentile — expensive vs history")
        return p, n, []

    def _mos_raw():
        return compute_margin_of_safety(
            fcf_yield,
            debt_to_equity=debt_to_equity,
            quality_score=quality_score if quality_score is not None else 50.0,
            growth_score=growth_score if growth_score is not None else 50.0,
        )

    def _mos_desc(v):
        p, n, f = [], [], []
        if v is not None:
            if v >= 0.03:
                p.append(f"Margin of safety {v:.1%} — large discount to required return")
            elif fcf_yield is not None and fcf_yield < 0:
                n.append("No FCF-based margin of safety while free cash flow is negative")
            elif v <= -0.03:
                n.append(f"Margin of safety {v:.1%} — trading above required return")

            if v <= -0.02:
                f.append("low_margin_of_safety")
        return p, n, f

    return [
        Metric(
            "fcf_yield",
            lambda: fcf_yield,
            lambda v: _linear_score(v, 0.0, 0.10),
            weight=0.24,
            transformation="linear[0,0.10]",
            dependencies=("free_cash_flow", "market_cap"),
            describe=_fcf_yield_desc,
        ),
        Metric(
            "earnings_yield",
            lambda: earnings_yield,
            lambda v: _linear_score(v, 0.0, 0.10),
            weight=0.16,
            transformation="linear[0,0.10]",
            dependencies=("net_income", "market_cap"),
            describe=_earn_yield_desc,
        ),
        Metric(
            "ev_ebit_percentile",
            lambda: valuation.ev_ebit_percentile,
            lambda v: _inverted_linear_score(v, 0.0, 100.0),
            weight=0.16,
            transformation="inverted_linear[0,100]",
            dependencies=("ev_ebit_history",),
            describe=_ev_ebit_desc,
        ),
        Metric(
            "pe_percentile",
            lambda: valuation.pe_percentile,
            lambda v: _inverted_linear_score(v, 0.0, 100.0),
            weight=0.12,
            transformation="inverted_linear[0,100]",
            dependencies=("pe_history",),
            describe=_pe_desc,
        ),
        Metric(
            "margin_of_safety",
            _mos_raw,
            lambda v: _linear_score(v, -0.05, 0.05),
            weight=0.12,
            transformation="linear[-0.05,0.05]",
            dependencies=("fcf_yield", "debt_to_equity", "quality_score", "growth_score"),
            cross_category_dependencies=("quality_score", "growth_score"),
            describe=_mos_desc,
        ),
        Metric(
            "price_to_book",
            lambda: getattr(valuation, "price_to_book", None),
            lambda v: _inverted_linear_score(v, 0.0, 5.0),
            weight=0.10,
            transformation="inverted_linear[0,5.0]",
            dependencies=("equity", "market_cap"),
        ),
        Metric(
            "dividend_yield",
            lambda: getattr(valuation, "dividend_yield", None),
            lambda v: _linear_score(v, 0.0, 8.0),
            weight=0.10,
            transformation="linear[0,8.0]",
            dependencies=("dividends", "market_cap"),
        ),
    ]


def score_valuation(
    valuation: ValuationResult | None,
    debt_to_equity: float | None = None,
    quality_score: float | None = None,
    growth_score: float | None = None,
) -> dict:
    if valuation is None:
        return {
            "score": 0.0,
            "positives": [],
            "negatives": [],
            "missing": ["valuation data not available"],
            "available": False,
        }

    metric_defs = _valuation_metrics_definitions(
        valuation, debt_to_equity, quality_score, growth_score
    )
    results = [m.evaluate() for m in metric_defs]

    available = [r for r in results if r.score is not None]
    total_weight = sum(r.weight for r in available)
    score = sum(r.score * r.weight for r in available) / total_weight if total_weight else 0.0

    positives = [p for r in results for p in r.positives]
    negatives = [n for r in results for n in r.negatives]
    missing = [m for r in results for m in r.missing]
    flags = [f for r in results for f in r.flags]

    return {
        "score": score,
        "positives": positives,
        "negatives": negatives,
        "missing": missing,
        "flags": flags,
        "available": bool(available),
    }


def _balance_metrics_definitions(financial, fundamental_kpis) -> list[Metric]:
    fundamental_kpis = fundamental_kpis or {}
    dte = financial.debt_to_equity
    net_debt = getattr(financial, "net_debt", None)
    equity = getattr(financial, "equity", None)
    net_debt_ebitda = fundamental_kpis.get(KpiIds.NET_DEBT_EBITDA)

    def _dte_score(v):
        if equity is not None and equity <= 0:
            return 0.0
        if net_debt is not None and net_debt < 0:
            return 100.0
        if v is None:
            return None
        if v < 0:
            return 0.0
        if v == 0:
            return 100.0
        return _inverted_linear_score(v, 0.0, 2.0)

    def _dte_desc(v):
        p, n = [], []
        if equity is not None and equity <= 0:
            n.append("Negative equity — balance-sheet risk")
        elif net_debt is not None and net_debt < 0:
            p.append("Net cash position")
        elif v is not None:
            if v < 0:
                n.append(f"D/E {v:.1f}x — negative equity or invalid")
            elif v == 0:
                p.append("Net D/E 0.0x — no debt")
            elif v <= 0.3:
                p.append(f"Net D/E {v:.1f}x — low leverage")
            elif v >= 1.5:
                n.append(f"Net D/E {v:.1f}x — high leverage")
        return p, n, []

    def _nde_score(v):
        if equity is not None and equity <= 0:
            return None
        return _inverted_linear_score(v, 0.0, 4.0)

    def _nde_desc(v):
        p, n = [], []
        if v is not None:
            if v <= 1:
                p.append(f"Net debt/EBITDA {v:.1f}x — low")
            elif v >= 3:
                n.append(f"Net debt/EBITDA {v:.1f}x — high")
        return p, n, []

    return [
        Metric(
            "debt_to_equity",
            lambda: dte,
            _dte_score,
            transformation="inverted_linear[0,2.0]",
            dependencies=("total_debt", "equity"),
            describe=_dte_desc,
        ),
        Metric(
            "net_debt_ebitda",
            lambda: net_debt_ebitda,
            _nde_score,
            transformation="inverted_linear[0,4.0]",
            dependencies=("net_debt", "ebitda"),
            describe=_nde_desc,
        ),
    ]


def score_balance_sheet(
    financial: FinancialResult | None,
    fundamental_kpis: dict[int, float | None] | None = None,
) -> dict:
    if financial is None:
        return {
            "score": 0.0,
            "positives": [],
            "negatives": [],
            "missing": ["financial data not available"],
            "available": False,
        }

    metric_defs = _balance_metrics_definitions(financial, fundamental_kpis)
    results = [m.evaluate() for m in metric_defs]

    scores = [r.score for r in results if r.score is not None]
    positives = [p for r in results for p in r.positives]
    negatives = [n for r in results for n in r.negatives]
    missing = [m for r in results for m in r.missing]

    score = sum(scores) / len(scores) if scores else 0.0
    return {
        "score": score,
        "positives": positives,
        "negatives": negatives,
        "missing": missing,
        "available": bool(scores),
    }
