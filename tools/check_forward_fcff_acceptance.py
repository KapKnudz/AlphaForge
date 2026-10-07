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

CASE_GROUPS = (
    "flat_reference",
    "inflation_2_reference",
    "contraction_stress",
    "tail_inflation_1pct_after_year10",
    "tail_inflation_2pct_after_year10",
    "late2_refund_two_year_lag",
)
TRANSITION_GROUPS = {
    "tail_inflation_1pct_after_year10",
    "tail_inflation_2pct_after_year10",
    "late2_refund_two_year_lag",
}
REQUIRED_CASES = {
    f"{group}-return-{return_number}": {
        "horizon": 16 if group == "late2_refund_two_year_lag" else 15,
        "transition": group in TRANSITION_GROUPS,
    }
    for group in CASE_GROUPS
    for return_number in range(1, 4)
}
REQUIRED_FINANCIAL_PATHS = {
    "book_da",
    "boundary_tax_payment",
    "boundary_tax_refund",
    "cash_asset_purchases",
    "cash_operating_profit_before_da",
    "cash_operating_tax",
    "delta_wc",
    "ebit",
    "economic_lease_additions",
    "fcff_operations",
    "fcff_with_boundary_tax",
    "gross_economic_investment",
    "gross_tax_payment",
    "gross_tax_refund",
    "legs.asset/customer/closing",
    "legs.asset/customer/commissioned_additions",
    "legs.asset/customer/depreciation",
    "legs.asset/ppe/closing",
    "legs.asset/ppe/commissioned_additions",
    "legs.asset/ppe/depreciation",
    "legs.asset/rou/closing",
    "legs.asset/rou/commissioned_additions",
    "legs.asset/rou/depreciation",
    "legs.asset/software/closing",
    "legs.asset/software/commissioned_additions",
    "legs.asset/software/depreciation",
    "legs.tax/tax/arising",
    "legs.tax/tax/basis",
    "legs.tax/tax/entitlement",
    "legs.tax/tax/payable_close",
    "legs.tax/tax/receivable_close",
}


class AcceptanceError(RuntimeError):
    pass


def require(condition, message, details=None):
    if not condition:
        suffix = "" if details is None else f": {details}"
        raise AcceptanceError(f"{message}{suffix}")


def verify_reference(manifest, root):
    pinned = json.loads(manifest.read_text())
    hashes = pinned["all_accepted_artifact_hashes"]
    require(
        hashlib.sha256(canonical(hashes).encode()).hexdigest()
        == pinned["reference_identity_sha256"],
        "reference identity mismatch",
    )
    if "manifest_entries" in pinned:
        require(len(hashes) == pinned["manifest_entries"], "reference entry count mismatch")
    for name, expected in hashes.items():
        require(
            hashlib.sha256((root / name).read_bytes()).hexdigest() == expected,
            "reference artifact hash mismatch",
            name,
        )
    require(
        hashlib.sha256((root / "artifact-checksums.json").read_bytes()).hexdigest()
        == pinned["artifact_checksums_sha256"],
        "reference checksum manifest mismatch",
    )
    return {"matched": len(hashes), "identity": pinned["reference_identity_sha256"]}


def check_output(actual, expected, absolute, relative, case_id):
    def near(lhs, rhs, label):
        require(
            math.isfinite(lhs)
            and math.isfinite(rhs)
            and math.isclose(lhs, rhs, abs_tol=absolute, rel_tol=relative),
            "financial comparison mismatch",
            (label, lhs, rhs),
        )

    require(actual.get("scenario_id") == case_id, "scenario identity mismatch", case_id)
    require(actual["denomination"]["currency"] == "EUR", "reference currency mismatch", case_id)
    require(
        actual["operating_cash_flows"]["available"],
        "operating cash flows unavailable",
        actual["operating_cash_flows"],
    )
    require(
        actual["operating_value"]["available"],
        "operating value unavailable",
        actual["operating_value"],
    )
    require(
        not actual["common_equity"]["available"] and not actual["per_share"]["available"],
        "reference must remain operating-only",
        case_id,
    )
    rows = actual["operating_cash_flows"]["value"]
    require(len(rows) == len(expected["rows"]) == 22, "annual row coverage mismatch", case_id)
    checks = 0
    for row, oracle in zip(rows, expected["rows"], strict=True):
        require(row["index"] == oracle["index"], "annual row index mismatch", case_id)
        require(
            set(oracle["values"]) == REQUIRED_FINANCIAL_PATHS,
            "required financial path coverage mismatch",
            (case_id, row["index"]),
        )
        for path, value in oracle["values"].items():
            parts = path.split(".")
            v = row
            for part in parts:
                v = v[part]
            near(v, float(value), f"year {row['index']} {path}")
            checks += 1
    require(
        expected["horizon"] == REQUIRED_CASES[case_id]["horizon"]
        and actual["terminal"]["horizon"] == expected["horizon"],
        "corrected horizon mismatch",
        case_id,
    )
    near(
        actual["terminal"]["next_year_fcff"],
        float(expected["terminal_numerator"]),
        "exact next-year numerator",
    )
    near(actual["explicit_pv"], float(expected["explicit_pv"]), "explicit PV")
    near(actual["terminal"]["present_value"], float(expected["terminal_pv"]), "terminal PV")
    near(actual["operating_value"]["value"], float(expected["operating_value"]), "operating value")
    return checks + 4


