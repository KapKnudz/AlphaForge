"""Hosted review regressions through actual two-connection and CLI boundaries."""

import argparse
import json
import sqlite3

import pytest
from test_executed_numerical_runs import companies, file_backed_seeded, seeded
from test_method_date_growth_selection import CUTOFF, annual, rank_exports, upsert_financial_periods

from alphaforge.cli.main import cmd_rank, cmd_replay
from alphaforge.db.migrations import migrate
from alphaforge.db.numerical_runs import (
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
from alphaforge.db.repositories import save_ranking_run, upsert_company


def args():
    return argparse.Namespace(dsn="sqlite:///:memory:", as_of=CUTOFF, watchlist=None)


class InterleavedConnection:
    """Finish a real absent SELECT, then let the other connection commit first."""

    def __init__(self, conn, after_absence):
        self.conn = conn
        self.after_absence = after_absence
        self.fired = False

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def execute(self, sql, parameters=()):
        cursor = self.conn.execute(sql, parameters)
        if not self.fired and sql.startswith(
            "SELECT financial_inputs_hash,body,rules FROM numerical_input_bodies"
        ):
            owner = self

            class PausedCursor:
                def fetchone(self):
                    value = cursor.fetchone()
                    if value is None:
                        owner.fired = True
                        owner.after_absence(parameters[0])
                    return value

            return PausedCursor()
        return cursor


def test_two_connection_identical_retention_interleaving_rank_and_replay(monkeypatch, tmp_path):
    first, second, _ = file_backed_seeded(tmp_path)
    monkeypatch.chdir(tmp_path)
    wrapped = None

    def rank_peer(identity):
        monkeypatch.setattr("alphaforge.db.connection.get_connection", lambda settings: second)
        try:
            assert cmd_rank(args()) == 0
            assert (
                second.execute("SELECT numerical_identity FROM numerical_input_bodies").fetchone()[
                    0
                ]
                == identity
            )
        finally:
            monkeypatch.setattr("alphaforge.db.connection.get_connection", lambda settings: wrapped)

    wrapped = InterleavedConnection(first, rank_peer)
    monkeypatch.setattr("alphaforge.db.connection.get_connection", lambda settings: wrapped)
    assert cmd_rank(args()) == 0
    assert wrapped.fired
    assert second.execute("SELECT COUNT(*) FROM numerical_input_bodies").fetchone()[0] == 1
    assert second.execute("SELECT COUNT(*) FROM ranking_runs").fetchone()[0] == 2
    records = second.execute("SELECT * FROM executed_numerical_runs ORDER BY run_id").fetchall()
    assert len(records) == 2
    assert records[0]["numerical_identity"] == records[1]["numerical_identity"]
    assert records[0]["outputs"] == records[1]["outputs"]
    for record in records:
        replay = replay_run(second, record["run_id"])
        artifact = tmp_path / "exports" / "runs" / replay["artifact_id"]
        assert json.loads((artifact / "outputs.json").read_text()) == replay["outputs"]
        assert replay["outputs"] == json.loads(record["outputs"])
    first.close()
    second.close()


def test_two_connection_contradictory_insertion_refuses_without_consumption(monkeypatch, tmp_path):
    first, second, _ = file_backed_seeded(tmp_path)
    monkeypatch.chdir(tmp_path)

    def conflicting_peer(identity):
        second.execute(
            "INSERT INTO numerical_input_bodies VALUES (?,?,?,?)",
            (identity, "contradictory", canonical({"wrong": True}), canonical(rules_bundle())),
        )
        second.commit()

    wrapped = InterleavedConnection(first, conflicting_peer)
    monkeypatch.setattr("alphaforge.db.connection.get_connection", lambda settings: wrapped)
    with pytest.raises(ReplayRefusal, match="conflicting_immutable_insertion"):
        cmd_rank(args())
    first.rollback()
    assert wrapped.fired
    assert second.execute("SELECT COUNT(*) FROM ranking_runs").fetchone()[0] == 0
    assert not (tmp_path / "exports").exists()
    first.close()
    second.close()


def rejected_fixture(extra_rows):
    conn, cid = seeded()
    outside = upsert_company(
        conn,
        {
            "insId": 2002,
            "name": "Outside universe",
            "ticker": "ZZZ",
            "stockPriceCurrency": "SEK",
            "reportCurrency": "SEK",
        },
    )
    for index in range(extra_rows):
        upsert_financial_periods(conn, outside, [{**annual(2026, 999 + index), "period_end": None}])
    upsert_financial_periods(conn, cid, [{**annual(2026, 777), "period_end": None}])
    # Keep consumed diagnostic dates identical across reconstructed fixtures.
    conn.execute("UPDATE financial_period_rejections SET rejected_at=?", (CUTOFF + "T00:00:00Z",))
    conn.commit()
    selected = [c for c in companies(conn) if c.id == cid]
    return conn, cid, selected


def test_source_mapping_does_not_change_canonical_body_identity_or_outputs(monkeypatch, tmp_path):
    captured = []
    for extra in (0, 2):
        conn, cid, selected = rejected_fixture(extra)
        source_rows = {}
        body, text = capture_inputs(conn, selected, CUTOFF, source_rows=source_rows)
        plain_body, plain_text = capture_inputs(conn, selected, CUTOFF)
        assert body == plain_body and text == plain_text
        identity = retain_inputs(conn, body, rules_bundle())
        root = tmp_path / str(extra)
        root.mkdir()
        rank_exports(conn, monkeypatch, root)
        record = conn.execute("SELECT * FROM executed_numerical_runs").fetchone()
        replay = replay_run(conn, record["run_id"])
        captured.append((identity, record["outputs"], source_rows, replay["artifact_id"]))
        selected_map = next(
            r for r in source_rows["rows"] if r["table"] == "financial_period_rejections"
        )
        assert selected_map["snapshot_row_id"] == 1
        assert selected_map["source_row_id"] == extra + 1
        conn.close()
    assert captured[0][:2] == captured[1][:2]
    assert captured[0][2] != captured[1][2]
    assert captured[0][3] != captured[1][3]  # Complete audit artifacts must not collide.


def test_actual_rejection_export_and_replay_carry_explicit_source_namespace(
    monkeypatch, tmp_path, capsys
):
    conn, cid, _ = rejected_fixture(1)
    rank_exports(conn, monkeypatch, tmp_path)
    metadata = json.loads((tmp_path / "exports" / CUTOFF / "run.json").read_text())
    ranking = json.loads((tmp_path / "exports" / CUTOFF / "ranking.json").read_text())
    selection = ranking["scores"][0]["input_selection"]
    refusal = next(r for r in selection["rejected_reports"] if r["source"] == "ingestion_rejection")
    assert refusal["id"] == "rejection:1"
    original = conn.execute(
        "SELECT company_id,payload_hash FROM financial_period_rejections WHERE id=2"
    ).fetchone()
    assert original["company_id"] == cid and original["payload_hash"] == refusal["payload_hash"]
    assert (
        conn.execute("SELECT company_id FROM financial_period_rejections WHERE id=1").fetchone()[0]
        != cid
    )
    provenance = ranking["numerical_provenance"]
    assert provenance["row_id_namespace"] == "retained_numerical_body"
    assert provenance["source_rows_reference"] == "run.json#source_rows"
    source_rows = metadata["source_rows"]
    assert source_rows["snapshot_namespace"] == "retained_numerical_body"
    assert source_rows["source_namespace"] == "originating_sqlite_database"
    origin = next(r for r in source_rows["rows"] if r["table"] == "financial_period_rejections")
    assert origin["snapshot_row_id"] == 1 and origin["source_row_id"] == 2
    assert origin["company_id"] == cid
    run = metadata["run_id"]
    expected = replay_run(conn, run)
    conn.execute("DELETE FROM financial_period_rejections")
    conn.execute("DELETE FROM financial_periods")
    conn.execute("DELETE FROM prices")
    conn.execute("DELETE FROM kpi_observations")
    conn.commit()
    capsys.readouterr()
    assert cmd_replay(argparse.Namespace(dsn="sqlite:///:memory:", run_id=run)) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["source_rows"] == source_rows
    assert replay["source_rows_hash"] == metadata["source_rows_hash"] == digest(source_rows)
    assert replay["outputs"] == expected["outputs"]
    assert replay["artifact_id"] == metadata["artifact_id"]
    assert (
        canonical(metadata)
        == (tmp_path / "exports" / "runs" / replay["artifact_id"] / "run.json").read_text()
    )


def test_source_mapping_retention_fault_is_atomic(monkeypatch, tmp_path):
    writer, reader, _ = file_backed_seeded(tmp_path)
    writer.execute(
        "CREATE TRIGGER fail_source_audit BEFORE INSERT ON numerical_run_source_rows BEGIN SELECT RAISE(ABORT, 'source audit fault'); END"
    )
    writer.commit()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("alphaforge.db.connection.get_connection", lambda settings: writer)
    with pytest.raises(sqlite3.IntegrityError, match="source audit fault"):
        cmd_rank(args())
    for conn in (writer, reader):
        assert conn.execute("SELECT COUNT(*) FROM ranking_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM executed_numerical_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM numerical_run_source_rows").fetchone()[0] == 0
    assert not (tmp_path / "exports").exists()
    writer.close()
    reader.close()


def test_source_mapping_immutable_and_corrupt_map_refuses(monkeypatch, tmp_path):
    conn, _, _ = rejected_fixture(1)
    rank_exports(conn, monkeypatch, tmp_path)
    original = conn.execute("SELECT * FROM executed_numerical_runs").fetchone()
    mapping = replay_run(conn, 1)["source_rows"]
    expected = replay_run(conn, 1)["artifact_id"]
    arguments = (
        conn,
        1,
        original["numerical_identity"],
        json.loads(original["textual_context"]),
        json.loads(original["outputs"]),
    )
    assert retain_outputs(*arguments, source_rows=mapping) == expected
    with pytest.raises(ReplayRefusal, match="conflicting_immutable_insertion"):
        retain_outputs(*arguments, source_rows={**mapping, "source_namespace": "wrong"})
    assert replay_run(conn, 1)["source_rows"] == mapping
    for sql in (
        "UPDATE numerical_run_source_rows SET source_rows='{}'",
        "DELETE FROM numerical_run_source_rows",
        "INSERT OR REPLACE INTO numerical_run_source_rows SELECT * FROM numerical_run_source_rows",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="immutable numerical source rows"):
            conn.execute(sql)
    conn.execute("DROP TRIGGER numerical_source_rows_no_update")
    conn.execute("UPDATE numerical_run_source_rows SET source_rows='{}'")
    with pytest.raises(ReplayRefusal, match="invalid_source_row_map"):
        replay_run(conn, 1)


def test_v17_upgrade_preserves_records_and_does_not_invent_source_ids():
    conn, _ = seeded()
    body, text = capture_inputs(conn, companies(conn), CUTOFF)
    identity, _ = retain_inputs(conn, body, rules_bundle())
    _, _, outputs = evaluate(body, text)
    run = save_ranking_run(
        conn,
        as_of=CUTOFF,
        model_version="fixture",
        packet_hash=None,
        universe_hash=None,
        company_count=1,
        eligible_count=1,
        scores=outputs["scores"],
        commit=False,
    )
    artifact_id = retain_outputs(conn, run, identity, text, outputs)
    before = {
        table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")]
        for table in ("ranking_runs", "executed_numerical_runs", "numerical_input_bodies")
    }
    conn.execute("DROP TABLE numerical_run_source_rows")
    conn.execute("PRAGMA user_version=17")
    migrate(conn)
    assert conn.execute("SELECT COUNT(*) FROM numerical_run_source_rows").fetchone()[0] == 0
    for table, rows in before.items():
        assert [tuple(r) for r in conn.execute(f"SELECT * FROM {table}")] == rows
    replay = replay_run(conn, 1)
    assert replay["source_rows"] is None
    assert replay["source_rows_status"] == "not_retained"
    assert replay["artifact_id"] == artifact_id
    assert replay["outputs"] == outputs
    with pytest.raises(ReplayRefusal, match="conflicting_immutable_insertion"):
        retain_outputs(conn, run, identity, text, outputs, source_rows={"invented": True})
    assert conn.execute("SELECT COUNT(*) FROM numerical_run_source_rows").fetchone()[0] == 0
