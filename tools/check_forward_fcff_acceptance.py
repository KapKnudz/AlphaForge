#!/usr/bin/env python3
"""Portable private reference acceptance. Does not acquire or modify reference bytes.

Inputs and expected receipts must be prepared independently of the new evaluator.
Financial comparison is tolerant; retained-output replay is exact canonical bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

# Allow the repository script to execute without an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alphaforge.core.valuation.forward_fcff import evaluate  # noqa: E402
from alphaforge.core.valuation.forward_fcff_contract import canonical, freeze  # noqa: E402


def verify_reference(manifest, root):
    pinned = json.loads(manifest.read_text())
    hashes = pinned["all_accepted_artifact_hashes"]
    assert (
        hashlib.sha256(canonical(hashes).encode()).hexdigest()
        == pinned["reference_identity_sha256"]
    )
    if "manifest_entries" in pinned:
        assert len(hashes) == pinned["manifest_entries"]
    for name, expected in hashes.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected, name
    assert (
        hashlib.sha256((root / "artifact-checksums.json").read_bytes()).hexdigest()
        == pinned["artifact_checksums_sha256"]
    )
    return {"matched": len(hashes), "identity": pinned["reference_identity_sha256"]}


def check_output(actual, expected, absolute, relative):
    def near(lhs, rhs, label):
        assert (
            math.isfinite(lhs)
            and math.isfinite(rhs)
            and math.isclose(lhs, rhs, abs_tol=absolute, rel_tol=relative)
        ), (label, lhs, rhs)

    assert actual["operating_cash_flows"]["available"], actual["operating_cash_flows"]
    assert actual["operating_value"]["available"], actual["operating_value"]
    assert not actual["common_equity"]["available"] and not actual["per_share"]["available"]
    rows = actual["operating_cash_flows"]["value"]
    assert len(rows) == len(expected["rows"])
    checks = 0
    for row, oracle in zip(rows, expected["rows"], strict=True):
        assert row["index"] == oracle["index"]
        for path, value in oracle["values"].items():
            parts = path.split(".")
            v = row
            for part in parts:
                v = v[part]
            near(v, float(value), f"year {row['index']} {path}")
            checks += 1
    assert actual["terminal"]["horizon"] == expected["horizon"]
    near(
        actual["terminal"]["next_year_fcff"],
        float(expected["terminal_numerator"]),
        "exact next-year numerator",
    )
    near(actual["explicit_pv"], float(expected["explicit_pv"]), "explicit PV")
    near(actual["terminal"]["present_value"], float(expected["terminal_pv"]), "terminal PV")
    near(actual["operating_value"]["value"], float(expected["operating_value"]), "operating value")
    return checks + 4


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--cases",
        type=Path,
        required=True,
        help="Private manifest, input and independent expected receipts",
    )
    p.add_argument(
        "--out", type=Path, required=True, help="Private SQLite, exports and receipt directory"
    )
    p.add_argument("--reference-freeze", type=Path, required=True)
    p.add_argument("--reference-root", type=Path, required=True)
    args = p.parse_args()
    before = verify_reference(args.reference_freeze, args.reference_root)
    manifest = json.loads(args.cases.read_text())
    base = args.cases.parent
    absolute = float(manifest["tolerance"]["absolute_millions"])
    relative = float(manifest["tolerance"]["relative"])
    assert 0 < absolute <= 2e-8 and 0 < relative <= 2e-12
    assert manifest["oracle_basis"] and manifest["tolerance"]["justification"]
    args.out.mkdir(parents=True, exist_ok=True)
    dsn = f"sqlite:///{(args.out / 'research.db').resolve()}"
    receipt = {
        "tolerance": manifest["tolerance"],
        "oracle_basis": manifest["oracle_basis"],
        "reference_before": before,
        "cases": [],
    }
    for case in manifest["cases"]:
        input_path = base / case["input_file"]
        expected = json.loads((base / case["expected_file"]).read_text())
        frozen = freeze(input_path.read_text())
        pure = evaluate(frozen)
        checks = check_output(pure, expected, absolute, relative)
        if case.get("transitional_input_file"):
            failed = evaluate(freeze((base / case["transitional_input_file"]).read_text()))
            assert (
                failed["operating_cash_flows"]["available"]
                and not failed["operating_value"]["available"]
            )
            assert failed["terminal"]["certificate"]["failures"]
            assert failed["terminal"]["present_value"] is None

        def cli(*words):
            completed = subprocess.run(
                [sys.executable, "-m", "alphaforge.cli.main", "--dsn", dsn, *words],
                text=True,
                capture_output=True,
                check=True,
            )
            return json.loads(completed.stdout)

        if case.get("transitional_input_file"):
            transitional_run = cli(
                "forward-fcff",
                "--input-json",
                str(base / case["transitional_input_file"]),
                "--exports-dir",
                str(args.out / "exports"),
            )
            transitional_bytes = (
                Path(transitional_run["export_directory"]) / "outputs.json"
            ).read_bytes()
            transitional_output = json.loads(transitional_bytes)
            assert (
                transitional_output["operating_cash_flows"]["available"]
                and not transitional_output["operating_value"]["available"]
            )
            transitional_replay = cli(
                "forward-fcff-replay", "--run-id", str(transitional_run["run_id"])
            )
            assert transitional_output["terminal"]["certificate"]["failures"]
            assert canonical(transitional_replay["outputs"]).encode() == transitional_bytes
        run = cli(
            "forward-fcff",
            "--input-json",
            str(input_path),
            "--exports-dir",
            str(args.out / "exports"),
        )
        exported = Path(run["export_directory"]) / "outputs.json"
        output_bytes = exported.read_bytes()
        actual = json.loads(output_bytes)
        checks += check_output(actual, expected, absolute, relative)
        assert output_bytes == canonical(pure).encode(), "pure/CLI output mismatch"
        replay = cli("forward-fcff-replay", "--run-id", str(run["run_id"]))
        assert replay["status"] == "exact_replay"
        assert canonical(replay["outputs"]).encode() == output_bytes, (
            "replay is exact, not tolerance-based"
        )
        receipt["cases"].append(
            {
                "id": case["id"],
                "run_id": run["run_id"],
                "artifact_id": run["artifact_id"],
                "numerical_identity": run["numerical_identity"],
                "financial_comparisons": checks,
                "exact_replay": True,
                "horizon": actual["terminal"]["horizon"],
                "terminal_numerator": actual["terminal"]["next_year_fcff"],
                "conditional_operating_value": actual["operating_value"]["value"],
                "currency": actual["denomination"]["currency"],
                "equity_available": False,
                "per_share_available": False,
            }
        )
    receipt["reference_after"] = verify_reference(args.reference_freeze, args.reference_root)
    assert receipt["reference_after"] == before
    (args.out / "acceptance-receipt.json").write_text(canonical(receipt) + "\n")
    print(canonical(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
