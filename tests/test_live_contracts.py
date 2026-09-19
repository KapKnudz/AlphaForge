"""Trimmed Börsdata response contracts plus opt-in live smoke coverage."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from dotenv import load_dotenv

from alphaforge.config import Settings
from alphaforge.db.connection import get_connection
from alphaforge.db.migrations import migrate
from alphaforge.db.repositories import relink_watchlist, upsert_company
from alphaforge.providers.borsdata.adapter import BorsdataAdapter, BorsdataContractError

FIXTURES = Path(__file__).parent / "fixtures" / "borsdata"
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


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


def test_sync_persists_fixture_kpi_history_idempotently():
    import argparse

    from alphaforge.cli.main import cmd_sync

    conn = get_connection(Settings.from_env(dsn="sqlite:///:memory:"))
    migrate(conn)
    payload = json.loads((FIXTURES / "envelopes.json").read_text())
    history_rows = BorsdataAdapter._normalize_keys(payload["kpi_history"])["kpiHistoryMetadatas"]

    class FixtureAdapter:
        def get_instruments(self):
            return [
                {
                    "insId": ins_id,
                    "name": f"Company {ins_id}",
                    "ticker": ticker,
                    "instrument": 1,
                    "branchId": 1,
                }
                for ins_id, ticker in ((29, "BEIA B"), (221, "SYSR"), (424, "INWI"))
            ]

        def get_branches(self):
            return []

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

        def get_reports(self, ins_ids, *, original=0):
            return []

        def get_stock_prices(self, ins_id, *, max_count=None):
            return []

        def get_kpi_summary(self, ins_id, report_type):
            if report_type in ("year", "r12"):
                return {"kpis": [{"kpiId": 2, "values": [12.5]}]}
            return {"kpis": []}

        def get_kpi_history(self, ins_id, kpi_id, report_type, price_type):
            return history_rows

        def get_dividends(self):
            return []

        def get_stock_splits(self):
            return []

        def get_report_calendar(self):
            return []

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
        assert cmd_sync(args) == 0
        assert conn.execute("SELECT count(*) FROM kpi_observations").fetchone()[0] == first_count


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("BORSDATA_API_KEY"), reason="BORSDATA_API_KEY not set")
def test_live_instruments_contract():
    """Opt-in network check; CI remains fixture-only without a key."""
    instruments = BorsdataAdapter().get_instruments()
    assert instruments
    assert all("insId" in row or "id" in row for row in instruments[:10])
