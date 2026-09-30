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

    upsert_kpi_observations(
        conn, cid, 37, "year", "mean", [observation(30)]
    )
    assert load_results_for_company(conn, cid, CUTOFF)["fundamental_kpis"][37] == 30

    upsert_kpi_observations(
        conn, cid, 37, "year", "mean", [observation(40, observed=False)]
    )
    stored = conn.execute(
        "SELECT value, observation_date FROM kpi_observations WHERE kpi_id=37"
    ).fetchone()
    assert tuple(stored) == (40, None)
    result = load_results_for_company(conn, cid, CUTOFF)
    assert 37 not in result["fundamental_kpis"]
    assert result["selection"]["rejected_kpis"][0]["reason"] == "KPI observation date unverified"

    upsert_kpi_observations(
        conn, cid, 37, "year", "mean", [observation(50)]
    )
    assert load_results_for_company(conn, cid, CUTOFF)["fundamental_kpis"][37] == 50


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
        for row in conn.execute(
            "SELECT * FROM financial_periods ORDER BY period_end"
        ).fetchall()
    ]
    rows[0]["report_year"] = "not-a-year"
    selected, reasons, excluded = _annual_series(rows)
    assert [row["report_year"] for row in selected] == [2026]
    assert reasons == ["annual fiscal-year metadata unverified"]
    assert excluded[0]["reason"] == "annual fiscal-year metadata unverified"


@pytest.mark.parametrize("age,count", [(0, 1), (7, 1), (8, 0)])
def test_historical_price_pairing_boundary(age, count):
    conn, cid = setup(periods=[annual(2025), annual(2026)])
    end = date(2025, 3, 31)
    upsert_prices(
        conn, cid, [{"d": (end - timedelta(days=age)).isoformat(), "c": 10}], currency="SEK"
    )
    # A price after period end is never paired, however close.
    upsert_prices(conn, cid, [{"d": "2025-04-01", "c": 999}], currency="SEK")
    loaded = load_results_for_company(conn, cid, CUTOFF)
    assert loaded["valuation"].ev_ebit_history_count == count
    if count:
        assert loaded["valuation"].ev_ebit_guardrail_low is None


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
    "publication,admitted",
    [
        (CUTOFF, True),
        ("2026-06-02", False),
        (None, False),
        ("2026", False),
        ("2026-02-30", False),
        ("2026-03-30", False),
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
    assert RankingEngine.RANKING_MODEL_VERSION == "2026-09-30-growth-horizons-v13"


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


def test_actual_rank_excludes_undated_kpi_and_exports_provisional_roic(monkeypatch, tmp_path):
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
    assert roic["raw_value"] is None and not roic["available"]
    assert "roic" in row["missing_data"]
    assert "roic" in dcf[str(cid)]["dcf"]["missing_information"]
    assert dcf[str(cid)]["dcf"]["assumptions"]["net_reinvestment_rate"] == 0
    assert score["readiness_status"] == "ready"  # Explicit provisional ROIC remains supported.
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


def test_all_selection_refusals_retain_provenance_and_reach_exports(
    monkeypatch, tmp_path
):
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
    assert selection["historical_price_pairings"][0]["period_end"] == "2024-03-31"

    score, row, dcf = rank_exports(conn, monkeypatch, tmp_path)
    reasons = score["missing_data"]
    assert any("fiscal end unavailable or invalid" in reason for reason in reasons)
    assert any("KPI observation date unverified" in reason for reason in reasons)
    assert any("historical price missing" in reason for reason in reasons)
    assert all(
        any(reason in limitation for limitation in score["readiness_limitations"])
        for reason in selection["refusal_reasons"]
    )
    assert json.loads(row["input_selection"]) == score["input_selection"]
    assert dcf[str(cid)]["selection"] == score["input_selection"]


def test_annual_rejections_block_only_while_unresolved_and_applicable(
    monkeypatch, tmp_path
):
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


def test_same_year_distinct_fiscal_end_rejection_blocks_rank_exports(
    monkeypatch, tmp_path
):
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
        if item["source"] == "financial_periods"
        and item["period_end"] == "2026-04-30"
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
        item
        for item in exported["rejected_reports"]
        if item["source"] == "ingestion_rejection"
    )
    assert not retained_export["current_refusal"]
    assert not any("fiscal end unavailable or invalid" in item for item in score["missing_data"])
    assert json.loads(row["input_selection"]) == exported
    assert dcf[str(cid)]["selection"] == exported


@pytest.mark.parametrize("case", ["known_end_conflict", "cross_type", "unknown_slot"])
def test_nonannual_rejection_conflicts_remain_current_in_rank_exports(
    case, monkeypatch, tmp_path
):
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
    assert any(
        retained["reason"] in reason
        for reason in loaded["selection"]["refusal_reasons"]
    )

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
    assert result["reverse_dcf"]["dcf"]["assumptions"]["revenue_growth"] == pytest.approx(
        0.15
    )
    assert any(
        "clamped from 0.2000" in warning
        for warning in result["reverse_dcf"]["dcf"]["warnings"]
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
        "Revenue/share growth" in item and "(2y CAGR)" in item
        for item in score["positives"]
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
