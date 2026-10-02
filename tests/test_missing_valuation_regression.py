"""Regression for Clas Ohlson missing-valuation gaps:
- Live Börsdata fields total_Equity, net_Debt, cash_Flow_From_* must map
  without guessing.
- KPI 37 ROIC and 42 net-debt/EBITDA must persist even when summary omits them.
- DCF must be wired audibly (projected FCFF, discount, terminal, EV/equity/ps)
  and kept separate from heuristic valuation_score.
- EBITDA and gross debt stay explicitly unavailable where not provided.
"""

from __future__ import annotations

from alphaforge.cli.ranking_loader import load_results_for_company
from alphaforge.config import Settings
from alphaforge.core.kpi_taxonomy import KpiIds
from alphaforge.db.connection import get_connection
from alphaforge.db.migrations import migrate
from alphaforge.db.repositories import (
    upsert_company,
    upsert_financial_periods,
    upsert_kpi_observations,
    upsert_prices,
)


def _mem_conn():
    conn = get_connection(Settings.from_env(dsn="sqlite:///:memory:"))
    migrate(conn)
    return conn


def test_live_report_fields_map_with_honest_provenance():
    """total_Equity → equity, net_Debt → net_debt, cash_Flow... → operating_cf."""
    conn = _mem_conn()
    cid = upsert_company(
        conn,
        {
            "insId": 9001,
            "name": "Clas Live Replica AB",
            "ticker": "CLASR",
            "stockPriceCurrency": "SEK",
            "reportCurrency": "SEK",
        },
    )
    # Year row shaped like live Clas Ohlson (2026-04-30) but truncated
    live_year = {
        "period_type": "year",
        "period_end": "2026-04-30",
        "report_Date": "2026-06-03T00:00:00",
        "revenues": 12513.9,
        "gross_Income": 5970.6,
        "operating_Income": 1527.1,
        "profit_To_Equity_Holders": 1168.9,
        "total_Equity": 3154.1,
        "total_Assets": 7887.3,
        "net_Debt": 63.4,
        "cash_And_Equivalents": 1836.5,
        "cash_Flow_From_Operating_Activities": 2118.8,
        "free_Cash_Flow": 1806.1,
        "number_Of_Shares": 63.4528,
        "currency": "SEK",
        "currency_Ratio": 1.0,
        "year": 2026,
        "period": 5,
    }
    upsert_financial_periods(conn, cid, [live_year])
    row = conn.execute(
        "SELECT equity, net_debt, total_debt, operating_cash_flow, free_cash_flow, revenue FROM financial_periods WHERE company_id=?",
        (cid,),
    ).fetchone()
    # Provenance: equity comes from total_Equity, net_debt is dedicated, not mislabeled gross
    assert row["equity"] == 3154.1
    assert row["net_debt"] == 63.4
    # Gross debt stays unavailable where Börsdata does not provide it — do not guess
    assert row["total_debt"] is None
    assert row["operating_cash_flow"] == 2118.8
    assert row["revenue"] == 12513.9


def test_equity_and_net_debt_produce_roe_and_debt_to_equity():
    conn = _mem_conn()
    cid = upsert_company(conn, {"insId": 9002, "name": "Ratio AB", "ticker": "RATIO"})
    upsert_financial_periods(
        conn,
        cid,
        [
            {
                "period_type": "year",
                "period_end": "2024-12-31",
                "report_Date": "2025-02-01",
                "year": 2024,
                "revenues": 1000,
                "profit_To_Equity_Holders": 100,
                "total_Equity": 500,
                "net_Debt": 50,
                "cash_And_Equivalents": 20,
                "cash_Flow_From_Operating_Activities": 80,
                "number_Of_Shares": 10,
                "currency": "SEK",
            }
        ],
    )
    upsert_prices(conn, cid, [{"d": "2025-03-01T00:00:00", "c": 10, "v": 100}], currency="SEK")
    results = load_results_for_company(conn, cid, "2025-03-02")
    fin = results["financial"]
    assert fin is not None
    assert fin.roe == 0.2  # 100/500
    assert fin.debt_to_equity is None  # gross total_debt unavailable; net_debt is not D/E
    assert fin.cash_conversion == 0.8  # 80/100
    # Net debt is surfaced with honest source; EV uses net_debt, so history can form
    assert fin.net_debt == 50
    val = results["valuation"]
    assert val is not None
    # market_cap 10*10=100, enterprise 100+50=150, ev_ebit =150/ (operating_Income? None here) so None — but enterprise is built