def validate_manifest(manifest):
    require(
        set(manifest) == {"tolerance", "oracle_basis", "cases"},
        "acceptance manifest fields mismatch",
    )
    tolerance = manifest["tolerance"]
    require(
        set(tolerance) == {"absolute_millions", "relative", "justification"},
        "tolerance fields mismatch",
    )
    absolute = float(tolerance["absolute_millions"])
    relative = float(tolerance["relative"])
    require(0 < absolute <= 2e-8 and 0 < relative <= 2e-12, "tolerance exceeds admission")
    require(manifest["oracle_basis"] and tolerance["justification"], "oracle basis missing")
    cases = manifest["cases"]
    require(isinstance(cases, list), "cases must be an array")
    by_id = {}
    for case in cases:
        require(isinstance(case, dict), "case must be an object")
        identity = case.get("id")
        require(identity not in by_id, "duplicate acceptance case", identity)
        by_id[identity] = case
    require(set(by_id) == set(REQUIRED_CASES), "acceptance case matrix incomplete")
    for identity, dimensions in REQUIRED_CASES.items():
        case = by_id[identity]
        fields = {"id", "input_file", "expected_file"}
        if dimensions["transition"]:
            fields.add("transitional_input_file")
        require(set(case) == fields, "acceptance case dimensions mismatch", identity)
        require(case["input_file"] == f"{identity}.input.json", "input pairing mismatch", identity)
        require(
            case["expected_file"] == f"{identity}.expected.json",
            "expected pairing mismatch",
            identity,
        )
        if dimensions["transition"]:
            require(
                case["transitional_input_file"] == f"{identity}.transitional.json",
                "transition pairing mismatch",
                identity,
            )
    return absolute, relative


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
    absolute, relative = validate_manifest(manifest)
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
        require(
            set(expected) == {
                "basis",
                "rows",
                "horizon",
                "terminal_numerator",
                "explicit_pv",
                "terminal_pv",
                "operating_value",
            },
            "expected receipt fields mismatch",
            case["id"],
        )
        require(expected["basis"] == manifest["oracle_basis"], "oracle basis mismatch", case["id"])
        frozen = freeze(input_path.read_text())
        pure = evaluate(frozen)
        checks = check_output(pure, expected, absolute, relative, case["id"])
        if "transitional_input_file" in case:
            transitional_frozen = freeze((base / case["transitional_input_file"]).read_text())
            corrected_request = frozen.unpack()
            transitional_request = transitional_frozen.unpack()
            corrected_terminal = corrected_request.pop("terminal")
            transitional_terminal = transitional_request.pop("terminal")
            require(
                corrected_request == transitional_request,
                "transition pair changed nonterminal inputs",
                case["id"],
            )
            require(
                corrected_terminal["horizon"] == transitional_terminal["horizon"] + 1,
                "transition pair horizon mismatch",
                case["id"],
            )
            failed = evaluate(transitional_frozen)
            require(
                failed.get("scenario_id") == case["id"],
                "transition scenario identity mismatch",
                case["id"],
            )
            require(
                failed["operating_cash_flows"]["available"]
                and not failed["operating_value"]["available"],
                "transition did not preserve cash and refuse value",
                case["id"],
            )
            require(
                canonical(failed["operating_cash_flows"])
                == canonical(pure["operating_cash_flows"]),
                "transition pair changed operating cash flows",
                case["id"],
            )
            require(
                failed["terminal"]["horizon"] == expected["horizon"] - 1,
                "transition horizon mismatch",
                case["id"],
            )
            require(
                bool(failed["terminal"]["certificate"]["failures"]),
                "transition refusal certificate missing",
                case["id"],
            )
            require(
                failed["terminal"]["present_value"] is None,
                "transition emitted terminal value",
                case["id"],
            )

        def cli(*words):
            completed = subprocess.run(
                [sys.executable, "-m", "alphaforge.cli.main", "--dsn", dsn, *words],
                text=True,
                capture_output=True,
                check=True,
            )
            return json.loads(completed.stdout)

        if "transitional_input_file" in case:
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
            require(
                transitional_output["operating_cash_flows"]["available"]
                and not transitional_output["operating_value"]["available"],
                "CLI transition did not preserve cash and refuse value",
                case["id"],
            )
            transitional_replay = cli(
                "forward-fcff-replay", "--run-id", str(transitional_run["run_id"])
            )
            require(
                bool(transitional_output["terminal"]["certificate"]["failures"]),
                "CLI transition refusal certificate missing",
                case["id"],
            )
            require(
                canonical(transitional_replay["outputs"]).encode() == transitional_bytes,
                "CLI transition replay mismatch",
                case["id"],
            )
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
        checks += check_output(actual, expected, absolute, relative, case["id"])
        require(output_bytes == canonical(pure).encode(), "pure/CLI output mismatch", case["id"])
        replay = cli("forward-fcff-replay", "--run-id", str(run["run_id"]))
        require(replay["status"] == "exact_replay", "replay status mismatch", case["id"])
        require(
            canonical(replay["outputs"]).encode() == output_bytes,
            "replay is not exact",
            case["id"],
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
    require(receipt["reference_after"] == before, "reference changed during acceptance")
    (args.out / "acceptance-receipt.json").write_text(canonical(receipt) + "\n")
    print(canonical(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
