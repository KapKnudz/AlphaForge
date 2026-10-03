from alphaforge.core.kpi_taxonomy import KpiIds
from alphaforge.core.ranking.audit import (
    build_general_scoring_audit,
)
from alphaforge.core.ranking.score_rules import (
    score_balance_sheet,
    score_growth,
    score_quality,
    score_valuation,
)
from alphaforge.core.ranking.sector_rules import (
    ranking_model_for_branch,
    score_bank,
    score_property,
)
from alphaforge.core.ranking.types import (
    CompanyScore,
    WatchlistRanking,
)
from alphaforge.core.types import DataQuality, RankingModel


def _has_material_data(value, fields: tuple[str, ...]) -> bool:
    if value is None:
        return False
    if isinstance(value, dict):
        return any(value.get(field) is not None for field in fields)
    return any(getattr(value, field, None) is not None for field in fields)


def _rank_eligibility(ranking_model, financial, valuation, sector_data):
    reasons = []
    if not _has_material_data(
        financial,
        ("revenue", "operating_margin", "net_margin", "equity", "net_income"),
    ):
        reasons.append("financial data not available")

    # Unpack the new {current, histories} structure, falling back for plain-dict callers.
    sector_kpis = sector_data.get("current", {}) if isinstance(sector_data, dict) else sector_data
    if not isinstance(sector_data, dict) or "current" not in sector_data:
        sector_kpis = sector_data or {}

    if ranking_model == RankingModel.PROPERTY:
        required = {
            KpiIds.PROPERTY_OCCUPANCY: "occupancy",
            KpiIds.PROPERTY_INTEREST_COVERAGE: "interest coverage",
            KpiIds.PROPERTY_LTV: "LTV",
        }
        for kpi_id, label in required.items():
            if sector_kpis.get(kpi_id) is None:
                reasons.append(f"property {label} not available")
        if all(
            sector_kpis.get(kpi_id) is None
            for kpi_id in (
                KpiIds.PROPERTY_NAV_DISCOUNT,
                KpiIds.PROPERTY_PRICE_TO_INCOME,
            )
        ):
            reasons.append("property valuation KPI not available")
    elif ranking_model == RankingModel.BANK:
        required = {
            KpiIds.BANK_COST_INCOME: "cost/income",
            KpiIds.BANK_CREDIT_LOSSES: "credit losses",
            KpiIds.BANK_CET1: "CET1",
            KpiIds.BANK_CAPITAL_ADEQUACY: "capital adequacy",
        }
        for kpi_id, label in required.items():
            if sector_kpis.get(kpi_id) is None:
                reasons.append(f"bank {label} not available")
    elif not _has_material_data(
        valuation, ("pe", "ev_ebit", "pb", "ps", "pfcf", "market_cap", "enterprise_value")
    ):
        reasons.append("valuation data not available")

    return not reasons, reasons


def _compute_flags(
    quality: dict, growth: dict, val: dict, balance: dict, missing_data: list[str]
) -> list[str]:
    q = quality["score"]
    g = growth["score"]
    v = val["score"]
    b = balance["score"]

    # Cross-category flags
    quality_available = quality.get("available", True)
    growth_available = growth.get("available", True)
    valuation_available = val.get("available", True)
    balance_available = balance.get("available", True)

    predicates = (
        (quality_available and valuation_available and q >= 75 and v >= 70, "cheap_quality"),
        (
            quality_available and valuation_available and q >= 75 and v <= 30,
            "high_quality_expensive",
        ),
        (
            growth_available and valuation_available and g >= 75 and v <= 30,
            "strong_growth_expensive",
        ),
        (growth_available and valuation_available and g < 25 and v >= 70, "cheap_but_weak_growth"),
        (balance_available and b <= 25, "balance_sheet_risk"),
        (any("FCF margin" in p for p in quality["positives"]) and v >= 70, "fcf_quality"),
        (growth_available and g < 25, "negative_growth"),
        (
            any("possible one-off" in warning for warning in growth["negatives"]),
            "earnings_one_off_risk",
        ),
        (len(missing_data) >= 4, "low_data_quality"),
    )
    return [label for condition, label in predicates if condition]


OPTIONAL_METRICS = {
    "roic not available",
    "recent_revenue_growth not available",
    "price_to_book not available",
    "dividend_yield not available",
    "net_debt_ebitda not available",
}


def _compute_data_quality(missing_data: list[str]) -> DataQuality:
    missing_count = sum(item not in OPTIONAL_METRICS for item in missing_data)
    if missing_count == 0:
        return DataQuality.HIGH
    elif missing_count <= 3:
        return DataQuality.MEDIUM
    else:
        return DataQuality.LOW


