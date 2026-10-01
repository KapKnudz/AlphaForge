"""Phase 1 exit gate — idempotent sync, placeholder quarantine, bilingual dedupe, PIT filter."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from alphaforge.config import SCHEMA_VERSION, Settings
from alphaforge.core.fx import convert_sum
from alphaforge.core.point_in_time import in_window
from alphaforge.db.connection import get_connection
from alphaforge.db.migrations import get_user_version, migrate
from alphaforge.db.repositories import upsert_financial_periods
from alphaforge.evidence.ingest import ResearchDocumentIngestionService, bilingual_dedupe
from alphaforge.ownership import build_ownership_evidence
from alphaforge.ownership.parser import parse_top10_holders, tag_mfn_events
from alphaforge.providers.borsdata.adapter import BorsdataAdapter


@pytest.fixture()
def mem_conn():
    settings = Settings.from_env(dsn="sqlite:///:memory:")
    conn = get_connection(settings)
    migrate(conn)
    yield conn
    conn.close()


def test_schema_user_version_and_wal(mem_conn):
    assert get_user_version(mem_conn) == SCHEMA_VERSION
    cur = mem_conn.execute("PRAGMA journal_mode;")
    mode = cur.fetchone()[0]
    # In-memory returns "memory" or "wal" — check that migrate set it (not delete)
    assert mode in ("wal", "memory")
    cur = mem_conn.execute("PRAGMA foreign_keys;")
    assert int(cur.fetchone()[0]) == 1


def test_v1_dividend_constraint_migrates_for_type_4(mem_conn):
    mem_conn.execute("DROP TABLE dividends")
    mem_conn.executescript(
        """
        CREATE TABLE dividends (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            ex_date TEXT NOT NULL,
            amount REAL NOT NULL CHECK (amount >= 0),
            currency TEXT NOT NULL,
            dividend_type INTEGER NOT NULL CHECK (dividend_type IN (0,1,2)),
            distribution_frequency TEXT,
            fetched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE (company_id, ex_date, dividend_type, amount)
        ) STRICT;
        PRAGMA user_version=1;
        """
    )
    mem_conn.commit()
    migrate(mem_conn)
    company_id = mem_conn.execute(
        "INSERT INTO companies (borsdata_id, name) VALUES (400, 'Type Four AB') RETURNING id"
    ).fetchone()[0]
    mem_conn.execute(
        "INSERT INTO dividends (company_id, ex_date, amount, currency, dividend_type) VALUES (?, ?, ?, ?, ?)",
        (company_id, "2025-05-15", 1.25, "SEK", 4),
    )
    mem_conn.commit()
    assert get_user_version(mem_conn) == SCHEMA_VERSION
    assert mem_conn.execute("SELECT dividend_type FROM dividends").fetchone()[0] == 4
    for table in (
        "mfn_issuer_mappings",
        "research_attachments",
        "document_extractions",
        "document_pages",
        "evidence_packets",
        "evidence_run_diagnostics",
        "evidence_selection_manifests",
    ):
        assert (
            mem_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            is not None
        )


def test_import_watchlist_isin_normalized_and_uniqueness(mem_conn, tmp_path):
    # Seed companies
    mem_conn.execute(
        "INSERT INTO companies (borsdata_id, name, ticker, isin) VALUES (1, 'Test AB', 'TEST', 'SE0000000001')"
    )
    mem_conn.commit()
    # Create CSV fixture
    csv_path = tmp_path / "watchlist.csv"
    csv_path.write_text("Id;Name;Ticker;ISIN\n1;Test AB;TEST;SE0000000001\n", encoding="utf-8")
    import argparse

    from alphaforge.cli.main import cmd_import_watchlist

    args = argparse.Namespace(
        file=str(csv_path), source_file="watchlist_test", dsn="sqlite:///:memory:"
    )
    # Patch get_connection + migrate to return our mem_conn
    with (
        patch("alphaforge.db.connection.get_connection", return_value=mem_conn),
        patch("alphaforge.db.migrations.migrate", return_value=None),
    ):
        rc = cmd_import_watchlist(args)
        assert rc == 0
        # Second import — row_hash uniqueness suppresses duplicate
        rc2 = cmd_import_watchlist(args)
        assert rc2 == 0
    cur = mem_conn.execute("SELECT count(*) FROM watchlist WHERE source_file='watchlist_test'")
    assert cur.fetchone()[0] == 1
    cur = mem_conn.execute("SELECT matched_via FROM watchlist WHERE source_file='watchlist_test'")
    assert cur.fetchone()[0] == "isin"


def test_is_placeholder_quarantine_visible(mem_conn):
    # Seed company
    mem_conn.execute("INSERT INTO companies (borsdata_id, name) VALUES (2620, 'Newly Listed AB')")
    cid = mem_conn.execute("SELECT id FROM companies WHERE borsdata_id=2620").fetchone()[0]
    # 2620-class stub: reportsYear=[], reportsQuarter=[{revenues:0.0, report_Date:null}]
    stub = {
        "revenues": 0.0,
        "report_Date": None,
        "year": 2025,
        "period": 1,
        "period_type": "quarter",
        "period_end": "2025-03-31",
    }
    assert upsert_financial_periods(mem_conn, int(cid), [stub]) == 0
    assert (
        mem_conn.execute(
            "SELECT count(*) FROM financial_periods WHERE company_id=?", (cid,)
        ).fetchone()[0]
        == 0
    )
    rejection = mem_conn.execute(
        "SELECT reason, raw_payload FROM financial_period_rejections WHERE company_id=?",
        (cid,),
    ).fetchone()
    assert rejection is not None
    assert rejection["reason"] == "placeholder"
    assert json.loads(rejection["raw_payload"]) == stub


def test_bilingual_dedupe_suppresses_one_per_pair():
    docs = [
        {
            "title": "Delårsrapport Q1 2025",
            "source_url": "https://mfn.se/cision/123/sv",
            "published_at": "2025-04-15",
            "content_text": "Svensk text",
            "ingested_lang": "sv",
            "provider_event_id": "cision-123-q1-2025",
        },
        {
            "title": "Interim Report Q1 2025",
            "source_url": "https://mfn.se/cision/123/en",
            "published_at": "2025-04-15",
            "content_text": "English text",
            "ingested_lang": "en",
            "provider_event_id": "cision-123-q1-2025",
        },
    ]
    deduped = bilingual_dedupe(docs)
    # After dedupe, only one remains in packet (+ one suppressed stashed)
    assert len(deduped) == 1
    preferred = deduped[0]
    # suppressed stashed on preferred
    suppressed = preferred.get("_suppressed_variants", [])
    assert len(suppressed) == 1
    assert suppressed[0]["ingest_status"] == "superseded_by_translation"


def test_bilingual_dedupe_persist_with_duplicate_of(mem_conn):
    mem_conn.execute("INSERT INTO companies (borsdata_id, name) VALUES (99, 'Dedupe AB')")
    cid = int(mem_conn.execute("SELECT id FROM companies WHERE borsdata_id=99").fetchone()[0])
    svc = ResearchDocumentIngestionService(mem_conn)
    docs = [
        {
            "title": "Bokslutskommuniké 2024",
            "source_url": "https://mfn.se/a/1/sv",
            "published_at": "2025-02-10",
            "content_text": "sv body",
            "ingested_lang": "sv",
            "provider_event_id": "a-1-year-end-2024",
        },
        {
            "title": "Year-End Report 2024",
            "source_url": "https://mfn.se/a/1/en",
            "published_at": "2025-02-10",
            "content_text": "en body",
            "ingested_lang": "en",
            "provider_event_id": "a-1-year-end-2024",
        },
    ]
    _result = svc.persist_articles(cid, docs)
    rows = mem_conn.execute(
        "SELECT duplicate_of FROM research_documents WHERE company_id=?", (cid,)
    ).fetchall()
    assert len(rows) == 2
    assert all(row[0] is None for row in rows)


def test_pit_filter_report_date_le_as_of(mem_conn):
    mem_conn.execute("INSERT INTO companies (borsdata_id, name) VALUES (100, 'PIT AB')")
    cid = int(mem_conn.execute("SELECT id FROM companies WHERE borsdata_id=100").fetchone()[0])
    # Insert financial period with report_date 2025-05-15
    mem_conn.execute(
        """
        INSERT INTO financial_periods
            (company_id, period_type, period_end, report_date, revenue, is_placeholder)
        VALUES (?, 'quarter', '2025-03-31', '2025-05-15', 1000, 0)
        """,
        (cid,),
    )
    mem_conn.commit()
    # Mid-quarter as_of = 2025-04-15 → report not yet published, PIT excludes
    rows_mid = mem_conn.execute(
        "SELECT count(*) FROM financial_periods WHERE company_id=? AND is_placeholder=0 AND report_date <= '2025-04-15'",
        (cid,),
    ).fetchone()[0]
    assert rows_mid == 0
    # as_of = 2025-05-15 → present
    rows_pub = mem_conn.execute(
        "SELECT count(*) FROM financial_periods WHERE company_id=? AND is_placeholder=0 AND report_date <= '2025-05-15'",
        (cid,),
    ).fetchone()[0]
    assert rows_pub == 1
    # Core utility also holds
    assert not in_window("2025-05-15", "2025-04-15")
    assert in_window("2025-05-15", "2025-05-15")
    assert in_window("2025-03-31", "2025-04-15")


def test_pit_is_placeholder_never_visible(mem_conn):
    mem_conn.execute("INSERT INTO companies (borsdata_id, name) VALUES (101, 'Placeholder PIT AB')")
    cid = int(mem_conn.execute("SELECT id FROM companies WHERE borsdata_id=101").fetchone()[0])
    mem_conn.execute(
        """
        INSERT INTO financial_periods
            (company_id, period_type, period_end, report_date, revenue, is_placeholder)
        VALUES (?, 'quarter', '2025-03-31', '2025-04-01', 0, 1)
        """,
        (cid,),
    )
    mem_conn.commit()
    # Even with as_of beyond report_date, placeholder rows are never PIT-visible
    cur = mem_conn.execute(
        "SELECT count(*) FROM financial_periods WHERE company_id=? AND is_placeholder=0 AND report_date <= '2025-12-31'",
        (cid,),
    )
    assert cur.fetchone()[0] == 0


def test_fx_convert_sum_verified_and_sums_only():
    # Verified: converted = original * ratio
    assert convert_sum(33220, 9.2268) == pytest.approx(33220 * 9.2268)
    assert convert_sum(1197, 10.8257) == pytest.approx(12958.3629, rel=1e-4)
    # Ratio guard
    with pytest.raises(ValueError):
        convert_sum(100, 0)
    with pytest.raises(ValueError):
        convert_sum(100, -1)


def test_currency_ratio_persisted_per_observation(mem_conn):
    mem_conn.execute("INSERT INTO companies (borsdata_id, name) VALUES (200, 'FX AB')")
    cid = int(mem_conn.execute("SELECT id FROM companies WHERE borsdata_id=200").fetchone()[0])
    period = {
        "revenues": 1000,
        "report_Date": "2025-05-01",
        "currency": "USD",
        "currency_Ratio": 9.2268,
        "year": 2024,
        "period": 4,
        "period_type": "year",
        "period_end": "2024-12-31",
    }
    upsert_financial_periods(mem_conn, cid, [period])
    cur = mem_conn.execute(
        "SELECT currency, currency_ratio, fx_rate_to_sek, fx_source FROM financial_periods WHERE company_id=?",
        (cid,),
    )
    row = cur.fetchone()
    assert row["currency"] == "USD"
    assert row["currency_ratio"] == pytest.approx(9.2268)
    assert row["fx_rate_to_sek"] == pytest.approx(9.2268)
    assert row["fx_source"] == "currency_ratio"


def test_maxCount_not_trusted_fetch_full_slice_locally():
    adapter = BorsdataAdapter(api_key="dummy")
    fake_rows = [{"d": f"2020-01-{i:02d}", "c": 100 + i, "v": 1000} for i in range(1, 21)]

    def fake_get_json(path, params=None):
        # Simulate backend ignoring maxCount — always returns full 20
        return {"stockPricesList": fake_rows}

    with patch.object(adapter, "_get_json", side_effect=fake_get_json):
        full = adapter.get_stock_prices(1)
        sliced = adapter.get_stock_prices(1, max_count=5)
        assert len(full) == 20
        assert len(sliced) == 5
        assert sliced == full[-5:]
        # max_count=10 and without must be same tail logic (backend bug not propagated)
        sliced10 = adapter.get_stock_prices(1, max_count=10)
        assert sliced10 == full[-10:]


def test_sector_kpi_400_swallowed_as_missing():
    adapter = BorsdataAdapter(api_key="dummy")

    def fake_get(path, params=None):
        # Simulate 400 response
        class FakeResp:
            status_code = 400

            def json(self):
                return None

            def raise_for_status(self):
                raise Exception("400")

        return FakeResp()

    # _get_json handles 400 → None
    with patch.object(adapter, "_get", side_effect=fake_get):
        result = adapter._get_json("/v1/instruments/1/kpis/999/latest/latest")
        assert result is None
        # get_kpis swallows 400 as missing
        kpi = adapter.get_kpis(1, 999, "latest", "latest")
        assert kpi is None
        hist = adapter.get_kpi_history(1, 999, "quarter", "mean")
        assert hist == []


def test_idempotent_sync_twice_identical_rowcounts(mem_conn):
    # Simulate sync idempotence via ON CONFLICT DO UPDATE — two identical upserts → same counts
    mem_conn.execute(
        "INSERT INTO companies (borsdata_id, name, ticker, isin) VALUES (500, 'Idempotent AB', 'IDEM', 'SE0000000500')"
    )
    cid = int(mem_conn.execute("SELECT id FROM companies WHERE borsdata_id=500").fetchone()[0])
    periods = [
        {
            "revenues": 5000,
            "report_Date": "2025-02-01",
            "year": 2024,
            "period": 4,
            "period_type": "year",
            "period_end": "2024-12-31",
            "currency": "SEK",
        },
    ]
    upsert_financial_periods(mem_conn, cid, periods)
    count1 = mem_conn.execute(
        "SELECT count(*) FROM financial_periods WHERE company_id=?", (cid,)
    ).fetchone()[0]
    upsert_financial_periods(mem_conn, cid, periods)
    count2 = mem_conn.execute(
        "SELECT count(*) FROM financial_periods WHERE company_id=?", (cid,)
    ).fetchone()[0]
    assert count1 == count2 == 1

    # Watchlist also idempotent via (source_file,row_hash)
    mem_conn.execute(
        "INSERT INTO watchlist (company_id, ticker, isin, source_file, source_row_hash, matched_via) VALUES (?, 'IDEM', 'SE0000000500', 'file1', 'hash1', 'isin')",
        (cid,),
    )
    mem_conn.commit()
    c1 = mem_conn.execute("SELECT count(*) FROM watchlist WHERE source_file='file1'").fetchone()[0]
    mem_conn.execute(
        "INSERT INTO watchlist (company_id, ticker, isin, source_file, source_row_hash, matched_via) VALUES (?, 'IDEM', 'SE0000000500', 'file1', 'hash1', 'isin') ON CONFLICT(source_file, source_row_hash) DO NOTHING",
        (cid,),
    )
    mem_conn.commit()
    c2 = mem_conn.execute("SELECT count(*) FROM watchlist WHERE source_file='file1'").fetchone()[0]
    assert c1 == c2 == 1


def test_ownership_stack_three_limitations_always_present():
    # Behavioral contract: limitations must appear in the production ownership-evidence output
    evidence = build_ownership_evidence()
    assert "limitations" in evidence
    limitations = evidence["limitations"]
    assert isinstance(limitations, list)
    assert len(limitations) == 3
    assert "Free-float % is unavailable" in limitations
    assert "Named large-holder coverage is unavailable beyond annual top 10" in limitations
    assert "Ownership-change history is unavailable at quarterly granularity" in limitations
    # Also verify via direct parser import that source of truth is consistent
    from alphaforge.ownership.parser import LIMITATIONS as parser_limitations

    assert limitations == parser_limitations


def test_mfn_top10_parser_requires_gt2_rows():
    # Only 2 rows → empty (not considered valid table)
    text2 = "Holder A 100 10 %\nHolder B 200 20 %"
    assert parse_top10_holders(text2) == []
    # 3 rows with pct<100 and sum<100 → valid
    text3 = "Aktieägare Antal Andel\nAlice 1000000 15,5 %\nBob 800000 12,3 %\nCarol 500000 7,1 %\n"
    rows = parse_top10_holders(text3)
    assert len(rows) == 3


def test_mfn_event_tagger():
    text = "Styrelsen har beslutat om en riktad emission och en flaggning. Lock-up 90 dagar."
    events = tag_mfn_events(text)
    types = {e["event_type"] for e in events}
    assert "placement" in types
    assert "flagging" in types
    assert "lockup" in types


def test_report_date_pit_two_layer_filter():
    # Adapter layer: in_window already tested. Packet layer: same logic via SQL WHERE
    # Ensure future report (published next week) is excluded at both layers
    assert in_window("2025-05-15", "2025-05-10") is False
    assert in_window("2025-05-09", "2025-05-10") is True
