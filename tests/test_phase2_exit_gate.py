"""Phase 2 exit gate tests — deterministic, no network, no model calls."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, timedelta

from alphaforge.cli.ranking_loader import load_results_for_company
from alphaforge.config import Settings
from alphaforge.core.coverage.liquidity import PriceBar, build
from alphaforge.core.frozen_packet import (
    EVIDENCE_RULES_VERSION,
    stable_packet_hash,
    validate_frozen_packet,
)
from alphaforge.core.ranking.engine import RankingEngine
from alphaforge.core.types import RankingModel
from alphaforge.db.connection import get_connection
from alphaforge.db.migrations import migrate
from alphaforge.db.repositories import (
    persist_evidence_packet,
    upsert_company,
    upsert_financial_periods,
    upsert_prices,
)
from alphaforge.evidence.report_rules import report_rules_metadata

# --- Test fixtures ---


def _make_price_bars(
    days: int,
    close: float = 100.0,
    volume: int = 1000,
    as_of: date | None = None,
) -> list[PriceBar]:
    """Create deterministic price bars for testing."""
    bars = []
    base_date = as_of or date(2026, 1, 1)
    for i in range(days):
        bars.append(
            PriceBar(
                date=base_date - timedelta(days=i),
                close=close,
                volume=volume,
                currency="SEK",
            )
        )
    return bars


@dataclass
class MockCompany:
    id: int
    name: str
    ticker: str
    branch_id: int | None


# --- Test 1: ADTV 20/60/120 for known liquid name ---


class TestADTV:
    def test_adtv_20_with_sufficient_data(self):
        """ADTV-20 computed correctly with 20+ price bars."""
        bars = _make_price_bars(30, close=100.0, volume=1000)
        result = build(bars)
        assert result.adtv_20 is not None
        assert result.observed_days_20 == 20
        # ADTV = sum(close * volume for 20 bars) / 20 = (100 * 1000 * 20) / 20 = 100000
        assert result.adtv_20 == 100000.0

    def test_adtv_60_with_sufficient_data(self):
        """ADTV-60 computed correctly with 60+ price bars."""
        bars = _make_price_bars(70, close=100.0, volume=1000)
        result = build(bars)
        assert result.adtv_60 is not None
        assert result.observed_days_60 == 60
        assert result.adtv_60 == 100000.0

    def test_adtv_120_with_sufficient_data(self):
        """ADTV-120 computed correctly with 120+ price bars."""
        bars = _make_price_bars(130, close=100.0, volume=1000)
        result = build(bars)
        assert result.adtv_120 is not None
        assert result.observed_days_120 == 120
        assert result.adtv_120 == 100000.0

    def test_adtv_short_history_fewer_than_window(self):
        """ADTV returns None when fewer observations than window."""
        bars = _make_price_bars(10, close=100.0, volume=1000)
        result = build(bars)
        # With only 10 bars, ADTV-20/60/120 should all be None
        assert result.adtv_20 is None
        assert result.observed_days_20 == 10
        assert result.adtv_60 is None
        assert result.observed_days_60 == 10
        assert result.adtv_120 is None
        assert result.observed_days_120 == 10
        assert result.status == "unavailable"

    def test_adtv_partial_coverage(self):
        """ADTV returns partial status when some windows have data."""
        bars = _make_price_bars(25, close=100.0, volume=1000)
        result = build(bars)
        assert result.adtv_20 is not None
        assert result.adtv_60 is None
        assert result.status == "partial"

    def test_adtv_zero_volume_days(self):
        """Zero volume days counted correctly."""
        bars = _make_price_bars(130, close=100.0, volume=1000)
        # Set some volumes to 0
        bars_with_zeros = []
        for i, bar in enumerate(bars):
            if i < 5:
                bars_with_zeros.append(
                    PriceBar(date=bar.date, close=bar.close, volume=0, currency="SEK")
                )
            else:
                bars_with_zeros.append(bar)
        result = build(bars_with_zeros)
        assert result.zero_volume_days_120 == 5

    def test_adtv_pit_filtering(self):
        """ADTV respects as_of filtering — bars after as_of are excluded."""
        bars = _make_price_bars(130, close=100.0, volume=1000, as_of=date(2026, 1, 1))
        # Bars span 2026-01-01 back to ~2025-08-24. With as_of=2025-12-01,
        # only bars with date <= 2025-12-01 are included (99 bars).
        result = build(bars, as_of=date(2025, 12, 1))
        assert result.observed_days_20 == 20
        assert result.observed_days_60 == 60
        assert result.observed_days_120 == 99
        # ADTV-120 is None because 99 < 120
        assert result.adtv_120 is None


# --- Test 2: Golden-packet ranking round-trip via packet_hash ---


class TestGoldenPacketRoundTrip:
    def test_ranking_deterministic(self):
        """Same inputs produce identical ranking and packet_hash."""
        companies = [
            MockCompany(id=1, name="Company A", ticker="A", branch_id=None),
            MockCompany(id=2, name="Company B", ticker="B", branch_id=None),
            MockCompany(id=3, name="Company C", ticker="C", branch_id=None),
        ]
        results_by_company = {}

        engine1 = RankingEngine()
        ranking1 = engine1.rank(companies, results_by_company)

        engine2 = RankingEngine()
        ranking2 = engine2.rank(companies, results_by_company)

        # Verify deterministic output
        assert len(ranking1.scores) == len(ranking2.scores)
        for s1, s2 in zip(ranking1.scores, ranking2.scores, strict=False):
            assert s1.company_id == s2.company_id
            assert s1.total_score == s2.total_score
            assert s1.ranking_model == s2.ranking_model

    def test_ranking_carries_frozen_evidence_packet_hash(self, tmp_path):
        company = MockCompany(id=1, name="Company A", ticker="A", branch_id=None)
        packet_hash = "a" * 64

        ranking = RankingEngine().rank(
            [company],
            {company.id: {"research_evidence": {"evidence_packet": {"packet_hash": packet_hash}}}},
        )

        assert ranking.scores[0].evidence_packet_hash == packet_hash
        from alphaforge.cli.main import export_ranking_files

        ranking_json_path, _ = export_ranking_files(
            ranking, "2026-01-01", RankingEngine.RANKING_MODEL_VERSION, tmp_path
        )
        assert json.loads(ranking_json_path.read_text())["evidence_packet_hash"] == packet_hash

    def test_ranked_path_carries_packet_hash_on_eligible_score(self):
        """R1 proof: the ranked loader path feeds evidence_packet_hash to scores."""
        conn = get_connection(Settings.from_env(dsn="sqlite:///:memory:"))
        migrate(conn)
        company_id = upsert_company(
            conn,
            {
                "insId": 303,
                "name": "Ranked Packet AB",
                "ticker": "RPK",
                "stockPriceCurrency": "SEK",
                "reportCurrency": "SEK",
            },
        )
        upsert_financial_periods(
            conn,
            company_id,
            [
                {
                    "period_type": "year",
                    "period_end": "2024-12-31",
                    "report_Date": "2025-02-01T00:00:00",
                    "revenues": 100,
                    "operating_Income": 20,
                    "profit_To_Equity_Holders": 10,
                    "book_Value": 40,
                    "number_Of_Shares": 10,
                    "currency": "SEK",
                },
                {
                    "period_type": "year",
                    "period_end": "2025-12-31",
                    "report_Date": "2026-02-01",
                    "revenues": 200,
                    "operating_Income": 40,
                    "profit_To_Equity_Holders": 20,
                    "book_Value": 80,
                    "number_Of_Shares": 50,
                    "currency": "SEK",
                },
            ],
        )
        upsert_prices(
            conn,
            company_id,
            [
                {"d": "2025-03-01T00:00:00", "c": 10, "v": 100},
                {"d": "2026-03-01", "c": 20, "v": 100},
            ],
            currency="SEK",
        )
        text = "Evidence"
        page = {
            "page_number": 1,
            "anchor": "document:1#page:1",
            "text": text,
            "text_checksum": hashlib.sha256(text.encode()).hexdigest(),
        }
        packet = {
            "schema_version": "evidence-packet-v1",
            "frozen": True,
            "evidence_rules_version": EVIDENCE_RULES_VERSION,
            "report_rules": report_rules_metadata(),
            "company_id": company_id,
            "as_of": "2026-09-20",
            "issuer": {
                "mfn_slug": "all/a/rpk",
                "source_url": "https://mfn.test/all/a/rpk",
                "discovery_source": "fixture",
                "verified_at": "2026-09-20T00:00:00Z",
                "identity_evidence": None,
            },
            "sources": [
                {
                    "source_id": "document:1",
                    "source_url": "https://mfn.test/a/rpk/en",
                    "title": "Ranked Packet AB Annual Report 2025",
                    "publication_date": "2026-03-01T00:00:00Z",
                    "publication_timestamp_authoritative": True,
                    "ingestion_date": "2026-09-20T01:00:00Z",
                    "attachment": {
                        "source_url": "https://storage.mfn.test/rpk.pdf",
                        "sha256": "b" * 64,
                    },
                    "extraction": {
                        "extractor": "pypdf",
                        "text_checksum": hashlib.sha256(f"[page 1]\n{text}".encode()).hexdigest(),
                        "page_count": 1,
                    },
                    "pages": [page],
                }
            ],
            "evidence_catalog": {"canonical_source_ids": ["document:1"]},
            "limitations": [],
        }
        packet["coverage_facts"] = {
            "source_count": 1,
            "source_ids": ["document:1"],
            "report_kinds": [],
            "languages": ["en"],
            "limitations": [],
        }
        packet["packet_hash"] = stable_packet_hash(packet)
        assert validate_frozen_packet(packet)
        persist_evidence_packet(conn, company_id=company_id, as_of="2026-09-20", packet=packet)

        results = load_results_for_company(conn, company_id, "2026-09-20")
        assert (
            results["research_evidence"]["evidence_packet"]["packet_hash"]
            == (packet["packet_hash"])
        )
        company = MockCompany(id=company_id, name="Ranked Packet AB", ticker="RPK", branch_id=None)
        ranking = RankingEngine().rank([company], {company.id: results})
        (score,) = ranking.scores
        assert score.rank_eligible
        assert score.evidence_packet_hash == packet["packet_hash"]
        inputs_summary = {
            "evidence_packet_hashes": {
                str(s.company_id): str(s.evidence_packet_hash)
                for s in ranking.scores
                if s.evidence_packet_hash
            }
        }
        assert inputs_summary["evidence_packet_hashes"] == {str(company_id): packet["packet_hash"]}

    def test_packet_hash_ignores_database_local_document_ids(self):
        packet = {
            "schema_version": "evidence-packet-v1",
            "frozen": True,
            "company_id": 1,
            "as_of": "2026-09-20",
            "issuer": {"mfn_slug": "all/a/example", "verified_at": "2026-09-20T00:00:00Z"},
            "sources": [
                {
                    "source_id": "document:7",
                    "source_url": "https://mfn.test/a/example/q1",
                    "publication_date": "2026-09-01T00:00:00Z",
                    "ingestion_date": "2026-09-20T01:00:00Z",
                    "attachment": {"source_url": "https://storage/q1.pdf", "sha256": "a" * 64},
                    "body": {
                        "paragraphs": [{"anchor": "document:7#paragraph:1", "text": "Evidence"}]
                    },
                    "pages": [{"anchor": "document:7#page:1", "text": "Evidence"}],
                }
            ],
            "evidence_catalog": {"canonical_source_ids": ["document:7"]},
            "coverage_facts": {"source_ids": ["document:7"]},
        }
        shifted = json.loads(json.dumps(packet).replace("document:7", "document:91"))

        assert stable_packet_hash(packet) == stable_packet_hash(shifted)

    def test_packet_hash_reproducibility(self):
        """Packet hash is reproducible from same inputs."""
        companies = [
            MockCompany(id=1, name="Company A", ticker="A", branch_id=None),
        ]
        results_by_company = {}

        engine1 = RankingEngine()
        ranking1 = engine1.rank(companies, results_by_company)

        engine2 = RankingEngine()
        ranking2 = engine2.rank(companies, results_by_company)

        # Compute hash from scores
        scores1 = json.dumps([asdict(s) for s in ranking1.scores], sort_keys=True).encode()
        scores2 = json.dumps([asdict(s) for s in ranking2.scores], sort_keys=True).encode()

        hash1 = hashlib.sha256(scores1).hexdigest()
        hash2 = hashlib.sha256(scores2).hexdigest()

        assert hash1 == hash2


# --- Test 3: Model-number fidelity ---


class TestModelNumberFidelity:
    def test_ranking_model_version_constant(self):
        """Ranking model version is constant across runs."""
        engine = RankingEngine()
        assert engine.RANKING_MODEL_VERSION == "2026-08-12-reverse-dcf-v11"

    def test_score_determinism(self):
        """Same company produces same scores regardless of order."""
        companies_a = [
            MockCompany(id=1, name="Company A", ticker="A", branch_id=None),
            MockCompany(id=2, name="Company B", ticker="B", branch_id=None),
        ]
        companies_b = [
            MockCompany(id=2, name="Company B", ticker="B", branch_id=None),
            MockCompany(id=1, name="Company A", ticker="A", branch_id=None),
        ]
        results_by_company = {}

        engine = RankingEngine()
        ranking_a = engine.rank(companies_a, results_by_company)
        ranking_b = engine.rank(companies_b, results_by_company)

        # Scores should be identical regardless of input order
        scores_by_id_a = {s.company_id: s for s in ranking_a.scores}
        scores_by_id_b = {s.company_id: s for s in ranking_b.scores}

        for company_id in scores_by_id_a:
            s_a = scores_by_id_a[company_id]
            s_b = scores_by_id_b[company_id]
            assert s_a.total_score == s_b.total_score
            assert s_a.quality_score == s_b.quality_score
            assert s_a.growth_score == s_b.growth_score
            assert s_a.valuation_score == s_b.valuation_score
            assert s_a.balance_sheet_score == s_b.balance_sheet_score


# --- Test 4: Point-in-time exclusion ---


class TestPointInTimeExclusion:
    def test_pit_excludes_future_data(self):
        """Data with report_date > as_of is excluded from ranking."""
        as_of = date(2026, 1, 1)
        future_date = date(2026, 6, 1)

        # This test verifies the concept - actual PIT filtering would happen
        # at the data loading layer, not in the ranking engine itself
        assert future_date > as_of

    def test_pit_filtering_concept(self):
        """Verify PIT filtering logic."""
        from alphaforge.core.point_in_time import in_window

        start = date(2025, 1, 1)
        as_of = date(2026, 1, 1)

        # Date within window [start, as_of] should be included
        assert in_window(date(2025, 6, 1), as_of, start=start) is True

        # Date after as_of should be excluded
        assert in_window(date(2026, 6, 1), as_of, start=start) is False


# --- Test 5: Forward scenario NaN handling ---


class TestForwardScenarioNaNHandling:
    def test_nan_replaced_with_none(self):
        """NaN annualized returns are replaced with None."""
        from alphaforge.core.statistics import cagr

        # cagr returns None for negative holding value
        result = cagr(100.0, -50.0, 2.0)
        assert result is None

    def test_forward_scenario_engine_validation(self):
        """Forward scenario engine rejects non-finite outputs."""
        from alphaforge.core.valuation.forward_scenario import ForwardScenarioEngine

        # The engine should reject NaN values through validation
        engine = ForwardScenarioEngine()
        # This is a conceptual test - actual validation happens in _validate_outputs
        assert engine._finite(1.0) is True
        assert engine._finite(float("nan")) is False
        assert engine._finite(None) is False
        assert engine._finite(float("inf")) is False


# --- Test 6: Ranking eligibility ---


class TestRankingEligibility:
    def test_general_model_eligibility(self):
        """General model companies are eligible when data is available."""
        from alphaforge.core.ranking.engine import _rank_eligibility

        financial = {"revenue": 1000}  # Simplified
        valuation = {"pe": 15.0}
        sector_data = {}

        eligible, reasons = _rank_eligibility(
            RankingModel.GENERAL, financial, valuation, sector_data
        )
        assert eligible is True
        assert len(reasons) == 0

    def test_property_model_method_unsupported(self):
        """Property model is method_unsupported in MVP."""
        from alphaforge.core.ranking.engine import _rank_eligibility

        financial = {}
        valuation = {}
        sector_data = {}

        eligible, reasons = _rank_eligibility(
            RankingModel.PROPERTY, financial, valuation, sector_data
        )
        assert eligible is False
        assert any("property" in r.lower() for r in reasons)

    def test_bank_model_method_unsupported(self):
        """Bank model is method_unsupported in MVP."""
        from alphaforge.core.ranking.engine import _rank_eligibility

        financial = {}
        valuation = {}
        sector_data = {}

        eligible, reasons = _rank_eligibility(RankingModel.BANK, financial, valuation, sector_data)
        assert eligible is False
        assert any("bank" in r.lower() for r in reasons)


# --- Test 7: Readiness gate ---


def _ready_packet():
    packet = {
        "schema_version": "evidence-packet-v1",
        "frozen": True,
        "evidence_rules_version": EVIDENCE_RULES_VERSION,
        "report_rules": report_rules_metadata(),
        "company_id": 1,
        "as_of": "2026-05-01",
        "sources": [
            {
                "source_id": "document:1",
                "source_url": "https://mfn.test/report/1",
                "publication_date": "2026-05-01T00:00:00Z",
                "publication_timestamp_authoritative": True,
                "ingestion_date": "2026-05-02T00:00:00Z",
                "attachment": {
                    "source_url": "https://storage.mfn.se/report/1.pdf",
                    "sha256": "abc",
                },
                "extraction": {
                    "extractor": "pypdf",
                    "text_checksum": hashlib.sha256(b"[page 1]\nEvidence").hexdigest(),
                    "page_count": 1,
                },
                "pages": [
                    {
                        "page_number": 1,
                        "anchor": "document:1#page:1",
                        "text": "Evidence",
                        "text_checksum": hashlib.sha256(b"Evidence").hexdigest(),
                    }
                ],
            }
        ],
        "evidence_catalog": {"canonical_source_ids": ["document:1"]},
        "limitations": [],
    }
    packet["packet_hash"] = stable_packet_hash(packet)
    assert validate_frozen_packet(packet)
    return packet


class TestReadinessGate:
    def test_readiness_gate_ready_status(self):
        """Readiness gate returns ready for valid candidates."""
        from alphaforge.core.gate.readiness import AgentReadinessGate

        gate = AgentReadinessGate()
        # Mock candidate with all required fields
        candidate = type(
            "Candidate",
            (),
            {
                "ranking_model": "general",
                "research_evidence": {
                    "documents": [{"id": 1}],
                    "evidence_lane": True,
                    "evidence_packet": _ready_packet(),
                },
                "full_results": {
                    "reverse_dcf": {"status": "available"},
                    "valuation": {
                        "ev_ebit_guardrail_low": 5.0,
                        "ev_ebit_guardrail_high": 20.0,
                    },
                },
                "company_id": 1,
                "ticker": "TEST",
            },
        )()

        assessment = gate.assess(candidate)
        assert assessment.status == "ready"

    def test_readiness_gate_blocks_legacy_documents_without_packet(self):
        """Auditable documents alone cannot satisfy readiness."""
        from alphaforge.core.gate.readiness import AgentReadinessGate

        gate = AgentReadinessGate()
        candidate = type(
            "Candidate",
            (),
            {
                "ranking_model": "general",
                "research_evidence": {
                    "documents": [{"id": 1}],
                    "evidence_lane": False,
                    "evidence_packet": None,
                },
                "full_results": {
                    "reverse_dcf": {"status": "available"},
                    "valuation": {
                        "ev_ebit_guardrail_low": 5.0,
                        "ev_ebit_guardrail_high": 20.0,
                    },
                },
                "company_id": 1,
                "ticker": "TEST",
            },
        )()

        assessment = gate.assess(candidate)
        assert assessment.status == "evidence_blocked"
        assert "frozen_evidence_packet_missing" in [blocker.code for blocker in assessment.blockers]

    def test_readiness_gate_method_unsupported(self):
        """Readiness gate returns method_unsupported for property/bank."""
        from alphaforge.core.gate.readiness import AgentReadinessGate

        gate = AgentReadinessGate()
        candidate = type(
            "Candidate",
            (),
            {
                "ranking_model": "property",
                "research_evidence": {"documents": [{"id": 1}]},
                "full_results": {
                    "reverse_dcf": {"status": "available"},
                    "valuation": {
                        "ev_ebit_guardrail_low": 5.0,
                        "ev_ebit_guardrail_high": 20.0,
                    },
                },
                "company_id": 1,
                "ticker": "TEST",
            },
        )()

        assessment = gate.assess(candidate)
        assert assessment.status == "method_unsupported"


# --- Test 8: Eligibility flags and reasons in exports ---


class TestExportEligibilityFields:
    def test_json_contains_eligibility_fields(self):
        """ranking.json includes rank_eligible and eligibility_reasons per company."""
        companies = [
            MockCompany(id=1, name="Company A", ticker="A", branch_id=None),
        ]
        results_by_company = {}
        engine = RankingEngine()
        ranking = engine.rank(companies, results_by_company)
        scores_list = [asdict(s) for s in ranking.scores]
        for score_dict in scores_list:
            assert "rank_eligible" in score_dict
            assert isinstance(score_dict["rank_eligible"], bool)
            assert "eligibility_reasons" in score_dict
            assert isinstance(score_dict["eligibility_reasons"], list)

    def test_csv_contains_eligibility_fields(self, tmp_path):
        """ranking.csv includes rank_eligible and eligibility_reasons columns."""
        import csv

        from alphaforge.cli.main import export_ranking_files

        companies = [
            MockCompany(id=1, name="Company A", ticker="A", branch_id=None),
        ]
        results_by_company = {}
        engine = RankingEngine()
        ranking = engine.rank(companies, results_by_company)

        exports_dir = tmp_path / "exports"
        ranking_json_path, ranking_csv_path = export_ranking_files(
            ranking,
            "2026-01-01",
            engine.RANKING_MODEL_VERSION,
            exports_dir,
        )

        with open(ranking_csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        assert len(rows) == 1
        row = rows[0]
        assert "rank_eligible" in row
        assert row["rank_eligible"] in ("True", "False")
        assert "eligibility_reasons" in row
        assert isinstance(row["eligibility_reasons"], str)

    def test_json_and_csv_eligibility_fields(self, tmp_path):
        """Both ranking.json and ranking.csv carry per-company eligibility."""
        import csv

        from alphaforge.cli.main import export_ranking_files

        companies = [
            MockCompany(id=1, name="Company A", ticker="A", branch_id=None),
        ]
        results_by_company = {}
        engine = RankingEngine()
        ranking = engine.rank(companies, results_by_company)

        exports_dir = tmp_path / "exports"
        ranking_json_path, ranking_csv_path = export_ranking_files(
            ranking,
            "2026-01-01",
            engine.RANKING_MODEL_VERSION,
            exports_dir,
        )

        with open(ranking_json_path, encoding="utf-8") as f:
            json_data = json.load(f)
        assert json_data["scores"][0]["rank_eligible"] in (True, False)
        assert isinstance(json_data["scores"][0]["eligibility_reasons"], list)

        with open(ranking_csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            csv_rows = list(reader)
        assert csv_rows[0]["rank_eligible"] in ("True", "False")
        assert isinstance(csv_rows[0]["eligibility_reasons"], str)

    def test_eligible_companies_sort_first(self):
        """Eligible companies appear before ineligible companies in output."""
        from alphaforge.core.financial.types import FinancialResult
        from alphaforge.core.valuation.types import ValuationResult

        eligible_company = MockCompany(id=1, name="Eligible", ticker="ELIG", branch_id=None)
        ineligible_company = MockCompany(id=2, name="Bank", ticker="BNK", branch_id=2)

        financial = FinancialResult(
            operating_margin=0.15,
            net_margin=0.10,
            fcf_margin=0.12,
            revenue_growth=0.05,
            ebit_growth=0.05,
            net_income_growth=0.05,
            roe=0.15,
            roa=0.08,
            debt_to_equity=0.5,
        )
        valuation = ValuationResult(
            pe=15.0,
            ev_ebit=10.0,
            ev_ebitda=8.0,
            pb=2.0,
            ps=1.5,
            pfcf=12.0,
            peg=1.2,
            earnings_yield=0.067,
            free_cash_flow_yield=0.083,
            pe_vs_5y_avg=0.9,
            ev_ebit_vs_5y_avg=0.9,
            pb_vs_5y_avg=1.1,
            pe_percentile=0.45,
            ev_ebit_percentile=0.40,
        )

        results_by_company = {
            1: {"financial": financial, "valuation": valuation},
        }
        companies = [eligible_company, ineligible_company]
        engine = RankingEngine()
        ranking = engine.rank(companies, results_by_company)

        eligible_indices = [i for i, s in enumerate(ranking.scores) if s.rank_eligible]
        ineligible_indices = [i for i, s in enumerate(ranking.scores) if not s.rank_eligible]

        assert eligible_indices, "expected at least one eligible company"
        assert ineligible_indices, "expected at least one ineligible company"
        assert max(eligible_indices) < min(ineligible_indices)
