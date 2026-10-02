"""Option B via real repositories, loader, ranking, exports and audit replay."""

import argparse
import copy
import json
import sqlite3
from dataclasses import make_dataclass
from pathlib import Path

import pytest
from test_method_date_growth_selection import (
    CUTOFF,
    annual,
    packet,
    rank_exports,
    setup,
    upsert_financial_periods,
)

from alphaforge.cli.main import cmd_rank, cmd_replay
from alphaforge.db.connection import get_connection
from alphaforge.db.numerical_runs import (
    TABLES,
    ReplayRefusal,
    canonical,
    capture_inputs,
    digest,
    evaluate,
    replay_run,
    retain_inputs,
    retain_outputs,
    rules_bundle,
)
from alphaforge.db.repositories import (
    upsert_company,
    upsert_dividend_window_coverage,
    upsert_dividends,
    upsert_kpi_observations,
    upsert_prices,
    upsert_stock_splits,
)


def seeded(reverse=False, branch=None, multiple=False):
    reports = [annual(2024, 100), annual(2025, 110), annual(2026, 121)]
    conn, cid = setup(branch=branch, periods=list(reversed(reports)) if reverse else reports)
    packet(conn, cid)
    prices = [{"d": f"{year}-03-31", "c": 8 + year - 2024} for year in range(2024, 2027)]
    upsert_prices(conn, cid, list(reversed(prices)) if reverse else prices, currency="SEK")
    upsert_kpi_observations(
        conn, cid, 37, "r12", "latest", [{"year": 2026, "date": "2026-05-01", "v": 20}]
    )
    upsert_stock_splits(
        conn, [{"insId": 991, "splitDate": "2025-06-01", "splitType": "split", "ratio": "2:1"}]
    )
    upsert_dividends(conn, cid, [{"exDate": "2026-05-01", "amount": 1, "currency": "SEK"}])
    upsert_dividend_window_coverage(
        conn,
        cid,
        "2025-06-01",
        CUTOFF,
        status="complete",
        source="fixture",
        assurance="independent complete fixture",
        verified_at=CUTOFF + "T00:00:00Z",
    )
    if multiple:
        second = upsert_company(
            conn,
            {
                "insId": 992,
                "name": "Sector Fixture",
                "ticker": "SEC",
                "branchId": 75,
                "stockPriceCurrency": "SEK",
                "reportCurrency": "SEK",
            },
        )
        upsert_financial_periods(conn, second, [annual(2026, 100, net_Debt=0)])
        upsert_prices(conn, second, [{"d": CUTOFF, "c": 20}], currency="SEK")
        conn.execute(
            "INSERT INTO watchlist(company_id,ticker,source_file,source_row_hash) VALUES (?, 'SEC', 'fixture', 'second')",
            (second,),
        )
        conn.commit()
    return conn, cid


def file_backed_seeded(tmp_path):
    source, cid = seeded()
    database = tmp_path / "executed-runs.db"
    writer = get_connection(path=database)
    source.backup(writer)
    source.close()
    writer.commit()
    reader = get_connection(path=database)
    return writer, reader, cid


def run_counts(conn):
    return tuple(
        conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("ranking_runs", "executed_numerical_runs")
    )


