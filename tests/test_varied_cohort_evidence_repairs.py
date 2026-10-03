"""Actual Swedish cohort fiscal inputs through public CLI and immutable replay.

Only HTTP is replaced. Feed/title/body excerpts are observed; detail wrappers,
authoritative timestamp markup and PDF bytes are synthetic (fixture README).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from test_method_date_growth_selection import annual, upsert_financial_periods
from test_post26_evidence_repairs import pdf

from alphaforge.cli.main import main
from alphaforge.cli.ranking_loader import load_results_for_company
from alphaforge.config import Settings
from alphaforge.db.connection import get_connection
from alphaforge.db.evidence_repository import current_candidate_observations
from alphaforge.db.migrations import migrate
from alphaforge.db.repositories import load_evidence_packet, upsert_company, upsert_prices
from alphaforge.evidence import ingest, report_rules
from alphaforge.evidence.ingest import resolve_fiscal_identity

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/mfn/varied_cohort_repairs.json").read_text()
)
AS_OF = "2026-10-03"


@pytest.fixture
def cli_lane(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALPHAFORGE_JEV_SHADOW_ENABLED", "false")
    dsn = f"sqlite:///{tmp_path / 'live.db'}"
    conn = get_connection(Settings.from_env(dsn=dsn))
    migrate(conn)

    def run(row, *, allow_pdf=True):
        ticker = row["ticker"]
        slug = urlsplit(row["source_url"]).path.split("/a/", 1)[1].split("/")[0]
        company_id = upsert_company(
            conn,
            {
                "insId": 424,
                "name": ticker,
                "ticker": ticker,
                "stockPriceCurrency": "SEK",
                "reportCurrency": "SEK",
            },
        )
        if not conn.execute("SELECT 1 FROM mfn_issuer_mappings").fetchone():
            conn.execute(
                "INSERT INTO mfn_issuer_mappings (company_id,mfn_slug,source_url,status,"
                "discovery_source,verified_at,identity_evidence) "
                "VALUES (?,?,?,'mapped','fixture',?,?)",
                (
                    company_id,
                    f"all/a/{slug}",
                    f"https://mfn.se/all/a/{slug}",
                    "2026-10-03T00:00:00Z",
                    '{"provenance":"fixture","reason":"exact retained issuer fixture"}',
                ),
            )
            # Synthetic numerical controls, not claimed issuer financial facts.
            # Use a non-calendar chronology to exercise the real DCF loader.
            upsert_financial_periods(
                conn,
                company_id,
                [annual(year, revenue=100 * 1.1 ** (year - 2023)) for year in range(2023, 2027)],
            )
            upsert_prices(conn, company_id, [{"d": AS_OF, "c": 10}], currency="SEK")
        conn.commit()
        entry = {
            "url": row["source_url"],
            "properties": {"lang": "en", "tags": None},
            "content": {"title": row["input"]["title"], "publish_date": row["published_at"]},
        }
        path = urlsplit(row["source_url"]).path.split("/a/", 1)[1]
        html = (
            f'<meta property="article:published_time" content="{row["published_at"]}">'
            f'<link rel="canonical" href="https://mfn.se/all/a/{path}">'
            f'<h1>{row["input"]["title"]}</h1><div class="release-body"><p>{row["body"]}</p>'
            '<a class="mfn-primary" href="https://storage.mfn.se/fixture/report.pdf">Report</a></div>'
        )

        def transport(_method, url, **_kwargs):
            if "?offset=" in url:
                return SimpleNamespace(
                    status_code=200,
                    headers={"Content-Type": "application/json"},
                    text=json.dumps({"items": [entry]}),
                )
            assert url == row["source_url"], f"unexpected retrieval: {url}"
            return SimpleNamespace(status_code=200, text=html)

        def download(_method, url, **_kwargs):
            assert allow_pdf, f"retained replay attempted PDF fetch: {url}"
            assert url == "https://storage.mfn.se/fixture/report.pdf"
            return SimpleNamespace(
                status_code=200, headers={"Content-Type": "application/pdf"}, content=pdf()
            )

        def unexpected_network(*_args, **_kwargs):
            pytest.fail("unexpected issuer discovery or paid-model network request")

        monkeypatch.setattr("requests.sessions.Session.request", unexpected_network)
        monkeypatch.setattr("alphaforge.providers.mfn.scraper.request_with_retry", transport)
        monkeypatch.setattr("alphaforge.providers.mfn.scraper.time.sleep", lambda *_: None)
        monkeypatch.setattr("alphaforge.evidence.flow.request_with_retry", download)
        exit_code = main(
            ["--dsn", dsn, "evidence", "--ticker", ticker, "--as-of", AS_OF, "--diagnostic"]
        )
        diagnostic = json.loads(capsys.readouterr().out)
        return exit_code, diagnostic, company_id

    return conn, run


@pytest.mark.parametrize("row", [*FIXTURE["fiscal"], *FIXTURE["controls"]])
def test_actual_fiscal_inputs_and_known_working_paths(row):
    assert resolve_fiscal_identity(row["input"]) == (row["expected"], "report_title", None)


@pytest.mark.parametrize("row", [*FIXTURE["fiscal"], *FIXTURE["controls"]])
def test_actual_swedish_reproductions_through_public_cli(cli_lane, row):
    conn, run = cli_lane
    code, diagnostic, company_id = run(row)
    assert code == 0 and diagnostic["status"] == "complete"
    assert diagnostic["pdf_fetch_attempts"] == 1
    packet = load_evidence_packet(conn, company_id, AS_OF)
    assert packet["sources"][0]["source_url"] == row["source_url"]
    assert packet["sources"][0]["fiscal_period"] == row["expected"]
    assert packet["sources"][0]["fiscal_period_source"] == "report_title"
    old_rows = list(conn.execute("SELECT * FROM evidence_candidate_observations"))
    old_manifests = list(conn.execute("SELECT * FROM evidence_selection_manifests"))
    replay_code, replay, _ = run(row, allow_pdf=False)
    assert replay_code == 0 and replay["status"] == "complete"
    assert replay["pdf_fetch_attempts"] == 0
    assert load_evidence_packet(conn, company_id, AS_OF)["packet_hash"] == packet["packet_hash"]
    assert list(conn.execute("SELECT * FROM evidence_candidate_observations")) == old_rows
    assert list(conn.execute("SELECT * FROM evidence_selection_manifests")) == old_manifests


@pytest.mark.parametrize(
    "row", [row for row in FIXTURE["fiscal"] if row["old_period"] != row["expected"]]
)
def test_actual_fiscal_change_appends_new_interpretation_not_restamp(cli_lane, monkeypatch, row):
    conn, run = cli_lane
    # Pin the scout's old resolver output, not the new code with an old stamp.
    # All persistence, extraction, CLI and manifest code remains real.
    with monkeypatch.context() as historical:
        historical.setattr(report_rules, "REPORT_RULES_VERSION", 11)
        historical.setattr(
            ingest,
            "resolve_fiscal_identity",
            lambda _doc: (
                row["old_period"],
                "report_title" if row["old_period"] else "unresolved",
                None if row["old_period"] else "fiscal_identity_unresolved",
            ),
        )
        _, old_diagnostic, company_id = run(row)
        old_hash = load_evidence_packet(conn, company_id, AS_OF)["packet_hash"]
        old_numbers = load_results_for_company(conn, company_id, AS_OF)
        old_dcf = old_numbers["reverse_dcf"]
        assert old_dcf["dcf"]["available"] is True
    assert old_diagnostic["status"] == "complete"
    old = current_candidate_observations(conn, company_id=company_id, as_of=AS_OF)[0]
    manifests = list(
        conn.execute("SELECT manifest_id, manifest_json FROM evidence_selection_manifests")
    )
    old_rows = list(conn.execute("SELECT * FROM evidence_candidate_observations ORDER BY id"))
    code, diagnostic, _ = run(row, allow_pdf=False)
    assert code == 0 and diagnostic["status"] == "complete"
    assert diagnostic["pdf_fetch_attempts"] == 0
    current = current_candidate_observations(conn, company_id=company_id, as_of=AS_OF)[0]
    assert current["fiscal_period"] == row["expected"]
    assert current["candidate_observation_id"] != old["candidate_observation_id"]
    assert current["report_rules_fingerprint"] != old["report_rules_fingerprint"]
    assert current["extraction_id"] == old["extraction_id"]
    assert load_evidence_packet(conn, company_id, AS_OF)["packet_hash"] != old_hash
    repaired_numbers = load_results_for_company(conn, company_id, AS_OF)
    assert repaired_numbers["reverse_dcf"] == old_dcf
    assert repaired_numbers["selection"] == old_numbers["selection"]
    assert repaired_numbers["financial"] == old_numbers["financial"]
    assert (
        list(conn.execute("SELECT * FROM evidence_candidate_observations ORDER BY id"))[
            : len(old_rows)
        ]
        == old_rows
    )
    for manifest_id, manifest_json in manifests:
        assert (
            conn.execute(
                "SELECT manifest_json FROM evidence_selection_manifests WHERE manifest_id=?",
                (manifest_id,),
            ).fetchone()[0]
            == manifest_json
        )


@pytest.mark.parametrize(
    "doc,expected",
    [
        (
            {
                "title": "Annual and Sustainability Report 2025 compared with 2024",
                "report_kind": "annual",
            },
            "2025",
        ),
        (
            {"title": "Annual and Sustainability Report forecast 2027", "report_kind": "annual"},
            None,
        ),
        ({"title": "Annual and Sustainability Report 2025", "fiscal_period": "2024"}, None),
        (
            {
                "title": "Report publication",
                "body": "Annual and Sustainability Report 2025. Annual and Sustainability Report 2024.",
                "report_kind": "annual",
            },
            None,
        ),
        (
            {
                "title": "Report publication",
                "body": "Annual and Sustainability Report for 2025.",
                "report_kind": "annual",
            },
            "2025",
        ),
        (
            {
                "title": "Annual Report 2025, Q4 2025",
                "document_type": "ANNUAL_REPORT",
                "report_kind": "annual",
            },
            "2025",
        ),
        (
            {"title": "Year-end Report 2025 compared with Q1 2024", "report_kind": "annual"},
            "2025/2025-q4",
        ),
    ],
)
def test_compound_and_year_end_retain_comparator_forecast_conflict_guards(doc, expected):
    assert resolve_fiscal_identity(doc)[0] == expected