def _compute_candidate_reason(quality: dict, growth: dict, val: dict, balance: dict) -> str:
    q = quality["score"]
    v = val["score"]

    if q >= 75 and v >= 70:
        return "High-quality company with attractive valuation."
    elif q >= 75 and v < 40:
        return "Strong business, but valuation looks demanding."
    elif v >= 70 and q < 40:
        return "Cheap valuation, but business quality is weak."
    elif q >= 75:
        return "High-quality business with moderate valuation."
    elif v >= 70:
        return "Attractive valuation with mixed quality."
    else:
        return "Mixed profile."


class RankingEngine:
    RANKING_MODEL_VERSION = "2026-10-03-dcf-availability-diagnostics-v18"

    def __init__(self, ranking_repository=None):
        self.ranking_repository = ranking_repository

    def rank(self, companies: list, results_by_company: dict[int, dict]) -> WatchlistRanking:
        scores: list[CompanyScore] = []

        for company in companies:
            results = results_by_company.get(company.id, {})
            research_evidence = results.get("research_evidence") or {}
            evidence_packet = research_evidence.get("evidence_packet")
            evidence_packet_hash = (
                str(evidence_packet.get("packet_hash"))
                if isinstance(evidence_packet, dict) and evidence_packet.get("packet_hash")
                else None
            )
            financial = results.get("financial")
            valuation = results.get("valuation")
            sector_kpis = results.get("sector_kpis") or {}
            fundamental_kpis = results.get("fundamental_kpis")
            ranking_model = ranking_model_for_branch(company.branch_id)
            rank_eligible, eligibility_reasons = _rank_eligibility(
                ranking_model,
                financial,
                valuation,
                sector_kpis,
            )

            if ranking_model == RankingModel.PROPERTY:
                quality, growth, val, balance = score_property(
                    financial,
                    valuation,
                    sector_kpis,
                )
            elif ranking_model == RankingModel.BANK:
                quality, growth, val, balance = score_bank(
                    financial,
                    valuation,
                    sector_kpis,
                )
            else:
                quality = score_quality(financial, fundamental_kpis)
                growth = score_growth(financial)
                balance = score_balance_sheet(financial, fundamental_kpis)

                # Pass quality/growth/leverage context into valuation scoring so the
                # margin-of-safety component can adjust the required-return spread.
                dte = financial.debt_to_equity if financial else None
                val = score_valuation(
                    valuation,
                    debt_to_equity=dte,
                    quality_score=quality["score"] if quality else None,
                    growth_score=growth["score"] if growth else None,
                )

            if ranking_model == RankingModel.PROPERTY:
                weights = (0.25, 0.15, 0.30, 0.30)
            elif ranking_model == RankingModel.BANK:
                weights = (0.30, 0.20, 0.25, 0.25)
            else:
                weights = (0.30, 0.25, 0.30, 0.15)
            scoring_audit = {}
            if ranking_model == RankingModel.GENERAL:
                scoring_audit = build_general_scoring_audit(
                    financial,
                    valuation,
                    fundamental_kpis,
                    {
                        "quality": quality,
                        "growth": growth,
                        "valuation": val,
                        "balance_sheet": balance,
                    },
                    weights,
                )
            # Applies to general and sector scoring; the missing/zero distinction
            # and exact consumed window remain inspectable in ranking.json/audit.
            if results.get("dividend_yield") is not None:
                yield_audit = results["dividend_yield"]
                scoring_audit["dividend_yield"] = yield_audit
                for component in scoring_audit.get("valuation", {}).get("components", []):
                    if component["name"] == "dividend_yield":
                        component["provenance"] = (
                            "verified_dividend_window"
                            if yield_audit["value"] is not None
                            else "dividend_window_unavailable"
                        )
                        component["unavailable_reason"] = yield_audit["reason"]
                        component["policy_version"] = yield_audit["policy_version"]
            total = sum(
                score["score"] * weight
                for score, weight in zip((quality, growth, val, balance), weights, strict=False)
            )

            missing_data = (
                quality["missing"] + growth["missing"] + val["missing"] + balance["missing"]
            )
            selection = results.get("selection") or {}
            missing_data += selection.get("refusal_reasons", [])
            missing_data = list(dict.fromkeys(missing_data))
            flags = _compute_flags(quality, growth, val, balance, missing_data)
            for category in (quality, growth, val, balance):
                for flag in category.get("flags", []):
                    if flag not in flags:
                        flags.append(flag)
            data_quality = _compute_data_quality(missing_data)
            if not rank_eligible:
                data_quality = DataQuality.LOW
                if "incomplete_data" not in flags:
                    flags.append("incomplete_data")
            candidate_reason = _compute_candidate_reason(quality, growth, val, balance)
            revenue_growth = getattr(financial, "revenue_growth", None)
            ebit_growth = getattr(financial, "ebit_growth", None)
            net_income_growth = getattr(financial, "net_income_growth", None)
            revenue_per_share_growth = getattr(financial, "revenue_per_share_growth", None)
            ebit_per_share_growth = getattr(financial, "ebit_per_share_growth", None)
            net_income_per_share_growth = getattr(financial, "net_income_per_share_growth", None)
            fcf_per_share_growth = getattr(financial, "fcf_per_share_growth", None)
            book_value_per_share_growth = getattr(financial, "book_value_per_share_growth", None)
            share_count_growth = getattr(financial, "share_count_growth", None)

            cs = CompanyScore(
                company_id=company.id,
                ticker=company.ticker,
                name=company.name,
                quality_score=round(quality["score"], 1),
                growth_score=round(growth["score"], 1),
                valuation_score=round(val["score"], 1),
                balance_sheet_score=round(balance["score"], 1),
                total_score=round(total, 1),
                revenue_growth=revenue_growth,
                revenue_growth_years=(
                    getattr(financial, "revenue_growth_years", 0)
                    if revenue_growth is not None
                    else 0
                ),
                ebit_growth=ebit_growth,
                ebit_growth_years=(
                    getattr(financial, "ebit_growth_years", 0) if ebit_growth is not None else 0
                ),
                net_income_growth=net_income_growth,
                net_income_growth_years=(
                    getattr(financial, "net_income_growth_years", 0)
                    if net_income_growth is not None
                    else 0
                ),
                revenue_per_share_growth=revenue_per_share_growth,
                revenue_per_share_growth_years=(
                    getattr(financial, "revenue_per_share_growth_years", 0)
                    if revenue_per_share_growth is not None
                    else 0
                ),
                ebit_per_share_growth=ebit_per_share_growth,
                ebit_per_share_growth_years=(
                    getattr(financial, "ebit_per_share_growth_years", 0)
                    if ebit_per_share_growth is not None
                    else 0
                ),
                net_income_per_share_growth=net_income_per_share_growth,
                net_income_per_share_growth_years=(
                    getattr(financial, "net_income_per_share_growth_years", 0)
                    if net_income_per_share_growth is not None
                    else 0
                ),
                fcf_per_share_growth=fcf_per_share_growth,
                fcf_per_share_growth_years=(
                    getattr(financial, "fcf_per_share_growth_years", 0)
                    if fcf_per_share_growth is not None
                    else 0
                ),
                book_value_per_share_growth=book_value_per_share_growth,
                book_value_per_share_growth_years=(
                    getattr(financial, "book_value_per_share_growth_years", 0)
                    if book_value_per_share_growth is not None
                    else 0
                ),
                share_count_growth=share_count_growth,
                share_count_growth_years=(
                    getattr(financial, "share_count_growth_years", 0)
                    if share_count_growth is not None
                    else 0
                ),
                positives=quality["positives"]
                + growth["positives"]
                + val["positives"]
                + balance["positives"],
                negatives=quality["negatives"]
                + growth["negatives"]
                + val["negatives"]
                + balance["negatives"],
                missing_data=missing_data,
                flags=flags,
                data_quality=data_quality,
                candidate_reason=candidate_reason,
                ranking_model=ranking_model,
                rank_eligible=rank_eligible,
                ranking_section=(
                    "ranked"
                    if rank_eligible
                    else (
                        "unranked_method_unsupported"
                        if ranking_model != RankingModel.GENERAL
                        else "unranked_missing_data"
                    )
                ),
                eligibility_reasons=eligibility_reasons,
                evidence_packet_hash=evidence_packet_hash,
                scoring_audit=scoring_audit,
                input_selection=selection,
            )
            scores.append(cs)

        # Never let database/input order decide a tie.  Eligible companies are
        # ordered by score; ties and the explicit unranked sections use stable
        # identity keys.  Ineligible numeric scores are diagnostic only.
        scores.sort(
            key=lambda s: (
                0 if s.rank_eligible else 1,
                -s.total_score if s.rank_eligible else 0.0,
                s.ranking_section,
                s.ticker.casefold(),
                s.name.casefold(),
                s.company_id,
            )
        )

        if self.ranking_repository is not None:
            eligible_count = sum(1 for s in scores if s.rank_eligible)
            self.ranking_repository.save_ranking_run(
                model_version=self.RANKING_MODEL_VERSION,
                company_count=len(scores),
                eligible_count=eligible_count,
                scores=[s.to_dict() for s in scores],
                inputs_summary={
                    "ranking_type": "deterministic_watchlist",
                    "total_companies": len(companies),
                    "eligible_count": eligible_count,
                    "ranking_models_used": list({s.ranking_model for s in scores}),
                },
            )

        return WatchlistRanking(scores=scores)
