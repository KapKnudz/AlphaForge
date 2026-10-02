"""Published schema shapes migrate without erasing evidence or inventing assurance."""

import json

import pytest
from test_method_date_growth_selection import annual, packet, rank_exports, setup

from alphaforge.cli.ranking_loader import load_results_for_company
from alphaforge.db.migrations import migrate
from alphaforge.db.repositories import (
    upsert_dividend_window_coverage,
    upsert_dividends,
    upsert_financial_periods,
    upsert_kpi_observations,
    upsert_prices,
)

SHAPES = ["market13", "dividend13", "faulty14", "healthy14", "partial14", "older12", "v15"]


def old_database(shape):
    conn, cid = setup(periods=[annual(2024, 100), annual(2025, 110), annual(2026, 121)])
    packet(conn, cid)
    upsert_dividends(conn, cid, [{"exDate": "2026-05-01", "amount": 1, "currency": "SEK"}])
    upsert_dividend_window_coverage(
        conn,
        cid,
        "2025-06-01",
        "2026-06-01",
        status="complete",
        source="fixture",
        assurance="independent complete calendar",
        verified_at="2026-06-01T00:00:00Z",
    )
    upsert_kpi_observations(conn, cid, 37, "year", "mean", [{"y": 2026, "v": 7}])
    conn.execute(
        "INSERT INTO dividend_coverage(company_id,covered_from,covered_through) VALUES (?,?,?)",
        (cid, "2020-01-01", "2026-05-01"),
    )
    if shape in {"market13", "faulty14", "older12"}:
        conn.execute("ALTER TABLE dividends DROP COLUMN currency_verified")
        conn.execute("ALTER TABLE dividends DROP COLUMN currency_conflicted")
        conn.execute("DROP TABLE dividend_window_coverage")
    if shape == "partial14":
        conn.execute("ALTER TABLE dividends DROP COLUMN currency_conflicted")
    if shape in {"dividend13", "older12"}:
        conn.execute("ALTER TABLE prices DROP COLUMN raw_payload")
        conn.execute("ALTER TABLE kpi_observations DROP COLUMN raw_payload")
        conn.execute("DROP TABLE market_input_rejections")
    for column in ("values_currency", "conversion_mode", "conversion_target_currency"):
        conn.execute(f"ALTER TABLE financial_periods DROP COLUMN {column}")
    version = (
        12 if shape == "older12" else 13 if shape.endswith("13") else 15 if shape == "v15" else 14
    )
    conn.execute(f"PRAGMA user_version={version}")
    conn.commit()
    return conn, cid


def contents(conn):
    result = {}
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        if table.startswith("sqlite_"):
            continue
        columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
        result[table] = (
            columns,
            sorted(tuple(row) for row in conn.execute(f'SELECT * FROM "{table}"')),
        )
    return result


@pytest.mark.parametrize("shape", SHAPES)
def test_upgrade_preserves_every_existing_column_and_row(shape):
    conn, cid = old_database(shape)
    before = contents(conn)
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 16
    for table, (columns, rows) in before.items():
        projection = ",".join(f'"{column}"' for column in columns)
        assert (
            sorted(tuple(row) for row in conn.execute(f'SELECT {projection} FROM "{table}"'))
            == rows
        )
    flags = tuple(
        conn.execute("SELECT currency_verified,currency_conflicted FROM dividends").fetchone()
    )
    assert flags == ((0, 0) if shape in {"market13", "faulty14", "older12"} else (1, 0))
    if shape in {"market13", "faulty14", "older12"}:
        assert conn.execute("SELECT count(*) FROM dividend_window_coverage").fetchone()[0] == 0
    else:
        assert conn.execute("SELECT status,assurance FROM dividend_window_coverage").fetchone()[
            :
        ] == ("complete", "independent complete calendar")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    once = contents(conn)
    migrate(conn)
    assert contents(conn) == once
    conn.close()


@pytest.mark.parametrize("shape", SHAPES)
def test_upgrade_executes_loader_and_real_rank_exports(shape, monkeypatch, tmp_path):
    conn, cid = old_database(shape)
    migrate(conn)
    loaded = load_results_for_company(conn, cid, "2026-06-01")
    assert loaded["financial"] is None
    assert any(
        "report conversion mode unavailable" in reason
        for reason in loaded["selection"]["refusal_reasons"]
    )
    score, csv_row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    assert score["input_selection"] == json.loads(csv_row["input_selection"])
    assert dcf[str(cid)]["selection"] == score["input_selection"]
    # Migration is not acquisition: exact report reacquisition restores denomination.
    reacquired = annual(2026, 121)
    reacquired.update(
        conversion_mode="original",
        conversion_target_currency="SEK",
        values_currency="SEK",
    )
    upsert_financial_periods(conn, cid, [reacquired])
    loaded = load_results_for_company(conn, cid, "2026-06-01")
    assert loaded["financial"] is not None
    # Actual source writers can restore dividend usability; no migration-created assurance is used.
    upsert_dividends(conn, cid, [{"exDate": "2026-05-01", "amount": 1, "currency": "SEK"}])
    upsert_dividend_window_coverage(
        conn,
        cid,
        "2025-06-01",
        "2026-06-01",
        status="complete",
        source="fixture",
        assurance="genuine reacquisition fixture",
        verified_at="2026-06-01T00:00:00Z",
    )
    upsert_prices(conn, cid, [{"d": "2026-06-01", "c": 10}], currency="SEK")
    loaded = load_results_for_company(conn, cid, "2026-06-01")
    assert loaded["dividend_yield"]["reason"] is None
    assert loaded["dividend_yield"]["value"] == 10
    conn.close()


def test_fresh_schema_and_repeated_migration():
    conn, cid = setup()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 16
    before = contents(conn)
    migrate(conn)
    assert contents(conn) == before
    assert load_results_for_company(conn, cid, "2026-06-01")["financial"] is not None
    conn.close()


def test_existing_conflict_remains_refused_after_upgrade():
    conn, cid = old_database("healthy14")
    row = {"exDate": "2026-04-01", "amount": 2, "currency": "SEK"}
    upsert_dividends(conn, cid, [row, {**row, "currency": "USD"}])
    before_dividends = contents(conn)["dividends"]
    migrate(conn)
    assert contents(conn)["dividends"] == before_dividends
    assert conn.execute(
        "SELECT currency_verified,currency_conflicted FROM dividends WHERE amount=2"
    ).fetchone()[:] == (0, 1)
    reacquired = annual(2026, 121)
    reacquired.update(
        conversion_mode="original",
        conversion_target_currency="SEK",
        values_currency="SEK",
    )
    upsert_financial_periods(conn, cid, [reacquired])
    assert load_results_for_company(conn, cid, "2026-06-01")["dividend_yield"]["value"] is None
    conn.close()
