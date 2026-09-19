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
from alphaforge.db.repositories import relink_watchlist, upsert_company
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
    with patch.object(adapter, "_get_json", side_effect=[
        BorsdataAdapter._normalize_keys(envelopes["kpi_history"]),
        BorsdataAdapter._normalize_keys(envelopes["report_metadata"]),
        BorsdataAdapter._normalize_keys(envelopes["stock_splits"]),
        BorsdataAdapter._normalize_keys(envelopes["plain_list"]),
    ]):
        assert adapter.get_kpi_history(101, 2, "year", "mean")[0]["kpiId"] == 2
        assert adapter.get_report_metadata()[0]["property"] == "revenues"
        assert adapter.get_stock_splits()[0]["insId"] == 101
        assert adapter.get_stock_prices(101) == [{"kpiId": 2, "v": 12.5}]


def test_unrecognized_200_shape_is_explicit_error():
    adapter = BorsdataAdapter(api_key="fixture")
    with patch.object(adapter, "_get_json", return_value={"unexpected": {"rows": []}}):
        with pytest.raises(BorsdataContractError):
            adapter.get_instruments()


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


@pytest.mark.integration
@pytest.mark.skipif(not os.environ.get("BORSDATA_API_KEY"), reason="BORSDATA_API_KEY not set")
def test_live_instruments_contract():
    """Opt-in network check; CI remains fixture-only without a key."""
    instruments = BorsdataAdapter().get_instruments()
    assert instruments
    assert all("insId" in row or "id" in row for row in instruments[:10])
