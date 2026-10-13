"""Retained-only forward research execution, distinct from ranking run IDs."""

from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
import subprocess
from pathlib import Path

from alphaforge.core.valuation.forward_fcff import evaluate
from alphaforge.core.valuation.forward_fcff_contract import (
    CONVENTION,
    METHOD,
    SCHEMA,
    VALIDATION,
    FrozenRequest,
    canonical,
    digest,
    freeze,
)
from alphaforge.db.numerical_runs import ReplayRefusal, retain_inputs

ROOT = Path(__file__).resolve().parents[2]
ENCODING = "forward-fcff-retained-v1"


def rules_bundle():
    paths = [
        "alphaforge/core/valuation/forward_fcff.py",
        "alphaforge/core/valuation/forward_fcff_contract.py",
        "alphaforge/core/frozen_packet.py",
        "alphaforge/db/forward_fcff_runs.py",
        "alphaforge/cli/forward_fcff.py",
        "alphaforge/cli/main.py",
        "alphaforge/db/numerical_runs.py",
        "alphaforge/db/migrations.py",
        "alphaforge/db/connection.py",
        "alphaforge/config.py",
        "db/alphaforge.sqlite.sql",
    ] + [str(p.relative_to(ROOT)) for p in sorted((ROOT / "db/migrations").glob("*.sql"))]
    return {
        "encoding": ENCODING,
        "schema": SCHEMA,
        "method": METHOD,
        "economic_convention": CONVENTION,
        "validation": VALIDATION,
        "code_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "sources": {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths},
        "runtime": platform.python_implementation() + "-" + platform.python_version(),
        "sqlite_runtime": sqlite3.sqlite_version,
        "serialization": "sorted compact UTF-8 finite IEEE-754; exact replay bytes",
    }


def execute(conn, request: FrozenRequest):
    # Separate retained textual body, bound by hash inside the numerical request.
    request = freeze(request.body)
    full = request.unpack()
    context = full.pop("evidence_context")
    body = {"encoding": ENCODING, "request": full, "context_hash": digest(context)}
    rules = rules_bundle()
    identity, financial_hash = retain_inputs(conn, body, rules)  # commits before evaluate
    retained = conn.execute(
        "SELECT body FROM numerical_input_bodies WHERE numerical_identity=?", (identity,)
    ).fetchone()
    if retained is None or retained[0] != canonical(body):
        raise ReplayRefusal("corrupt_retained_body")
    outputs = evaluate(request)
    outputs_json = canonical(outputs)
    with conn:
        cursor = conn.execute(
            "INSERT INTO forward_fcff_runs(numerical_identity,textual_context,textual_context_hash,outputs,outputs_hash) VALUES (?,?,?,?,?)",
            (identity, canonical(context), digest(context), outputs_json, digest(outputs)),
        )
        run_id = cursor.lastrowid
    receipt = {
        "lane": ENCODING,
        "run_id": run_id,
        "numerical_identity": identity,
        "financial_inputs_hash": financial_hash,
        "textual_context_hash": digest(context),
        "outputs_hash": digest(outputs),
        "rules": rules,
    }
    receipt["artifact_id"] = digest(
        {
            k: receipt[k]
            for k in (
                "lane",
                "run_id",
                "numerical_identity",
                "textual_context_hash",
                "outputs_hash",
            )
        }
    )
    return receipt, outputs


def replay(conn, run_id):
    row = conn.execute(
        "SELECT numerical_identity,textual_context,textual_context_hash,outputs,outputs_hash FROM forward_fcff_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise ReplayRefusal("missing_run")
    identity, text_body, text_hash, outputs_body, outputs_hash = tuple(row)
    retained = conn.execute(
        "SELECT financial_inputs_hash,body,rules FROM numerical_input_bodies WHERE numerical_identity=?",
        (identity,),
    ).fetchone()
    if retained is None:
        raise ReplayRefusal("missing_snapshot")
    financial_hash, body_json, rules_json = tuple(retained)
    try:
        body = json.loads(body_json)
        rules = json.loads(rules_json)
        context = json.loads(text_body)
        original = json.loads(outputs_body)
        if (
            canonical(body) != body_json
            or digest(body) != financial_hash
            or digest({"financial_inputs_hash": financial_hash, "rules": rules}) != identity
        ):
            raise ReplayRefusal("corrupt_retained_body")
        if (
            digest(context) != text_hash
            or text_hash != body["context_hash"]
            or canonical(context) != text_body
        ):
            raise ReplayRefusal("invalid_textual_context")
        if digest(original) != outputs_hash or canonical(original) != outputs_body:
            raise ReplayRefusal("corrupt_retained_outputs")
        if body["encoding"] != ENCODING or rules != rules_bundle():
            raise ReplayRefusal("unsupported_rules_or_code")
        request = freeze({**body["request"], "evidence_context": context})
        actual = evaluate(request)
        if canonical(actual) != outputs_body:
            raise ReplayRefusal("output_mismatch")
        return {
            "status": "exact_replay",
            "run_id": run_id,
            "numerical_identity": identity,
            "outputs_hash": outputs_hash,
            "outputs": actual,
        }
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, ReplayRefusal):
            raise
        raise ReplayRefusal("corrupt_retained_body") from error