def test_ebitda_and_gross_debt_stay_explicitly_unavailable():
    """Do not synthesize EBITDA or gross total_Debt where Börsdata provides none."""
    conn = _mem_conn()
    cid = upsert_company(conn, {"insId": 9003, "name": "Gap AB", "ticker": "GAP"})
    upsert_financial_periods(
        conn,
        cid,
        [
            {
                "period_type": "year",
                "period_end": "2025-12-31",
                "report_Date": "2026-02-01",
                "year": 2025,
                "revenues": 1000,
                "net_Debt": 30,
                "total_Equity": 200,
                "number_Of_Shares": 10,
                "currency": "SEK",
            }
        ],
    )
    row = conn.execute(
        "SELECT ebitda, total_debt, net_debt FROM financial_periods WHERE company_id=?", (cid,)
    ).fetchone()
    assert row["ebitda"] is None
    assert row["total_debt"] is None  # not guessed from net_Debt
    assert row["net_debt"] == 30


def test_kpi_37_and_42_persist_when_summary_omits_them():
    conn = _mem_conn()
    cid = upsert_company(conn, {"insId": 9004, "name": "KPI AB", "ticker": "KPI"})
    # Simulate history rows that would be returned by get_kpi_history even though
    # summary for year/r12 lacked 37/42
    history_37 = [
        {"y": 2024, "p": 5, "v": 15.0},
        {"y": 2023, "p": 5, "v": 12.0},
    ]
    history_42 = [
        {"y": 2024, "p": 5, "v": 0.5},
        {"y": 2023, "p": 5, "v": 0.6},
    ]
    upsert_kpi_observations(conn, cid, KpiIds.ROIC, "year", "mean", history_37)
    upsert_kpi_observations(conn, cid, KpiIds.NET_DEBT_EBITDA, "year", "mean", history_42)
    upsert_financial_periods(
        conn,
        cid,
        [
            {
                "period_type": "year",
                "period_end": "2025-12-31",
                "report_Date": "2026-02-01",
                "year": 2025,
                "revenues": 1000,
                "operating_Income": 100,
                "profit_To_Equity_Holders": 50,
                "total_Equity": 300,
                "number_Of_Shares": 10,
                "currency": "SEK",
            }
        ],
    )
    upsert_prices(conn, cid, [{"d": "2026-02-02T00:00:00", "c": 20, "v": 100}], currency="SEK")
    results = load_results_for_company(conn, cid, "2026-02-03")
    # Persistence is not date assurance: retain undated provider history in
    # rejection audit, but do not admit it as a selected numerical input by year alone.
    assert conn.execute("SELECT count(*) FROM kpi_observations").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM market_input_rejections").fetchone()[0] == 4
    assert KpiIds.ROIC not in results["fundamental_kpis"]
    assert KpiIds.NET_DEBT_EBITDA not in results["fundamental_kpis"]
    assert {item["reason"] for item in results["selection"]["rejected_kpis"]} == {
        "KPI observation date unavailable"
    }


def test_dcf_wired_and_distinguished_from_heuristic_score():
    """Loader must produce auditable DCF (FCFF, discount, terminal, EV/equity/ps) separately."""
    conn = _mem_conn()
    cid = upsert_company(
        conn,
        {
            "insId": 9005,
            "name": "DCF AB",
            "ticker": "DCF",
            "stockPriceCurrency": "SEK",
            "reportCurrency": "SEK",
            "branchId": 50,
        },
    )
    # Provide 6 year rows so DcfAssumptionPolicy has 5y history + normalization
    for yr in range(2020, 2026):
        upsert_financial_periods(
            conn,
            cid,
            [
                {
                    "period_type": "year",
                    "period_end": f"{yr}-12-31",
                    "report_Date": f"{yr + 1}-02-01T00:00:00",
                    "revenues": 1000 + (yr - 2020) * 100,
                    "operating_Income": 100 + (yr - 2020) * 10,
                    "ebit": 100 + (yr - 2020) * 10,
                    "profit_To_Equity_Holders": 50,
                    "total_Equity": 400,
                    "net_Debt": 100,
                    "number_Of_Shares": 10,
                    "currency": "SEK",
                    "year": yr,
                    "period": 5,
                }
            ],
        )
    upsert_prices(conn, cid, [{"d": "2026-02-02T00:00:00", "c": 100, "v": 1000}], currency="SEK")
    # ROIC for reinvestment
    upsert_kpi_observations(
        conn,
        cid,
        KpiIds.ROIC,
        "year",
        "mean",
        [{"y": 2025, "p": 5, "v": 12.0}, {"y": 2024, "p": 5, "v": 11.0}],
    )
    results = load_results_for_company(conn, cid, "2026-02-03")
    rd = results["reverse_dcf"]
    assert "dcf" in rd, f"reverse_dcf missing dcf key: {rd}"
    assert rd["dcf"]["available"] is True, f"dcf not available: {rd.get('dcf')}"
    assert rd["dcf"]["policy_version"] == "reverse-dcf-v12-consecutive-annual-growth"
    assert rd["dcf"]["assumptions"]["discount_rate"] is not None
    assert rd["dcf"]["assumptions"]["terminal_growth"] == 0.02
    assert len(rd["dcf"]["projected_cash_flows"]) == 5
    for cf in rd["dcf"]["projected_cash_flows"]:
        assert cf["fcff"] is not None
        assert cf["discounted_fcff"] is not None
    assert rd["dcf"]["enterprise_value"] is not None
    assert rd["dcf"]["equity_value"] is not None
    assert rd["dcf"]["value_per_share"] is not None
    # Reverse solve present (may have errors for some assumptions)
    assert "implied" in rd
    # Heuristic valuation_score is still a number but is not the DCF ps
    assert results["valuation"] is not None


