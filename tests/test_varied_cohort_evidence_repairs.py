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
from alphaforge.config import Settings
from alphaforge.core.kpi_taxonomy import KpiIds
from alphaforge.db.connection import get_connection
from alphaforge.db.evidence_repository import current_candidate_observations
from alphaforge.db.migrations import migrate
from alphaforge.db.repositories import (
    load_evidence_packet,
    upsert_company,
    upsert_kpi_observations,
    upsert_prices,
)
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

    from alphaforge.providers.mfn.scraper import MfnScraper

    scrape_details = MfnScraper.scrape_details

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
            # Use a non-calendar chronology and dated positive ROIC to exercise
            # the real DCF loader's available path.
            upsert_financial_periods(
                conn,
                company_id,
                [annual(year, revenue=100 * 1.1 ** (year - 2023)) for year in range(2023, 2027)],
            )
            upsert_kpi_observations(
                conn,
                company_id,
                KpiIds.ROIC,
                "year",
                "mean",
                [{"y": 2026, "p": 5, "v": 12.0, "observationDate": AS_OF}],
            )
            upsert_prices(conn, company_id, [{"d": AS_OF, "c": 10}], currency="SEK")
        conn.commit()
        entry = {
            "url": row["source_url"],
            "properties": {"lang": "en", "tags": row.get("feed_tags")},
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

        with monkeypatch.context() as acquisition:
            acquisition.setattr("requests.sessions.Session.request", unexpected_network)
            acquisition.setattr("alphaforge.providers.mfn.scraper.request_with_retry", transport)
            if row["input"].get("fiscal_period"):

                def with_provider_fiscal_period(scraper, *args, **kwargs):
                    return [
                        {**article, "fiscal_period": row["input"]["fiscal_period"]}
                        for article in scrape_details(scraper, *args, **kwargs)
                    ]

                acquisition.setattr(MfnScraper, "scrape_details", with_provider_fiscal_period)
            acquisition.setattr("alphaforge.providers.mfn.scraper.time.sleep", lambda *_: None)
            acquisition.setattr("alphaforge.evidence.flow.request_with_retry", download)
            exit_code = main(
                [
                    "--dsn",
                    dsn,
                    "evidence",
                    "--ticker",
                    ticker,
                    "--as-of",
                    AS_OF,
                    "--diagnostic",
                ]
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
def test_actual_fiscal_change_appends_new_interpretation_not_restamp(
    cli_lane, monkeypatch, capsys, row
):
    conn, run = cli_lane
    dsn = f"sqlite:///{conn.execute('PRAGMA database_list').fetchone()[2]}"
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
    assert old_diagnostic["status"] == "complete"
    old = current_candidate_observations(conn, company_id=company_id, as_of=AS_OF)[0]
    assert old["fiscal_period"] == row["old_period"]
    conn.execute(
        "INSERT INTO watchlist(company_id,ticker,source_file,source_row_hash) VALUES (?,?,?,?)",
        (company_id, row["ticker"], "fiscal-repair", "retained-financial-inputs"),
    )
    conn.commit()
    assert main(["--dsn", dsn, "rank", "--as-of", AS_OF]) == 0
    capsys.readouterr()
    old_run = conn.execute(
        "SELECT id,inputs_summary FROM ranking_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert main(["--dsn", dsn, "replay", "--run-id", str(old_run["id"])]) == 0
    old_replay = json.loads(capsys.readouterr().out)
    old_summary = json.loads(old_run["inputs_summary"])
    old_input_body = tuple(
        conn.execute(
            "SELECT financial_inputs_hash,body,rules FROM numerical_input_bodies "
            "WHERE numerical_identity=?",
            (old_summary["numerical_identity"],),
        ).fetchone()
    )
    old_output_body = tuple(
        conn.execute(
            "SELECT numerical_identity,textual_context,textual_context_hash,outputs,outputs_hash "
            "FROM executed_numerical_runs WHERE run_id=?",
            (old_run["id"],),
        ).fetchone()
    )
    old_text_hash = old_output_body[2]
    assert old_replay["outputs"]["dcf"][str(company_id)]["dcf"]["available"] is True
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
    # Acquisition above uses synthetic HTTP, but this is the native retained-run
    # replay command: it must consume the original immutable bodies after upgrade.
    assert main(["--dsn", dsn, "replay", "--run-id", str(old_run["id"])]) == 0
    post_upgrade_old_replay = json.loads(capsys.readouterr().out)
    assert post_upgrade_old_replay["outputs"] == old_replay["outputs"]
    assert main(["--dsn", dsn, "rank", "--as-of", AS_OF]) == 0
    capsys.readouterr()
    repaired_run = conn.execute(
        "SELECT id,inputs_summary FROM ranking_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert main(["--dsn", dsn, "replay", "--run-id", str(repaired_run["id"])]) == 0
    repaired_replay = json.loads(capsys.readouterr().out)
    repaired_summary = json.loads(repaired_run["inputs_summary"])
    repaired_text_hash = conn.execute(
        "SELECT textual_context_hash FROM executed_numerical_runs WHERE run_id=?",
        (repaired_run["id"],),
    ).fetchone()[0]
    old_outputs = old_replay["outputs"]
    repaired_outputs = repaired_replay["outputs"]
    assert repaired_text_hash != old_text_hash
    assert repaired_summary["financial_inputs_hash"] == old_summary["financial_inputs_hash"]
    assert repaired_summary["numerical_identity"] == old_summary["numerical_identity"]
    assert repaired_outputs["dcf"] == old_outputs["dcf"]
    assert repaired_outputs["metrics"] == old_outputs["metrics"]
    assert (
        repaired_outputs["scores"][0]["input_selection"]
        == old_outputs["scores"][0]["input_selection"]
    )
    assert (
        tuple(
            conn.execute(
                "SELECT financial_inputs_hash,body,rules FROM numerical_input_bodies "
                "WHERE numerical_identity=?",
                (old_summary["numerical_identity"],),
            ).fetchone()
        )
        == old_input_body
    )
    assert (
        tuple(
            conn.execute(
                "SELECT numerical_identity,textual_context,textual_context_hash,outputs,outputs_hash "
                "FROM executed_numerical_runs WHERE run_id=?",
                (old_run["id"],),
            ).fetchone()
        )
        == old_output_body
    )
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
    "historical_version,case,body,old_identity,current_period,current_basis",
    [
        (
            16,
            "conflicting-covered-years",
            "Annual and Sustainability Report for 2025 and for fiscal year 2024.",
            ("2024", "covered_report_heading", None),
            None,
            "conflicting_covered_headings",
        ),
        (
            17,
            "covered-year-before-outlook",
            "Annual and Sustainability Report for 2025, outlook for fiscal year 2026.",
            (None, "unresolved", "fiscal_identity_unresolved"),
            "2025",
            "covered_report_heading",
        ),
        (
            18,
            "forecast-before-heading",
            "Forecast Annual and Sustainability Report for fiscal year 2026.",
            ("2026", "covered_report_heading", None),
            None,
            "unresolved",
        ),
    ],
)
def test_retained_fiscal_interpretation_is_reclassified_without_mutating_history(
    cli_lane,
    monkeypatch,
    historical_version,
    case,
    body,
    old_identity,
    current_period,
    current_basis,
):
    conn, run = cli_lane
    row = {
        "ticker": "UPGRADE",
        "source_url": f"https://mfn.se/a/upgrade/{case}",
        "published_at": "2026-09-30T08:00:00Z",
        "body": body,
        "feed_tags": ["sub:report", "sub:report:annual"],
        "input": {"title": "Annual and Sustainability Report"},
    }
    current_report_rules_inputs = report_rules.report_rules_inputs

    def historical_report_rules_inputs():
        inputs = current_report_rules_inputs()
        inputs["version"] = historical_version
        fiscal_rules = inputs["fiscal_interpretation"]
        if historical_version <= 17:
            fiscal_rules.pop("non_covered_context_guard")
            fiscal_rules["forecast_context_guard"] = [
                "forecast",
                "forecasts",
                "forecasting",
            ]
        else:
            fiscal_rules["non_covered_context_guard"]["covered_cue_context"] = (
                "comma-or-semicolon-delimited local segment"
            )
        if historical_version == 16:
            guard = fiscal_rules["compound_annual_publication_year_guard"]
            generic_cue = guard.pop("generic_covered_year_cue")
            guard["covered_year_before_publication_cue"] = generic_cue["pattern"]
        return inputs

    with monkeypatch.context() as historical:
        historical.setattr(report_rules, "REPORT_RULES_VERSION", historical_version)
        historical.setattr(
            report_rules, "report_rules_inputs", historical_report_rules_inputs
        )
        historical.setattr(
            ingest,
            "resolve_fiscal_identity",
            lambda _doc: old_identity,
        )
        old_code, old_diagnostic, company_id = run(row)
        old_packet = load_evidence_packet(conn, company_id, AS_OF)
    assert old_code == 0 and old_diagnostic["status"] == "complete"
    assert old_packet["report_rules"]["version"] == historical_version
    old_fingerprint = old_packet["report_rules"]["fingerprint"]
    old_observation = current_candidate_observations(
        conn, company_id=company_id, as_of=AS_OF
    )[0]
    assert old_observation["fiscal_period"] == old_identity[0]
    assert old_observation["report_rules_fingerprint"] == old_fingerprint
    old_observations = list(
        conn.execute("SELECT * FROM evidence_candidate_observations ORDER BY id")
    )
    old_manifests = list(conn.execute("SELECT * FROM evidence_selection_manifests ORDER BY id"))
    old_packets = list(conn.execute("SELECT * FROM evidence_packets ORDER BY id"))

    code, diagnostic, _ = run(row, allow_pdf=False)
    assert code == 0 and diagnostic["status"] == "complete"
    assert diagnostic["pdf_fetch_attempts"] == 0
    current_packet = load_evidence_packet(conn, company_id, AS_OF)
    assert current_packet["report_rules"]["version"] == 19
    assert current_packet["report_rules"]["fingerprint"] != old_fingerprint
    assert current_packet["sources"][0]["fiscal_period"] == current_period
    assert current_packet["sources"][0]["fiscal_period_source"] == current_basis
    current_observation = current_candidate_observations(
        conn, company_id=company_id, as_of=AS_OF
    )[0]
    assert current_observation["candidate_observation_id"] != old_observation[
        "candidate_observation_id"
    ]
    assert current_observation["extraction_id"] == old_observation["extraction_id"]
    assert (
        current_observation["report_rules_fingerprint"]
        == current_packet["report_rules"]["fingerprint"]
    )
    assert load_evidence_packet(
        conn,
        company_id,
        AS_OF,
        current_rules_fingerprint=old_fingerprint,
    ) == old_packet
    assert list(conn.execute("SELECT * FROM evidence_candidate_observations ORDER BY id"))[
        : len(old_observations)
    ] == old_observations
    assert list(conn.execute("SELECT * FROM evidence_selection_manifests ORDER BY id"))[
        : len(old_manifests)
    ] == old_manifests
    assert list(conn.execute("SELECT * FROM evidence_packets ORDER BY id"))[
        : len(old_packets)
    ] == old_packets


@pytest.mark.parametrize(
    "marker",
    [
        "forecast",
        "forecasts",
        "forecasting",
        "outlook",
        "compared",
        "comparison",
        "previous",
        "prognos",
        "föregående",
        "jämfört",
        "jämförelse",
    ],
)
@pytest.mark.parametrize(
    "body_template",
    [
        "Annual and Sustainability Report for 2025, {marker} for fiscal year 2026.",
        "Annual and Sustainability Report {marker} for fiscal year 2026, for 2025.",
    ],
)
def test_non_covered_cue_preserves_covered_year(cli_lane, marker, body_template):
    conn, run = cli_lane
    row = {
        "ticker": "GUARD",
        "source_url": "https://mfn.se/a/guard/non-covered-cue",
        "published_at": "2026-09-30T08:00:00Z",
        "body": body_template.format(marker=marker),
        "feed_tags": ["sub:report", "sub:report:annual"],
        "input": {"title": "Annual and Sustainability Report"},
    }
    code, diagnostic, company_id = run(row)
    assert code == 0 and diagnostic["status"] == "complete"
    source = load_evidence_packet(conn, company_id, AS_OF)["sources"][0]
    assert source["fiscal_period"] == "2025"
    assert source["fiscal_period_source"] == "covered_report_heading"


@pytest.mark.parametrize(
    "marker",
    [
        "forecast",
        "forecasts",
        "forecasting",
        "outlook",
        "compared",
        "comparison",
        "previous",
        "prognos",
        "föregående",
        "jämfört",
        "jämförelse",
    ],
)
@pytest.mark.parametrize(
    "body_template,expected,basis,limitation",
    [
        (
            "{marker} Annual and Sustainability Report for fiscal year 2026.",
            None,
            "unresolved",
            "fiscal_identity_unresolved",
        ),
        (
            "{marker}, Annual and Sustainability Report for fiscal year 2026.",
            "2026",
            "covered_report_heading",
            None,
        ),
    ],
)
def test_non_covered_sentence_prefix_respects_local_delimiter(
    cli_lane, marker, body_template, expected, basis, limitation
):
    conn, run = cli_lane
    row = {
        "ticker": "GUARD",
        "source_url": "https://mfn.se/a/guard/non-covered-prefix",
        "published_at": "2026-09-30T08:00:00Z",
        "body": body_template.format(marker=marker),
        "feed_tags": ["sub:report", "sub:report:annual"],
        "input": {"title": "Annual and Sustainability Report"},
    }
    code, diagnostic, company_id = run(row)
    assert code == 0 and diagnostic["status"] == "complete"
    source = load_evidence_packet(conn, company_id, AS_OF)["sources"][0]
    assert source["fiscal_period"] == expected
    assert source["fiscal_period_source"] == basis
    if limitation:
        assert limitation in diagnostic["limitations"]


@pytest.mark.parametrize("verb", ["forecasts", "forecasting"])
@pytest.mark.parametrize(
    "title,body,expected,basis",
    [
        ("Annual and Sustainability Report {verb} 2027", "", None, "unresolved"),
        (
            "Annual Report publication",
            "Annual and Sustainability Report {verb} 2027.",
            None,
            "unresolved",
        ),
        (
            "Annual and Sustainability Report 2025 {verb} 2027",
            "",
            "2025",
            "report_title",
        ),
        (
            "Annual Report publication",
            "Annual and Sustainability Report for 2025. The company {verb} 2027 growth.",
            "2025",
            "covered_report_heading",
        ),
    ],
)
def test_forecast_inflections_through_public_evidence_cli(
    cli_lane, verb, title, body, expected, basis
):
    conn, run = cli_lane
    row = {
        "ticker": "GUARD",
        "source_url": "https://mfn.se/a/guard/forecast-inflection",
        "published_at": "2026-09-30T08:00:00Z",
        "body": body.format(verb=verb),
        "feed_tags": ["sub:report", "sub:report:annual"],
        "input": {"title": title.format(verb=verb)},
    }
    code, diagnostic, company_id = run(row)
    assert code == 0 and diagnostic["status"] == "complete"
    source = load_evidence_packet(conn, company_id, AS_OF)["sources"][0]
    assert source["fiscal_period"] == expected
    assert source["fiscal_period_source"] == basis
    if expected is None:
        assert "fiscal_identity_unresolved" in diagnostic["limitations"]


@pytest.mark.parametrize(
    "case,title,body,feed_tags,provider_period,expected,basis,limitation",
    [
        (
            "comparator",
            "Annual and Sustainability Report 2025 compared with 2024",
            "The report covers financial year 2025.",
            ["sub:report", "sub:report:annual"],
            None,
            "2025",
            "report_title",
            None,
        ),
        (
            "forecast",
            "Annual and Sustainability Report forecast 2027",
            "The forecast concerns 2027.",
            ["sub:report", "sub:report:annual"],
            None,
            None,
            "unresolved",
            "fiscal_identity_unresolved",
        ),
        (
            "provider-conflict",
            "Annual and Sustainability Report 2025",
            "The report covers financial year 2025.",
            ["sub:report", "sub:report:annual"],
            "2024",
            None,
            "conflicting_provider_title",
            "fiscal_identity_ambiguous",
        ),
        (
            "contradictory-headings",
            "Annual Report publication",
            "Annual and Sustainability Report 2025. Annual and Sustainability Report 2024.",
            ["sub:report", "sub:report:annual"],
            None,
            None,
            "conflicting_covered_headings",
            "fiscal_identity_ambiguous",
        ),
        (
            "published-year-heading",
            "Annual and Sustainability Report",
            "Annual and Sustainability Report published on 19 March 2026. The report covers fiscal year 2025.",
            ["sub:report", "sub:report:annual"],
            None,
            None,
            "unresolved",
            "fiscal_identity_unresolved",
        ),
        (
            "publication-year-heading",
            "Annual and Sustainability Report",
            "Annual and Sustainability Report publication 19 March 2026.",
            ["sub:report", "sub:report:annual"],
            None,
            None,
            "unresolved",
            "fiscal_identity_unresolved",
        ),
        (
            "publication-marker-after-date",
            "Annual and Sustainability Report",
            "Annual and Sustainability Report 19 March 2026 publication.",
            ["sub:report", "sub:report:annual"],
            None,
            None,
            "unresolved",
            "fiscal_identity_unresolved",
        ),
        (
            "ordinary-annual-publication-label",
            "Annual Report publication",
            "Annual Report published on 19 March 2026.",
            ["sub:report", "sub:report:annual"],
            None,
            "2026",
            "covered_report_heading",
            None,
        ),
        (
            "interim-publication-label",
            "Interim Report publication",
            "Interim report published 30 April 2026.",
            ["sub:report", "sub:report:interim:q1"],
            None,
            "2026",
            "covered_report_heading",
            None,
        ),
        (
            "publication-marked-covered-fiscal-year",
            "Annual and Sustainability Report",
            "Annual and Sustainability Report publication for fiscal year 2025.",
            ["sub:report", "sub:report:annual"],
            None,
            "2025",
            "covered_report_heading",
            None,
        ),
        (
            "publication-year-before-covered-fiscal-year",
            "Annual and Sustainability Report",
            "Annual and Sustainability Report published 19 March 2026 for fiscal year 2025.",
            ["sub:report", "sub:report:annual"],
            None,
            "2025",
            "covered_report_heading",
            None,
        ),
        (
            "conflicting-covered-years-without-publication",
            "Annual and Sustainability Report",
            "Annual and Sustainability Report for 2025 and for fiscal year 2024.",
            ["sub:report", "sub:report:annual"],
            None,
            None,
            "conflicting_covered_headings",
            "fiscal_identity_ambiguous",
        ),
        (
            "conflicting-generic-covered-years-without-publication",
            "Annual and Sustainability Report",
            "Annual and Sustainability Report for 2025 and for 2024.",
            ["sub:report", "sub:report:annual"],
            None,
            None,
            "conflicting_covered_headings",
            "fiscal_identity_ambiguous",
        ),
        (
            "matching-covered-years-without-publication",
            "Annual and Sustainability Report",
            "Annual and Sustainability Report for 2025 and for fiscal year 2025.",
            ["sub:report", "sub:report:annual"],
            None,
            "2025",
            "covered_report_heading",
            None,
        ),
        (
            "conflicting-published-covered-years",
            "Annual and Sustainability Report",
            "Annual and Sustainability Report for 2025, published 19 March 2026 for fiscal year 2024.",
            ["sub:report", "sub:report:annual"],
            None,
            None,
            "conflicting_covered_headings",
            "fiscal_identity_ambiguous",
        ),
        (
            "matching-covered-years-around-publication",
            "Annual and Sustainability Report",
            "Annual and Sustainability Report for 2025, published 19 March 2026 for fiscal year 2025.",
            ["sub:report", "sub:report:annual"],
            None,
            "2025",
            "covered_report_heading",
            None,
        ),
        (
            "unbound-fiscal-year-cue",
            "Annual and Sustainability Report",
            "Annual and Sustainability Report for fiscal year ended 31 December; published 19 March 2026.",
            ["sub:report", "sub:report:annual"],
            None,
            None,
            "unresolved",
            "fiscal_identity_unresolved",
        ),
        (
            "covered-year-before-publication-date",
            "Annual and Sustainability Report",
            "Annual and Sustainability Report for 2025, published on 19 March 2026.",
            ["sub:report", "sub:report:annual"],
            None,
            "2025",
            "covered_report_heading",
            None,
        ),
        (
            "covered-heading",
            "Annual Report publication",
            "Annual and Sustainability Report for 2025.",
            ["sub:report", "sub:report:annual"],
            None,
            "2025",
            "covered_report_heading",
            None,
        ),
        (
            "misleading-title-q4",
            "Annual Report 2025, Q4 2025",
            "The annual report covers 2025.",
            ["sub:report", "sub:report:annual"],
            None,
            "2025",
            "report_title",
            None,
        ),
        (
            "misleading-body-q4",
            "Annual and Sustainability Report 2025",
            "Fourth quarter 2025 highlights are included in this annual report.",
            ["sub:report", "sub:report:annual"],
            None,
            "2025",
            "report_title",
            None,
        ),
        (
            "year-end-comparator",
            "Year-end Report 2025 compared with Q1 2024",
            "Fourth quarter 2025.",
            ["sub:report", "sub:report:interim:q4"],
            None,
            "2025/2025-q4",
            "report_title",
            None,
        ),
    ],
)
def test_compound_and_year_end_guards_through_public_evidence_cli(
    cli_lane,
    case,
    title,
    body,
    feed_tags,
    provider_period,
    expected,
    basis,
    limitation,
):
    conn, run = cli_lane
    row = {
        "ticker": "GUARD",
        "source_url": f"https://mfn.se/a/guard/{case}",
        "published_at": "2026-09-30T08:00:00Z",
        "body": body,
        "feed_tags": feed_tags,
        "input": {"title": title, "fiscal_period": provider_period},
    }
    code, diagnostic, company_id = run(row)
    assert code == 0 and diagnostic["status"] == "complete"
    source = load_evidence_packet(conn, company_id, AS_OF)["sources"][0]
    assert source["fiscal_period"] == expected
    assert source["fiscal_period_source"] == basis
    if limitation is not None:
        assert limitation in diagnostic["limitations"]
