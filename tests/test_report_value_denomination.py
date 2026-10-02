"""Acquired Börsdata report denominations stay distinct through ranking/export."""

import argparse
import json
from unittest.mock import patch

import pytest

from alphaforge.cli.main import cmd_rank
from alphaforge.cli.ranking_loader import load_results_for_company
from alphaforge.config import Settings
from alphaforge.db.connection import get_connection
from alphaforge.db.migrations import migrate
from alphaforge.db.repositories import upsert_company, upsert_financial_periods, upsert_prices
from alphaforge.providers.borsdata.adapter import BorsdataAdapter

AS_OF = "2026-06-01"


def make_report(currency="USD", ratio=10.0, **overrides):
    return {
        "period_type": "year",
        "period_end": "2025-12-31",
        "report_Date": "2026-02-01",
        "year": 2025,
        "period": 5,
        "revenues": 100,
        "operating_Income": 20,
        "ebit": 20,
        "profit_To_Equity_Holders": 10,
        "free_Cash_Flow": 10,
        "total_Equity": 40,
        "net_Debt": 5,
        "number_Of_Shares": 10,
        "currency": currency,
        "currency_Ratio": ratio,
        **overrides,
    }


def setup_company(target="SEK"):
    conn = get_connection(Settings.from_env(dsn="sqlite:///:memory:"))
    migrate(conn)
    cid = upsert_company(
        conn,
        {
            "insId": 101,
            "name": "Denomination Fixture AB",
            "ticker": "DEN",
            "stockPriceCurrency": target,
            "reportCurrency": "USD",
        },
    )
    conn.execute(
        "INSERT INTO watchlist(company_id,ticker,source_file,source_row_hash) "
        "VALUES (?, 'DEN', 'fixture', 'denomination-fixture')",
        (cid,),
    )
    upsert_prices(
        conn,
        cid,
        [{"d": AS_OF, "c": 10, "v": 100, "currency": target}],
        currency=target,
    )
    return conn, cid


def acquire(conn, cid, *, original=0, target="SEK", report=None, targets=None):
    payload = {
        "reportList": [
            {
                "insId": 101,
                "reportsYear": [report or make_report()],
            }
        ]
    }
    adapter = BorsdataAdapter(api_key="fixture")
    with patch.object(adapter, "_get_json", return_value=payload):
        rows = adapter.get_reports(
            [101], original=original, target_currencies=targets or {101: target}
        )
    assert len(rows) == 1
    upsert_financial_periods(conn, cid, rows)
    return rows[0]


