from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_evidence_flow import _FakeScraper, _mapped_company, _pdf

from alphaforge.cli.main import build_parser
from alphaforge.cli.ranking_loader import load_results_for_company
from alphaforge.db.connection import get_connection, init_db
from alphaforge.db.numerical_runs import capture_inputs, replay_run
from alphaforge.evidence.artifact_store import (
    ArtifactChecksumMismatchError,
    ArtifactUnavailableError,
    LocalPdfArtifactStore,
)
from alphaforge.evidence.flow import OneCompanyEvidenceFlow


@dataclass
class Company:
    id: int
    ticker: str
    name: str
    branch_id: int | None = None


@pytest.fixture
def saved_evidence(tmp_path, monkeypatch):
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    monkeypatch.chdir(snapshot)
    database = snapshot / "saved.db"
    conn = get_connection(path=database)
    init_db(conn)
    company_id = _mapped_company(conn)
    conn.execute(
        "INSERT INTO watchlist(company_id,ticker,source_file,source_row_hash) VALUES (?,?,?,?)",
        (company_id, "FLOW", "fixture", "fixture"),
    )
    # Relative roots bind at construction, not at a subsequent read.
    store = LocalPdfArtifactStore("pdf-objects")
    article = {
        "source_url": "https://mfn.test/a/flow/q2",
        "title": "Flow AB Interim Report Q2 2026",
        "published_at": "2026-07-15T08:00:00Z",
        "report_kind": "quarterly",
        "attachment_url": "https://storage.mfn.test/flow/q2.pdf",
        "attachment_tier": "mfn-primary",
        "lang": "en",
    }
    content = _pdf()
    response = SimpleNamespace(
        status_code=200, headers={"Content-Type": "application/pdf"}, content=content
    )
    with patch("alphaforge.evidence.flow.request_with_retry", return_value=response):
        result = OneCompanyEvidenceFlow(
            conn,
            scraper=_FakeScraper([article]),
            artifact_store=store,
            now=lambda: datetime(2026, 9, 20, tzinfo=UTC),
        ).run(company_id, as_of="2026-09-20")
    assert result.status == "complete"
    conn.commit()
    conn.close()
    yield database, store, company_id, content


def test_saved_evidence_public_loading_and_rank_are_cwd_independent(
    saved_evidence, tmp_path, monkeypatch
):
    database, store, company_id, content = saved_evidence
    expected = None
    expected_capture = None
    outputs = None
    parser = build_parser()
    for name in ("launch-a", "launch-b"):
        cwd = tmp_path / name
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        conn = get_connection(path=database)
        try:
            with patch(
                "alphaforge.evidence.flow.request_with_retry",
                side_effect=AssertionError("saved reads must not download"),
            ):
                results = load_results_for_company(
                    conn, company_id, "2026-09-20", artifact_store=store
                )
                research = results["research_evidence"]
                assert research["evidence_packet"]["sources"]
                if expected is None:
                    expected = research
                assert research == expected
                companies = [Company(id=company_id, ticker="FLOW", name="Flow AB")]
                captured = capture_inputs(conn, companies, "2026-09-20", artifact_store=store)
                if expected_capture is None:
                    expected_capture = captured
                assert captured == expected_capture
                # The omitted legacy default still refuses; no directory search.
                with pytest.raises(ArtifactUnavailableError):
                    load_results_for_company(conn, company_id, "2026-09-20")
                args = parser.parse_args(
                    [
                        "--dsn",
                        f"sqlite:///{database}",
                        "--evidence-root",
                        str(store.root),
                        "rank",
                        "--as-of",
                        "2026-09-20",
                    ]
                )
                assert args.func(args) == 0
                run_id = conn.execute("SELECT max(run_id) FROM executed_numerical_runs").fetchone()[
                    0
                ]
                replay = replay_run(conn, run_id)
                if outputs is None:
                    outputs = replay["outputs"]
                assert replay["outputs"] == outputs
                sha = research["evidence_packet"]["sources"][0]["attachment"]["sha256"]
                assert store.read_pdf(sha, expected_size=len(content)) == content
        finally:
            conn.close()


