"""Fixture-only coverage/unit regressions through the consumed ranking lane."""

import argparse
import csv
import json
import sqlite3
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from alphaforge.cli.main import cmd_rank, cmd_sync, export_ranking_files
from alphaforge.cli.ranking_loader import load_results_for_company
from alphaforge.config import Settings
from alphaforge.core.ranking.engine import RankingEngine
from alphaforge.core.valuation.dividend_yield import (
    DIVIDEND_YIELD_POLICY_VERSION,
    calculate_dividend_yield,
    trailing_dividend_window,
)
from alphaforge.db.connection import get_connection
from alphaforge.db.migrations import migrate
from alphaforge.db.repositories import (
    record_job,
    upsert_company,
    upsert_dividend_window_coverage,
    upsert_dividends,
    upsert_financial_periods,
    upsert_prices,
)
from alphaforge.providers.borsdata.adapter import BorsdataAdapter

AS_OF = "2026-01-01"
START = "2025-01-01"


def seeded(as_of=AS_OF):
    conn = get_connection(Settings.from_env(dsn="sqlite:///:memory:"))
    migrate(conn)
    cid = upsert_company(
        conn,
        {
            "insId": 1,
            "name": "Synthetic",
            "ticker": "FIX",
            "stockPriceCurrency": "SEK",
            "reportCurrency": "SEK",
        },
    )
    upsert_financial_periods(
        conn,
        cid,
        [
            {
                "period_type": "year",
                "period_end": "2024-12-31",
                "report_Date": "2025-02-01",
                "revenues": 100,
                "operating_Income": 20,
                "profit_To_Equity_Holders": 4,
                "book_Value": 40,
                "number_Of_Shares": 10,
                "currency": "SEK",
                "cash_Flow_For_The_Year": 10,
                "netDebt": 5,
            }
        ],
    )
    upsert_prices(conn, cid, [{"d": as_of, "c": 10}], currency="SEK")
    return conn, cid


def coverage(conn, cid, status="complete", as_of=AS_OF):
    start, end = trailing_dividend_window(date.fromisoformat(as_of))
    upsert_dividend_window_coverage(
        conn,
        cid,
        start.isoformat(),
        end.isoformat(),
        status=status,
        source="synthetic_fixture",
        assurance="Independently verified exhaustive fixture calendar"
        if status == "complete"
        else None,
        verified_at="2026-01-01T00:00:00Z" if status == "complete" else None,
    )


def rank(conn, cid, as_of=AS_OF, branch=None):
    loaded = load_results_for_company(conn, cid, as_of)
    company = SimpleNamespace(id=cid, name="Synthetic", ticker="FIX", branch_id=branch)
    ranking = RankingEngine().rank([company], {cid: loaded})
    return loaded, ranking


def component(ranking):
    return next(
        c
        for c in ranking.scores[0].scoring_audit["valuation"]["components"]
        if c["name"] == "dividend_yield"
    )


def migrated_dividend(currency="SEK"):
    """Replay the actual v12 -> v13 upgrade with an assumed legacy tag."""
    conn, cid = seeded()
    conn.execute("ALTER TABLE dividends DROP COLUMN currency_conflicted")
    conn.execute("ALTER TABLE dividends DROP COLUMN currency_verified")
    conn.execute("DROP TABLE dividend_window_coverage")
    conn.execute(
        "INSERT INTO dividends (company_id, ex_date, amount, currency, dividend_type) "
        "VALUES (?, '2025-06-01', 1, ?, 0)",
        (cid, currency),
    )
    conn.execute("PRAGMA user_version=12")
    conn.commit()
    migrate(conn)
    upsert_prices(conn, cid, [{"d": AS_OF, "c": 10, "currency": "USD"}])
    assert dividend_state(conn) == (currency, 0, 0)
    return conn, cid


def dividend_state(conn):
    return conn.execute(
        "SELECT currency, currency_verified, currency_conflicted FROM dividends"
    ).fetchone()[:]


def dividend_sync_adapter(currency):
    adapter = Mock(spec=BorsdataAdapter)
    for name in (
        "get_instruments",
        "get_sectors",
        "get_branches",
        "get_countries",
        "get_translation_metadata",
        "get_kpi_metadata",
        "get_report_metadata",
        "get_reports",
        "get_stock_prices",
        "get_kpi_history",
        "get_stock_splits",
        "get_report_calendar",
        "get_shorts",
    ):
        getattr(adapter, name).return_value = []
    adapter.get_kpi_summary.return_value = {"kpis": []}
    adapter.get_dividends.return_value = [
        {"insId": 1, "exDate": "2025-06-01", "amount": 1, "currency": currency}
    ]
    return adapter