def rank_export(conn, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    with patch("alphaforge.db.connection.get_connection", return_value=conn):
        assert (
            cmd_rank(argparse.Namespace(dsn="sqlite:///:memory:", as_of=AS_OF, watchlist=None)) == 0
        )
    ranking = json.loads((tmp_path / "exports" / AS_OF / "ranking.json").read_text())
    dcf = json.loads((tmp_path / "exports" / AS_OF / "dcf.json").read_text())
    score = ranking["scores"][0]
    return score, dcf[str(score["company_id"])]


@pytest.mark.parametrize(
    "original_currency,target,ratio,expected_market_cap",
    [("USD", "SEK", 10.0, 100.0), ("SEK", "SEK", 1.0, 100.0)],
)
def test_converted_and_same_currency_reports_round_trip_without_double_conversion(
    original_currency, target, ratio, expected_market_cap, monkeypatch, tmp_path
):
    conn, cid = setup_company(target)
    acquire(conn, cid, target=target, report=make_report(original_currency, ratio))
    stored = conn.execute(
        "SELECT currency, values_currency, conversion_mode, conversion_target_currency, "
        "currency_ratio, fx_rate_to_sek, revenue FROM financial_periods WHERE company_id=?",
        (cid,),
    ).fetchone()
    assert tuple(stored) == (
        original_currency,
        target,
        "converted",
        target,
        ratio,
        ratio if target == "SEK" else None,
        100.0,
    )
    loaded = load_results_for_company(conn, cid, AS_OF)
    assert loaded["reverse_dcf"]["current_revenue"] == 100
    assert loaded["valuation"].raw_market_cap == expected_market_cap
    assert loaded["reverse_dcf"]["market_cap"] == expected_market_cap
    assert loaded["reverse_dcf"]["financial_currency"] == target
    denomination = loaded["selection"]["report_denominations"][0]
    assert denomination == {
        "period_type": "year",
        "period_end": "2025-12-31",
        "original_currency": original_currency,
        "values_currency": target,
        "conversion_mode": "converted",
        "conversion_target_currency": target,
        "currency_ratio": ratio,
        "fx_rate_to_sek": ratio if target == "SEK" else None,
    }
    score, dcf = rank_export(conn, monkeypatch, tmp_path)
    exported = score["input_selection"]["report_denominations"][0]
    assert exported["original_currency"] == original_currency
    assert exported["values_currency"] == target
    assert exported["conversion_mode"] == "converted"
    assert exported["conversion_target_currency"] == target
    assert dcf["report_denomination"]["original_currency"] == original_currency
    assert dcf["report_denomination"]["values_currency"] == target
    assert dcf["market_cap"] == expected_market_cap


def test_verified_eur_values_allow_raw_multiples_but_not_sek_hurdle(monkeypatch, tmp_path):
    conn, cid = setup_company("EUR")
    acquire(conn, cid, target="EUR", report=make_report("USD", 0.9))
    loaded = load_results_for_company(conn, cid, AS_OF)
    assert loaded["reverse_dcf"]["current_revenue"] == 100
    assert loaded["valuation"].raw_market_cap == 100
    assert loaded["valuation"].raw_ev_ebit == pytest.approx(5.25)
    assert loaded["reverse_dcf"]["status"] == "unavailable"
    assert loaded["reverse_dcf"]["dcf"]["available"] is False
    assert "received EUR" in " ".join(loaded["reverse_dcf"]["dcf"]["missing_information"])
    assert (
        conn.execute(
            "SELECT fx_rate_to_sek FROM financial_periods WHERE company_id=?", (cid,)
        ).fetchone()[0]
        is None
    )
    score, dcf = rank_export(conn, monkeypatch, tmp_path)
    assert score["input_selection"]["report_denominations"][0]["values_currency"] == "EUR"
    assert dcf["market_cap"] == 100
    assert dcf["dcf"]["available"] is False


def test_verified_acquisition_target_is_not_relabelled_by_later_company_update():
    conn, cid = setup_company("SEK")
    acquire(conn, cid, target="SEK", report=make_report("USD", 10.0))
    conn.execute("UPDATE companies SET stock_price_currency='USD' WHERE id=?", (cid,))
    conn.commit()
    loaded = load_results_for_company(conn, cid, AS_OF)
    assert loaded["reverse_dcf"]["current_revenue"] == 100
    assert loaded["reverse_dcf"]["financial_currency"] == "SEK"
    assert loaded["selection"]["report_denominations"][0]["conversion_target_currency"] == "SEK"
    assert loaded["valuation"].raw_market_cap == 100


def test_original_mode_ratio_is_provenance_not_a_value_conversion():
    conn, cid = setup_company("SEK")
    acquire(conn, cid, original=1, target="SEK", report=make_report("USD", 10.0))
    loaded = load_results_for_company(conn, cid, AS_OF)
    assert loaded["reverse_dcf"]["current_revenue"] == 100
    assert loaded["selection"]["report_denominations"][0]["values_currency"] == "USD"
    assert loaded["valuation"].raw_market_cap is None
    assert any(
        "conflicts with stock price currency" in reason
        for reason in loaded["selection"]["refusal_reasons"]
    )
    assert loaded["reverse_dcf"]["status"] == "unavailable"
    assert "received USD" in " ".join(loaded["reverse_dcf"]["dcf"]["missing_information"])


@pytest.mark.parametrize(
    "report,reason",
    [
        (
            {
                **make_report(),
                "conversion_mode": "mystery",
                "conversion_target_currency": "SEK",
                "values_currency": "SEK",
            },
            "report conversion mode unavailable or unsupported",
        ),
        (
            {
                **make_report(),
                "conversion_mode": "converted",
                "conversion_target_currency": None,
                "values_currency": None,
            },
            "report conversion target currency unavailable or invalid",
        ),
        (
            {
                **make_report(),
                "conversion_mode": "converted",
                "conversion_target_currency": "SEK",
                "values_currency": "EUR",
            },
            "report values currency conflicts with acquisition mode and target",
        ),
        (
            {
                **make_report("USD", -2.0),
                "conversion_mode": "original",
                "conversion_target_currency": "SEK",
                "values_currency": "USD",
            },
            "report currency ratio is invalid",
        ),
    ],
)
def test_unknown_or_conflicting_acquisition_metadata_refuses(report, reason):
    conn, cid = setup_company()
    # Use the repository seam directly so malformed metadata remains auditable.
    upsert_financial_periods(conn, cid, [report])
    loaded = load_results_for_company(conn, cid, AS_OF)
    assert loaded["financial"] is None
    assert any(reason in refusal for refusal in loaded["selection"]["refusal_reasons"])
    assert any(reason in item["reason"] for item in loaded["selection"]["rejected_reports"])


def test_legacy_report_remains_unverifiable_after_company_currency_changes():
    conn, cid = setup_company("SEK")
    legacy = make_report("SEK", 1.0)
    upsert_financial_periods(conn, cid, [legacy])
    conn.execute(
        "UPDATE financial_periods SET values_currency=NULL, conversion_mode=NULL, "
        "conversion_target_currency=NULL WHERE company_id=?",
        (cid,),
    )
    conn.execute("UPDATE companies SET stock_price_currency='USD' WHERE id=?", (cid,))
    conn.commit()
    loaded = load_results_for_company(conn, cid, AS_OF)
    assert loaded["financial"] is None
    assert any(
        "report conversion mode unavailable" in reason
        for reason in loaded["selection"]["refusal_reasons"]
    )
    assert conn.execute(
        "SELECT currency, values_currency FROM financial_periods WHERE company_id=?", (cid,)
    ).fetchone()[:] == ("SEK", None)


@pytest.mark.parametrize(
    "persisted_currency,refusal",
    [
        (None, "currencies are not both verified"),
        ("USD", "conflicts with stock price currency USD"),
    ],
)
def test_unverified_or_conflicting_price_currency_blocks_current_valuation_and_dcf(
    persisted_currency, refusal
):
    conn, cid = setup_company("SEK")
    acquire(conn, cid, target="SEK", report=make_report("USD", 10.0))
    conn.execute(
        "UPDATE prices SET currency=?, raw_payload=? WHERE company_id=?",
        (persisted_currency, json.dumps({"d": AS_OF, "c": 10, "v": 100}), cid),
    )
    conn.execute("UPDATE companies SET stock_price_currency='USD' WHERE id=?", (cid,))
    conn.commit()

    before = load_results_for_company(conn, cid, AS_OF)
    conn.execute("UPDATE companies SET stock_price_currency='SEK' WHERE id=?", (cid,))
    conn.commit()
    after = load_results_for_company(conn, cid, AS_OF)

    for loaded in (before, after):
        assert loaded["financial"] is not None
        assert loaded["valuation"].raw_market_cap is None
        assert loaded["reverse_dcf"]["market_cap"] is None
        assert loaded["reverse_dcf"]["status"] == "unavailable"
        assert refusal in " ".join(loaded["selection"]["valuation_refusals"])
        assert refusal in " ".join(loaded["reverse_dcf"]["dcf"]["missing_information"])


def test_unverified_historical_price_currency_blocks_historical_multiples():
    conn, cid = setup_company("SEK")
    acquire(
        conn,
        cid,
        target="SEK",
        report=make_report(
            "USD",
            10.0,
            period_end="2024-12-31",
            report_Date="2025-02-01",
            year=2024,
        ),
    )
    acquire(conn, cid, target="SEK", report=make_report("USD", 10.0))
    upsert_prices(
        conn,
        cid,
        [{"d": "2024-12-31", "c": 10, "v": 100}],
        currency="SEK",
    )
    conn.execute(
        "UPDATE prices SET currency=NULL WHERE company_id=? AND price_date='2024-12-31'",
        (cid,),
    )
    conn.execute("UPDATE companies SET stock_price_currency='SEK' WHERE id=?", (cid,))
    conn.commit()

    loaded = load_results_for_company(conn, cid, AS_OF)

    assert loaded["valuation"].raw_market_cap == 100
    assert loaded["valuation"].ev_ebit_history_count == 0
    assert loaded["selection"]["historical_price_pairings"][0]["reason"] == (
        "historical report and stock price currencies are not both verified"
    )
