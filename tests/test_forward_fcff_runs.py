"""Actual CLI/SQLite/export/retained replay, failure injection and migration tests."""

from __future__ import annotations

import json
import sqlite3

import pytest
from forward_fcff_fixtures import request

from alphaforge.cli.forward_fcff import export
from alphaforge.cli.main import main
from alphaforge.core.valuation.forward_fcff import evaluate
from alphaforge.core.valuation.forward_fcff_contract import canonical, freeze
from alphaforge.db import forward_fcff_runs as runs
from alphaforge.db.migrations import migrate
from alphaforge.db.numerical_runs import ReplayRefusal


def db(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    return conn


@pytest.mark.parametrize("currency", ["SEK", "EUR"])
def test_a17_a18_actual_cli_export_retained_only_exact_replay(tmp_path, capsys, currency):
    req, _ = request(currency)
    input_path = tmp_path / "input.json"
    input_path.write_text(json.dumps(req))
    database = tmp_path / "research.db"
    dsn = f"sqlite:///{database}"
    args = [
        "--dsn",
        dsn,
        "forward-fcff",
        "--input-json",
        str(input_path),
        "--exports-dir",
        str(tmp_path / "exports"),
    ]
    assert main(args) == 0
    receipt = json.loads(capsys.readouterr().out)
    artifact = tmp_path / "exports" / "forward-fcff" / "runs" / receipt["artifact_id"]
    original = (artifact / "outputs.json").read_bytes()
    result = json.loads(original)
    assert result["operating_value"]["available"]
    assert not result["common_equity"]["available"] and not result["per_share"]["available"]
    assert currency in (artifact / "report.md").read_text()
    input_path.unlink()
    conn = db(database)
    conn.execute("DELETE FROM companies")
    conn.commit()

    # Any mutable-table access is denied by SQLite itself during replay.
    def authorize(action, table, *_):
        if action == sqlite3.SQLITE_READ and table not in {
            "numerical_input_bodies",
            "forward_fcff_runs",
        }:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorize)
    replayed = runs.replay(conn, receipt["run_id"])
    assert canonical(replayed["outputs"]).encode() == original
    conn.close()
    assert main(["--dsn", dsn, "forward-fcff-replay", "--run-id", str(receipt["run_id"])]) == 0
    assert canonical(json.loads(capsys.readouterr().out)["outputs"]).encode() == original
    assert {p.name for p in artifact.iterdir()} == {
        "input.json",
        "context.json",
        "outputs.json",
        "run.json",
        "cash-flows.csv",
        "report.md",
    }


