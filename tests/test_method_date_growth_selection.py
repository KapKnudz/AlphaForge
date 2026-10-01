"""Repository -> frozen-input loader -> real rank exports, no live acquisition."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from dataclasses import asdict
from datetime import date, timedelta

import pytest

from alphaforge.cli.main import cmd_rank
from alphaforge.cli.ranking_loader import _annual_series, load_results_for_company
from alphaforge.config import Settings
from alphaforge.core.frozen_packet import (
    EVIDENCE_RULES_VERSION,
    stable_packet_hash,
    validate_frozen_packet,
)
from alphaforge.core.gate.readiness import AgentReadinessGate
from alphaforge.core.ranking.engine import RankingEngine
from alphaforge.db.connection import get_connection
from alphaforge.db.migrations import migrate
from alphaforge.db.repositories import (
    persist_evidence_packet,
    upsert_company,
    upsert_financial_periods,
    upsert_kpi_observations,
    upsert_prices,
    upsert_stock_splits,
)
from alphaforge.evidence.report_rules import report_rules_metadata

CUTOFF = "2026-06-01"


def annual(year, revenue=100, **overrides):
    return {
        "period_type": "year",
        "period_end": f"{year}-03-31",
        "report_Date": f"{year}-05-01",
        "year": year,
        "period": 5,
        "broken_Fiscal_Year": True,  # Non-calendar fiscal year, not a stub flag.
        "revenues": revenue,
        "operating_Income": revenue * 0.2,
        "profit_To_Equity_Holders": revenue * 0.1,
        "free_Cash_Flow": revenue * 0.1,
        "total_Equity": revenue * 0.4,
        "net_Debt": 5,
        "number_Of_Shares": 10,
        "currency": "SEK",
        **overrides,
    }


def setup(branch=None, periods=None, price_date=CUTOFF):
    conn = get_connection(Settings.from_env(dsn="sqlite:///:memory:"))
    migrate(conn)
    cid = upsert_company(
        conn,
        {
            "insId": 991,
            "name": "Synthetic AB",
            "ticker": "FIX",
            "stockPriceCurrency": "SEK",
            "reportCurrency": "SEK",
            "branchId": branch,
        },
    )
    conn.execute(
        "INSERT INTO watchlist(company_id,ticker,source_file,source_row_hash) VALUES (?,?,?,?)",
        (cid, "FIX", "fixture", "fixed"),
    )
    upsert_financial_periods(conn, cid, periods if periods is not None else [annual(2026)])
    if price_date:
        upsert_prices(conn, cid, [{"d": price_date, "c": 10}], currency="SEK")
    conn.commit()
    return conn, cid


def packet(conn, cid, cutoff=CUTOFF):
    body = {
        "schema_version": "evidence-packet-v1",
        "frozen": True,
        "evidence_rules_version": EVIDENCE_RULES_VERSION,
        "report_rules": report_rules_metadata(),
        "company_id": cid,
        "as_of": cutoff,
        "issuer": {"mfn_slug": "all/a/fix", "verified_at": cutoff + "T00:00:00Z"},
        "sources": [
            {
                "source_id": "document:1",
                "source_url": "https://mfn.test/fix",
                "title": "Synthetic annual",
                "publication_date": "2026-05-01T00:00:00Z",
                "publication_timestamp_authoritative": True,
                "ingestion_date": cutoff + "T00:00:00Z",
                "attachment": {"source_url": "https://storage.test/fix.pdf", "sha256": "a" * 64},
                "extraction": {
                    "extractor": "fixture",
                    "page_count": 1,
                    "text_checksum": hashlib.sha256(b"[page 1]\nEvidence").hexdigest(),
                },
                "pages": [
                    {
                        "page_number": 1,
                        "anchor": "document:1#page:1",
                        "text": "Evidence",
                        "text_checksum": hashlib.sha256(b"Evidence").hexdigest(),
                    }
                ],
            }
        ],
        "evidence_catalog": {"canonical_source_ids": ["document:1"]},
        "limitations": [],
    }
    body["packet_hash"] = stable_packet_hash(body)
    assert validate_frozen_packet(body)
    persist_evidence_packet(conn, company_id=cid, as_of=cutoff, packet=body)
    return body


def rank_exports(conn, monkeypatch, tmp_path, cutoff=CUTOFF):
    monkeypatch.setattr("alphaforge.db.connection.get_connection", lambda settings: conn)
    monkeypatch.chdir(tmp_path)
    assert cmd_rank(argparse.Namespace(dsn="sqlite:///:memory:", as_of=cutoff, watchlist=None)) == 0
    output = tmp_path / "exports" / cutoff
    data = json.loads((output / "ranking.json").read_text())
    with (output / "ranking.csv").open() as stream:
        rows = list(csv.DictReader(stream))
    dcf = json.loads((output / "dcf.json").read_text())
    return data["scores"][0], rows[0], dcf


@pytest.mark.parametrize(
    "branch,model", [(None, "general"), (75, "property"), (68, "bank"), (69, "bank"), (70, "bank")]
)
@pytest.mark.parametrize("has_financials", [False, True])
def test_actual_rank_method_priority(branch, model, has_financials, monkeypatch, tmp_path):
    conn, cid = setup(branch, periods=None if has_financials else [])
    if has_financials:
        packet(conn, cid)
    loaded = load_results_for_company(conn, cid, CUTOFF)
    assert loaded["candidate"].ranking_model == model
    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    expected = "ready" if has_financials else "evidence_blocked"
    if model != "general":
        expected = "method_unsupported"
        assert "forward_method_unsupported" in row["readiness_blockers"]
        assert dcf[str(cid)]["status"] == "unavailable"  # Preserve FCFF sector refusal.
        loaded["candidate"].full_results["reverse_dcf"] = {"status": "available"}
        assert AgentReadinessGate().assess(loaded["candidate"]).status == expected
    assert score["ranking_model"] == row["ranking_model"] == model
    assert score["readiness_status"] == row["readiness_status"] == expected


@pytest.mark.parametrize("age,available", [(0, True), (7, True), (8, False), (334, False)])
def test_current_price_age_boundary_exports(age, available, monkeypatch, tmp_path):
    price_date = (date.fromisoformat(CUTOFF) - timedelta(days=age)).isoformat()
    conn, cid = setup(price_date=price_date)
    packet(conn, cid)
    loaded = load_results_for_company(conn, cid, CUTOFF)
    assert (loaded["valuation"].raw_market_cap is not None) == available
    if not available:
        price_selection = loaded["selection"]["price"]
        assert price_selection["value"] == 10
        assert price_selection["date_facts"] == {"d": price_date}
        assert price_selection["raw_payload"] == {"d": price_date, "c": 10}
    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    assert (
        score["readiness_status"]
        == row["readiness_status"]
        == ("ready" if available else "valuation_blocked")
    )
    assert (dcf[str(cid)]["status"] == "available") == available
    if not available:
        assert not score["rank_eligible"]
        assert "stock_price_stale" in row["readiness_blockers"]
        assert dcf[str(cid)]["current_price"] is None
        assert score["input_selection"]["price"]["raw_payload"] == {
            "d": price_date,
            "c": 10,
        }
        assert json.loads(row["input_selection"]) == score["input_selection"]
        assert dcf[str(cid)]["selection"] == score["input_selection"]


@pytest.mark.parametrize("report_period", [None, 5])
def test_kpi_replacement_does_not_inherit_date_authority(report_period):
    conn, cid = setup()

    def observation(value, observed=True):
        row = {"y": 2026, "v": value}
        if report_period is not None:
            row["p"] = report_period
        if observed:
            row["observationDate"] = CUTOFF
        return row

    upsert_kpi_observations(conn, cid, 37, "year", "mean", [observation(30)])
    assert load_results_for_company(conn, cid, CUTOFF)["fundamental_kpis"][37] == 30

    upsert_kpi_observations(conn, cid, 37, "year", "mean", [observation(40, observed=False)])
    stored = conn.execute(
        "SELECT value, observation_date FROM kpi_observations WHERE kpi_id=37"
    ).fetchone()
    assert tuple(stored) == (30, CUTOFF)
    result = load_results_for_company(conn, cid, CUTOFF)
    assert result["fundamental_kpis"][37] == 30
    rejected = result["selection"]["rejected_kpis"][0]
    assert rejected["reason"] == "KPI observation date unavailable"
    assert rejected["raw_payload"] == observation(40, observed=False)
    assert not rejected["current_refusal"]

    upsert_kpi_observations(conn, cid, 37, "year", "mean", [observation(50)])
    assert load_results_for_company(conn, cid, CUTOFF)["fundamental_kpis"][37] == 50


@pytest.mark.parametrize(
    "period_type,observed,expected_count,stored_date",
    [
        ("year", "2026-06-01T12:00:00Z", 1, CUTOFF),
        ("year", "2026-06-01garbage", 1, None),
        ("last", "2026-06-01T12:00:00+02:00", 1, CUTOFF),
        ("last", "2026-06-01garbage", 1, None),
    ],
)
def test_kpi_ingestion_parses_complete_iso_dates(
    period_type, observed, expected_count, stored_date
):
    conn, cid = setup()
    row = {"v": 30, "observationDate": observed}
    if period_type == "year":
        row.update({"y": 2026, "p": 5})

    assert upsert_kpi_observations(conn, cid, 37, period_type, "mean", [row]) == expected_count
    stored = conn.execute(
        "SELECT observation_date FROM kpi_observations WHERE kpi_id=37"
    ).fetchone()
    assert (stored["observation_date"] if stored else None) == stored_date
    loaded = load_results_for_company(conn, cid, CUTOFF)
    if stored_date is None:
        rejected = loaded["selection"]["rejected_kpis"][0]
        assert rejected["raw_payload"] == row
        assert rejected["value"] == 30
        assert rejected["date_facts"] == {"observationDate": observed}
    if period_type == "year":
        assert (37 in loaded["fundamental_kpis"]) is (stored_date is not None)


@pytest.mark.parametrize("reverse", [False, True])
def test_price_and_kpi_date_alias_conflicts_retain_original_evidence(
    reverse, monkeypatch, tmp_path
):
    conn, cid = setup()
    price = {
        "price_Date": CUTOFF,
        "date": "2026-06-10",
        "c": 11,
    }
    kpi = {
        "observationDate": CUTOFF,
        "date": "2026-06-10",
        "y": 2026,
        "p": 5,
        "v": 31,
    }
    if reverse:
        price = dict(reversed(tuple(price.items())))
        kpi = dict(reversed(tuple(kpi.items())))

    assert upsert_prices(conn, cid, [price], currency="SEK") == 0
    assert upsert_kpi_observations(conn, cid, 37, "year", "mean", [kpi]) == 1
    assert upsert_prices(conn, cid, [price], currency="SEK") == 0
    assert conn.execute("SELECT count(*) FROM market_input_rejections").fetchone()[0] == 2

    loaded = load_results_for_company(conn, cid, CUTOFF)
    rejected_price = loaded["selection"]["rejected_prices"][0]
    rejected_kpi = loaded["selection"]["rejected_kpis"][0]
    assert rejected_price["raw_payload"] == price
    assert rejected_price["value"] == 11
    assert rejected_price["date_facts"] == {
        "price_Date": CUTOFF,
        "date": "2026-06-10",
    }
    assert rejected_price["current_refusal"]
    assert rejected_kpi["raw_payload"] == kpi
    assert rejected_kpi["value"] == 31
    assert rejected_kpi["current_refusal"]
    assert loaded["reverse_dcf"]["current_price"] is None

    packet(conn, cid)
    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    assert score["readiness_status"] == "valuation_blocked"
    assert any("aliases conflict" in item for item in score["missing_data"])
    assert json.loads(row["input_selection"]) == score["input_selection"]
    assert dcf[str(cid)]["selection"] == score["input_selection"]


def test_agreeing_price_and_kpi_date_aliases_accept_full_iso_values():
    conn, cid = setup(price_date=None)
    assert (
        upsert_prices(
            conn,
            cid,
            [{"price_Date": CUTOFF, "date": CUTOFF + "T23:59:59Z", "c": 10}],
            currency="SEK",
        )
        == 1
    )
    assert (
        upsert_kpi_observations(
            conn,
            cid,
            37,
            "year",
            "mean",
            [
                {
                    "observationDate": CUTOFF,
                    "date": CUTOFF + "T12:00:00+02:00",
                    "y": 2026,
                    "p": 5,
                    "v": 30,
                }
            ],
        )
        == 1
    )
    loaded = load_results_for_company(conn, cid, CUTOFF)
    assert loaded["selection"]["price"]["selected_date"] == CUTOFF
    assert loaded["fundamental_kpis"][37] == 30
    assert not loaded["selection"]["rejected_prices"]
    assert not loaded["selection"]["rejected_kpis"]


def test_migrated_rawless_market_rows_require_verified_reacquisition(monkeypatch, tmp_path):
    conn, cid = setup(price_date=None)
    conn.execute(
        """INSERT INTO prices(company_id, price_date, close, volume, currency)
           VALUES (?, ?, ?, ?, ?)""",
        (cid, CUTOFF, 10, 100, "SEK"),
    )
    conn.execute(
        """INSERT INTO kpi_observations
           (company_id, kpi_id, period_type, price_type, observation_date,
            year, report_period, value)
           VALUES (?, ?, 'year', 'mean', ?, 2026, 5, 30)""",
        (cid, 37, CUTOFF),
    )
    legacy_price = tuple(
        conn.execute(
            "SELECT company_id, price_date, close, volume, currency FROM prices"
        ).fetchone()
    )
    legacy_kpi = tuple(
        conn.execute(
            """SELECT company_id, kpi_id, period_type, price_type,
                      observation_date, year, report_period, value
               FROM kpi_observations"""
        ).fetchone()
    )
    conn.execute("DROP TABLE market_input_rejections")
    conn.execute("ALTER TABLE prices DROP COLUMN raw_payload")
    conn.execute("ALTER TABLE kpi_observations DROP COLUMN raw_payload")
    conn.execute("PRAGMA user_version=12")
    conn.commit()

    migrate(conn)
    assert (
        tuple(
            conn.execute(
                "SELECT company_id, price_date, close, volume, currency FROM prices"
            ).fetchone()
        )
        == legacy_price
    )
    assert (
        tuple(
            conn.execute(
                """SELECT company_id, kpi_id, period_type, price_type,
                      observation_date, year, report_period, value
               FROM kpi_observations"""
            ).fetchone()
        )
        == legacy_kpi
    )
    assert conn.execute("SELECT raw_payload FROM prices").fetchone()[0] is None
    assert conn.execute("SELECT raw_payload FROM kpi_observations").fetchone()[0] is None

    packet(conn, cid)
    loaded = load_results_for_company(conn, cid, CUTOFF)
    legacy_price_diagnostic = loaded["selection"]["rejected_prices"][0]
    legacy_kpi_diagnostic = loaded["selection"]["rejected_kpis"][0]
    assert legacy_price_diagnostic["provenance"] == "legacy_missing_raw_payload"
    assert legacy_price_diagnostic["raw_payload"] == {}
    assert legacy_price_diagnostic["value"] == 10
    assert legacy_price_diagnostic["price_date"] == CUTOFF
    assert legacy_kpi_diagnostic["provenance"] == "legacy_missing_raw_payload"
    assert legacy_kpi_diagnostic["raw_payload"] == {}
    assert legacy_kpi_diagnostic["value"] == 30
    assert legacy_kpi_diagnostic["observation_date"] == CUTOFF
    assert loaded["reverse_dcf"]["current_price"] is None
    assert 37 not in loaded["fundamental_kpis"]

    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    assert any("legacy stock price" in item for item in score["missing_data"])
    assert any("legacy KPI" in item for item in score["missing_data"])
    assert json.loads(row["input_selection"]) == score["input_selection"]
    assert dcf[str(cid)]["selection"] == score["input_selection"]

    price = {"price_Date": CUTOFF, "date": CUTOFF + "T23:59:59Z", "c": 10, "v": 100}
    kpi = {
        "observationDate": CUTOFF,
        "date": CUTOFF + "T12:00:00Z",
        "y": 2026,
        "p": 5,
        "v": 30,
    }
    assert upsert_prices(conn, cid, [price, price], currency="SEK") == 2
    assert upsert_kpi_observations(conn, cid, 37, "year", "mean", [kpi, kpi]) == 2
    assert conn.execute("SELECT count(*) FROM prices").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM kpi_observations").fetchone()[0] == 1

    corrected = load_results_for_company(conn, cid, CUTOFF)
    assert corrected["reverse_dcf"]["current_price"] == 10
    assert corrected["fundamental_kpis"][37] == 30
    assert not corrected["selection"]["rejected_prices"]
    assert not corrected["selection"]["rejected_kpis"]
    corrected_score, corrected_row, corrected_dcf = rank_exports(conn, monkeypatch, tmp_path)
    assert not any("legacy" in item for item in corrected_score["missing_data"])
    assert json.loads(corrected_row["input_selection"]) == corrected_score["input_selection"]
    assert corrected_dcf[str(cid)]["selection"] == corrected_score["input_selection"]


def test_rejected_market_inputs_survive_early_missing_financial_exports(monkeypatch, tmp_path):
    conn, cid = setup(periods=[], price_date=None)
    price = {"c": 10}
    kpi = {"y": 2026, "p": 5, "v": 30}
    assert upsert_prices(conn, cid, [price], currency="SEK") == 0
    assert upsert_kpi_observations(conn, cid, 37, "year", "mean", [kpi]) == 1
    packet(conn, cid)

    loaded = load_results_for_company(conn, cid, CUTOFF)
    assert loaded["selection"]["rejected_prices"][0]["raw_payload"] == price
    assert loaded["selection"]["rejected_kpis"][0]["raw_payload"] == kpi
    assert loaded["reverse_dcf"]["selection"] == loaded["selection"]

    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    assert score["input_selection"]["rejected_prices"][0]["value"] == 10
    assert score["input_selection"]["rejected_kpis"][0]["value"] == 30
    assert json.loads(row["input_selection"]) == score["input_selection"]
    assert dcf[str(cid)]["selection"] == score["input_selection"]


def test_future_and_historical_price_rejections_remain_audit_only():
    conn, cid = setup()
    future = {"d": "2026-06-10", "c": 12}
    older_invalid = {"d": "2025-06-01", "c": "invalid"}
    corrected_invalid = {"d": CUTOFF, "c": "invalid"}
    assert upsert_prices(conn, cid, [future], currency="SEK") == 1
    assert upsert_prices(conn, cid, [older_invalid, corrected_invalid], currency="SEK") == 0

    loaded = load_results_for_company(conn, cid, CUTOFF)
    assert loaded["reverse_dcf"]["current_price"] == 10
    by_raw = {
        json.dumps(item["raw_payload"], sort_keys=True): item
        for item in loaded["selection"]["rejected_prices"]
    }
    assert not by_raw[json.dumps(future, sort_keys=True)]["current_refusal"]
    assert not by_raw[json.dumps(older_invalid, sort_keys=True)]["current_refusal"]
    assert not by_raw[json.dumps(corrected_invalid, sort_keys=True)]["current_refusal"]
    assert not any("price" in reason for reason in loaded["selection"]["refusal_reasons"])


@pytest.mark.parametrize("rejection_first", [False, True])
def test_conflicting_older_price_dates_are_audit_only(
    rejection_first, monkeypatch, tmp_path
):
    conn, cid = setup(price_date=None)
    rejected = {
        "price_Date": "2025-01-01",
        "date": "2025-01-02",
        "c": 9,
    }
    fresh = {"d": CUTOFF, "c": 10}
    batches = ([rejected], [fresh]) if rejection_first else ([fresh], [rejected])
    for rows in batches:
        upsert_prices(conn, cid, rows, currency="SEK")
    assert upsert_prices(conn, cid, [rejected], currency="SEK") == 0
    assert (
        conn.execute(
            "SELECT count(*) FROM market_input_rejections WHERE input_type='price'"
        ).fetchone()[0]
        == 1
    )

    loaded = load_results_for_company(conn, cid, CUTOFF)
    retained = loaded["selection"]["rejected_prices"][0]
    assert retained["raw_payload"] == rejected
    assert retained["date_facts"] == {
        "price_Date": "2025-01-01",
        "date": "2025-01-02",
    }
    assert not retained["current_refusal"]
    assert loaded["reverse_dcf"]["current_price"] == 10
    assert not any("aliases conflict" in item for item in loaded["selection"]["refusal_reasons"])

    packet(conn, cid)
    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    assert score["input_selection"]["rejected_prices"][0] == retained
    assert json.loads(row["input_selection"]) == score["input_selection"]
    assert dcf[str(cid)]["selection"] == score["input_selection"]


def test_malformed_kpi_replacement_preserves_prior_date_authority():
    conn, cid = setup()
    upsert_kpi_observations(
        conn,
        cid,
        37,
        "year",
        "mean",
        [{"y": 2026, "p": 5, "v": 30, "observationDate": CUTOFF}],
    )
    upsert_kpi_observations(
        conn,
        cid,
        37,
        "year",
        "mean",
        [{"y": 2026, "p": 5, "v": 40, "observationDate": "2026-06-01garbage"}],
    )

    stored = conn.execute(
        "SELECT value, observation_date FROM kpi_observations WHERE kpi_id=37"
    ).fetchone()
    assert tuple(stored) == (30, CUTOFF)
    loaded = load_results_for_company(conn, cid, CUTOFF)
    assert loaded["fundamental_kpis"][37] == 30
    rejected = loaded["selection"]["rejected_kpis"][0]
    assert rejected["reason"] == "KPI observation date invalid"
    assert rejected["raw_payload"]["v"] == 40
    assert not rejected["current_refusal"]


@pytest.mark.parametrize("order", ["forward", "reverse", "rotated"])
def test_annual_growth_invariant_with_quarter_r12_insertions(order, monkeypatch, tmp_path):
    annuals = [annual(year, 100 * 1.1 ** (year - 2023)) for year in range(2023, 2027)]
    conn, cid = setup(periods=annuals)
    packet(conn, cid)
    original = load_results_for_company(conn, cid, CUTOFF)
    original_score, _, _ = rank_exports(conn, monkeypatch, tmp_path)
    additions = [
        annual(
            2025,
            30.25,
            period_type="quarter",
            period_end="2025-06-30",
            report_Date="2025-08-01",
            period=2,
        ),
        annual(
            2025,
            121,
            period_type="r12",
            period_end="2025-06-30",
            report_Date="2025-08-01",
            period=2,
        ),
    ]
    all_rows = annuals + additions
    if order == "reverse":
        all_rows.reverse()
    elif order == "rotated":
        all_rows = all_rows[3:] + all_rows[:3]
    upsert_financial_periods(conn, cid, all_rows)
    result = load_results_for_company(conn, cid, CUTOFF)
    assert result["financial"].revenue_growth == pytest.approx(0.1)
    assert result["financial"].revenue_growth_years == 3
    assert result["financial"].revenue_per_share_growth == pytest.approx(0.1)
    assert asdict(result["financial"]) == asdict(original["financial"])
    score, row, _ = rank_exports(conn, monkeypatch, tmp_path)
    assert score["growth_score"] == original_score["growth_score"]
    assert row["growth_score"] == str(score["growth_score"])


def test_provider_declared_annual_period_four_is_comparable():
    rows = [annual(year, 100 * 1.1 ** (year - 2023), period=4) for year in range(2023, 2027)]
    conn, cid = setup(periods=rows)
    result = load_results_for_company(conn, cid, CUTOFF)
    assert result["financial"].revenue_growth == pytest.approx(0.1)
    assert result["financial"].revenue_growth_years == 3
    assert result["selection"]["annual_history"]["reasons"] == []


@pytest.mark.parametrize(
    "defect",
    [
        "gap",
        "duplicate",
        "stub",
        "unknown_year",
        "currency",
        "unknown_end",
        "start_stub",
        "invalid_start",
    ],
)
def test_incomparable_latest_annual_history_is_unavailable(defect):
    rows = [annual(year, 100 * 1.1 ** (year - 2023)) for year in range(2023, 2027)]
    if defect == "gap":
        rows.pop(-2)
    elif defect == "duplicate":
        rows.append(annual(2026, period_end="2026-04-30"))
    elif defect == "stub":
        rows[-1]["period_end"] = "2026-04-30"
    elif defect == "unknown_year":
        rows[-1].pop("year")
    elif defect == "currency":
        rows[-1]["currency"] = "USD"
    elif defect == "start_stub":
        rows[-1]["period_start"] = "2025-10-01"
    elif defect == "invalid_start":
        rows[-1]["period_start"] = "2026"
    conn, cid = setup(periods=rows)
    if defect == "unknown_end":
        conn.execute(
            "UPDATE financial_periods SET raw_payload=? WHERE report_year=2026",
            (json.dumps({"year": 2026, "report_Date": "2026-03-31"}),),
        )
    result = load_results_for_company(conn, cid, CUTOFF)
    fin = result["financial"]
    assert fin.revenue_growth is None
    assert fin.revenue_per_share_growth is None
    assert fin.share_count_growth is None
    assert fin.positive_fcf_ratio is None
    assert fin.revenue_growth_years == fin.per_share_growth_years == 0
    selection = result["selection"]
    assert (
        selection["annual_history"]["reasons"]
        or selection["annual_history"]["excluded"]
        or selection["rejected_reports"]
    )


@pytest.mark.parametrize(
    "defect", ["gap", "duplicate", "stub", "unknown_year", "currency", "unknown_end"]
)
def test_older_annual_defect_preserves_latest_comparable_suffix(defect):
    older = [annual(2019, 80), annual(2020, 88)]
    if defect == "duplicate":
        older.append(annual(2020, 88, period_end="2020-04-30"))
    elif defect == "stub":
        older[-1]["period_end"] = "2020-09-30"
    elif defect == "unknown_year":
        older[-1].pop("year")
    elif defect == "currency":
        older[-1]["currency"] = "USD"
    recent = [annual(year, 100 * 1.1 ** (year - 2023)) for year in range(2023, 2027)]
    conn, cid = setup(periods=[*older, *recent])
    if defect == "unknown_end":
        conn.execute(
            "UPDATE financial_periods SET raw_payload=? WHERE report_year=2020",
            (json.dumps({"year": 2020, "report_Date": "2020-03-31"}),),
        )
    result = load_results_for_company(conn, cid, CUTOFF)
    assert result["financial"].revenue_growth == pytest.approx(0.1)
    assert result["financial"].revenue_growth_years == 3
    selection = result["selection"]
    assert selection["annual_history"]["period_ends"] == [
        "2023-03-31",
        "2024-03-31",
        "2025-03-31",
        "2026-03-31",
    ]
    assert selection["annual_history"]["excluded"] or selection["rejected_reports"]


@pytest.mark.parametrize("reverse_insertion", [False, True])
def test_old_mislabeled_year_does_not_poison_or_archive_latest_suffix(reverse_insertion):
    old = annual(
        2026,
        70,
        period_end="2019-03-31",
        report_Date="2020-05-01",
    )
    recent = [annual(year, 100 * 1.1 ** (year - 2023)) for year in range(2023, 2027)]
    rows = [old, *recent]
    if reverse_insertion:
        rows.reverse()
    conn, cid = setup(periods=rows)

    restated = annual(2026, recent[-1]["revenues"] + 1)
    assert upsert_financial_periods(conn, cid, [restated, restated]) == 2
    assert conn.execute("SELECT count(*) FROM financial_period_rejections").fetchone()[0] == 0

    loaded = load_results_for_company(conn, cid, CUTOFF)
    assert loaded["selection"]["annual_history"]["period_ends"] == [
        "2023-03-31",
        "2024-03-31",
        "2025-03-31",
        "2026-03-31",
    ]
    assert loaded["financial"].revenue_growth is not None
    assert loaded["financial"].revenue_growth_years == 3


def test_gap_before_latest_pair_uses_only_one_year_horizon():
    rows = [annual(2023, 100), annual(2025, 121), annual(2026, 133.1)]
    conn, cid = setup(periods=rows)
    result = load_results_for_company(conn, cid, CUTOFF)
    assert result["financial"].revenue_growth == pytest.approx(0.1)
    assert result["financial"].revenue_growth_years == 1
    assert result["selection"]["annual_history"]["period_ends"] == [
        "2025-03-31",
        "2026-03-31",
    ]


def test_latest_suffix_survives_actual_rank_exports(monkeypatch, tmp_path):
    rows = [annual(2018, 70), annual(2020, 80)] + [
        annual(year, 100 * 1.1 ** (year - 2023)) for year in range(2023, 2027)
    ]
    conn, cid = setup(periods=rows)
    packet(conn, cid)
    score, csv_row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    selected = ["2023-03-31", "2024-03-31", "2025-03-31", "2026-03-31"]
    assert score["growth_score"] > 0
    assert score["input_selection"]["annual_history"]["period_ends"] == selected
    assert json.loads(csv_row["input_selection"])["annual_history"]["period_ends"] == selected
    assert dcf[str(cid)]["selection"]["annual_history"]["period_ends"] == selected


def test_nonnumeric_older_fiscal_year_is_excluded_without_arithmetic():
    conn, cid = setup(periods=[annual(2025), annual(2026, 110)])
    rows = [
        dict(row)
        for row in conn.execute("SELECT * FROM financial_periods ORDER BY period_end").fetchall()
    ]
    rows[0]["report_year"] = "not-a-year"
    selected, reasons, excluded = _annual_series(rows)
    assert [row["report_year"] for row in selected] == [2026]
    assert reasons == ["annual fiscal-year metadata unverified"]
    assert excluded[0]["reason"] == "annual fiscal-year metadata unverified"


@pytest.mark.parametrize("age,count", [(0, 1), (7, 1), (8, 0)])
def test_historical_price_pairing_boundary(age, count, monkeypatch, tmp_path):
    conn, cid = setup(periods=[annual(2025), annual(2026)])
    end = date(2025, 3, 31)
    historical_date = (end - timedelta(days=age)).isoformat()
    historical_raw = {"d": historical_date, "c": 10}
    upsert_prices(conn, cid, [historical_raw], currency="SEK")
    # A price after period end is never paired, however close.
    upsert_prices(conn, cid, [{"d": "2025-04-01", "c": 999}], currency="SEK")
    loaded = load_results_for_company(conn, cid, CUTOFF)
    assert loaded["valuation"].ev_ebit_history_count == count
    pairing = loaded["selection"]["historical_price_pairings"][0]
    assert pairing["value"] == 10
    assert pairing["date_facts"] == {"d": historical_date}
    assert pairing["raw_payload"] == historical_raw
    if count:
        assert loaded["valuation"].ev_ebit_guardrail_low is None
    else:
        assert pairing["reason"] == "historical price missing or older than seven calendar days"
    packet(conn, cid)
    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    assert json.loads(row["input_selection"]) == score["input_selection"]
    assert dcf[str(cid)]["selection"] == score["input_selection"]
    assert score["input_selection"]["historical_price_pairings"][0]["raw_payload"] == (
        historical_raw
    )


def test_publication_is_not_a_fiscal_end():
    conn, cid = setup(periods=[])
    payload = annual(2026)
    payload.pop("period_end")
    assert upsert_financial_periods(conn, cid, [payload]) == 0
    assert conn.execute("SELECT count(*) FROM financial_periods").fetchone()[0] == 0
    assert load_results_for_company(conn, cid, CUTOFF)["financial"] is None


@pytest.mark.parametrize("cutoff", ["2026-01-03", "2024-03-01", "2026-06-07"])
@pytest.mark.parametrize("age", [7, 8])
def test_price_calendar_age_across_year_leap_and_weekend(cutoff, age):
    day = date.fromisoformat(cutoff)
    year = day.year - 1
    conn, cid = setup(periods=[annual(year)], price_date=(day - timedelta(days=age)).isoformat())
    result = load_results_for_company(conn, cid, cutoff)
    assert (result["valuation"].raw_market_cap is not None) == (age == 7)
    assert result["selection"]["price"]["age_calendar_days"] == age


@pytest.mark.parametrize("price_date", [None, "2026-06-02", "not-a-date"])
def test_missing_future_or_invalid_price_cannot_authorize_valuation(
    price_date, monkeypatch, tmp_path
):
    conn, cid = setup(price_date=price_date)
    packet(conn, cid)
    result = load_results_for_company(conn, cid, CUTOFF)
    assert result["financial"].operating_margin == 0.2
    assert result["valuation"].raw_market_cap is None
    assert result["reverse_dcf"]["current_price"] is None
    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    assert not score["rank_eligible"]
    assert row["readiness_status"] == "valuation_blocked"
    assert "stock_price_missing" in row["readiness_blockers"]
    assert dcf[str(cid)]["dcf"]["available"] is False


@pytest.mark.parametrize(
    "raw_date,expected_count,stored_date",
    [
        ("2026-06-01T23:59:59Z", 1, CUTOFF),
        ("2026-06-01garbage", 0, None),
    ],
)
def test_price_ingestion_parses_complete_iso_dates(raw_date, expected_count, stored_date):
    conn, cid = setup(price_date=None)
    assert upsert_prices(conn, cid, [{"d": raw_date, "c": 10}], currency="SEK") == (expected_count)
    stored = conn.execute("SELECT price_date FROM prices").fetchone()
    assert (stored["price_date"] if stored else None) == stored_date
    loaded = load_results_for_company(conn, cid, CUTOFF)
    assert loaded["selection"]["price"]["selected_date"] == stored_date
    if stored_date is None:
        rejected = loaded["selection"]["rejected_prices"][0]
        assert rejected["raw_payload"] == {"d": raw_date, "c": 10}
        assert rejected["value"] == 10
        assert rejected["date_facts"] == {"d": raw_date}


@pytest.mark.parametrize(
    "publication,admitted",
    [
        (CUTOFF, True),
        ("2026-06-01T23:59:59Z", True),
        ("2026-06-02", False),
        (None, False),
        ("2026", False),
        ("2026-02-30", False),
        ("2026-03-30", False),
        ("2026-05-01garbage", False),
    ],
)
def test_report_publication_is_a_separate_verified_date(
    publication, admitted, monkeypatch, tmp_path
):
    conn, cid = setup(periods=[annual(2026, report_Date=publication)])
    packet(conn, cid)
    result = load_results_for_company(conn, cid, CUTOFF)
    assert (result["financial"] is not None) == admitted
    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    assert (
        score["readiness_status"]
        == row["readiness_status"]
        == ("ready" if admitted else "valuation_blocked")
    )
    assert (dcf[str(cid)]["status"] == "available") == admitted
    if admitted:
        assert result["selection"]["annual_history"]["period_ends"] == ["2026-03-31"]
    else:
        assert dcf[str(cid)]["missing_information"]


@pytest.mark.parametrize(
    "field_names,aliases,admitted",
    [
        (
            ("report_Date", "reportDate"),
            (("report_Date", "2026-05-01"), ("reportDate", "2026-08-01")),
            False,
        ),
        (
            ("report_Date", "reportDate"),
            (("reportDate", "2026-08-01"), ("report_Date", "2026-05-01")),
            False,
        ),
        (
            ("report_Date", "reportDate"),
            (("report_Date", "2026-05-01"), ("reportDate", "not-a-date")),
            False,
        ),
        (
            ("report_Date", "reportDate"),
            (("reportDate", "2026-05-01"), ("report_Date", "2026-05-01")),
            True,
        ),
        (("year", "report_year"), (("year", 2026), ("report_year", 2027)), False),
        (("year", "report_year"), (("report_year", 2027), ("year", 2026)), False),
        (("year", "report_year"), (("year", 2026), ("report_year", "bad")), False),
        (("year", "report_year"), (("report_year", "2026"), ("year", 2026)), True),
        (
            ("period_start", "period_Start"),
            (("period_start", "2025-04-01"), ("period_Start", "2025-10-01")),
            False,
        ),
        (
            ("period_start", "period_Start"),
            (("period_Start", "2025-10-01"), ("period_start", "2025-04-01")),
            False,
        ),
        (
            ("period_start", "period_Start"),
            (("period_start", "2025-04-01"), ("period_Start", "bad")),
            False,
        ),
        (
            ("period_start", "period_Start"),
            (("period_Start", "2025-04-01"), ("period_start", "2025-04-01")),
            True,
        ),
        (
            ("period_start", "period_Start"),
            (("period_Start", "2025-04-01T12:00:00Z"),),
            True,
        ),
        (
            ("period_start", "period_Start"),
            (("period_start", "2025-04-01garbage"),),
            False,
        ),
        (("period", "report_period"), (("period", 5), ("report_period", 4)), False),
        (("period", "report_period"), (("report_period", 4), ("period", 5)), False),
        (("period", "report_period"), (("period", 5), ("report_period", "bad")), False),
        (("period", "report_period"), (("report_period", "5"), ("period", 5)), True),
    ],
)
def test_report_aliases_must_parse_and_agree(field_names, aliases, admitted):
    payload = annual(2026)
    for field in field_names:
        payload.pop(field, None)
    for field, value in aliases:
        payload[field] = value

    conn, cid = setup(periods=[payload])
    loaded = load_results_for_company(conn, cid, CUTOFF)
    assert (loaded["financial"] is not None) is admitted
    stored = conn.execute(
        "SELECT raw_payload FROM financial_periods WHERE company_id=?", (cid,)
    ).fetchone()
    assert json.loads(stored["raw_payload"]) == payload
    if admitted:
        assert not loaded["selection"]["rejected_reports"]
    else:
        rejection = loaded["selection"]["rejected_reports"][0]
        assert rejection["raw_payload"] == payload
        assert rejection["current_refusal"]


@pytest.mark.parametrize(
    "aliases,admitted",
    [
        ((("period_end", "2026-03-31"), ("periodEnd", "2026-04-30")), False),
        ((("periodEnd", "2026-04-30"), ("period_end", "2026-03-31")), False),
        ((("period_end", "2026-03-31"), ("periodEnd", "bad")), False),
        ((("period_end", "2026-03-31garbage"),), False),
        ((("periodEnd", "2026-03-31"), ("period_end", "2026-03-31")), True),
        ((("periodEnd", "2026-03-31T12:00:00+01:00"),), True),
    ],
)
def test_fiscal_end_aliases_are_order_independent(aliases, admitted):
    payload = annual(2026)
    payload.pop("period_end")
    for field, value in aliases:
        payload[field] = value
    conn, cid = setup(periods=[])

    assert upsert_financial_periods(conn, cid, [payload]) == int(admitted)
    assert conn.execute("SELECT count(*) FROM financial_periods").fetchone()[0] == int(admitted)
    if admitted:
        assert load_results_for_company(conn, cid, CUTOFF)["financial"] is not None
    else:
        rejection = load_results_for_company(conn, cid, CUTOFF)["selection"]["rejected_reports"][0]
        assert rejection["raw_payload"] == payload
        assert rejection["current_refusal"]


def test_conflicting_publication_aliases_block_rank_exports(monkeypatch, tmp_path):
    rows = [annual(year, 100 * 1.1 ** (year - 2023)) for year in range(2023, 2026)]
    rows.append(
        annual(
            2026,
            133.1,
            report_Date="2026-05-01",
            reportDate="2026-08-01",
        )
    )
    conn, cid = setup(periods=rows)
    packet(conn, cid)

    loaded = load_results_for_company(conn, cid, CUTOFF)
    assert loaded["financial"].revenue_growth is None
    rejection = next(
        item
        for item in loaded["selection"]["rejected_reports"]
        if item["raw_payload"].get("reportDate") == "2026-08-01"
    )
    assert rejection["current_refusal"]

    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    exported = score["input_selection"]
    assert score["revenue_growth"] is None
    assert any(item.get("current_refusal") for item in exported["rejected_reports"])
    assert json.loads(row["input_selection"]) == exported
    assert dcf[str(cid)]["selection"] == exported
    assert dcf[str(cid)]["dcf"]["assumptions"]["revenue_growth"] == 0


def test_latest_r12_values_do_not_replace_the_annual_growth_anchor():
    rows = [annual(year, 100 * 1.1 ** (year - 2023)) for year in range(2023, 2027)]
    rows.extend(
        [
            annual(
                2026,
                40,
                period_type="quarter",
                period_end="2026-05-31",
                report_Date=CUTOFF,
                period=1,
            ),
            annual(
                2026, 150, period_type="r12", period_end="2026-05-31", report_Date=CUTOFF, period=1
            ),
        ]
    )
    for ordered in (rows, list(reversed(rows))):
        conn, cid = setup(periods=ordered)
        result = load_results_for_company(conn, cid, CUTOFF)
        assert result["financial"].revenue_growth == pytest.approx(0.1)
        assert result["financial"].revenue_growth_years == 3
        assert result["reverse_dcf"]["current_revenue"] == 150
        assert result["valuation"].ps == pytest.approx(100 / 150)


def test_valid_annual_split_history_has_no_false_dilution():
    rows = [annual(2025, 100), annual(2026, 110, number_Of_Shares=50)]
    conn, cid = setup(periods=rows)
    upsert_stock_splits(
        conn, [{"instrumentId": 991, "splitType": "S", "ratio": "5:1", "splitDate": "2025-04-09"}]
    )
    conn.execute("UPDATE stock_splits SET company_id=?", (cid,))
    result = load_results_for_company(conn, cid, CUTOFF)
    fin = result["financial"]
    assert fin.revenue_per_share_growth == pytest.approx(0.1)
    assert fin.share_count_growth == 0
    assert not fin.share_dilution
    assert (
        conn.execute(
            "SELECT shares_outstanding FROM financial_periods ORDER BY period_end"
        ).fetchone()[0]
        == 10
    )


@pytest.mark.parametrize("value", [None, 0])
def test_missing_versus_zero_current_inputs_survive_selection(value):
    conn, cid = setup(periods=[annual(2026, free_Cash_Flow=value, net_Debt=value)])
    result = load_results_for_company(conn, cid, CUTOFF)
    assert result["financial"].fcf_margin == (None if value is None else 0)
    assert result["financial"].net_debt == value
    assert (result["reverse_dcf"]["status"] == "available") == (value == 0)


@pytest.mark.parametrize("candidate_present", [False, True])
def test_cmd_rank_uses_scored_method_even_if_loader_candidate_disagrees(
    candidate_present, monkeypatch, tmp_path
):
    conn, cid = setup(branch=75)
    packet(conn, cid)
    loaded = load_results_for_company(conn, cid, CUTOFF)
    loaded["candidate"].ranking_model = "general"
    loaded["candidate"].full_results["reverse_dcf"] = {"status": "available"}
    if not candidate_present:
        del loaded["candidate"]
    monkeypatch.setattr(
        "alphaforge.cli.ranking_loader.load_results_for_company", lambda *args: loaded
    )
    score, row, _ = rank_exports(conn, monkeypatch, tmp_path)
    assert score["readiness_status"] == row["readiness_status"] == "method_unsupported"


def test_invalid_or_stale_text_packet_remains_evidence_blocked(monkeypatch, tmp_path):
    conn, cid = setup(price_date="2025-07-02")
    original = packet(conn, cid)
    # A usable financial result never overrides textual authority.
    conn.execute("UPDATE evidence_packets SET usable=0, usable_reason='fixture invalidation'")
    score, row, _ = rank_exports(conn, monkeypatch, tmp_path)
    assert score["readiness_status"] == row["readiness_status"] == "evidence_blocked"
    assert original["packet_hash"] != score.get("evidence_packet_hash")


def test_unavailable_annual_reason_survives_json_csv_and_dcf(monkeypatch, tmp_path):
    conn, cid = setup(periods=[annual(2023), annual(2026, 133.1)])
    packet(conn, cid)
    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    assert any("not consecutive" in item for item in score["missing_data"])
    assert "not consecutive" in row["missing_data"]
    assert dcf[str(cid)]["selection"]["annual_history"]["excluded"]
    assert score["growth_score"] == 0
    for metric in (
        "revenue_growth",
        "ebit_growth",
        "net_income_growth",
        "revenue_per_share_growth",
        "ebit_per_share_growth",
        "net_income_per_share_growth",
        "fcf_per_share_growth",
        "book_value_per_share_growth",
        "share_count_growth",
    ):
        assert score[metric] is None
        assert score[f"{metric}_years"] == 0
        assert row[metric] == ""
        assert row[f"{metric}_years"] == "0"
    assert RankingEngine.RANKING_MODEL_VERSION == "2026-09-30-annual-rejection-span-v16"


@pytest.mark.parametrize("baseline", [None, 0, -100])
def test_nonpositive_or_missing_baseline_has_no_growth_horizon(baseline):
    conn, cid = setup(periods=[annual(2025, revenues=baseline), annual(2026, 110)])
    fin = load_results_for_company(conn, cid, CUTOFF)["financial"]
    assert fin.revenue_growth is None
    assert fin.revenue_growth_years == 0
    assert fin.revenue_per_share_growth is None
    assert fin.per_share_growth_years == 0


def test_leap_month_end_is_a_valid_fiscal_anniversary():
    rows = [
        annual(
            year,
            100 * 1.1 ** (year - 2023),
            period_end=f"{year}-02-{'29' if year == 2024 else '28'}",
            period_start=f"{year - 1}-03-01",
        )
        for year in range(2023, 2027)
    ]
    conn, cid = setup(periods=rows)
    fin = load_results_for_company(conn, cid, CUTOFF)["financial"]
    assert fin.revenue_growth == pytest.approx(0.1)
    assert fin.revenue_growth_years == 3


def test_yearless_last_kpi_does_not_expand_existing_selection():
    conn, cid = setup()
    upsert_kpi_observations(conn, cid, 37, "last", "latest", [{"d": CUTOFF, "v": 99}])
    assert 37 not in load_results_for_company(conn, cid, CUTOFF)["fundamental_kpis"]


def test_future_report_and_placeholder_do_not_change_selected_growth():
    rows = [annual(year, 100 * 1.1 ** (year - 2023)) for year in range(2023, 2027)]
    conn, cid = setup(periods=rows)
    before = load_results_for_company(conn, cid, CUTOFF)
    upsert_financial_periods(conn, cid, [annual(2027, 999), annual(2028, 0, report_Date=None)])
    after = load_results_for_company(conn, cid, CUTOFF)
    assert asdict(before["financial"]) == asdict(after["financial"])
    assert after["reverse_dcf"]["current_revenue"] == before["reverse_dcf"]["current_revenue"]


def test_rejected_kpi_replacement_preserves_verified_roic(monkeypatch, tmp_path):
    conn, cid = setup(
        periods=[annual(year, 100 * 1.1 ** (year - 2023)) for year in range(2023, 2027)]
    )
    packet(conn, cid)
    upsert_kpi_observations(
        conn,
        cid,
        37,
        "year",
        "mean",
        [{"y": 2026, "p": 5, "v": 30, "observationDate": CUTOFF}],
    )
    upsert_kpi_observations(conn, cid, 37, "year", "mean", [{"y": 2026, "p": 5, "v": 40}])
    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    roic = next(
        component
        for component in score["scoring_audit"]["quality"]["components"]
        if component["name"] == "roic"
    )
    assert roic["raw_value"] == 0.3 and roic["available"]
    assert "roic" not in row["missing_data"]
    assert "roic" not in dcf[str(cid)]["dcf"]["missing_information"]
    assert dcf[str(cid)]["dcf"]["assumptions"]["net_reinvestment_rate"] > 0
    rejected = score["input_selection"]["rejected_kpis"][0]
    assert rejected["raw_payload"]["v"] == 40
    assert not rejected["current_refusal"]
    assert score["readiness_status"] == "ready"
    upsert_kpi_observations(
        conn, cid, 37, "year", "mean", [{"y": 2026, "p": 5, "v": 50, "observationDate": CUTOFF}]
    )
    dated, _, dated_dcf = rank_exports(conn, monkeypatch, tmp_path)
    dated_roic = next(
        component
        for component in dated["scoring_audit"]["quality"]["components"]
        if component["name"] == "roic"
    )
    assert dated_roic["raw_value"] == 0.5 and dated_roic["available"]
    assert dated_dcf[str(cid)]["dcf"]["assumptions"]["net_reinvestment_rate"] > 0


@pytest.mark.parametrize(
    "field",
    [
        "period_end",
        "periodEnd",
        "period_End",
        "reportEndDate",
        "report_End_Date",
        "report_end_date",
    ],
)
def test_explicit_fiscal_aliases_verify_without_publication_substitution(field):
    payload = annual(2026)
    end = payload.pop("period_end")
    payload[field] = end
    conn, cid = setup(periods=[payload])
    result = load_results_for_company(conn, cid, CUTOFF)
    assert result["selection"]["annual_history"]["period_ends"] == [end]
    assert result["financial"].operating_margin == 0.2


def test_foreign_report_currency_does_not_expand_valuation_availability():
    conn, cid = setup(periods=[annual(2025, currency="USD"), annual(2026, 110, currency="USD")])
    result = load_results_for_company(conn, cid, CUTOFF)
    assert result["valuation"].raw_market_cap is None
    assert result["reverse_dcf"]["status"] == "unavailable"
    assert not result["reverse_dcf"]["dcf"]["available"]


def test_all_selection_refusals_retain_provenance_and_reach_exports(monkeypatch, tmp_path):
    conn, cid = setup(periods=[annual(2024), annual(2025, 110)])
    rejected = annual(2026, 121)
    rejected.pop("period_end")
    assert upsert_financial_periods(conn, cid, [rejected]) == 0
    upsert_kpi_observations(conn, cid, 37, "year", "mean", [{"y": 2026, "p": 5, "v": 30}])
    packet(conn, cid)

    loaded = load_results_for_company(conn, cid, CUTOFF)
    selection = loaded["selection"]
    ingestion = next(
        item for item in selection["rejected_reports"] if item["source"] == "ingestion_rejection"
    )
    assert ingestion["raw_payload"]["year"] == 2026
    assert ingestion["current_refusal"]
    assert ingestion["payload_hash"]
    assert selection["rejected_kpis"][0]["observation_date"] is None
    assert selection["rejected_kpis"][0]["raw_payload"]["v"] == 30
    assert selection["historical_price_pairings"][0]["period_end"] == "2024-03-31"

    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    reasons = score["missing_data"]
    assert any("fiscal end unavailable or invalid" in reason for reason in reasons)
    assert any("KPI observation date unavailable" in reason for reason in reasons)
    assert any("historical price missing" in reason for reason in reasons)
    assert all(
        any(reason in limitation for limitation in score["readiness_limitations"])
        for reason in selection["refusal_reasons"]
    )
    assert json.loads(row["input_selection"]) == score["input_selection"]
    assert dcf[str(cid)]["selection"] == score["input_selection"]


def test_annual_rejections_block_only_while_unresolved_and_applicable(monkeypatch, tmp_path):
    rows = [annual(year, 100 * 1.1 ** (year - 2023)) for year in range(2023, 2026)]
    conn, cid = setup(periods=rows)
    rejected = annual(2026, 133.1)
    rejected.pop("period_end")
    assert upsert_financial_periods(conn, cid, [rejected]) == 0

    unresolved = load_results_for_company(conn, cid, CUTOFF)
    assert unresolved["financial"].revenue_growth is None
    assert unresolved["selection"]["annual_history"]["period_ends"] == []
    rejected_item = next(
        item
        for item in unresolved["selection"]["rejected_reports"]
        if item["source"] == "ingestion_rejection"
    )
    assert rejected_item["current_refusal"]
    assert any(
        "fiscal end unavailable or invalid" in reason
        for reason in unresolved["selection"]["refusal_reasons"]
    )

    upsert_financial_periods(conn, cid, [annual(2026, 133.1)])
    older = annual(2022, 90)
    older.pop("period_end")
    future = annual(2027, 146.41)
    future.pop("period_end")
    assert upsert_financial_periods(conn, cid, [older, future]) == 0

    corrected = load_results_for_company(conn, cid, CUTOFF)
    assert corrected["financial"].revenue_growth == pytest.approx(0.1)
    assert corrected["financial"].revenue_growth_years == 3
    ingestion_rejections = [
        item
        for item in corrected["selection"]["rejected_reports"]
        if item["source"] == "ingestion_rejection"
    ]
    assert {item["raw_payload"]["year"] for item in ingestion_rejections} == {
        2022,
        2026,
        2027,
    }
    assert not any(item["current_refusal"] for item in ingestion_rejections)
    assert not any(
        reason.startswith("report rejection:")
        for reason in corrected["selection"]["refusal_reasons"]
    )

    packet(conn, cid)
    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    exported_rejections = [
        item
        for item in score["input_selection"]["rejected_reports"]
        if item["source"] == "ingestion_rejection"
    ]
    assert score["revenue_growth_years"] == 3
    assert not any(item["current_refusal"] for item in exported_rejections)
    assert json.loads(row["input_selection"]) == score["input_selection"]
    assert dcf[str(cid)]["selection"] == score["input_selection"]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "source,end",
    [
        ("financial_periods", "2025-04-30"),
        ("financial_periods", "2024-04-30"),
        ("ingestion_rejection", "2025-04-30"),
    ],
)
def test_interior_annual_rejection_blocks_growth_and_exports(
    source, end, reverse, monkeypatch, tmp_path
):
    rows = [annual(year, 100 * 1.1 ** (year - 2024)) for year in range(2024, 2027)]
    rejected = annual(int(end[:4]), 115, period_end=end, report_Date=None)
    if source == "ingestion_rejection":
        rejected["periodEnd"] = "2025-05-31"  # An unresolved, conflicting fiscal slot.
    rows.append(rejected)
    conn, cid = setup(periods=list(reversed(rows)) if reverse else rows)
    upsert_prices(conn, cid, [{"d": f"{year}-03-31", "c": 10} for year in range(2024, 2027)])
    packet(conn, cid)

    loaded = load_results_for_company(conn, cid, CUTOFF)
    rejection = next(
        item for item in loaded["selection"]["rejected_reports"] if item["raw_payload"] == rejected
    )
    assert rejection["source"] == source
    assert rejection["current_refusal"]
    assert loaded["selection"]["annual_history"]["period_ends"] == []
    assert loaded["financial"].revenue_growth is None
    assert loaded["financial"].revenue_growth_years == 0
    # Preserve the existing explicit fallback, never a CAGR across the uncertain span.
    policy = loaded["dcf"]["policy"]
    assert policy.assumptions.revenue_growth == 0
    assert "historical revenue growth unavailable" in policy.assumption_sources["revenue_growth"]

    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    assert score["growth_score"] == 0
    for metric in (
        "revenue_growth",
        "ebit_growth",
        "fcf_per_share_growth",
        "book_value_per_share_growth",
    ):
        assert score[metric] is None
        assert score[f"{metric}_years"] == 0
        assert row[metric] == ""
        assert row[f"{metric}_years"] == "0"
    assert any("unresolved applicable annual rejection" in item for item in score["missing_data"])
    assert json.loads(row["input_selection"]) == score["input_selection"]
    assert dcf[str(cid)]["selection"] == score["input_selection"]
    assert dcf[str(cid)]["dcf"]["assumptions"]["revenue_growth"] == 0
    exported = next(
        item
        for item in score["input_selection"]["rejected_reports"]
        if item["raw_payload"] == rejected
    )
    assert exported["current_refusal"]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("case", ["older", "future", "corrected", "before_suffix_gap"])
def test_nonblocking_annual_rejections_preserve_selected_span(case, reverse, monkeypatch, tmp_path):
    rows = [annual(year, 100 * 1.1 ** (year - 2024)) for year in range(2024, 2027)]
    year, end = {
        "older": (2023, "2023-04-30"),
        "future": (2027, "2027-04-30"),
        "corrected": (2025, "2025-03-31"),
        "before_suffix_gap": (2024, "2024-04-30"),
    }[case]
    rejected = annual(year, 115, period_end=end, report_Date=None)
    if case == "before_suffix_gap":
        rows = [annual(2022, 80), *rows[1:]]
    conn, cid = setup(periods=[rejected])
    upsert_financial_periods(conn, cid, list(reversed(rows)) if reverse else rows)
    packet(conn, cid)
    loaded = load_results_for_company(conn, cid, CUTOFF)
    rejection = next(
        item for item in loaded["selection"]["rejected_reports"] if item["raw_payload"] == rejected
    )
    assert not rejection["current_refusal"]
    assert loaded["financial"].revenue_growth == pytest.approx(0.1)
    assert loaded["financial"].revenue_growth_years == (1 if case == "before_suffix_gap" else 2)
    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    assert score["revenue_growth"] == pytest.approx(0.1)
    assert json.loads(row["input_selection"]) == score["input_selection"]
    assert dcf[str(cid)]["selection"] == score["input_selection"]
    assert dcf[str(cid)]["dcf"]["assumptions"]["revenue_growth"] == pytest.approx(0.1)


def test_consecutive_dcf_growth_has_new_exported_policy_provenance(monkeypatch, tmp_path):
    conn, cid = setup(
        periods=[annual(2023, 50), annual(2024, 0), annual(2025, 110), annual(2026, 121)]
    )
    packet(conn, cid)
    loaded = load_results_for_company(conn, cid, CUTOFF)
    expected = "reverse-dcf-v12-consecutive-annual-growth"
    assert loaded["dcf"]["policy"].policy_version == expected
    assert loaded["dcf"]["policy"].assumptions.revenue_growth == pytest.approx(0.1)
    assert loaded["reverse_dcf"]["dcf"]["policy_version"] == expected
    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    assert score["revenue_growth"] == pytest.approx(0.1)
    assert score["revenue_growth_years"] == 1
    assert row["revenue_growth_years"] == "1"
    assert dcf[str(cid)]["dcf"]["policy_version"] == expected
    assert dcf[str(cid)]["dcf"]["assumptions"]["revenue_growth"] == pytest.approx(0.1)
    assert score["input_selection"]["version"] == "verified-dates-consecutive-annual-v2"
    assert json.loads(row["input_selection"])["version"] == "verified-dates-consecutive-annual-v2"
    assert dcf[str(cid)]["selection"]["version"] == "verified-dates-consecutive-annual-v2"


@pytest.mark.parametrize(
    "publication_fields,current_refusal",
    [
        ({"report_Date": "2026-08-01"}, False),
        ({"report_Date": "2026-05-01", "reportDate": "2026-08-01"}, True),
        ({"report_Date": "2026-08-01", "reportDate": "not-a-date"}, True),
    ],
)
def test_future_publication_is_audit_only_without_hiding_alias_conflicts(
    publication_fields, current_refusal
):
    rows = [annual(year, 100 * 1.1 ** (year - 2023)) for year in range(2023, 2027)]
    conn, cid = setup(periods=rows)
    rejected = annual(2026, 140, **publication_fields)
    rejected.pop("period_end")
    rejected.pop("year")
    assert upsert_financial_periods(conn, cid, [rejected]) == 0

    loaded = load_results_for_company(conn, cid, CUTOFF)
    retained = next(
        item
        for item in loaded["selection"]["rejected_reports"]
        if item["source"] == "ingestion_rejection"
    )
    assert retained["current_refusal"] is current_refusal
    assert (
        any(
            "fiscal end unavailable or invalid" in reason
            for reason in loaded["selection"]["refusal_reasons"]
        )
        is current_refusal
    )


def test_same_year_distinct_fiscal_end_rejection_blocks_rank_exports(monkeypatch, tmp_path):
    rows = [annual(year, 100 * 1.1 ** (year - 2023)) for year in range(2023, 2027)]
    conn, cid = setup(periods=rows)
    transition = annual(
        2026,
        140,
        period_end="2026-04-30",
        report_Date=None,
    )
    assert upsert_financial_periods(conn, cid, [transition]) == 1
    packet(conn, cid)

    loaded = load_results_for_company(conn, cid, CUTOFF)
    transition_rejection = next(
        item
        for item in loaded["selection"]["rejected_reports"]
        if item["source"] == "financial_periods" and item["period_end"] == "2026-04-30"
    )
    assert transition_rejection["report_year"] == 2026
    assert transition_rejection["current_refusal"]
    assert loaded["selection"]["annual_history"]["period_ends"] == []
    assert loaded["financial"].revenue_growth is None

    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    exported = score["input_selection"]
    assert score["revenue_growth"] is None
    assert score["revenue_growth_years"] == 0
    assert any(
        item.get("period_end") == "2026-04-30" and item["current_refusal"]
        for item in exported["rejected_reports"]
    )
    assert json.loads(row["input_selection"]) == exported
    assert dcf[str(cid)]["selection"] == exported
    assert dcf[str(cid)]["dcf"]["assumptions"]["revenue_growth"] == 0


@pytest.mark.parametrize(
    "defect,expected_reason",
    [
        ("r12_publication", "fiscal end or publication date unverified"),
        ("annual_publication", "fiscal end or publication date unverified"),
        ("publication_order", "publication precedes fiscal end"),
        ("annual_year", "annual fiscal-year metadata unverified"),
        ("annual_stub", "annual stub or duration unverified"),
        ("annual_currency", "annual currency comparability unverified"),
        ("placeholder", "placeholder"),
    ],
)
def test_keyable_rejection_survives_same_slot_correction_and_exports(
    defect, expected_reason, monkeypatch, tmp_path
):
    annuals = []
    for year in range(2023, 2027 if defect == "r12_publication" else 2026):
        revenue = 100 * 1.1 ** (year - 2023)
        annuals.append(annual(year, revenue, ebit=revenue * 0.2))
    conn, cid = setup(periods=annuals)
    if defect == "r12_publication":
        rejected = annual(
            2026,
            150,
            period_type="r12",
            period=1,
            period_end="2026-05-31",
            report_Date=None,
            ebit=30,
        )
        corrected = annual(
            2026,
            150,
            period_type="r12",
            period=1,
            period_end="2026-05-31",
            report_Date=CUTOFF,
            ebit=30,
        )
    else:
        rejected = annual(2026, 133.1, ebit=26.62)
        corrected = annual(2026, 133.1, ebit=26.62)
        if defect == "annual_publication":
            rejected["report_Date"] = None
        elif defect == "publication_order":
            rejected["report_Date"] = "2026-03-30"
        elif defect == "annual_year":
            rejected.pop("year")
        elif defect == "annual_stub":
            rejected["period_start"] = "2025-10-01"
        elif defect == "annual_currency":
            rejected["currency"] = "USD"
        elif defect == "placeholder":
            rejected = annual(2026, 0, report_Date=None, ebit=0)

    assert upsert_financial_periods(conn, cid, [rejected]) == 1
    assert conn.execute("SELECT count(*) FROM financial_period_rejections").fetchone()[0] == 0
    assert upsert_financial_periods(conn, cid, [corrected, corrected]) == 2

    archived_rows = conn.execute(
        "SELECT reason, raw_payload FROM financial_period_rejections"
    ).fetchall()
    assert len(archived_rows) == 1
    archived = archived_rows[0]
    assert archived["reason"] == expected_reason
    assert json.loads(archived["raw_payload"]) == rejected
    stored = conn.execute(
        "SELECT raw_payload FROM financial_periods WHERE period_type=? AND period_end=?",
        (corrected["period_type"], corrected["period_end"]),
    ).fetchone()
    assert json.loads(stored["raw_payload"]) == corrected

    loaded = load_results_for_company(conn, cid, CUTOFF)
    audit = next(
        item
        for item in loaded["selection"]["rejected_reports"]
        if item["source"] == "ingestion_rejection"
    )
    assert audit["raw_payload"] == rejected
    assert not audit["current_refusal"]
    assert loaded["financial"].revenue_growth == pytest.approx(0.1)
    assert loaded["financial"].revenue_growth_years == 3
    assert not any(expected_reason in reason for reason in loaded["selection"]["refusal_reasons"])

    packet(conn, cid)
    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    exported = score["input_selection"]
    exported_audit = next(
        item for item in exported["rejected_reports"] if item["source"] == "ingestion_rejection"
    )
    assert exported_audit["raw_payload"] == rejected
    assert not exported_audit["current_refusal"]
    assert score["readiness_status"] == "ready"
    assert score["revenue_growth_years"] == 3
    assert not any(expected_reason in item for item in score["missing_data"])
    assert json.loads(row["input_selection"]) == exported
    assert dcf[str(cid)]["selection"] == exported


def test_conflicting_alias_original_is_retained_once_across_repeated_replacement():
    conn, cid = setup(periods=[annual(2025)])
    rejected = annual(
        2026,
        110,
        report_Date="2026-05-01",
        reportDate="2026-08-01",
    )
    corrected = annual(2026, 110)

    assert upsert_financial_periods(conn, cid, [rejected]) == 1
    assert upsert_financial_periods(conn, cid, [corrected, corrected]) == 2
    archived = conn.execute(
        "SELECT reason, raw_payload FROM financial_period_rejections"
    ).fetchall()
    assert len(archived) == 1
    assert archived[0]["reason"] == "fiscal end or publication date unverified"
    assert json.loads(archived[0]["raw_payload"]) == rejected

    audit = next(
        item
        for item in load_results_for_company(conn, cid, CUTOFF)["selection"]["rejected_reports"]
        if item["source"] == "ingestion_rejection"
    )
    assert audit["raw_payload"] == rejected
    assert audit["current_refusal"]


@pytest.mark.parametrize("period_type", ["year", "r12", "quarter"])
def test_conflicting_fiscal_aliases_remain_current_after_same_key_correction(
    period_type, monkeypatch, tmp_path
):
    rows = [
        annual(year, 100 * 1.1 ** (year - 2023), ebit=20 * 1.1 ** (year - 2023))
        for year in range(2023, 2027)
    ]
    conn, cid = setup(periods=rows)
    end = "2026-03-31" if period_type == "year" else "2026-05-31"
    rejected = annual(
        2026,
        150,
        period_type=period_type,
        period=5 if period_type == "year" else 1,
        period_end=end,
        periodEnd="2026-04-30",
        report_Date=CUTOFF,
        ebit=30,
    )
    corrected = dict(rejected)
    corrected.pop("periodEnd")
    assert upsert_financial_periods(conn, cid, [rejected]) == 0
    assert upsert_financial_periods(conn, cid, [corrected]) == 1
    packet(conn, cid)

    loaded = load_results_for_company(conn, cid, CUTOFF)
    audit = next(
        item
        for item in loaded["selection"]["rejected_reports"]
        if item["source"] == "ingestion_rejection"
    )
    assert audit["raw_payload"] == rejected
    assert audit["current_refusal"]
    if period_type == "year":
        assert loaded["financial"].revenue_growth is None
    else:
        assert loaded["financial"].revenue_growth == pytest.approx(0.1)

    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    exported = score["input_selection"]
    exported_audit = next(
        item for item in exported["rejected_reports"] if item["source"] == "ingestion_rejection"
    )
    assert exported_audit["current_refusal"]
    assert any("fiscal end" in item for item in score["missing_data"])
    assert json.loads(row["input_selection"]) == exported
    assert dcf[str(cid)]["selection"] == exported


@pytest.mark.parametrize(
    "case,expected_reason",
    [
        ("duplicate", "duplicate annual fiscal slot"),
        ("gap", "annual periods are not consecutive fiscal anniversaries"),
        ("anniversary", "annual periods are not consecutive fiscal anniversaries"),
        ("currency", "annual currency comparability unverified"),
        ("ordinary", None),
    ],
)
def test_contextual_annual_rejections_archive_only_rejected_restatements(case, expected_reason):
    if case == "duplicate":
        rows = [annual(2025), annual(2026, 110), annual(2026, 120, period_end="2026-04-30")]
        target = rows[-1]
    elif case == "gap":
        rows = [annual(2023), annual(2025, 121), annual(2026, 133.1)]
        target = rows[0]
    elif case == "anniversary":
        rows = [annual(2025), annual(2026, 110, period_end="2026-04-30")]
        target = rows[-1]
    elif case == "currency":
        rows = [annual(2025), annual(2026, 110, currency="USD")]
        target = rows[-1]
    else:
        rows = [annual(2025), annual(2026, 110)]
        target = rows[0]
    conn, cid = setup(periods=rows)
    original = dict(target)
    restated = dict(target)
    restated["revenues"] = float(restated["revenues"]) + 1

    assert upsert_financial_periods(conn, cid, [restated]) == 1
    assert upsert_financial_periods(conn, cid, [restated]) == 1
    archived = conn.execute(
        "SELECT reason, raw_payload FROM financial_period_rejections"
    ).fetchall()
    if expected_reason is None:
        assert archived == []
    else:
        assert len(archived) == 1
        assert archived[0]["reason"] == expected_reason
        assert json.loads(archived[0]["raw_payload"]) == original


@pytest.mark.parametrize(
    "case,current_refusal",
    [
        ("future", False),
        ("future_undated", False),
        ("older_undated", False),
        ("annual_shadowed_by_r12", False),
        ("newer_undated", True),
    ],
)
def test_kpi_rejections_only_refuse_applicable_selection(
    case, current_refusal, monkeypatch, tmp_path
):
    conn, cid = setup()
    if case in {"future", "future_undated"}:
        upsert_kpi_observations(
            conn, cid, 37, "year", "mean", [{"y": 2026, "p": 5, "v": 30, "observationDate": CUTOFF}]
        )
        future = {"y": 2027, "p": 5, "v": 40}
        if case == "future":
            future["observationDate"] = "2027-05-01"
        upsert_kpi_observations(conn, cid, 37, "year", "mean", [future])
    elif case == "older_undated":
        upsert_kpi_observations(conn, cid, 37, "year", "mean", [{"y": 2025, "p": 5, "v": 20}])
        upsert_kpi_observations(
            conn, cid, 37, "year", "mean", [{"y": 2026, "p": 5, "v": 30, "observationDate": CUTOFF}]
        )
    elif case == "annual_shadowed_by_r12":
        upsert_kpi_observations(conn, cid, 37, "year", "mean", [{"y": 2026, "p": 5, "v": 20}])
        upsert_kpi_observations(
            conn, cid, 37, "r12", "mean", [{"y": 2026, "p": 1, "v": 30, "observationDate": CUTOFF}]
        )
    else:
        upsert_kpi_observations(
            conn,
            cid,
            37,
            "year",
            "mean",
            [{"y": 2025, "p": 5, "v": 20, "observationDate": "2025-05-01"}],
        )
        upsert_kpi_observations(conn, cid, 37, "year", "mean", [{"y": 2026, "p": 5, "v": 30}])
    packet(conn, cid)

    loaded = load_results_for_company(conn, cid, CUTOFF)
    rejected = loaded["selection"]["rejected_kpis"][0]
    assert rejected["current_refusal"] is current_refusal
    assert (
        any("KPI" in reason for reason in loaded["selection"]["refusal_reasons"]) is current_refusal
    )

    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    exported = score["input_selection"]
    assert exported["rejected_kpis"][0]["current_refusal"] is current_refusal
    assert any("KPI" in item for item in score["missing_data"]) is current_refusal
    assert json.loads(row["input_selection"]) == exported
    assert dcf[str(cid)]["selection"] == exported


@pytest.mark.parametrize(
    "period_type,period_end",
    [("r12", "2026-05-31"), ("quarter", "2026-06-01")],
)
def test_corrected_nonannual_rejection_is_audit_only_in_rank_exports(
    period_type, period_end, monkeypatch, tmp_path
):
    rows = [annual(year, 100 * 1.1 ** (year - 2023)) for year in range(2023, 2027)]
    conn, cid = setup(periods=rows)
    rejected = annual(2026, 150, period_type=period_type, period=1)
    rejected.pop("period_end")
    assert upsert_financial_periods(conn, cid, [rejected]) == 0
    corrected = annual(
        2026,
        150,
        period_type=period_type,
        period=1,
        period_end=period_end,
        report_Date=CUTOFF,
    )
    assert upsert_financial_periods(conn, cid, [corrected]) == 1
    packet(conn, cid)

    loaded = load_results_for_company(conn, cid, CUTOFF)
    retained = next(
        item
        for item in loaded["selection"]["rejected_reports"]
        if item["source"] == "ingestion_rejection"
    )
    assert retained["period_type"] == period_type
    assert not retained["current_refusal"]
    assert not any(
        "fiscal end unavailable or invalid" in reason
        for reason in loaded["selection"]["refusal_reasons"]
    )

    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    exported = score["input_selection"]
    retained_export = next(
        item for item in exported["rejected_reports"] if item["source"] == "ingestion_rejection"
    )
    assert not retained_export["current_refusal"]
    assert not any("fiscal end unavailable or invalid" in item for item in score["missing_data"])
    assert json.loads(row["input_selection"]) == exported
    assert dcf[str(cid)]["selection"] == exported


@pytest.mark.parametrize("period_type", ["r12", "quarter"])
@pytest.mark.parametrize(
    "case,year,period,current_refusal",
    [
        ("older_year", 2020, 1, False),
        ("older_period", 2026, 1, False),
        ("exact_slot", 2026, 2, False),
        ("year_only", 2026, None, True),
        ("newer_period", 2026, 3, True),
        ("future", 2027, 3, False),
    ],
)
def test_nonannual_rejections_use_exact_slots_and_latest_applicability(
    period_type, case, year, period, current_refusal, monkeypatch, tmp_path
):
    rows = [annual(value, 100 * 1.1 ** (value - 2023)) for value in range(2023, 2027)]
    rows.append(
        annual(
            2026,
            150,
            period_type=period_type,
            period=2,
            period_end="2026-05-31",
            report_Date=CUTOFF,
        )
    )
    conn, cid = setup(periods=rows)
    rejected = annual(year, 140, period_type=period_type)
    rejected.pop("period_end")
    if period is None:
        rejected.pop("period")
    else:
        rejected["period"] = period
    assert upsert_financial_periods(conn, cid, [rejected]) == 0
    packet(conn, cid)

    loaded = load_results_for_company(conn, cid, CUTOFF)
    retained = next(
        item
        for item in loaded["selection"]["rejected_reports"]
        if item["source"] == "ingestion_rejection"
    )
    assert retained["current_refusal"] is current_refusal, case
    assert (
        any(retained["reason"] in reason for reason in loaded["selection"]["refusal_reasons"])
        is current_refusal
    )

    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    exported = score["input_selection"]
    exported_rejection = next(
        item for item in exported["rejected_reports"] if item["source"] == "ingestion_rejection"
    )
    assert exported_rejection["current_refusal"] is current_refusal
    assert any(retained["reason"] in item for item in score["missing_data"]) is current_refusal
    assert (
        any(retained["reason"] in item for item in score["readiness_limitations"])
        is current_refusal
    )
    assert json.loads(row["input_selection"]) == exported
    assert dcf[str(cid)]["selection"] == exported


@pytest.mark.parametrize("period_type", ["r12", "quarter"])
def test_nonannual_year_period_identity_must_match_one_admitted_slot(period_type):
    rows = [annual(year) for year in range(2023, 2027)]
    rows.extend(
        [
            annual(
                2026,
                140,
                period_type=period_type,
                period=2,
                period_end="2026-03-31",
                report_Date="2026-05-01",
            ),
            annual(
                2026,
                150,
                period_type=period_type,
                period=2,
                period_end="2026-05-31",
                report_Date=CUTOFF,
            ),
        ]
    )
    conn, cid = setup(periods=rows)
    rejected = annual(2026, 145, period_type=period_type, period=2)
    rejected.pop("period_end")
    assert upsert_financial_periods(conn, cid, [rejected]) == 0

    retained = next(
        item
        for item in load_results_for_company(conn, cid, CUTOFF)["selection"]["rejected_reports"]
        if item["source"] == "ingestion_rejection"
    )
    assert retained["current_refusal"]


@pytest.mark.parametrize("period_type", ["year", "r12", "quarter"])
def test_report_period_alone_never_supersedes_a_rejection(period_type):
    rows = [annual(year, 100 * 1.1 ** (year - 2023)) for year in range(2023, 2027)]
    conn, cid = setup(periods=rows)
    period = 5 if period_type == "year" else 1
    rejected = annual(2026, 140, period_type=period_type, period=period)
    rejected.pop("period_end")
    rejected.pop("year")
    assert upsert_financial_periods(conn, cid, [rejected]) == 0
    if period_type != "year":
        corrected = annual(
            2026,
            150,
            period_type=period_type,
            period=period,
            period_end="2026-05-31",
            report_Date=CUTOFF,
        )
        assert upsert_financial_periods(conn, cid, [corrected]) == 1

    loaded = load_results_for_company(conn, cid, CUTOFF)
    retained = next(
        item
        for item in loaded["selection"]["rejected_reports"]
        if item["source"] == "ingestion_rejection"
    )
    assert retained["current_refusal"]
    assert any(retained["reason"] in reason for reason in loaded["selection"]["refusal_reasons"])


@pytest.mark.parametrize("case", ["known_end_conflict", "cross_type", "unknown_slot"])
def test_nonannual_rejection_conflicts_remain_current_in_rank_exports(case, monkeypatch, tmp_path):
    rows = [annual(year, 100 * 1.1 ** (year - 2023)) for year in range(2023, 2027)]
    conn, cid = setup(periods=rows)
    if case == "known_end_conflict":
        rejected = annual(
            2026,
            140,
            period_type="r12",
            period=1,
            period_end="2026-04-30",
            report_Date=None,
        )
        corrected = annual(
            2026,
            150,
            period_type="r12",
            period=1,
            period_end="2026-05-31",
            report_Date=CUTOFF,
        )
        assert upsert_financial_periods(conn, cid, [rejected]) == 1
    else:
        rejected = annual(2026, 140, period_type="r12", period=1)
        rejected.pop("period_end")
        if case == "unknown_slot":
            rejected.pop("year")
            rejected.pop("period")
        corrected = annual(
            2026,
            150,
            period_type="quarter" if case == "cross_type" else "r12",
            period=1,
            period_end="2026-05-31",
            report_Date=CUTOFF,
        )
        assert upsert_financial_periods(conn, cid, [rejected]) == 0
    assert upsert_financial_periods(conn, cid, [corrected]) == 1
    packet(conn, cid)

    loaded = load_results_for_company(conn, cid, CUTOFF)
    retained = next(
        item
        for item in loaded["selection"]["rejected_reports"]
        if item["period_type"] == "r12" and item["reason"] != "after cutoff"
    )
    assert retained["current_refusal"]
    assert any(retained["reason"] in reason for reason in loaded["selection"]["refusal_reasons"])

    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    exported = score["input_selection"]
    assert any(
        item["period_type"] == "r12" and item["current_refusal"]
        for item in exported["rejected_reports"]
    )
    assert any(retained["reason"] in item for item in score["missing_data"])
    assert json.loads(row["input_selection"]) == exported
    assert dcf[str(cid)]["selection"] == exported


@pytest.mark.parametrize("interior_revenue", [None, 0, -100])
def test_metric_spans_use_latest_contiguous_complete_suffix(interior_revenue):
    rows = [
        annual(2023, 100, operating_Income=10, free_Cash_Flow=10),
        annual(2024, 110, operating_Income=None, free_Cash_Flow=None),
        annual(2025, 121, operating_Income=36.3, free_Cash_Flow=-5),
        annual(2026, 145.2, operating_Income=14.52, free_Cash_Flow=10),
    ]
    rows[1]["revenues"] = interior_revenue
    conn, cid = setup(periods=rows)
    result = load_results_for_company(conn, cid, CUTOFF)
    financial = result["financial"]

    assert financial.revenue_growth == pytest.approx(0.2)
    assert financial.revenue_growth_years == 1
    assert financial.revenue_per_share_growth == pytest.approx(0.2)
    assert financial.revenue_per_share_growth_years == 1
    assert financial.net_income_growth_years == 3
    assert financial.share_count_growth_years == 3
    assert financial.positive_fcf_ratio == 0.5
    assert financial.operating_margin_volatility == pytest.approx(0.1)
    assert result["reverse_dcf"]["dcf"]["assumptions"]["revenue_growth"] == pytest.approx(0.15)
    assert any(
        "clamped from 0.2000" in warning for warning in result["reverse_dcf"]["dcf"]["warnings"]
    )


def test_each_per_share_growth_uses_its_actual_horizon(monkeypatch, tmp_path):
    rows = []
    for year in range(2023, 2027):
        revenue = 100 * 1.2 ** (year - 2023)
        rows.append(annual(year, revenue, ebit=revenue * 0.2))
    rows[0]["revenues"] = None
    conn, cid = setup(periods=rows)
    packet(conn, cid)

    financial = load_results_for_company(conn, cid, CUTOFF)["financial"]
    assert financial.revenue_per_share_growth_years == 2
    assert financial.ebit_per_share_growth_years == 3
    assert financial.net_income_per_share_growth_years == 3
    assert financial.fcf_per_share_growth_years == 3
    assert financial.book_value_per_share_growth_years == 3
    assert financial.share_count_growth_years == 3

    score, row, _ = rank_exports(conn, monkeypatch, tmp_path)
    assert score["revenue_per_share_growth_years"] == 2
    assert score["ebit_per_share_growth_years"] == 3
    assert score["fcf_per_share_growth_years"] == 3
    assert score["book_value_per_share_growth_years"] == 3
    assert row["revenue_per_share_growth_years"] == "2"
    assert row["ebit_per_share_growth_years"] == "3"
    assert row["fcf_per_share_growth_years"] == "3"
    assert row["book_value_per_share_growth_years"] == "3"
    assert any(
        "Revenue/share growth" in item and "(2y CAGR)" in item for item in score["positives"]
    )
    assert any("EBIT/share growth" in item and "(3y CAGR)" in item for item in score["positives"])


def test_moderate_growth_values_and_horizons_survive_actual_exports(monkeypatch, tmp_path):
    rows = []
    for year in range(2023, 2027):
        revenue = 100 * 1.05 ** (year - 2023)
        rows.append(annual(year, revenue, ebit=revenue * 0.2))
    conn, cid = setup(periods=rows)
    packet(conn, cid)

    score, row, _ = rank_exports(conn, monkeypatch, tmp_path)
    assert not any("growth" in item.lower() for item in score["positives"] + score["negatives"])
    for metric in (
        "revenue_growth",
        "ebit_growth",
        "net_income_growth",
        "revenue_per_share_growth",
        "ebit_per_share_growth",
        "net_income_per_share_growth",
        "fcf_per_share_growth",
        "book_value_per_share_growth",
    ):
        assert score[metric] == pytest.approx(0.05)
        assert score[f"{metric}_years"] == 3
        assert float(row[metric]) == pytest.approx(0.05)
        assert row[f"{metric}_years"] == "3"
    assert score["share_count_growth"] == 0
    assert score["share_count_growth_years"] == 3
    assert float(row["share_count_growth"]) == 0
    assert row["share_count_growth_years"] == "3"