@pytest.mark.parametrize("legacy_currency", ["SEK", "EUR", "USD"])
def test_first_authoritative_currency_replaces_unverified_legacy_tag(legacy_currency):
    conn, cid = migrated_dividend(legacy_currency)
    coverage(conn, cid)
    before, before_rank = rank(conn, cid)
    assert before["valuation"].dividend_yield is None
    row = {"exDate": "2025-06-01", "amount": 1, "currency": "USD"}
    for _ in range(2):
        upsert_dividends(conn, cid, [row])
        after, after_rank = rank(conn, cid)
        assert dividend_state(conn) == ("USD", 1, 0)
        assert after["valuation"].dividend_yield == 10
        assert after["dividend_yield"]["reason"] is None
        assert after_rank.scores[0].valuation_score != before_rank.scores[0].valuation_score
        assert after["reverse_dcf"]["status"] == "unavailable"
    assert conn.execute("SELECT count(*) FROM dividends").fetchone()[0] == 1


@pytest.mark.parametrize("window_status", ["complete", "partial", None])
@pytest.mark.parametrize("incoming_currency", ["USD", "SEK", None])
def test_real_sync_and_rank_legacy_refresh_requires_independent_coverage(
    tmp_path,
    monkeypatch,
    window_status,
    incoming_currency,
):
    conn, cid = migrated_dividend()
    if window_status is not None:
        coverage(conn, cid, status=window_status)
    conn.execute(
        "INSERT INTO watchlist (company_id, ticker, source_file, source_row_hash) "
        "VALUES (?, 'FIX', 'legacy-fixture', 'legacy-fixture')",
        (cid,),
    )
    adapter = dividend_sync_adapter(incoming_currency)
    sync_args = argparse.Namespace(
        dsn="sqlite:///:memory:", all=True, company=None, ticker=None, allow_empty_companies=True
    )
    rank_args = argparse.Namespace(dsn="sqlite:///:memory:", as_of=AS_OF, watchlist=None)
    monkeypatch.chdir(tmp_path)
    with (
        patch("alphaforge.db.connection.get_connection", return_value=conn),
        patch("alphaforge.providers.borsdata.adapter.BorsdataAdapter", return_value=adapter),
    ):
        for _ in range(2):
            assert cmd_sync(sync_args) == 0
            assert cmd_rank(rank_args) == 0
            assert dividend_state(conn) == (
                incoming_currency or "SEK",
                int(incoming_currency is not None),
                0,
            )
            exported = json.loads((tmp_path / "exports" / AS_OF / "ranking.json").read_text())
            audit = exported["scores"][0]["scoring_audit"]["dividend_yield"]
            if window_status == "complete" and incoming_currency == "USD":
                assert audit["value"] == 10
                assert audit["reason"] is None
                assert audit["coverage"]["source"] == "synthetic_fixture"
            else:
                assert audit["value"] is None
                expected_reason = (
                    "dividend_coverage_unknown"
                    if window_status is None
                    else "dividend_coverage_partial"
                    if window_status == "partial"
                    else "dividend_currency_unknown"
                    if incoming_currency is None
                    else "dividend_currency_mismatch"
                )
                assert audit["reason"] == expected_reason
            assert conn.execute("SELECT count(*) FROM dividend_window_coverage").fetchone()[0] == (
                0 if window_status is None else 1
            )
            with (tmp_path / "exports" / AS_OF / "ranking.csv").open() as f:
                csv_row = next(csv.DictReader(f))
            assert float(csv_row["valuation_score"]) == exported["scores"][0]["valuation_score"]
    assert conn.execute("SELECT count(*) FROM ranking_runs").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM dividends").fetchone()[0] == 1


