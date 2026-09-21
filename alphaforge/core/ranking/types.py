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
    # Ineligible scores are diagnostic only.  They are kept in an explicit
    # section so a missing-data 0.0 can never look like an economic rank.
    ranking_section: str = "ranked"
    eligibility_reasons: list[str] = field(default_factory=list)
    readiness_status: str | None = None
    readiness_blockers: list[str] = field(default_factory=list)
    readiness_limitations: list[str] = field(default_factory=list)
    # Frozen evidence provenance is carried with every ranked score.  Ranking
    # must never replace this with a hash of the score JSON itself.
    evidence_packet_hash: str | None = None
    scoring_audit: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.rank_eligible and self.ranking_section == "ranked":
            self.ranking_section = "unranked_missing_data"

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
            "ranking_section": self.ranking_section,
            "eligibility_reasons": self.eligibility_reasons,
            "readiness_status": self.readiness_status,
            "readiness_blockers": self.readiness_blockers,
            "readiness_limitations": self.readiness_limitations,
            "evidence_packet_hash": self.evidence_packet_hash,
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
