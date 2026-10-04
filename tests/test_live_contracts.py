"""Trimmed Börsdata response contracts plus opt-in live smoke coverage."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from alphaforge.config import Settings
from alphaforge.db.connection import get_connection
from alphaforge.db.migrations import migrate
from alphaforge.db.repositories import (
    relink_watchlist,
    upsert_company,
    upsert_financial_periods,
    upsert_kpi_observations,
)
from alphaforge.providers.borsdata.adapter import BorsdataAdapter, BorsdataContractError

FIXTURES = Path(__file__).parent / "fixtures" / "borsdata"


def test_report_list_period_arrays_are_flattened():
    adapter = BorsdataAdapter(api_key="fixture")
    payload = json.loads((FIXTURES / "report_list.json").read_text())
    with patch.object(adapter, "_get_json", return_value=payload):
        rows = adapter.get_reports([101])
    assert [row["period_type"] for row in rows] == ["year", "quarter", "r12"]
    assert all(row["insId"] == 101 for row in rows)


def test_live_envelopes_and_pascal_casing_are_recognized():
    adapter = BorsdataAdapter(api_key="fixture")
    envelopes = json.loads((FIXTURES / "envelopes.json").read_text())
    with patch.object(
        adapter,
        "_get_json",
        side_effect=[
            BorsdataAdapter._normalize_keys(envelopes["kpi_history"]),
            BorsdataAdapter._normalize_keys(envelopes["report_metadata"]),
            BorsdataAdapter._normalize_keys(envelopes["stock_splits"]),
            BorsdataAdapter._normalize_keys(envelopes["plain_list"]),
        ],
    ):
        assert adapter.get_kpi_history(101, 2, "year", "mean")[0]["kpiId"] == 2
        assert adapter.get_report_metadata()[0]["property"] == "revenues"
        assert adapter.get_stock_splits()[0]["insId"] == 101
        assert adapter.get_stock_prices(101) == [{"date": "2025-01-01", "c": 12.5}]


def test_metadata_envelopes_are_recognized():
    adapter = BorsdataAdapter(api_key="fixture")
    envelopes = json.loads((FIXTURES / "envelopes.json").read_text())
    with patch.object(
        adapter,
        "_get_json",
        side_effect=[
            BorsdataAdapter._normalize_keys(envelopes["translation_metadata"]),
            BorsdataAdapter._normalize_keys(envelopes["kpi_metadata"]),
            BorsdataAdapter._normalize_keys(envelopes["report_metadata"]),
        ],
    ):
        assert adapter.get_translation_metadata()[0]["translationKey"] == "branch"
        assert adapter.get_kpi_metadata()[0]["kpiId"] == 2
        assert adapter.get_report_metadata()[0]["property"] == "revenues"


def test_unrecognized_200_shape_is_explicit_error():
    adapter = BorsdataAdapter(api_key="fixture")
    with patch.object(adapter, "_get_json", return_value={"unexpected": {"rows": []}}):
        with pytest.raises(BorsdataContractError):
            adapter.get_instruments()


def test_metadata_envelope_rejects_malformed_rows():
    adapter = BorsdataAdapter(api_key="fixture")
    with patch.object(adapter, "_get_json", return_value={"translationMetadatas": [None]}):
        with pytest.raises(BorsdataContractError):
            adapter.get_translation_metadata()


def test_stock_price_envelope_rejects_malformed_rows():
    adapter = BorsdataAdapter(api_key="fixture")
    with patch.object(
        adapter,
        "_get_json",
        return_value={"stockPricesList": [{"date": "2025-01-01"}]},
    ):
        with pytest.raises(BorsdataContractError):
            adapter.get_stock_prices(101)


def test_values_envelopes_are_flattened_with_instrument_identity():
    adapter = BorsdataAdapter(api_key="fixture")
    envelopes = json.loads((FIXTURES / "envelopes.json").read_text())
    dividend_payload = BorsdataAdapter._normalize_keys(envelopes["dividend_values"])
    calendar_payload = BorsdataAdapter._normalize_keys(envelopes["report_calendar_values"])
    with patch.object(adapter, "_get_json", side_effect=[dividend_payload, calendar_payload]):
        dividends = adapter.get_dividends([29, 221])
        calendar = adapter.get_report_calendar([29, 221])
    assert dividends == [
        {
            "exDate": "2025-05-15",
            "amountPaid": 1.25,
            "currency": "SEK",
            "dividendType": 0,
            "insId": 29,
        },
        {
            "exDate": "2025-06-10",
            "amountPaid": 0.85,
            "currency": "SEK",
            "dividendType": 0,
            "insId": 221,
        },
    ]
    assert calendar == [
        {"releaseDate": "2025-04-30", "reportType": "Q1", "insId": 29},
        {"releaseDate": "2025-05-07", "reportType": "Q1", "insId": 221},
    ]


def test_live_dividend_fields_are_canonicalized_and_dated_zero_rows_retained():
    adapter = BorsdataAdapter(api_key="fixture")
    payload = BorsdataAdapter._normalize_keys(
        json.loads((FIXTURES / "live_dividend_calendar.json").read_text())
    )
    with patch.object(adapter, "_get_json", return_value=payload):
        dividends = adapter.get_dividends([29, 221, 424])
    assert dividends == [
        {
            "excludingDate": "2025-05-15",
            "amountPaid": 1.25,
            "currencyShortName": "SEK",
            "distributionFrequency": 1,
            "dividendType": 4,
            "exDate": "2025-05-15",
            "insId": 29,
        },
        {
            "excludingDate": "2025-06-10",
            "amountPaid": 0.85,
            "currencyShortName": "SEK",
            "distributionFrequency": 1,
            "dividendType": 4,
            "exDate": "2025-06-10",
            "insId": 221,
        },
        {
            "excludingDate": "2025-07-10",
            "amountPaid": 0.0,
            "currencyShortName": "SEK",
            "distributionFrequency": 1,
            "dividendType": 4,
            "exDate": "2025-07-10",
            "insId": 424,
        },
    ]


def test_values_envelopes_reject_rows_without_resource_fields():
    adapter = BorsdataAdapter(api_key="fixture")
    with patch.object(
        adapter,
        "_get_json",
        return_value={"values": [{"insId": 29, "values": [{"unexpected": True}]}]},
    ):
        with pytest.raises(BorsdataContractError):
            adapter.get_dividends([29])


def test_zero_dividend_markers_without_dates_are_ignored():
    adapter = BorsdataAdapter(api_key="fixture")
    with patch.object(
        adapter,
        "_get_json",
        return_value={
            "values": [{"insId": 29, "values": [{"amountPaid": 0.0, "currency": "SEK"}]}]
        },
    ):
        assert adapter.get_dividends([29]) == []


def test_watchlist_rows_relink_without_replacing_source_row():
    conn = get_connection(Settings.from_env(dsn="sqlite:///:memory:"))
    migrate(conn)
    conn.execute(
        "INSERT INTO watchlist (ticker, source_file, source_row_hash, matched_via) VALUES ('TEST', 'captain.csv', 'row-1', 'unmatched')"
    )
    conn.commit()
    upsert_company(conn, {"insId": 101, "name": "Test AB", "ticker": "TEST", "instrument": 163})
    assert relink_watchlist(conn) == 1
    row = conn.execute("SELECT id, company_id, matched_via FROM watchlist").fetchone()
    assert row[0] == 1
    assert row[1] is not None
    assert row[2] == "ticker"
    assert conn.execute("SELECT instrument FROM companies").fetchone()[0] == 1


def test_sync_persists_fixture_values_and_kpi_history_idempotently():
    import argparse

    from alphaforge.cli.main import cmd_sync

    conn = get_connection(Settings.from_env(dsn="sqlite:///:memory:"))
    migrate(conn)
    payload = json.loads((FIXTURES / "envelopes.json").read_text())
    history_rows = BorsdataAdapter._normalize_keys(payload["kpi_history"])["kpiHistoryMetadatas"]
    dividend_payload = BorsdataAdapter._normalize_keys(
        json.loads((FIXTURES / "live_dividend_calendar.json").read_text())
    )
    calendar_payload = BorsdataAdapter._normalize_keys(payload["report_calendar_values"])
    fixture_adapter = BorsdataAdapter(api_key="fixture")
    with patch.object(
        fixture_adapter,
        "_get_json",
        side_effect=[dividend_payload, calendar_payload],
    ):
        dividend_rows = fixture_adapter.get_dividends([29, 221, 424])
        calendar_rows = fixture_adapter.get_report_calendar([29, 221, 424])

    class FixtureAdapter:
        def get_instruments(self):
            return [
                {
                    "insId": ins_id,
                    "name": f"Company {ins_id}",
                    "ticker": ticker,
                    "instrument": 1,
                    "branchId": 1,
                    "stockPriceCurrency": "SEK",
                }
                for ins_id, ticker in ((29, "BEIA B"), (221, "SYSR"), (424, "INWI"))
            ]

        def get_branches(self):
            return [{"id": 1, "name": "Branch 1", "nameEn": "Branch 1", "sectorId": None}]

        def get_sectors(self):
            return []

        def get_countries(self):
            return []

        def get_translation_metadata(self):
            return []

        def get_kpi_metadata(self):
            return [
                {
                    "kpiId": 2,
                    "nameSv": "KPI 2",
                    "nameEn": "KPI 2",
                    "format": None,
                    "isString": False,
                }
            ]

        def get_report_metadata(self):
            return []

        def get_reports(self, ins_ids, *, original=0, target_currencies=None):
            return []

        def get_stock_prices(self, ins_id, *, max_count=None):
            return []

        def get_kpi_summary(self, ins_id, report_type):
            if report_type in ("year", "r12"):
                return {"kpis": [{"kpiId": 2, "values": [12.5]}]}
            return {"kpis": []}

        def get_kpi_history(self, ins_id, kpi_id, report_type, price_type):
            # Dedicated ROIC/NET_DEBT_EBITDA fetches (37/42) should be empty in this
            # fixture so the idempotent count stays 60 — they are tested with live
            # data elsewhere where history is present.
            if kpi_id in (37, 42):
                return []
            return history_rows

        def get_dividends(self, ins_ids=None):
            return dividend_rows

        def get_stock_splits(self):
            return []

        def get_report_calendar(self, ins_ids=None):
            return calendar_rows

        def get_shorts(self):
            return []

    args = argparse.Namespace(
        dsn="sqlite:///:memory:", all=True, company=None, ticker=None, allow_empty_companies=True
    )
    with (
        patch("alphaforge.db.connection.get_connection", return_value=conn),
        patch("alphaforge.db.migrations.migrate", return_value=None),
        patch("alphaforge.providers.borsdata.adapter.BorsdataAdapter", FixtureAdapter),
    ):
        assert cmd_sync(args) == 0
        first_count = conn.execute("SELECT count(*) FROM kpi_observations").fetchone()[0]
        assert first_count == 60
        assert (
            conn.execute("SELECT count(DISTINCT company_id) FROM kpi_observations").fetchone()[0]
            == 3
        )
        assert [
            row[0]
            for row in conn.execute(
                "SELECT count(*) FROM kpi_observations GROUP BY company_id ORDER BY company_id"
            ).fetchall()
        ] == [20, 20, 20]
        assert conn.execute("SELECT count(*) FROM dividends").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM report_calendar").fetchone()[0] == 2
        assert conn.execute("SELECT count(DISTINCT company_id) FROM dividends").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM dividend_window_coverage").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM dividend_coverage").fetchone()[0] == 0
        persisted_dividend_rows = conn.execute(
            """
            SELECT c.borsdata_id, d.amount, d.dividend_type
            FROM dividends d JOIN companies c ON c.id=d.company_id
            ORDER BY c.borsdata_id
            """
        ).fetchall()
        assert [tuple(row) for row in persisted_dividend_rows] == [
            (29, 1.25, 4),
            (221, 0.85, 4),
            (424, 0.0, 4),
        ]
        assert (
            conn.execute("SELECT count(DISTINCT company_id) FROM report_calendar").fetchone()[0]
            == 2
        )
        assert cmd_sync(args) == 0
        assert conn.execute("SELECT count(*) FROM kpi_observations").fetchone()[0] == first_count
        assert conn.execute("SELECT count(*) FROM dividends").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM dividend_window_coverage").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM report_calendar").fetchone()[0] == 2
        conn.execute(
            """INSERT INTO dividend_window_coverage
               (company_id, window_start, window_end, status, source, assurance, verified_at)
               SELECT id, '2025-01-01', '2026-01-01', 'complete',
                      'independent_fixture', 'synthetic independent proof', '2026-01-01'
               FROM companies"""
        )
        conn.commit()
        for response in ([], BorsdataContractError("synthetic calendar failure")):
            kwargs = (
                {"side_effect": response}
                if isinstance(response, Exception)
                else {"return_value": response}
            )
            with patch.object(FixtureAdapter, "get_dividends", **kwargs):
                assert cmd_sync(args) == int(isinstance(response, Exception))
            assert [
                tuple(row)
                for row in conn.execute(
                    """SELECT status, source, assurance, verified_at
                       FROM dividend_window_coverage ORDER BY company_id"""
                )
            ] == [
                (
                    "complete",
                    "independent_fixture",
                    "synthetic independent proof",
                    "2026-01-01",
                )
            ] * 3
            assert conn.execute("SELECT count(*) FROM dividends").fetchone()[0] == 3

        with patch.object(
            FixtureAdapter,
            "get_dividends",
            return_value=[
                {
                    "insId": 29,
                    "exDate": "2025-08-01",
                    "amountPaid": 9,
                    "currencyShortName": "SEK",
                    "dividendType": 4,
                },
                {
                    "insId": 29,
                    "exDate": "2025-08-99",
                    "amountPaid": 1,
                    "currencyShortName": "SEK",
                    "dividendType": 4,
                },
            ],
        ):
            assert cmd_sync(args) == 1
        company_id = conn.execute("SELECT id FROM companies WHERE borsdata_id=29").fetchone()[0]
        assert conn.execute("SELECT count(*) FROM dividends").fetchone()[0] == 3
        assert (
            conn.execute(
                "SELECT count(*) FROM dividends WHERE company_id=? AND ex_date='2025-08-01'",
                (company_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT status FROM jobs WHERE job_type='sync_dividends' AND company_id=?",
                (company_id,),
            ).fetchone()[0]
            == "failed"
        )


@pytest.mark.parametrize("legacy_payload", [None, "[]"], ids=["null", "array"])
def test_sync_retries_then_corrects_legacy_nonobject_financial_slot(legacy_payload):
    import argparse

    from alphaforge.cli.main import cmd_sync

    correction = {
        "insId": 909,
        "period_type": "year",
        "period_end": "2026-03-31",
        "report_Date": "2026-05-01",
        "year": 2026,
        "period": 5,
        "revenues": 200,
        "operating_Income": 40,
        "profit_To_Equity_Holders": 20,
        "free_Cash_Flow": 20,
        "total_Equity": 80,
        "net_Debt": 5,
        "number_Of_Shares": 10,
        "currency": "SEK",
    }

    class FinancialCorrectionAdapter:
        def get_instruments(self):
            return [
                {
                    "insId": 909,
                    "name": "Legacy Financial AB",
                    "ticker": "RAW",
                    "instrument": 1,
                    "branchId": 1,
                    "stockPriceCurrency": "SEK",
                }
            ]

        def get_branches(self):
            return [{"id": 1, "name": "Branch 1", "nameEn": "Branch 1"}]

        def get_sectors(self):
            return []

        def get_countries(self):
            return []

        def get_translation_metadata(self):
            return []

        def get_kpi_metadata(self):
            return []

        def get_report_metadata(self):
            return []

        def get_reports(self, ins_ids, *, original=0, target_currencies=None):
            target = (target_currencies or {}).get(909)
            return [
                {
                    **correction,
                    "conversion_mode": "converted",
                    "conversion_target_currency": target,
                    "values_currency": target,
                }
            ]

        def get_stock_prices(self, ins_id, *, max_count=None):
            return []

        def get_kpi_summary(self, ins_id, report_type):
            return {"kpis": []}

        def get_kpi_history(self, ins_id, kpi_id, report_type, price_type):
            return []

        def get_dividends(self, ins_ids=None):
            return []

        def get_stock_splits(self):
            return []

        def get_report_calendar(self, ins_ids=None):
            return []

        def get_shorts(self):
            return []

    conn = get_connection(Settings.from_env(dsn="sqlite:///:memory:"))
    migrate(conn)
    company_id = upsert_company(
        conn,
        {
            "insId": 909,
            "name": "Legacy Financial AB",
            "ticker": "RAW",
            "instrument": 1,
            "branchId": 1,
        },
    )
    original = dict(correction)
    original["revenues"] = 100
    assert upsert_financial_periods(conn, company_id, [original]) == 1
    conn.execute(
        "UPDATE financial_periods SET raw_payload=? WHERE company_id=?",
        (legacy_payload, company_id),
    )
    conn.commit()
    args = argparse.Namespace(
        dsn="sqlite:///:memory:",
        all=False,
        company=None,
        ticker="RAW",
        allow_empty_companies=False,
    )

    with (
        patch("alphaforge.db.connection.get_connection", return_value=conn),
        patch("alphaforge.db.migrations.migrate", return_value=None),
        patch(
            "alphaforge.providers.borsdata.adapter.BorsdataAdapter",
            FinancialCorrectionAdapter,
        ),
    ):
        with patch(
            "alphaforge.db.repositories.upsert_financial_periods",
            side_effect=RuntimeError("synthetic financial persistence failure"),
        ):
            assert cmd_sync(args) == 1
        failed = conn.execute(
            "SELECT status, error FROM jobs WHERE job_type='sync_reports' AND company_id=?",
            (company_id,),
        ).fetchone()
        assert failed["status"] == "failed"
        failure = json.loads(failed["error"])
        assert failure["code"] == "reports_upsert_failed"
        assert failure["retryable"] is True
        assert (
            conn.execute(
                "SELECT raw_payload FROM financial_periods WHERE company_id=?",
                (company_id,),
            ).fetchone()[0]
            == legacy_payload
        )

        assert cmd_sync(args) == 0
        stored = conn.execute(
            "SELECT revenue, raw_payload FROM financial_periods WHERE company_id=?",
            (company_id,),
        ).fetchone()
        assert stored["revenue"] == 200
        assert json.loads(stored["raw_payload"]) == {
            **correction,
            "conversion_mode": "converted",
            "conversion_target_currency": "SEK",
            "values_currency": "SEK",
        }
        assert conn.execute("SELECT count(*) FROM financial_period_rejections").fetchone()[0] == 0
        assert cmd_sync(args) == 0
        retried = conn.execute(
            "SELECT status FROM jobs WHERE job_type='sync_reports' AND company_id=?",
            (company_id,),
        ).fetchone()
        assert retried["status"] == "success"


def test_sync_counts_durable_kpi_rejections_and_retries_write_failures():
    import argparse

    from alphaforge.cli.main import cmd_sync

    class RejectedKpiAdapter:
        fail_prices = False
        summary_calls = 0

        def get_instruments(self):
            return [
                {
                    "insId": 909,
                    "name": "Rejected KPI AB",
                    "ticker": "RKPI",
                    "instrument": 1,
                    "branchId": 1,
                    "stockPriceCurrency": "SEK",
                }
            ]

        def get_branches(self):
            return [{"id": 1, "name": "Branch 1", "nameEn": "Branch 1"}]

        def get_sectors(self):
            return []

        def get_countries(self):
            return []

        def get_translation_metadata(self):
            return []

        def get_kpi_metadata(self):
            return [
                {
                    "kpiId": kpi_id,
                    "nameSv": f"KPI {kpi_id}",
                    "nameEn": f"KPI {kpi_id}",
                    "format": None,
                    "isString": False,
                }
                for kpi_id in (37, 42, 99)
            ]

        def get_report_metadata(self):
            return []

        def get_reports(self, ins_ids, *, original=0, target_currencies=None):
            return []

        def get_stock_prices(self, ins_id, *, max_count=None):
            if self.fail_prices:
                raise RuntimeError("synthetic price failure")
            return []

        def get_kpi_summary(self, ins_id, report_type):
            type(self).summary_calls += 1
            if report_type in {"year", "r12"}:
                return {
                    "kpis": [
                        {
                            "kpiId": 99,
                            "values": [
                                {"y": 2026, "p": 5, "v": 0},
                                {"y": 2026, "p": 4, "v": None},
                                {"y": 2026, "p": 1, "v": None, "value": 7},
                            ],
                        }
                    ]
                }
            return {"kpis": []}

        def get_kpi_history(self, ins_id, kpi_id, report_type, price_type):
            if kpi_id == 37:
                return [
                    {
                        "year": 2026,
                        "y": 2027,
                        "reportPeriod": 1.5,
                        "p": 1,
                        "v": 40,
                        "observationDate": "2026-06-01",
                    }
                ]
            period = 3 if kpi_id == 99 else 2
            return [{"y": 2026, "p": period, "v": 0}]

        def get_dividends(self, ins_ids=None):
            return []

        def get_stock_splits(self):
            return []

        def get_report_calendar(self, ins_ids=None):
            return []

        def get_shorts(self):
            return []

    args = argparse.Namespace(
        dsn="sqlite:///:memory:",
        all=True,
        company=None,
        ticker=None,
        allow_empty_companies=True,
    )
    conn = get_connection(Settings.from_env(dsn="sqlite:///:memory:"))
    migrate(conn)
    company_id = upsert_company(
        conn,
        {
            "insId": 909,
            "name": "Rejected KPI AB",
            "ticker": "RKPI",
            "instrument": 1,
            "branchId": 1,
        },
    )
    assert (
        upsert_kpi_observations(
            conn,
            company_id,
            37,
            "year",
            "mean",
            [{"y": 2026, "p": 1, "v": 30, "observationDate": "2026-06-01"}],
        )
        == 1
    )
    common_patches = (
        patch("alphaforge.db.connection.get_connection", return_value=conn),
        patch("alphaforge.db.migrations.migrate", return_value=None),
        patch("alphaforge.providers.borsdata.adapter.BorsdataAdapter", RejectedKpiAdapter),
    )
    with common_patches[0], common_patches[1], common_patches[2]:
        assert cmd_sync(args) == 0
        assert conn.execute("SELECT count(*) FROM kpi_observations").fetchone()[0] == 1
        stored = conn.execute(
            "SELECT value, raw_payload FROM kpi_observations WHERE kpi_id=37"
        ).fetchone()
        assert stored["value"] == 30
        assert json.loads(stored["raw_payload"]) == {
            "y": 2026,
            "p": 1,
            "v": 30,
            "observationDate": "2026-06-01",
        }
        assert conn.execute("SELECT count(*) FROM market_input_rejections").fetchone()[0] == 12
        assert [
            tuple(row)
            for row in conn.execute(
                """SELECT kpi_id, count(*) FROM market_input_rejections
                   GROUP BY kpi_id ORDER BY kpi_id"""
            ).fetchall()
        ] == [(37, 2), (42, 2), (99, 8)]
        assert cmd_sync(args) == 0
        assert conn.execute("SELECT count(*) FROM market_input_rejections").fetchone()[0] == 12
        assert {
            row[0]
            for row in conn.execute(
                "SELECT status FROM jobs WHERE job_type LIKE 'sync_kpis%'"
            ).fetchall()
        } == {"success"}

        with patch(
            "alphaforge.cli.kpi_sync.upsert_kpi_observations",
            side_effect=RuntimeError("synthetic KPI persistence failure"),
        ):
            assert cmd_sync(args) == 1
        failed = conn.execute(
            """SELECT status, error FROM jobs
               WHERE job_type='sync_kpis_37_year'"""
        ).fetchone()
        assert failed["status"] == "failed"
        failure = json.loads(failed["error"])
        assert failure["code"] == "kpi_history_upsert_failed"
        assert failure["retryable"] is True

        assert cmd_sync(args) == 0
        retried = conn.execute(
            "SELECT status FROM jobs WHERE job_type='sync_kpis_37_year'"
        ).fetchone()
        assert retried["status"] == "success"

        summary_calls_before_price_failure = RejectedKpiAdapter.summary_calls
        with patch.object(RejectedKpiAdapter, "fail_prices", True):
            assert cmd_sync(args) == 1
        assert RejectedKpiAdapter.summary_calls == summary_calls_before_price_failure + 3


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("BORSDATA_API_KEY"), reason="BORSDATA_API_KEY not set")
def test_live_instruments_contract():
    """Opt-in network check; CI remains fixture-only without a key."""
    instruments = BorsdataAdapter().get_instruments()
    assert instruments
    assert all("insId" in row or "id" in row for row in instruments[:10])