@pytest.mark.parametrize("unusable_currency", [None, "XXX", "GBp"])
def test_missing_observation_does_not_erase_verified_currency_conflict_evidence(unusable_currency):
    conn, cid = seeded()
    coverage(conn, cid)
    row = {"exDate": "2025-06-01", "amount": 1, "currency": "SEK"}
    upsert_dividends(conn, cid, [row])
    upsert_dividends(conn, cid, [{**row, "currency": unusable_currency}])
    assert dividend_state(conn) == ("SEK", 1, 0)
    upsert_dividends(conn, cid, [{**row, "currency": "USD"}])
    assert dividend_state(conn) == ("SEK", 0, 1)
    for currency in (None, "SEK", "USD"):
        upsert_dividends(conn, cid, [{**row, "currency": currency}])
        assert dividend_state(conn) == ("SEK", 0, 1)
        assert rank(conn, cid)[0]["valuation"].dividend_yield is None


def test_foreign_dividend_does_not_change_ranking():
    conn, cid = seeded()
    before, before_rank = rank(conn, cid)
    coverage(conn, cid)
    upsert_dividends(conn, cid, [{"exDate": "2025-06-01", "amount": 1, "currency": "USD"}])
    after, after_rank = rank(conn, cid)
    assert before["valuation"].dividend_yield is None
    assert after["valuation"].dividend_yield is None
    assert after["dividend_yield"]["reason"] == "dividend_currency_mismatch"
    assert after_rank.scores[0].total_score == before_rank.scores[0].total_score
    assert after_rank.scores[0].valuation_score == before_rank.scores[0].valuation_score
    assert not component(after_rank)["available"]
    assert component(after_rank)["provenance"] == "dividend_window_unavailable"


@pytest.mark.parametrize("amount", [None, 0, 1])
def test_complete_empty_zero_positive_and_coverage_removal(amount):
    conn, cid = seeded()
    if amount is not None:
        upsert_dividends(conn, cid, [{"exDate": "2025-06-01", "amount": amount, "currency": "SEK"}])
    unknown, unknown_rank = rank(conn, cid)
    assert unknown["valuation"].dividend_yield is None
    assert unknown["dividend_yield"]["reason"] == "dividend_coverage_unknown"
    coverage(conn, cid)
    coverage(conn, cid)
    assert conn.execute("SELECT count(*) FROM dividend_window_coverage").fetchone()[0] == 1
    complete, complete_rank = rank(conn, cid)
    assert complete["valuation"].dividend_yield == (amount or 0) * 10
    assert complete["dividend_yield"]["reason"] is None
    assert component(complete_rank)["available"]
    assert component(complete_rank)["provenance"] == "verified_dividend_window"
    assert not component(unknown_rank)["available"]
    assert complete_rank.scores[0].valuation_score != unknown_rank.scores[0].valuation_score
    if amount is not None:
        assert conn.execute("SELECT amount FROM dividends").fetchone()[0] == amount
    conn.execute("DELETE FROM dividend_window_coverage")
    removed, removed_rank = rank(conn, cid)
    assert removed["valuation"].dividend_yield is None
    assert removed_rank.scores[0].total_score == unknown_rank.scores[0].total_score


@pytest.mark.parametrize(
    "status,reason",
    [("unknown", "dividend_coverage_unknown"), ("partial", "dividend_coverage_partial")],
)
def test_interior_holes_and_legacy_extrema_cannot_certify_window(status, reason):
    conn, cid = seeded()
    upsert_dividends(
        conn,
        cid,
        [{"exDate": d, "amount": 1, "currency": "SEK"} for d in ["2024-12-31", "2026-01-01"]],
    )
    conn.execute(
        "INSERT INTO dividend_coverage (company_id, covered_from, covered_through) VALUES (?, '2024-12-31', '2026-01-01')",
        (cid,),
    )
    assert rank(conn, cid)[0]["dividend_yield"]["reason"] == "dividend_coverage_unknown"
    coverage(conn, cid, status)
    loaded, _ = rank(conn, cid)
    assert loaded["valuation"].dividend_yield is None
    assert loaded["dividend_yield"]["reason"] == reason
    # Completeness for another window/company must not rescue this one.
    upsert_dividend_window_coverage(
        conn,
        cid,
        "2024-01-01",
        "2026-01-01",
        status="complete",
        source="fixture",
        assurance="different window",
        verified_at=AS_OF,
    )
    assert rank(conn, cid)[0]["dividend_yield"]["reason"] == reason


