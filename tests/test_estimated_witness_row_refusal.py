"""Malformed row evidence must not erase unrelated finite forecast diagnostics."""

import copy
import csv
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from alphaforge.cli.estimated_forward import SOURCE_PATHS
from alphaforge.core.valuation.estimated_fcff import evaluate
from alphaforge.core.valuation.estimated_inputs import canonical
from tests.estimated_fixtures import pack

ROOT = Path(__file__).resolve().parents[1]


def case(index, defect):
    p = pack()
    p["records"]["row-only"] = copy.deepcopy(p["records"]["budget"])
    row = p["scenarios"]["base"]["operations"]["rows"][index]
    row["record_ids"] = ["row-only"]
    if defect == "rationale":
        p["records"]["row-only"]["rationale"] = ""
    elif defect != "valid":
        row["operating_cash_tax"] = None if defect == "missing" else "abc"
    return p


def assert_scoped(result, control, supplied, index, defect):
    if defect == "valid":
        assert result == control
        assert result["terminal"]["available"]
        return
    status, reason = {
        "rationale": (
            "invalid_input",
            {
                "code": "malformed_stage_input",
                "path": "nonempty_text_required: records.row-only.rationale",
            },
        ),
        "missing": (
            "insufficient_evidence",
            {"code": "missing_operand", "path": f"annual.{index + 1}.operating_cash_tax"},
        ),
        "invalid": (
            "invalid_input",
            {"code": "invalid_number", "path": f"annual.{index + 1}.operating_cash_tax"},
        ),
    }[defect]
    rows = result["annual_fcff"]["value"]
    assert len(rows) == 12
    for i, row in enumerate(rows):
        if i != index:
            assert row == control["annual_fcff"]["value"][i]
    assert not rows[index]["available"]
    assert rows[index]["status"] == status
    assert rows[index]["reasons"] == [reason]
    assert rows[index]["supplied_components_not_qualified_fcff"] == supplied
    if index >= 10:
        assert result["annual_fcff"]["available"]
        assert rows[:10] == control["annual_fcff"]["value"][:10]
        assert result["explicit_pv"] == control["explicit_pv"]
        assert len(result["explicit_pv"]["value"]["annual_pv"]) == 10
    else:
        assert not result["annual_fcff"]["available"]
        for key in ("annual_fcff", "explicit_pv"):
            assert result[key]["status"] == status
            assert result[key]["reasons"] == [reason]
    for key in ("terminal", "operating_value", "common_equity"):
        assert not result[key]["available"]
        assert result[key]["status"] == status
        assert result[key]["reasons"] == [reason]


@pytest.mark.parametrize("index", [2, 10, 11])
@pytest.mark.parametrize("defect", ["valid", "rationale", "missing", "invalid"])
def test_evaluate_row_evidence_refusal_is_scoped(index, defect):
    p = case(index, defect)
    before = copy.deepcopy(p)
    control = evaluate(case(index, "valid"))["scenarios"]["base"]
    result = evaluate(p)["scenarios"]["base"]
    assert p == before
    assert_scoped(
        result, control, before["scenarios"]["base"]["operations"]["rows"][index], index, defect
    )


@pytest.mark.parametrize("index", [2, 10, 11])
@pytest.mark.parametrize("defect", ["valid", "rationale", "missing", "invalid"])
def test_cli_row_evidence_keeps_diagnostics_and_bound_receipt(tmp_path, index, defect):
    p = case(index, defect)
    before = copy.deepcopy(p)
    input_path = tmp_path / "input.json"
    input_bytes = json.dumps(p).encode()
    input_path.write_bytes(input_bytes)
    run = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "alphaforge.cli.main",
            "estimated-forward",
            "--input-json",
            str(input_path),
            "--exports-dir",
            str(tmp_path / "exports"),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"},
        text=True,
        capture_output=True,
    )
    assert run.returncode == 0, run.stderr
    target = Path(json.loads(run.stdout)["export_directory"])
    outputs = json.loads((target / "outputs.json").read_text())
    control = json.loads(canonical(evaluate(case(index, "valid"))))["scenarios"]["base"]
    assert_scoped(
        outputs["scenarios"]["base"],
        control,
        before["scenarios"]["base"]["operations"]["rows"][index],
        index,
        defect,
    )
    with (target / "annual.csv").open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 12
    for i, row in enumerate(rows):
        diagnostic = outputs["scenarios"]["base"]["annual_fcff"]["value"][i]
        assert int(row["year"]) == i + 1
        assert row["status"] == diagnostic["status"]
        assert json.loads(row["reasons"]) == diagnostic["reasons"]
        assert json.loads(row["row"]) == diagnostic["value"]
    assert input_path.read_bytes() == input_bytes
    assert p == before
    receipt = outputs["execution"]
    assert (
        receipt["code_revision"]
        == subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    )
    assert receipt["code_sources"] == {
        path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in SOURCE_PATHS
    }
