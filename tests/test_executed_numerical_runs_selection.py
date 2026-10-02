"""Retention excludes unconsumed latest-state rows without losing refusal facts."""

import json

import pytest
from test_executed_numerical_runs import companies, latest, seeded
from test_method_date_growth_selection import CUTOFF, annual, rank_exports, upsert_financial_periods

from alphaforge.db.numerical_runs import capture_inputs, digest, replay_run
from alphaforge.db.repositories import (
    upsert_company,
    upsert_dividend_window_coverage,
    upsert_kpi_observations,
    upsert_prices,
    upsert_stock_splits,
)


def test_unselected_price_and_superseded_kpi_corrections_do_not_change_body():
    conn, cid = seeded()
    body, _ = capture_inputs(conn, companies(conn), CUTOFF)
    upsert_prices(conn, cid, [{"d": "2026-05-15", "c": 15}], currency="SEK")
    upsert_kpi_observations(
        conn, cid, 37, "r12", "latest", [{"year": 2025, "date": "2025-05-01", "v": 15}]
    )
    changed, _ = capture_inputs(conn, companies(conn), CUTOFF)
    assert digest(body) == digest(changed)
    assert len(changed["tables"]["prices"]) == 3  # current and two historical pairs
    assert len(changed["tables"]["kpi_observations"]) == 1


@pytest.mark.parametrize(
    "correction",
    ["date", "unit", "acquisition", "fiscal", "split", "branch", "coverage", "missing_kpi"],
)
def test_actual_corrections_keep_original_run_but_change_current_identity(
    monkeypatch, tmp_path, correction
):
    conn, cid = seeded()
    rank_exports(conn, monkeypatch, tmp_path)
    original_run, original_identity, original = latest(conn)
    if correction == "date":
        conn.execute("DELETE FROM prices WHERE price_date=?", (CUTOFF,))
        upsert_prices(conn, cid, [{"d": "2026-05-31", "c": 10}], currency="SEK")
    elif correction == "unit":
        upsert_prices(conn, cid, [{"d": CUTOFF, "c": 10}], currency="EUR")
    elif correction == "acquisition":
        upsert_financial_periods(
            conn,
            cid,
            [
                annual(
                    2026,
                    121,
                    conversion_mode="converted",
                    conversion_target_currency="EUR",
                    values_currency="EUR",
                )
            ],
        )
    elif correction == "fiscal":
        upsert_financial_periods(conn, cid, [{**annual(2026, 121), "year": 2025}])
    elif correction == "split":
        upsert_stock_splits(
            conn, [{"insId": 991, "splitDate": "2025-06-01", "splitType": "split", "ratio": "3:1"}]
        )
    elif correction == "branch":
        upsert_company(
            conn,
            {
                "insId": 991,
                "name": "Synthetic AB",
                "ticker": "FIX",
                "branchId": 75,
                "stockPriceCurrency": "SEK",
                "reportCurrency": "SEK",
            },
        )
    elif correction == "coverage":
        upsert_dividend_window_coverage(
            conn,
            cid,
            "2025-06-01",
            CUTOFF,
            status="partial",
            source="fixture",
            assurance=None,
            verified_at=None,
        )
    else:
        upsert_kpi_observations(conn, cid, 37, "r12", "latest", [{"year": 2026, "v": 0}])
    rank_exports(conn, monkeypatch, tmp_path)
    current_run, current_identity, current = latest(conn)
    assert current_run != original_run
    assert current_identity != original_identity
    assert current != original
    assert replay_run(conn, original_run)["outputs"] == original
    snapshots = list(
        conn.execute("SELECT textual_context FROM executed_numerical_runs ORDER BY run_id")
    )
    assert json.loads(snapshots[0][0]) == json.loads(snapshots[1][0])