@pytest.mark.parametrize(
    "amount_currency,price_currency,reason",
    [
        (None, "SEK", "dividend_currency_unknown"),
        ("USD", "SEK", "dividend_currency_mismatch"),
        ("XXX", "SEK", "dividend_currency_unknown"),
        ("GBp", "GBP", "dividend_currency_unknown"),
        ("SEK", None, "dividend_currency_unknown"),
    ],
)
def test_unknown_or_foreign_denomination_never_assumed(amount_currency, price_currency, reason):
    conn, cid = seeded()
    coverage(conn, cid)
    upsert_dividends(
        conn, cid, [{"exDate": "2025-06-01", "amount": 1, "currency": amount_currency}]
    )
    conn.execute("UPDATE prices SET currency=?", (price_currency,))
    loaded, _ = rank(conn, cid)
    assert loaded["valuation"].dividend_yield is None
    assert loaded["dividend_yield"]["reason"] == reason


@pytest.mark.parametrize("initial_currency", [None, "XXX", "GBp"])
def test_first_valid_currency_replaces_unverified_unknown(initial_currency):
    conn, cid = seeded()
    coverage(conn, cid)
    row = {"exDate": "2025-06-01", "amount": 1, "currency": initial_currency}
    upsert_dividends(conn, cid, [row])
    assert rank(conn, cid)[0]["dividend_yield"]["reason"] == "dividend_currency_unknown"
    assert conn.execute(
        "SELECT currency, currency_verified, currency_conflicted FROM dividends"
    ).fetchone()[:] == (initial_currency or "", 0, 0)
    row["currency"] = "SEK"
    upsert_dividends(conn, cid, [row])
    assert rank(conn, cid)[0]["valuation"].dividend_yield == 10
    assert conn.execute(
        "SELECT currency, currency_verified, currency_conflicted FROM dividends"
    ).fetchone()[:] == ("SEK", 1, 0)


def test_matching_foreign_currency_yield_does_not_enable_sek_dcf():
    conn, cid = seeded()
    coverage(conn, cid)
    upsert_prices(conn, cid, [{"d": AS_OF, "c": 10, "currency": "USD"}])
    upsert_dividends(conn, cid, [{"exDate": "2025-06-01", "amount": 1, "currency": "USD"}])
    loaded, _ = rank(conn, cid)
    assert loaded["valuation"].dividend_yield == 10
    assert loaded["reverse_dcf"]["status"] == "unavailable"


def test_conflicting_relevant_currencies_and_duplicate_currency_conflict():
    conn, cid = seeded()
    coverage(conn, cid)
    rows = [{"exDate": "2025-06-01", "amount": 1, "currency": "SEK"}]
    upsert_dividends(conn, cid, rows)
    upsert_dividends(conn, cid, rows)
    assert rank(conn, cid)[0]["valuation"].dividend_yield == 10
    upsert_dividends(conn, cid, [{"exDate": "2025-07-01", "amount": 0, "currency": "USD"}])
    assert rank(conn, cid)[0]["dividend_yield"]["reason"] == "dividend_currency_mismatch"
    upsert_dividends(conn, cid, [{"exDate": "2025-06-01", "amount": 1, "currency": "USD"}])
    upsert_dividends(conn, cid, rows)
    assert rank(conn, cid)[0]["dividend_yield"]["reason"] == "dividend_currency_unknown"


@pytest.mark.parametrize("field", ["exDate", "ex_date", "date"])
def test_repository_canonicalizes_valid_dividend_date_aliases(field):
    conn, cid = seeded()
    assert (
        upsert_dividends(
            conn,
            cid,
            [{field: "2025-06-01T23:59:59Z", "amount": 1, "currency": "SEK"}],
        )
        == 1
    )
    assert conn.execute("SELECT ex_date FROM dividends").fetchone()[0] == "2025-06-01"