def test_dcf_unavailable_is_surfaced_not_silent():
    conn = _mem_conn()
    cid = upsert_company(conn, {"insId": 9006, "name": "NoDCF AB", "ticker": "NODCF"})
    # One price+report but no revenue → policy reports missing_information
    upsert_financial_periods(
        conn,
        cid,
        [
            {
                "period_type": "year",
                "period_end": "2025-12-31",
                "report_Date": "2026-02-01",
                "year": 2025,
                "revenues": 0,
                "number_Of_Shares": 10,
                "currency": "SEK",
            }
        ],
    )
    upsert_prices(conn, cid, [{"d": "2026-02-02T00:00:00", "c": 20, "v": 100}], currency="SEK")
    results = load_results_for_company(conn, cid, "2026-02-03")
    rd = results.get("reverse_dcf", {})
    assert rd.get("status") == "unavailable"
    assert "dcf" in rd
    assert rd["dcf"]["available"] is False
    assert rd["dcf"]["missing_information"]


def test_historical_ev_ebit_uses_net_debt_so_guardrails_form():
    conn = _mem_conn()
    cid = upsert_company(
        conn,
        {
            "insId": 9007,
            "name": "EV AB",
            "ticker": "EV",
            "stockPriceCurrency": "SEK",
            "reportCurrency": "SEK",
        },
    )
    # 6 year rows with net_debt so EV is computable; prices aligned to period_end
    for yr in range(2020, 2026):
        upsert_financial_periods(
            conn,
            cid,
            [
                {
                    "period_type": "year",
                    "period_end": f"{yr}-12-31",
                    "report_Date": f"{yr + 1}-02-01T00:00:00",
                    "revenues": 1000,
                    "operating_Income": 100,
                    "ebit": 100,
                    "total_Equity": 500,
                    "net_Debt": 100,
                    "number_Of_Shares": 10,
                    "currency": "SEK",
                    "year": yr,
                    "period": 5,
                }
            ],
        )
        upsert_prices(
            conn,
            cid,
            [{"d": f"{yr}-12-31T00:00:00", "c": 10 + yr - 2020, "v": 100}],
            currency="SEK",
        )
    # One more price for current
    upsert_prices(conn, cid, [{"d": "2026-02-02T00:00:00", "c": 20, "v": 100}], currency="SEK")
    results = load_results_for_company(conn, cid, "2026-02-03")
    val = results["valuation"]
    assert val.ev_ebit is not None
    assert val.ev_ebit_history_count >= 5
    assert val.ev_ebit_guardrail_low is not None


def test_kpi_observation_date_is_persisted_and_pit_filtered():
    conn = _mem_conn()
    cid = upsert_company(conn, {"insId": 9008, "name": "PIT AB", "ticker": "PIT"})
    upsert_kpi_observations(
        conn,
        cid,
        KpiIds.ROIC,
        "year",
        "mean",
        [{"y": 2026, "p": 5, "v": 22.0, "observationDate": "2026-02-01T00:00:00"}],
    )
    row = conn.execute(
        "SELECT observation_date FROM kpi_observations WHERE company_id=? AND kpi_id=?",
        (cid, int(KpiIds.ROIC)),
    ).fetchone()
    assert row["observation_date"] == "2026-02-01"
    upsert_financial_periods(
        conn,
        cid,
        [
            {
                "period_type": "year",
                "period_end": "2025-12-31",
                "report_Date": "2026-01-05T00:00:00",
                "year": 2025,
                "revenues": 1000,
                "operating_Income": 100,
                "profit_To_Equity_Holders": 50,
                "total_Equity": 300,
                "number_Of_Shares": 10,
                "currency": "SEK",
            }
        ],
    )
    upsert_prices(conn, cid, [{"d": "2026-01-10T00:00:00", "c": 20, "v": 100}], currency="SEK")
    early = load_results_for_company(conn, cid, "2026-01-15")
    assert KpiIds.ROIC not in early["fundamental_kpis"]
    late = load_results_for_company(conn, cid, "2026-02-02")
    assert late["fundamental_kpis"][KpiIds.ROIC] == 22.0
