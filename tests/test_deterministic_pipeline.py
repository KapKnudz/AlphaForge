from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from alphaforge.cli.ranking_loader import load_results_for_company
from alphaforge.config import Settings
from alphaforge.core.financial.per_share import adjust_historical_shares
from alphaforge.core.ranking.engine import RankingEngine
from alphaforge.core.ranking.types import CompanyScore
from alphaforge.db.connection import get_connection
from alphaforge.db.migrations import migrate
from alphaforge.db.repositories import (
    upsert_company,
    upsert_prices,
    upsert_stock_splits,
)
from alphaforge.db.repositories import (
    upsert_financial_periods as _upsert_financial_periods,
)
from alphaforge.providers.borsdata.adapter import BorsdataAdapter

FIXTURES = Path(__file__).parent / "fixtures" / "borsdata"


def test_sqlite_dsn_relative_and_absolute_paths():
    assert Settings.from_env(dsn="sqlite:///data/a.db").sqlite_path == Path("data/a.db")
    assert Settings.from_env(dsn="sqlite://./data/a.db").sqlite_path == Path("data/a.db")
    assert Settings.from_env(dsn="sqlite:////tmp/a.db").sqlite_path == Path("/tmp/a.db")


def test_report_list_rows_persist_with_nested_period_type():
    conn = get_connection(Settings.from_env(dsn="sqlite:///:memory:"))
    migrate(conn)
    company_id = upsert_company(conn, {"insId": 101, "name": "Fixture AB", "ticker": "FIX"})
    payload = json.loads((FIXTURES / "report_list.json").read_text())
    adapter = BorsdataAdapter(api_key="fixture")
    with patch.object(adapter, "_get_json", return_value=payload):
        reports = adapter.get_reports([101])
    upsert_financial_periods(conn, company_id, reports)
    rows = conn.execute(
        "SELECT period_type, period_end FROM financial_periods ORDER BY period_type"
    ).fetchall()
    assert [(row[0], row[1]) for row in rows] == [
        ("quarter", "2025-03-31"),
        ("r12", "2025-03-31"),
        ("year", "2024-12-31"),
    ]


def test_split_adjustment_prevents_false_dilution():
    assert (
        adjust_historical_shares(10, "2024-12-31", "2025-12-31", [("S", "5:1", "2025-04-09")]) == 50
    )
    assert (
        adjust_historical_shares(100, "2024-12-31", "2025-12-31", [("RS", "1:100", "2025-04-09")])
        == 1
    )


def upsert_financial_periods(conn, company_id, periods):
    """Fixture rows carry explicit provider conversion provenance."""
    for report in periods:
        report.setdefault("conversion_mode", "original")
        report.setdefault("conversion_target_currency", "SEK")
        report.setdefault("values_currency", report.get("currency"))
    return _upsert_financial_periods(conn, company_id, periods)


def test_loader_uses_only_data_visible_at_as_of_and_split_adjusts_history():
    conn = get_connection(Settings.from_env(dsn="sqlite:///:memory:"))
    migrate(conn)
    company_id = upsert_company(
        conn,
        {
            "insId": 202,
            "name": "Point In Time AB",
            "ticker": "PIT",
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
                "year": 2024,
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
                "year": 2025,
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
        [{"d": "2025-12-30T00:00:00", "c": 10, "v": 100}, {"d": "2026-12-30", "c": 20, "v": 100}],
        currency="SEK",
    )
    upsert_stock_splits(
        conn,
        [{"instrumentId": 202, "splitType": "S", "ratio": "5:1", "splitDate": "2025-04-09"}],
    )
    # The split is linked after the company map lookup in production sync.
    conn.execute("UPDATE stock_splits SET company_id=? WHERE borsdata_id=202", (company_id,))
    conn.commit()

    historical = load_results_for_company(conn, company_id, "2025-12-31")
    current = load_results_for_company(conn, company_id, "2026-12-31")
    assert historical["financial"] is not None
    assert current["financial"] is not None
    assert historical["financial"].revenue_growth is None
    assert current["financial"].revenue_growth is not None
    # The later report and price must not affect the historical packet.
    assert historical["valuation"].pe != current["valuation"].pe


def test_unranked_missing_data_is_a_deterministic_section():
    class Company:
        def __init__(self, company_id, ticker):
            self.id = company_id
            self.name = ticker
            self.ticker = ticker
            self.branch_id = None

    ranking = RankingEngine().rank(
        [Company(2, "ZZZ"), Company(1, "AAA")],
        {},
    )
    assert [score.ticker for score in ranking.scores] == ["AAA", "ZZZ"]
    assert all(score.ranking_section == "unranked_missing_data" for score in ranking.scores)
    assert all(not score.rank_eligible for score in ranking.scores)
    assert all(
        "financial data not available" in score.eligibility_reasons for score in ranking.scores
    )


def test_company_score_export_contains_explicit_section():
    score = CompanyScore(company_id=1, ticker="T", name="Test", rank_eligible=False)
    assert asdict(score)["ranking_section"] == "unranked_missing_data"