@pytest.mark.parametrize(
    "invalid_row",
    [
        {"exDate": "2025-06-99", "amount": 1, "currency": "SEK"},
        {"exDate": "2025-07-01", "amount": 1, "currency": "SEK", "dividendType": "bad"},
        {"exDate": "2025-07-01", "amount": "bad", "currency": "SEK"},
        {"exDate": "2025-07-01", "amount": -1, "currency": "SEK"},
        {"exDate": "2025-07-01", "amount": 1, "currency": "SEK", "dividendType": 3},
    ],
)
def test_dividend_batch_failure_rolls_back_before_job_commit(invalid_row):
    conn, cid = seeded()
    coverage(conn, cid)
    before_loaded, before_ranking = rank(conn, cid)
    conn.execute("UPDATE companies SET name='Pending caller work' WHERE id=?", (cid,))
    with pytest.raises((ValueError, sqlite3.IntegrityError)):
        upsert_dividends(
            conn,
            cid,
            [
                {"exDate": "2025-06-01", "amount": 1, "currency": "SEK"},
                invalid_row,
            ],
        )
    record_job(
        conn,
        "sync_dividends",
        company_id=cid,
        borsdata_id=1,
        status="failed",
        error={"code": "dividends_upsert_failed"},
    )
    after_loaded, after_ranking = rank(conn, cid)
    assert conn.execute("SELECT count(*) FROM dividends").fetchone()[0] == 0
    assert conn.execute("SELECT name FROM companies WHERE id=?", (cid,)).fetchone()[0] == (
        "Pending caller work"
    )
    assert (
        conn.execute(
            "SELECT status FROM jobs WHERE job_type='sync_dividends' AND company_id=?", (cid,)
        ).fetchone()[0]
        == "failed"
    )
    assert before_loaded["valuation"].dividend_yield == 0
    assert after_loaded["valuation"].dividend_yield == 0
    assert after_ranking.scores[0].total_score == before_ranking.scores[0].total_score


def test_dividend_batch_failure_rolls_back_prior_currency_update():
    conn, cid = seeded()
    coverage(conn, cid)
    unknown = {"exDate": "2025-06-01", "amount": 1, "currency": None}
    upsert_dividends(conn, cid, [unknown])
    before_loaded, before_ranking = rank(conn, cid)
    with pytest.raises(ValueError):
        upsert_dividends(
            conn,
            cid,
            [
                {**unknown, "currency": "SEK"},
                {"exDate": "2025-06-99", "amount": 1, "currency": "SEK"},
            ],
        )
    after_loaded, after_ranking = rank(conn, cid)
    assert conn.execute(
        "SELECT currency, currency_verified, currency_conflicted FROM dividends"
    ).fetchone()[:] == ("", 0, 0)
    assert before_loaded["dividend_yield"]["reason"] == "dividend_currency_unknown"
    assert after_loaded["dividend_yield"]["reason"] == "dividend_currency_unknown"
    assert after_ranking.scores[0].total_score == before_ranking.scores[0].total_score


def test_repository_rejects_invalid_dates_without_inventing_missing_values():
    conn, cid = seeded()
    assert (
        upsert_dividends(
            conn,
            cid,
            [
                {"amount": 0, "currency": "SEK"},
                {"exDate": "2025-06-01", "currency": "SEK"},
            ],
        )
        == 0
    )
    with pytest.raises(ValueError):
        upsert_dividends(
            conn,
            cid,
            [{"exDate": "2025-06-99", "amount": 0, "currency": "SEK"}],
        )
    assert conn.execute("SELECT count(*) FROM dividends").fetchone()[0] == 0


@pytest.mark.parametrize(
    "as_of,start",
    [
        ("2026-01-01", "2025-01-01"),
        ("2028-02-29", "2027-02-28"),
        ("2025-02-28", "2024-02-28"),
        ("2029-02-28", "2028-02-28"),
        ("2026-03-01", "2025-03-01"),
    ],
)
def test_calendar_ttm_ex_date_edges_and_future_distributions(as_of, start):
    assert trailing_dividend_window(date.fromisoformat(as_of)) == (
        date.fromisoformat(start),
        date.fromisoformat(as_of),
    )
    conn, cid = seeded(as_of)
    coverage(conn, cid, as_of=as_of)
    # The start date is excluded, next day and cutoff included. Foreign/unknown
    # distributions outside the window cannot poison its sum/currency check.
    upsert_dividends(
        conn,
        cid,
        [
            {"exDate": start, "amount": 9, "currency": "USD"},
            {
                "exDate": (date.fromisoformat(start) + timedelta(days=1)).isoformat(),
                "amount": 1,
                "currency": "SEK",
            },
            {"exDate": as_of, "amount": 2, "currency": "SEK"},
            {"exDate": (date.fromisoformat(as_of) + timedelta(days=1)).isoformat(), "amount": 99},
        ],
    )
    loaded, _ = rank(conn, cid, as_of)
    assert loaded["valuation"].dividend_yield == 30
    assert len(loaded["dividend_yield"]["distributions"]) == 2
    assert loaded["dividend_yield"]["window_start"] == start