@pytest.mark.parametrize("root_args", [[], ["--evidence-root", ""]])
def test_cli_empty_and_omitted_roots_share_acquisition_and_loading_store(
    tmp_path, monkeypatch, root_args
):
    monkeypatch.chdir(tmp_path)
    database = tmp_path / "saved.db"
    conn = get_connection(path=database)
    init_db(conn)
    company_id = _mapped_company(conn)
    conn.execute(
        "INSERT INTO watchlist(company_id,ticker,source_file,source_row_hash) VALUES (?,?,?,?)",
        (company_id, "FLOW", "fixture", "fixture"),
    )
    conn.commit()
    conn.close()
    article = {
        "source_url": "https://mfn.test/a/flow/q2",
        "title": "Flow AB Interim Report Q2 2026",
        "published_at": "2026-07-15T08:00:00Z",
        "report_kind": "quarterly",
        "attachment_url": "https://storage.mfn.test/flow/q2.pdf",
        "attachment_tier": "mfn-primary",
        "lang": "en",
    }
    content = _pdf()
    response = SimpleNamespace(
        status_code=200, headers={"Content-Type": "application/pdf"}, content=content
    )

    def fixture_flow(conn, **kwargs):
        return OneCompanyEvidenceFlow(
            conn,
            scraper=_FakeScraper([article]),
            now=lambda: datetime(2026, 9, 20, tzinfo=UTC),
            **kwargs,
        )

    monkeypatch.setattr("alphaforge.evidence.flow.OneCompanyEvidenceFlow", fixture_flow)
    parser = build_parser()
    common = ["--dsn", f"sqlite:///{database}", *root_args]
    evidence_args = parser.parse_args(
        [
            *common,
            "evidence",
            "--company-id",
            str(company_id),
            "--as-of",
            "2026-09-20",
        ]
    )
    with patch("alphaforge.evidence.flow.request_with_retry", return_value=response):
        assert evidence_args.func(evidence_args) == 0
    rank_args = parser.parse_args([*common, "rank", "--as-of", "2026-09-20"])
    with patch(
        "alphaforge.evidence.flow.request_with_retry",
        side_effect=AssertionError("rank must read retained bytes without downloads"),
    ):
        assert rank_args.func(rank_args) == 0
    conn = get_connection(path=database)
    try:
        sha = conn.execute("SELECT sha256 FROM evidence_artifacts").fetchone()[0]
        assert LocalPdfArtifactStore().read_pdf(sha, expected_size=len(content)) == content
        assert conn.execute("SELECT count(*) FROM executed_numerical_runs").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize("damage", ["missing", "hash", "size"])
def test_explicit_root_preserves_retained_object_refusals(
    saved_evidence, tmp_path, monkeypatch, damage
):
    database, store, company_id, content = saved_evidence
    import hashlib

    sha = hashlib.sha256(content).hexdigest()
    path = store.root / "sha256" / sha[:2] / f"{sha}.pdf"
    if damage == "missing":
        path.unlink()
        error = ArtifactUnavailableError
    else:
        path.write_bytes(content[:-1] + b"X" if damage == "hash" else content + b"X")
        error = ArtifactChecksumMismatchError
    for name in ("launch-a", "launch-b"):
        cwd = tmp_path / name
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        conn = get_connection(path=database)
        try:
            with pytest.raises(error):
                load_results_for_company(conn, company_id, "2026-09-20", artifact_store=store)
            args = build_parser().parse_args(
                [
                    "--dsn",
                    f"sqlite:///{database}",
                    "--evidence-root",
                    str(store.root),
                    "rank",
                    "--as-of",
                    "2026-09-20",
                ]
            )
            with pytest.raises(error):
                args.func(args)
        finally:
            conn.close()