def latest(conn):
    row = conn.execute(
        "SELECT * FROM executed_numerical_runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    return row["run_id"], row["numerical_identity"], json.loads(row["outputs"])


def companies(conn):
    cls = make_dataclass("Company", ["id", "name", "ticker", "branch_id"])
    return [
        cls(*r)
        for r in conn.execute("SELECT id,name,ticker,branch_id FROM companies ORDER BY ticker,id")
    ]


@pytest.mark.parametrize("multiple", [False, True])
def test_corrections_and_deleted_mutable_rows_replay_actual_outputs(
    monkeypatch, tmp_path, multiple
):
    conn, cid = seeded(multiple=multiple)
    score, _, _ = rank_exports(conn, monkeypatch, tmp_path)
    run, identity, original = latest(conn)
    snapshot = conn.execute(
        "SELECT * FROM numerical_input_bodies WHERE numerical_identity=?", (identity,)
    ).fetchone()
    body = json.loads(snapshot["body"])
    assert body["tables"]["financial_periods"][-1]["raw_payload"]
    assert original["metrics"][str(cid)]["financial"]["revenue_growth"] == pytest.approx(0.10)
    assert original["metrics"][str(cid)]["dividend_yield"]["value"] == 10
    assert original["dcf"][str(cid)]["dcf"]["available"] is True
    assert original["scores"][0] == score
    artifact_id = json.loads((tmp_path / "exports" / CUTOFF / "run.json").read_text())[
        "artifact_id"
    ]
    artefact = tmp_path / "exports" / "runs" / artifact_id
    assert json.loads((artefact / "outputs.json").read_text()) == original
    text_hashes = []
    identities = [identity]
    outputs = [canonical(original)]
    for mutation in ("report", "price", "kpi"):
        if mutation == "report":
            upsert_financial_periods(conn, cid, [annual(2026, 121, net_Debt=50)])
        elif mutation == "price":
            upsert_prices(conn, cid, [{"d": CUTOFF, "c": 20}], currency="SEK")
        else:
            upsert_kpi_observations(
                conn, cid, 37, "r12", "latest", [{"year": 2026, "date": "2026-05-01", "v": 40}]
            )
        rank_exports(conn, monkeypatch, tmp_path)
        _, changed_identity, changed_outputs = latest(conn)
        identities.append(changed_identity)
        outputs.append(canonical(changed_outputs))
        text_hashes.append(
            conn.execute(
                "SELECT packet_hash FROM ranking_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()[0]
        )
        assert replay_run(conn, run)["outputs"] == original
    assert len(set(identities)) == 4
    assert len(set(outputs)) == 4
    assert len(set(text_hashes)) == 1
    assert (artefact / "outputs.json").read_text() == canonical(original)
    # Leave only retained run records. Also prove replay never attempts numerical SELECT.
    for table in reversed(TABLES):
        if table != "companies":
            conn.execute(f"DELETE FROM {table}")
    conn.commit()
    forbidden = set(TABLES)
    conn.set_authorizer(
        lambda action, table, *_: (
            sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ and table in forbidden
            else sqlite3.SQLITE_OK
        )
    )
    assert replay_run(conn, run)["outputs"] == original
    conn.set_authorizer(None)
    # Reconstruct the retained records in a fresh memory DB with no provider rows.
    fresh, _ = setup(periods=[], price_date=None)
    fresh.execute("DELETE FROM watchlist")
    fresh.execute("DELETE FROM companies")
    for table in ("ranking_runs", "numerical_input_bodies", "executed_numerical_runs"):
        for r in conn.execute(f"SELECT * FROM {table}"):
            columns = r.keys()
            fresh.execute(
                f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                tuple(r),
            )
    fresh.commit()
    assert canonical(replay_run(fresh, run)["outputs"]) == canonical(original)


def test_duplicate_csv_companies_are_normalized_before_capture(monkeypatch, tmp_path):
    conn, cid = seeded()
    watchlist = tmp_path / "watchlist.csv"
    watchlist.write_text("ticker\nFIX\nfix\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("alphaforge.db.connection.get_connection", lambda settings: conn)

    assert (
        cmd_rank(
            argparse.Namespace(
                dsn="sqlite:///duplicate-watchlist.db",
                as_of=CUTOFF,
                watchlist=str(watchlist),
            )
        )
        == 0
    )
    run, identity, original = latest(conn)
    body = json.loads(
        conn.execute(
            "SELECT body FROM numerical_input_bodies WHERE numerical_identity=?", (identity,)
        ).fetchone()[0]
    )
    assert [company["id"] for company in body["universe"]] == [cid]
    assert [company["id"] for company in body["tables"]["companies"]] == [cid]
    assert len(original["scores"]) == 1
    assert replay_run(conn, run)["outputs"] == original


@pytest.mark.parametrize("identical_inputs", [False, True])
def test_two_databases_share_export_root_without_run_id_collisions(
    monkeypatch, tmp_path, capsys, identical_inputs
):
    first, _ = seeded()
    second, second_company = seeded()
    if not identical_inputs:
        upsert_prices(second, second_company, [{"d": CUTOFF, "c": 20}], currency="SEK")

    databases = {
        "sqlite:///first.db": first,
        "sqlite:///second.db": second,
    }
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "alphaforge.db.connection.get_connection", lambda settings: databases[settings.dsn]
    )

    def rank_args(dsn):
        return argparse.Namespace(dsn=dsn, as_of=CUTOFF, watchlist=None)

    assert cmd_rank(rank_args("sqlite:///first.db")) == 0
    first_replay = replay_run(first, 1)
    first_dir = tmp_path / "exports" / "runs" / first_replay["artifact_id"]
    first_files = {path.name: path.read_bytes() for path in first_dir.iterdir()}

    assert cmd_rank(rank_args("sqlite:///second.db")) == 0
    second_replay = replay_run(second, 1)
    second_dir = tmp_path / "exports" / "runs" / second_replay["artifact_id"]

    assert (first_replay["artifact_id"] == second_replay["artifact_id"]) is identical_inputs
    assert {path.name: path.read_bytes() for path in first_dir.iterdir()} == first_files
    for replayed, directory in ((first_replay, first_dir), (second_replay, second_dir)):
        assert len(replayed["artifact_id"]) == 64
        assert json.loads((directory / "outputs.json").read_text()) == replayed["outputs"]
        assert "dsn" not in json.loads((directory / "run.json").read_text())

    latest_alias = json.loads((tmp_path / "exports" / CUTOFF / "latest.json").read_text())
    assert latest_alias == {
        "mutable_latest_alias": True,
        "run_id": 1,
        "artifact_id": second_replay["artifact_id"],
    }
    capsys.readouterr()
    for dsn, expected in databases.items():
        assert cmd_replay(argparse.Namespace(dsn=dsn, run_id=1)) == 0
        replayed = json.loads(capsys.readouterr().out)
        assert replayed["artifact_id"] == replay_run(expected, 1)["artifact_id"]
        assert replayed["outputs"] == replay_run(expected, 1)["outputs"]


@pytest.mark.parametrize("multiple", [False, True])
def test_canonical_identity_output_independent_of_numerical_insertion_order(
    monkeypatch, tmp_path, multiple
):
    runs = []
    for reverse in (False, True):
        conn, _ = seeded(reverse=reverse, multiple=multiple)
        destination = tmp_path / str(reverse)
        destination.mkdir()
        rank_exports(conn, monkeypatch, destination)
        runs.append(latest(conn)[1:])
    assert runs[0] == runs[1]


@pytest.mark.parametrize(
    "change",
    [
        "amount",
        "unit",
        "date",
        "fiscal",
        "branch",
        "split",
        "dividend",
        "coverage",
        "zero",
        "rules",
        "code",
    ],
)
def test_consumed_fields_and_rules_change_identity(change):
    conn, cid = seeded()
    body, text = capture_inputs(conn, companies(conn), CUTOFF)
    rules = rules_bundle()
    first, financial_hash = retain_inputs(conn, body, rules)
    changed = copy.deepcopy(body)
    if change == "amount":
        changed["tables"]["financial_periods"][-1]["net_debt"] = 51
    elif change == "unit":
        changed["tables"]["financial_periods"][-1]["values_currency"] = "EUR"
    elif change == "date":
        changed["tables"]["prices"][-1]["price_date"] = "2026-05-31"
    elif change == "fiscal":
        changed["tables"]["financial_periods"][-1]["report_year"] = 2023
    elif change == "branch":
        changed["tables"]["companies"][0]["branch_id"] = 75
        changed["universe"][0]["branch_id"] = 75
    elif change == "split":
        changed["tables"]["stock_splits"][0]["ratio"] = "3:1"
    elif change == "dividend":
        changed["tables"]["dividends"][0]["currency"] = "USD"
    elif change == "coverage":
        changed["tables"]["dividend_window_coverage"][0]["status"] = "partial"
    elif change == "zero":
        changed["tables"]["kpi_observations"][0]["value"] = 0
    elif change == "rules":
        rules["max_price_age_calendar_days"] = 30
    else:
        rules["code_revision"] = "0" * 40
    second, changed_hash = retain_inputs(conn, changed, rules)
    assert second != first
    assert (financial_hash == changed_hash) == (change in {"rules", "code"})
    assert digest(text) == digest(capture_inputs(conn, companies(conn), CUTOFF)[1])


@pytest.mark.parametrize(
    "dependency",
    [
        "alphaforge/evidence/report_rules.py",
        "alphaforge/evidence/mfn_taxonomy.py",
    ],
)
def test_readiness_rule_implementation_changes_numerical_identity(monkeypatch, dependency):
    conn, _ = seeded()
    body, text = capture_inputs(conn, companies(conn), CUTOFF)
    first, financial_hash = retain_inputs(conn, body, rules_bundle())
    target = Path(__file__).parents[1] / dependency
    original_read_bytes = Path.read_bytes

    def changed_read_bytes(path):
        contents = original_read_bytes(path)
        return contents + b"\n" if path == target else contents

    monkeypatch.setattr(Path, "read_bytes", changed_read_bytes)
    second, changed_hash = retain_inputs(conn, body, rules_bundle())
    assert second != first
    assert changed_hash == financial_hash
    assert digest(text) == digest(capture_inputs(conn, companies(conn), CUTOFF)[1])


def test_missing_and_zero_are_preserved_and_replayed(monkeypatch, tmp_path):
    conn, cid = seeded()
    outputs = []
    for value in (None, 0):
        upsert_financial_periods(
            conn, cid, [annual(2026, 121, net_Debt=value, free_Cash_Flow=value)]
        )
        rank_exports(conn, monkeypatch, tmp_path)
        run, _, out = latest(conn)
        assert replay_run(conn, run)["outputs"] == out
        outputs.append(out)
    assert outputs[0]["metrics"][str(cid)]["financial"]["fcf_margin"] is None
    assert outputs[1]["metrics"][str(cid)]["financial"]["fcf_margin"] == 0
    assert outputs[0]["dcf"][str(cid)]["current_net_debt"] is None
    assert outputs[1]["dcf"][str(cid)]["current_net_debt"] == 0


def test_immutable_conflicting_insertion_and_tamper_refuse(monkeypatch, tmp_path):
    conn, _ = seeded()
    rank_exports(conn, monkeypatch, tmp_path)
    run, identity, outputs = latest(conn)
    stored = conn.execute(
        "SELECT body,rules FROM numerical_input_bodies WHERE numerical_identity=?", (identity,)
    ).fetchone()
    body, rules = map(json.loads, stored)
    assert retain_inputs(conn, body, rules)[0] == identity
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE numerical_input_bodies SET body='{}'")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("DELETE FROM executed_numerical_runs")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "INSERT OR REPLACE INTO numerical_input_bodies SELECT * FROM numerical_input_bodies"
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "INSERT OR REPLACE INTO executed_numerical_runs SELECT * FROM executed_numerical_runs"
        )
    text = json.loads(
        conn.execute(
            "SELECT textual_context FROM executed_numerical_runs WHERE run_id=?", (run,)
        ).fetchone()[0]
    )
    with pytest.raises(ReplayRefusal, match="conflicting_immutable_insertion"):
        retain_outputs(conn, run, identity, text, {**outputs, "tamper": True})
    conn.execute("DROP TRIGGER numerical_body_no_update")
    conn.execute("UPDATE numerical_input_bodies SET body='{}'")
    with pytest.raises(ReplayRefusal, match="corrupt_retained_body"):
        replay_run(conn, run)
    with pytest.raises(ReplayRefusal, match="conflicting_immutable_insertion"):
        retain_inputs(conn, body, rules)


@pytest.mark.parametrize(
    "change,reason",
    [
        ("revision", "unavailable_code_revision"),
        ("rules", "unsupported_rules_or_code"),
        ("source", "unsupported_rules_or_code"),
        ("missing", "missing_snapshot"),
        ("text", "invalid_textual_context"),
        ("outputs", "corrupt_retained_body"),
    ],
)
def test_replay_refuses_without_live_fallback(monkeypatch, tmp_path, change, reason):
    conn, _ = seeded()
    rank_exports(conn, monkeypatch, tmp_path)
    run, identity, outputs = latest(conn)
    if change in {"revision", "rules", "source"}:
        snapshot = conn.execute(
            "SELECT body,rules FROM numerical_input_bodies WHERE numerical_identity=?", (identity,)
        ).fetchone()
        body, rules = map(json.loads, snapshot)
        if change == "source":
            rules["source_files"]["alphaforge/core/financial/calculator.py"] = "0" * 64
        else:
            rules["code_revision" if change == "revision" else "selection"] = (
                "0" * 40 if change == "revision" else "unsupported-version"
            )
        new_identity, _ = retain_inputs(conn, body, rules)
        conn.execute("DROP TRIGGER numerical_run_no_update")
        conn.execute("UPDATE executed_numerical_runs SET numerical_identity=?", (new_identity,))
    elif change == "missing":
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DROP TRIGGER numerical_body_no_delete")
        conn.execute("DELETE FROM numerical_input_bodies")
    else:
        conn.execute("DROP TRIGGER numerical_run_no_update")
        field = "textual_context" if change == "text" else "outputs"
        conn.execute(f"UPDATE executed_numerical_runs SET {field}='{{}}'")
    with pytest.raises(ReplayRefusal, match=reason):
        replay_run(conn, run)


def test_legacy_and_replay_cli_are_honest(monkeypatch, tmp_path, capsys):
    conn, _ = seeded()
    rank_exports(conn, monkeypatch, tmp_path)
    run, _, outputs = latest(conn)
    capsys.readouterr()
    assert cmd_replay(argparse.Namespace(dsn="sqlite:///:memory:", run_id=run)) == 0
    assert json.loads(capsys.readouterr().out)["outputs"] == outputs
    conn.execute("DROP TRIGGER numerical_run_no_delete")
    conn.execute("DELETE FROM executed_numerical_runs")
    conn.execute("UPDATE ranking_runs SET inputs_summary=NULL")
    conn.commit()
    assert cmd_replay(argparse.Namespace(dsn="sqlite:///:memory:", run_id=run)) == 1
    assert json.loads(capsys.readouterr().err)["reason"] == "legacy_not_replayable"
    with pytest.raises(ReplayRefusal, match="missing_run"):
        replay_run(conn, 99999)


@pytest.mark.parametrize("stage", ["inputs", "outputs"])
def test_retention_failure_prevents_export_consumption(monkeypatch, tmp_path, stage):
    conn, _ = seeded()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("alphaforge.db.connection.get_connection", lambda settings: conn)

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("retention disk full")

    monkeypatch.setattr(f"alphaforge.db.numerical_runs.retain_{stage}", fail)
    if stage == "inputs":
        monkeypatch.setattr(
            "alphaforge.db.numerical_runs.evaluate",
            lambda *args: pytest.fail("evaluated before retention"),
        )
    with pytest.raises(sqlite3.OperationalError, match="retention disk full"):
        cmd_rank(argparse.Namespace(dsn="sqlite:///:memory:", as_of=CUTOFF, watchlist=None))
    assert not (tmp_path / "exports").exists()
    assert run_counts(conn) == (0, 0)
    if stage == "outputs":
        assert conn.execute("SELECT COUNT(*) FROM numerical_input_bodies").fetchone()[0] == 1
        with pytest.raises(ReplayRefusal, match="missing_run"):
            replay_run(conn, 1)


def test_ranking_and_output_link_become_visible_atomically(monkeypatch, tmp_path):
    writer, reader, _ = file_backed_seeded(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("alphaforge.db.connection.get_connection", lambda settings: writer)
    visibility = {}

    def observe_retention(*args, **kwargs):
        visibility["before"] = run_counts(reader)
        artifact_id = retain_outputs(*args, **kwargs)
        visibility["after"] = run_counts(reader)
        return artifact_id

    monkeypatch.setattr("alphaforge.db.numerical_runs.retain_outputs", observe_retention)
    assert (
        cmd_rank(argparse.Namespace(dsn="sqlite:///executed-runs.db", as_of=CUTOFF, watchlist=None))
        == 0
    )
    assert visibility == {"before": (0, 0), "after": (1, 1)}
    writer.close()
    reader.close()


def test_repository_output_failure_rolls_back_ranking_visibility(monkeypatch, tmp_path):
    writer, reader, _ = file_backed_seeded(tmp_path)
    writer.execute(
        """
        CREATE TRIGGER fail_executed_run_retention
        BEFORE INSERT ON executed_numerical_runs
        BEGIN SELECT RAISE(ABORT, 'retention storage fault'); END
        """
    )
    writer.commit()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("alphaforge.db.connection.get_connection", lambda settings: writer)

    with pytest.raises(sqlite3.IntegrityError, match="retention storage fault"):
        cmd_rank(argparse.Namespace(dsn="sqlite:///executed-runs.db", as_of=CUTOFF, watchlist=None))
    assert run_counts(writer) == run_counts(reader) == (0, 0)
    assert writer.execute("SELECT COUNT(*) FROM numerical_input_bodies").fetchone()[0] == 1
    assert reader.execute("SELECT COUNT(*) FROM numerical_input_bodies").fetchone()[0] == 1
    assert not (tmp_path / "exports").exists()
    writer.close()
    reader.close()


def test_text_correction_does_not_replace_numerical_identity():
    conn, cid = seeded()
    body, text = capture_inputs(conn, companies(conn), CUTOFF)
    identity, financial = retain_inputs(conn, body, rules_bundle())
    conn.execute("UPDATE evidence_packets SET usable=0 WHERE company_id=?", (cid,))
    conn.commit()
    current_body, current_text = capture_inputs(conn, companies(conn), CUTOFF)
    assert digest(current_text) != digest(text)
    assert retain_inputs(conn, current_body, rules_bundle()) == (identity, financial)
    # Audit replay doesn't change mutable packet usability or promise live readiness.
    _, _, outputs = evaluate(body, text)
    assert (
        outputs["scores"][0]["evidence_packet_hash"]
        == text[str(cid)]["evidence_packet"]["packet_hash"]
    )