def test_date_growth_workers_2024_leap_cutoff_fixture():
    """Prior-year Feb-29 construction must not crash the shared loader."""
    conn = get_connection(Settings.from_env(dsn="sqlite:///:memory:"))
    migrate(conn)
    cid = upsert_company(
        conn,
        {
            "insId": 2,
            "name": "Leap fixture",
            "ticker": "LEAP",
            "stockPriceCurrency": "SEK",
            "reportCurrency": "SEK",
        },
    )
    upsert_financial_periods(
        conn,
        cid,
        [
            {
                "period_type": "year",
                "period_end": "2023-03-31",
                "report_Date": "2023-05-01",
                "revenues": 100,
                "number_Of_Shares": 10,
                "currency": "SEK",
            }
        ],
    )
    upsert_prices(conn, cid, [{"d": "2024-02-28", "c": 10}], currency="SEK")
    coverage(conn, cid, as_of="2024-02-29")
    loaded = load_results_for_company(conn, cid, "2024-02-29")
    assert loaded["financial"] is not None
    assert loaded["valuation"].dividend_yield == 0
    assert loaded["dividend_yield"]["window_start"] == "2023-02-28"
    assert loaded["dividend_yield"]["window_end"] == "2024-02-29"
    assert loaded["dividend_yield"]["price_date"] == "2024-02-28"


def test_additive_migration_refreshes_currency_without_erasing_verified_conflicts():
    conn, cid = seeded()
    conn.execute("ALTER TABLE dividends DROP COLUMN currency_verified")
    conn.execute("ALTER TABLE dividends DROP COLUMN currency_conflicted")
    conn.execute("DROP TABLE dividend_window_coverage")
    conn.execute(
        "INSERT INTO dividends (company_id, ex_date, amount, currency, dividend_type) VALUES (?, '2025-06-01', 1, 'SEK', 0)",
        (cid,),
    )
    conn.execute(
        "INSERT INTO dividend_coverage (company_id, covered_from, covered_through) VALUES (?, ?, ?)",
        (cid, START, AS_OF),
    )
    conn.execute("PRAGMA user_version=11")
    conn.commit()
    migrate(conn)
    migrate(conn)
    assert conn.execute(
        "SELECT amount, currency_verified, currency_conflicted FROM dividends"
    ).fetchone()[:] == (1, 0, 0)
    assert conn.execute("SELECT count(*) FROM dividend_coverage").fetchone()[0] == 1
    assert rank(conn, cid)[0]["dividend_yield"]["reason"] == "dividend_coverage_unknown"
    coverage(conn, cid)
    assert rank(conn, cid)[0]["dividend_yield"]["reason"] == "dividend_currency_unknown"
    row = {"exDate": "2025-06-01", "amount": 1, "currency": "SEK"}
    upsert_dividends(conn, cid, [row])
    assert rank(conn, cid)[0]["valuation"].dividend_yield == 10
    assert conn.execute("SELECT currency_verified, currency_conflicted FROM dividends").fetchone()[
        :
    ] == (1, 0)
    upsert_dividends(conn, cid, [{"exDate": "2025-06-01", "amount": 1, "currency": "USD"}])
    upsert_dividends(conn, cid, [row])
    assert rank(conn, cid)[0]["dividend_yield"]["reason"] == "dividend_currency_unknown"
    assert conn.execute("SELECT currency_verified, currency_conflicted FROM dividends").fetchone()[
        :
    ] == (0, 1)


@pytest.mark.parametrize("close", [None, 0, float("inf")])
def test_complete_window_requires_usable_selected_close(close):
    result = calculate_dividend_yield(
        date.fromisoformat(AS_OF),
        close,
        "SEK",
        [],
        {
            "window_start": START,
            "window_end": AS_OF,
            "status": "complete",
            "source": "fixture",
            "assurance": "independent fixture proof",
            "verified_at": AS_OF,
        },
    )
    assert result.value is None
    assert result.reason == "dividend_price_unavailable"


def test_complete_assertion_requires_independent_proof():
    conn, cid = seeded()
    with pytest.raises(Exception, match="CHECK constraint failed"):
        upsert_dividend_window_coverage(
            conn, cid, START, AS_OF, status="complete", source="fixture"
        )
    assert rank(conn, cid)[0]["valuation"].dividend_yield is None


