"""Full frozen evidence survives unsuccessful and interrupted research execution."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from forward_fcff_fixtures import request

from alphaforge.core.valuation.forward_fcff_contract import canonical, digest, freeze
from alphaforge.db import forward_fcff_runs as runs
from alphaforge.db.migrations import migrate

ROOT = Path(__file__).resolve().parents[1]


def prepare(tmp_path):
    req, _ = request()
    text = "Synthetic opening non-tax WC is zero. No issuer data."
    catalog = {
        "sources": [
            {
                "id": "synthetic-source",
                "url": "https://example.test/retention",
                "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "publication_date": "2026-06-30",
                "observation_date": "2026-06-30",
                "ingested_at": "2026-06-30T00:00:00Z",
                "anchors": {"p1": text},
                "limitations": [],
            }
        ]
    }
    req["evidence_context"] = {"catalog": catalog, "catalog_hash": digest(catalog)}
    req["operating_schedule"]["initial_wc"]["provenance"].update(
        {
            "class": "reported_fact",
            "uncertainty": "source_observed",
            "accounting_date": "2026-06-30",
            "source_refs": [{"source_id": "synthetic-source", "anchor": "p1"}],
        }
    )
    req = freeze(req).unpack()
    source = tmp_path / "source.txt"
    source.write_text(text)
    input_path = tmp_path / "input.json"
    input_path.write_text(canonical(req))
    database = tmp_path / "research.db"
    with sqlite3.connect(database) as conn:
        migrate(conn)
    args = [
        "--dsn",
        f"sqlite:///{database}",
        "forward-fcff",
        "--input-json",
        str(input_path),
        "--exports-dir",
        str(tmp_path / "exports"),
    ]
    return req, source, input_path, database, args


def child(tmp_path, command, label="command"):
    # Explicit minimal environment excludes all private acceptance opt-ins.
    env = {
        "PATH": os.defpath,
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    result = subprocess.run(command, env=env, cwd=ROOT, capture_output=True, text=True)
    (tmp_path / f"{label}-receipt.json").write_text(
        json.dumps(
            {
                "argv": command,
                "exit": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
    )
    return result


def retained(database, expected):
    with sqlite3.connect(database) as conn:
        identity, financial_hash, body_json, rules_json = conn.execute(
            "SELECT * FROM numerical_input_bodies"
        ).fetchone()
        body, rules = json.loads(body_json), json.loads(rules_json)
        assert canonical(body) == body_json
        assert digest(body) == financial_hash
        assert digest({"financial_inputs_hash": financial_hash, "rules": rules}) == identity
        assert body["context_hash"] == digest(expected["evidence_context"])
        assert canonical(body["request"]) == canonical(expected)
        assert freeze(body["request"]).body == freeze(expected).body
        return body


def test_public_output_failure_retains_complete_context(tmp_path):
    req, source, input_path, database, args = prepare(tmp_path)
    with sqlite3.connect(database) as conn:
        conn.execute(
            "CREATE TRIGGER fail_output BEFORE INSERT ON forward_fcff_runs "
            "BEGIN SELECT RAISE(ABORT,'synthetic output failure'); END"
        )
    result = child(tmp_path, [sys.executable, "-B", "-m", "alphaforge.cli.main", *args])
    assert result.returncode == 1 and "synthetic output failure" in result.stderr
    input_path.unlink()
    source.unlink()
    retained(database, req)
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM forward_fcff_runs").fetchone()[0] == 0
    assert not (tmp_path / "exports").exists()


def test_pre_evaluation_child_exit_reconstructs_from_durable_input(tmp_path):
    req, source, input_path, database, args = prepare(tmp_path)
    # Bounded test-only checkpoint at the actual owner's evaluator entry. The
    # owned child exits without evaluating or allowing Python connection cleanup.
    script = """
import json, os, sqlite3, sys
from pathlib import Path
from alphaforge.cli.main import main
from alphaforge.db import forward_fcff_runs as runs
from alphaforge.core.valuation.forward_fcff_contract import canonical

def checkpoint(request):
    with sqlite3.connect(sys.argv[1]) as reader:
        row = reader.execute("SELECT body,rules FROM numerical_input_bodies").fetchone()
        assert reader.execute("SELECT count(*) FROM forward_fcff_runs").fetchone()[0] == 0
    body = json.loads(row[0])
    assert canonical(body["request"]) == request.body
    Path(sys.argv[2]).write_text(json.dumps({"body": row[0], "rules": row[1]}))
    os._exit(73)

runs.evaluate = checkpoint
raise SystemExit(main(sys.argv[3:]))
"""
    checkpoint = tmp_path / "checkpoint.json"
    result = child(
        tmp_path,
        [sys.executable, "-B", "-c", script, str(database), str(checkpoint), *args],
    )
    assert result.returncode == 73, result.stderr
    input_path.unlink()
    source.unlink()
    reconstructed = retained(database, req)
    assert json.loads(checkpoint.read_text())["body"] == canonical(reconstructed)
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT count(*) FROM forward_fcff_runs").fetchone()[0] == 0
    assert not (tmp_path / "exports").exists()


def test_completed_replay_and_separate_corruption_copies(tmp_path):
    req, source, input_path, database, args = prepare(tmp_path)
    result = child(tmp_path, [sys.executable, "-B", "-m", "alphaforge.cli.main", *args])
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    artifact = tmp_path / "exports" / "forward-fcff" / "runs" / receipt["artifact_id"]
    original = (artifact / "outputs.json").read_bytes()
    input_path.unlink()
    source.unlink()
    retained(database, req)
    replay_args = ["forward-fcff-replay", "--run-id", str(receipt["run_id"])]
    replayed = child(
        tmp_path,
        [sys.executable, "-B", "-m", "alphaforge.cli.main", *args[:2], *replay_args],
        label="replay",
    )
    assert replayed.returncode == 0, replayed.stderr
    assert canonical(json.loads(replayed.stdout)["outputs"]).encode() == original
    with sqlite3.connect(database) as good:
        assert (
            good.execute("SELECT outputs FROM forward_fcff_runs").fetchone()[0].encode() == original
        )
        for part, reason in (
            ("body", "corrupt_retained_body"),
            ("textual_context", "invalid_textual_context"),
            ("outputs", "corrupt_retained_outputs"),
        ):
            copy = tmp_path / f"corrupt-{part}.db"
            with sqlite3.connect(copy) as bad:
                good.backup(bad)
                table = "numerical_input_bodies" if part == "body" else "forward_fcff_runs"
                trigger = "numerical_body_no_update" if part == "body" else "forward_fcff_no_update"
                bad.execute(f"DROP TRIGGER {trigger}")
                bad.execute(f"UPDATE {table} SET {part}='{{}}'")
            refused = child(
                tmp_path,
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "alphaforge.cli.main",
                    "--dsn",
                    f"sqlite:///{copy}",
                    *replay_args,
                ],
                label=f"corrupt-{part}",
            )
            assert refused.returncode == 1 and reason in refused.stderr
        assert canonical(runs.replay(good, receipt["run_id"])["outputs"]).encode() == original