def test_a18_input_and_output_retention_failures_prevent_publication(tmp_path, monkeypatch):
    conn = db(tmp_path / "db")
    req, _ = request()
    frozen = freeze(req)
    conn.execute(
        "CREATE TRIGGER fail_input BEFORE INSERT ON numerical_input_bodies BEGIN SELECT RAISE(ABORT,'injected input failure'); END"
    )
    called = []
    original = runs.evaluate
    monkeypatch.setattr(runs, "evaluate", lambda request: called.append(True) or original(request))
    with pytest.raises(sqlite3.IntegrityError):
        runs.execute(conn, frozen)
    assert not called and conn.execute("SELECT count(*) FROM forward_fcff_runs").fetchone()[0] == 0
    conn.execute("DROP TRIGGER fail_input")
    conn.execute(
        "CREATE TRIGGER fail_output BEFORE INSERT ON forward_fcff_runs BEGIN SELECT RAISE(ABORT,'injected output failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        runs.execute(conn, frozen)
    assert called and conn.execute("SELECT count(*) FROM numerical_input_bodies").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM forward_fcff_runs").fetchone()[0] == 0
    conn.close()


def test_a18_two_connection_visibility_before_evaluate_and_after_output(tmp_path, monkeypatch):
    path = tmp_path / "db"
    conn = db(path)
    reader = sqlite3.connect(path)
    req, _ = request()
    real = runs.evaluate

    def evaluate(frozen):
        assert reader.execute("SELECT count(*) FROM numerical_input_bodies").fetchone()[0] == 1
        assert reader.execute("SELECT count(*) FROM forward_fcff_runs").fetchone()[0] == 0
        return real(frozen)

    monkeypatch.setattr(runs, "evaluate", evaluate)
    receipt, outputs = runs.execute(conn, freeze(req))
    assert reader.execute(
        "SELECT outputs FROM forward_fcff_runs WHERE id=?", (receipt["run_id"],)
    ).fetchone()[0] == canonical(outputs)
    reader.close()
    conn.close()


def test_a18_write_once_export_conflict_and_identical_retry(tmp_path):
    conn = db(tmp_path / "db")
    req, _ = request()
    frozen = freeze(req)
    receipt, outputs = runs.execute(conn, frozen)
    target = export(tmp_path / "exports", frozen, receipt, outputs)
    assert export(tmp_path / "exports", frozen, receipt, outputs) == target
    (target / "outputs.json").write_text("edited")
    with pytest.raises(ReplayRefusal, match="conflicting_immutable_export"):
        export(tmp_path / "exports", frozen, receipt, outputs)
    assert (target / "outputs.json").read_text() == "edited"
    conn.close()


def test_a19_migration_append_only_and_old_bytes_preserved(tmp_path):
    conn = db(tmp_path / "db")
    # A retained old body survives v19 upgrade without interpretation or backfill.
    conn.execute("INSERT INTO numerical_input_bodies VALUES ('legacy','hash','{}','{}')")
    old = conn.execute("SELECT * FROM numerical_input_bodies").fetchall()
    conn.execute("DROP TABLE forward_fcff_runs")
    conn.execute("PRAGMA user_version=19")
    conn.commit()
    migrate(conn)
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 20
    assert conn.execute("SELECT * FROM numerical_input_bodies").fetchall() == old
    assert conn.execute("SELECT count(*) FROM forward_fcff_runs").fetchone()[0] == 0
    req, _ = request()
    receipt, _ = runs.execute(conn, freeze(req))
    for statement in (
        "UPDATE forward_fcff_runs SET outputs=outputs",
        "DELETE FROM forward_fcff_runs",
        "INSERT OR REPLACE INTO forward_fcff_runs SELECT * FROM forward_fcff_runs",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(statement)
        conn.rollback()
    assert runs.replay(conn, receipt["run_id"])["status"] == "exact_replay"
    conn.close()


def test_a17_changed_rules_refuse_not_reinterpret(tmp_path, monkeypatch):
    conn = db(tmp_path / "db")
    req, _ = request()
    receipt, _ = runs.execute(conn, freeze(req))
    original = runs.rules_bundle()
    monkeypatch.setattr(runs, "rules_bundle", lambda: {**original, "validation": "different"})
    with pytest.raises(ReplayRefusal, match="unsupported_rules_or_code"):
        runs.replay(conn, receipt["run_id"])
    assert conn.execute("SELECT count(*) FROM forward_fcff_runs").fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize("part", ["body", "context", "outputs"])
def test_a18_corrupt_retained_bytes_refuse(tmp_path, part):
    conn = db(tmp_path / "db")
    req, _ = request()
    receipt, _ = runs.execute(conn, freeze(req))
    if part == "body":
        conn.execute("DROP TRIGGER numerical_body_no_update")
        conn.execute("UPDATE numerical_input_bodies SET body='{}'")
    else:
        conn.execute("DROP TRIGGER forward_fcff_no_update")
        column = "textual_context" if part == "context" else "outputs"
        conn.execute(f"UPDATE forward_fcff_runs SET {column}='{{}}'")
    conn.commit()
    with pytest.raises(ReplayRefusal):
        runs.replay(conn, receipt["run_id"])
    conn.close()


def test_a19_real_existing_rank_packet_and_calibration_bytes_survive_upgrade(tmp_path, monkeypatch):
    from test_executed_numerical_runs import file_backed_seeded
    from test_method_date_growth_selection import rank_exports

    conn, reader, _ = file_backed_seeded(tmp_path)
    rank_exports(conn, monkeypatch, tmp_path)  # No mature route: genuine stored refusal.
    tables = (
        "companies",
        "financial_periods",
        "ranking_runs",
        "numerical_input_bodies",
        "executed_numerical_runs",
        "reinvestment_calibrations",
        "evidence_packets",
    )
    before = {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in tables
    }
    assert (
        before["executed_numerical_runs"]
        and before["reinvestment_calibrations"]
        and before["evidence_packets"]
    )
    conn.execute("DROP TABLE forward_fcff_runs")
    conn.execute("PRAGMA user_version=19")
    conn.commit()
    migrate(conn)
    after = {
        table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")] for table in tables
    }
    assert before == after
    assert reader.execute("SELECT count(*) FROM forward_fcff_runs").fetchone()[0] == 0
    reader.close()
    conn.close()


def test_transition_acceptance_requires_catalogued_nonstationarity():
    from tools.check_forward_fcff_acceptance import (
        AcceptanceError,
        check_transition_refusal,
    )

    case_id = "tail_inflation_1pct_after_year10-return-1"
    output = {
        "operating_cash_flows": {"available": True},
        "operating_value": {
            "available": False,
            "reason_codes": ["tail_primitive_nonstationary"],
        },
        "terminal": {
            "horizon": 14,
            "present_value": None,
            "certificate": {
                "failures": [
                    {
                        "code": "tail_primitive_nonstationary",
                        "path": "tax/tax/arising",
                    }
                ]
            },
        },
    }
    check_transition_refusal(output, case_id)
    unrelated = json.loads(json.dumps(output))
    unrelated["operating_value"]["reason_codes"] = ["terminal_growth_invalid"]
    unrelated["terminal"]["certificate"]["failures"] = [
        {"code": "terminal_growth_invalid", "path": ""}
    ]
    with pytest.raises(AcceptanceError, match="transition refusal reason mismatch"):
        check_transition_refusal(unrelated, case_id)


def test_transition_pair_rejects_changed_terminal_premises_losslessly():
    from forward_fcff_fixtures import set_tail
    from tools.check_forward_fcff_acceptance import AcceptanceError, check_transition_pair

    corrected, corrected_inputs = request(horizon=2)
    transitional, transitional_inputs = request(horizon=1)

    def split_payable_lots(req):
        replacements = {}
        lots = []
        for lot in req["operating_schedule"]["tax_lots"]:
            if lot["leg"] != "payable":
                lots.append(lot)
                continue
            split = []
            for suffix in ("a", "b"):
                part = json.loads(json.dumps(lot))
                part["id"] = f"{lot['id']}-{suffix}"
                part["amount"]["value"] /= 2
                part["amount"]["provenance"]["id"] += f"-{suffix}"
                lots.append(part)
                split.append(part["id"])
            replacements[lot["id"]] = split
        req["operating_schedule"]["tax_lots"] = lots
        for row in req["operating_schedule"]["periods"]:
            for ledger in row["tax_ledgers"]:
                allocations = []
                for allocation in ledger["allocations"]:
                    if allocation["lot_id"] not in replacements:
                        allocations.append(allocation)
                        continue
                    for suffix, lot_id in zip(("a", "b"), replacements[allocation["lot_id"]]):
                        part = json.loads(json.dumps(allocation))
                        part["id"] += f"-{suffix}"
                        part["lot_id"] = lot_id
                        part["amount"]["value"] /= 2
                        part["amount"]["provenance"]["id"] += f"-{suffix}"
                        allocations.append(part)
                ledger["allocations"] = allocations

    split_payable_lots(corrected)
    split_payable_lots(transitional)
    set_tail(corrected, corrected_inputs, 2, 0.1, {"tax/tax/arising": 2, "tax/tax/entitlement": 2})
    set_tail(
        transitional,
        transitional_inputs,
        1,
        0.1,
        {"tax/tax/arising": 2, "tax/tax/entitlement": 2},
    )
    for corrected_template, transitional_template in zip(
        corrected["terminal"]["templates"],
        transitional["terminal"]["templates"],
        strict=True,
    ):
        corrected_template["regime_start"] = transitional_template["regime_start"]
    corrected["terminal"]["exhausted_states"] = transitional["terminal"]["exhausted_states"]
    corrected_result = evaluate(corrected)
    transitional_result = evaluate(transitional)
    check_transition_pair(
        corrected,
        transitional,
        corrected_result,
        transitional_result,
        "synthetic-transition",
    )

    reordered = json.loads(json.dumps(corrected))
    reordered["terminal"]["tax_kernels"][0]["queues"].reverse()
    for index, queue in enumerate(reordered["terminal"]["tax_kernels"][0]["queues"]):
        queue["id"] = f"benign-queue-{index}"
    check_transition_pair(
        reordered,
        transitional,
        evaluate(reordered),
        transitional_result,
        "synthetic-transition",
    )

    changed_member = json.loads(json.dumps(corrected))
    changed_member["terminal"]["tax_kernels"][0]["queues"][0]["remaining_at_tail"][
        "provenance"
    ]["rationale"] = "Changed premise for one same-age lot"
    with pytest.raises(AcceptanceError, match="transition queue provenance mismatch"):
        check_transition_pair(
            changed_member,
            transitional,
            evaluate(changed_member),
            transitional_result,
            "synthetic-transition",
        )

    changed_weights = json.loads(json.dumps(transitional))
    changed_weights["terminal"]["tax_kernels"][0]["weights"][0]["weight"]["value"] = 0.9
    changed_weights["terminal"]["tax_kernels"][0]["weights"][1]["weight"]["value"] = 0.1
    with pytest.raises(AcceptanceError, match="transition kernel weights mismatch"):
        check_transition_pair(
            corrected,
            changed_weights,
            corrected_result,
            evaluate(changed_weights),
            "synthetic-transition",
        )


def test_portable_reference_acceptance_rejects_incomplete_matrix_under_optimization(tmp_path):
    import hashlib
    import subprocess
    import sys
    from pathlib import Path

    manifest = {
        "tolerance": {
            "absolute_millions": 2e-8,
            "relative": 2e-12,
            "justification": "Independent Decimal equations versus finite IEEE-754",
        },
        "oracle_basis": "Written source-free gross-tax equations",
        "cases": [
            {"id": "synthetic", "input_file": "input.json", "expected_file": "expected.json"}
        ],
    }
    (tmp_path / "cases.json").write_text(canonical(manifest))
    reference = tmp_path / "reference"
    reference.mkdir()
    (reference / "source.txt").write_bytes(b"source-free preserved reference")
    hashes = {"source.txt": hashlib.sha256((reference / "source.txt").read_bytes()).hexdigest()}
    (reference / "artifact-checksums.json").write_text(canonical(hashes))
    frozen = {
        "all_accepted_artifact_hashes": hashes,
        "artifact_checksums_sha256": hashlib.sha256(
            (reference / "artifact-checksums.json").read_bytes()
        ).hexdigest(),
        "reference_identity_sha256": hashlib.sha256(canonical(hashes).encode()).hexdigest(),
    }
    (tmp_path / "freeze.json").write_text(canonical(frozen))
    tool = Path(__file__).resolve().parents[1] / "tools/check_forward_fcff_acceptance.py"
    completed = subprocess.run(
        [
            sys.executable,
            "-O",
            "-B",
            str(tool),
            "--cases",
            str(tmp_path / "cases.json"),
            "--out",
            str(tmp_path / "acceptance"),
            "--reference-freeze",
            str(tmp_path / "freeze.json"),
            "--reference-root",
            str(reference),
        ],
        text=True,
        capture_output=True,
    )
    assert completed.returncode != 0
    assert "acceptance case matrix incomplete" in completed.stderr
    assert not (tmp_path / "acceptance" / "acceptance-receipt.json").exists()