@pytest.mark.parametrize("nested", [True, False])
def test_adapter_retains_dated_zero_not_undated_marker(nested):
    rows = [
        {"excludingDate": "2025-06-01", "amountPaid": 0, "currencyShortName": "SEK"},
        {"amountPaid": 0},
    ]
    payload = (
        {"values": [{"insId": 1, "values": rows}]}
        if nested
        else {"values": [{"insId": 1, **r} for r in rows]}
    )
    adapter = BorsdataAdapter(api_key="fixture")
    with patch.object(adapter, "_get_json", return_value=payload):
        dividends = adapter.get_dividends([1])
    assert len(dividends) == 1
    assert dividends[0]["exDate"] == "2025-06-01"
    conn, cid = seeded()
    upsert_dividends(conn, cid, dividends)
    assert rank(conn, cid)[0]["dividend_yield"]["reason"] == "dividend_coverage_unknown"
    coverage(conn, cid)
    assert rank(conn, cid)[0]["valuation"].dividend_yield == 0


@pytest.mark.parametrize("branch", [None, 75, 68])
def test_actual_ranking_exports_keep_yield_provenance_for_all_models(tmp_path, branch):
    conn, cid = seeded()
    coverage(conn, cid)
    upsert_dividends(conn, cid, [{"exDate": "2025-06-01", "amount": 1, "currency": "SEK"}])
    _, ranking = rank(conn, cid, branch=branch)
    json_path, csv_path = export_ranking_files(
        ranking, AS_OF, RankingEngine.RANKING_MODEL_VERSION, tmp_path
    )
    exported = json.loads(json_path.read_text())
    audit = exported["scores"][0]["scoring_audit"]["dividend_yield"]
    assert audit["value"] == 10
    assert audit["policy_version"] == DIVIDEND_YIELD_POLICY_VERSION
    assert audit["coverage"]["source"] == "synthetic_fixture"
    assert audit["price_currency"] == "SEK"
    assert exported["model_version"] == RankingEngine.RANKING_MODEL_VERSION
    with csv_path.open() as f:
        row = next(csv.DictReader(f))
    assert float(row["valuation_score"]) == ranking.scores[0].valuation_score


def test_real_cmd_rank_exports_and_persists_foreign_refusal(tmp_path, monkeypatch):
    conn, cid = seeded()
    conn.execute(
        "INSERT INTO watchlist (company_id, ticker, source_file, source_row_hash) VALUES (?, 'FIX', 'fixture', 'fixture')",
        (cid,),
    )
    coverage(conn, cid)
    args = argparse.Namespace(dsn="sqlite:///:memory:", as_of=AS_OF, watchlist=None)
    monkeypatch.chdir(tmp_path)
    with patch("alphaforge.db.connection.get_connection", return_value=conn):
        assert cmd_rank(args) == 0
        zero = json.loads((tmp_path / "exports" / AS_OF / "ranking.json").read_text())
        upsert_dividends(conn, cid, [{"exDate": "2025-06-01", "amount": 1, "currency": "USD"}])
        assert cmd_rank(args) == 0
    foreign = json.loads((tmp_path / "exports" / AS_OF / "ranking.json").read_text())
    assert zero["scores"][0]["scoring_audit"]["dividend_yield"]["value"] == 0
    assert (
        foreign["scores"][0]["scoring_audit"]["dividend_yield"]["reason"]
        == "dividend_currency_mismatch"
    )
    assert foreign["scores"][0]["scoring_audit"]["dividend_yield"]["value"] is None
    assert foreign["scores"][0]["valuation_score"] != zero["scores"][0]["valuation_score"]
    with (tmp_path / "exports" / AS_OF / "ranking.csv").open() as f:
        csv_row = next(csv.DictReader(f))
    assert float(csv_row["valuation_score"]) == foreign["scores"][0]["valuation_score"]
    assert conn.execute("SELECT count(*) FROM ranking_runs").fetchone()[0] == 2
    persisted = json.loads(
        conn.execute("SELECT scores FROM ranking_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
    )
    assert (
        persisted[0]["scoring_audit"]["dividend_yield"]
        == foreign["scores"][0]["scoring_audit"]["dividend_yield"]
    )
