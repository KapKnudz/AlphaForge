from dataclasses import dataclass, field

from alphaforge.core.types import DataQuality, RankingModel


@dataclass
class CompanyScore:
    company_id: int
    ticker: str
    name: str

    quality_score: float = 0.0
    growth_score: float = 0.0
    valuation_score: float = 0.0
    balance_sheet_score: float = 0.0

    total_score: float = 0.0

    positives: list[str] = field(default_factory=list)
    negatives: list[str] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    data_quality: DataQuality = DataQuality.MEDIUM
    candidate_reason: str | None = None
    ranking_model: RankingModel = RankingModel.GENERAL
    rank_eligible: bool = True
    eligibility_reasons: list[str] = field(default_factory=list)
    scoring_audit: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "company_id": self.company_id,
            "ticker": self.ticker,
            "name": self.name,
            "quality_score": self.quality_score,
            "growth_score": self.growth_score,
            "valuation_score": self.valuation_score,
            "balance_sheet_score": self.balance_sheet_score,
            "total_score": self.total_score,
            "positives": self.positives,
            "negatives": self.negatives,
            "missing_data": self.missing_data,
            "flags": self.flags,
            "data_quality": self.data_quality,
            "candidate_reason": self.candidate_reason,
            "ranking_model": self.ranking_model,
            "rank_eligible": self.rank_eligible,
            "eligibility_reasons": self.eligibility_reasons,
            "scoring_audit": self.scoring_audit,
        }


@dataclass
class WatchlistRanking:
    scores: list[CompanyScore]

    def top_n(self, n: int) -> list[CompanyScore]:
        return self.scores[:n]

    def shortlist_for_agent(
        self,
        top_n: int = 25,
        include_flags: bool = True,
        max_total: int = 30,
    ) -> list[CompanyScore]:
        shortlist = list(self.scores[:top_n])
        shortlist = [score for score in shortlist if score.rank_eligible]

        if include_flags:
            important_flags = {
                "cheap_quality",
                "fcf_quality",
                "cheap_but_weak_growth",
                "insider_buying_support",
                "major_recent_news",
                "ceo_outlook_positive",
                "turnaround_candidate",
            }

            for score in self.scores[top_n:]:
                if not score.rank_eligible:
                    continue
                if any(flag in important_flags for flag in score.flags):
                    shortlist.append(score)

                if len(shortlist) >= max_total:
                    break

        return shortlist
