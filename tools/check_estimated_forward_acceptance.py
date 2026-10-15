#!/usr/bin/env python3
"""Compare private estimated-forward inputs with independent expected outputs."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from alphaforge.core.valuation.estimated_fcff import evaluate  # noqa: E402
from alphaforge.core.valuation.estimated_inputs import (  # noqa: E402
    CONTRACT,
    LABEL,
    STANDARD,
    canonical,
    load,
)

ACCEPTANCE_CONTRACT = "estimated-forward-reference-acceptance-v1"
TOLERANCES = {
    "annual": {"absolute": "2e-8", "relative": "2e-12"},
    "value": {"absolute": "1e-7", "relative": "2e-11"},
    "per_share": {"absolute": "1e-8", "relative": "2e-11"},
}


class AcceptanceError(RuntimeError):
    pass


def require(condition, message, path=""):
    if not condition:
        raise AcceptanceError(f"{message}{': ' + path if path else ''}")


def _decimal(value):
    if not isinstance(value, str):
        return None
    try:
        result = Decimal(value)
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def _tolerance(path):
    if len(path) < 5 or path[0] != "scenarios" or path[3] != "value":
        return None
    output = path[2]
    leaf = path[-1]
    if output == "basic_shares" or leaf == "count":
        return None
    if output == "annual_fcff":
        return "annual"
    if output == "wacc":
        return "value" if leaf == "market_equity" else "annual"
    if output == "terminal":
        if leaf == "growth":
            return None
        return (
            "annual"
            if "witnesses" in path or leaf in {"numerator", "pole_distance"}
            else "value"
        )
    if output == "common_equity" and leaf in {"underlying_reporting", "per_right_reporting"}:
        return "per_share"
    if output in {"explicit_pv", "operating_value", "bridge", "common_equity"}:
        return "value"
    if output in {"per_share_reporting", "per_share_quote"}:
        return "per_share"
    return None


def compare_outputs(actual, expected, path=()):
    location = "/" + "/".join(str(part) for part in path)
    require(type(actual) is type(expected), "expected output type mismatch", location)
    if isinstance(expected, dict):
        require(set(actual) == set(expected), "expected output fields mismatch", location)
        for key in expected:
            compare_outputs(actual[key], expected[key], (*path, key))
        return
    if isinstance(expected, list):
        require(len(actual) == len(expected), "expected output length mismatch", location)
        for index, value in enumerate(expected):
            compare_outputs(actual[index], value, (*path, index))
        return
    tolerance = _tolerance(path)
    lhs, rhs = _decimal(actual), _decimal(expected)
    if tolerance is not None and lhs is not None and rhs is not None:
        limits = TOLERANCES[tolerance]
        limit = max(
            Decimal(limits["absolute"]),
            Decimal(limits["relative"]) * max(abs(lhs), abs(rhs)),
        )
        require(abs(lhs - rhs) <= limit, f"{tolerance} comparison mismatch", location)
        return
    require(actual == expected, "exact comparison mismatch", location)


def validate_manifest(manifest):
    require(
        set(manifest) == {"contract", "oracle_basis", "tolerances", "cases"},
        "acceptance manifest fields mismatch",
    )
    require(manifest["contract"] == ACCEPTANCE_CONTRACT, "acceptance contract mismatch")
    require(
        isinstance(manifest["oracle_basis"], str) and manifest["oracle_basis"].strip(),
        "oracle basis missing",
    )
    require(manifest["tolerances"] == TOLERANCES, "acceptance tolerances changed")
    require(
        isinstance(manifest["cases"], list) and manifest["cases"],
        "acceptance cases missing",
    )
    identities = set()
    for case in manifest["cases"]:
        require(
            set(case) == {"id", "input_file", "expected_file"},
            "acceptance case fields mismatch",
        )
        require(
            isinstance(case["id"], str) and case["id"].strip(),
            "acceptance case identity missing",
        )
        require(case["id"] not in identities, "duplicate acceptance case", case["id"])
        identities.add(case["id"])
        for field in ("input_file", "expected_file"):
            value = case[field]
            require(
                isinstance(value, str) and Path(value).name == value,
                "acceptance file must be a sibling basename",
                field,
            )


def validate_expected(expected, oracle_basis):
    require(expected.get("oracle_basis") == oracle_basis, "oracle basis mismatch")
    result = expected.get("result")
    require(isinstance(result, dict), "independent expected result missing")
    identity = result.get("identity", {})
    require(identity.get("standard") == STANDARD, "expected standard mismatch")
    require(identity.get("contract") == CONTRACT, "expected evaluator contract mismatch")
    require(identity.get("label") == LABEL, "expected label mismatch")
    for field in (
        "pack_content_identity",
        "source_catalog_identity",
        "pack_version",
        "entity",
        "perimeter",
        "valuation_date",
        "knowledge_cutoff",
        "currency",
        "quote_currency",
        "money_units",
        "share_units",
        "lease_basis",
    ):
        require(field in identity, "expected exact identity field missing", field)
    require(
        result.get("calculation_status") == "executed_not_financial_acceptance",
        "expected calculation status mismatch",
    )
    require(
        isinstance(result.get("scenarios"), dict) and "base" in result["scenarios"],
        "expected base scenario missing",
    )
    for name, scenario in result["scenarios"].items():
        require(isinstance(scenario, dict), "expected scenario missing", name)
        for output, value in scenario.items():
            require(
                isinstance(value, dict)
                and {
                    "status",
                    "calculation_status",
                    "available",
                    "value",
                    "reasons",
                }
                <= set(value),
                "expected output status fields missing",
                f"{name}/{output}",
            )


def _code_identity(root):
    paths = (
        "alphaforge/core/valuation/estimated_inputs.py",
        "alphaforge/core/valuation/estimated_projection.py",
        "alphaforge/core/valuation/estimated_fcff.py",
        "tools/check_estimated_forward_acceptance.py",
    )
    body = b"".join(path.encode() + b"\0" + (root / path).read_bytes() for path in paths)
    return hashlib.sha256(body).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = load(args.manifest.read_text(encoding="utf-8"))
        validate_manifest(manifest)
        root = Path(__file__).resolve().parents[1]
        base = args.manifest.parent
        for case in manifest["cases"]:
            supplied = load((base / case["input_file"]).read_text(encoding="utf-8"))
            expected = load((base / case["expected_file"]).read_text(encoding="utf-8"))
            validate_expected(expected, manifest["oracle_basis"])
            compare_outputs(evaluate(supplied), expected["result"])
        revision = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip()
        print(
            canonical(
                {
                    "status": "estimated_reference_matched",
                    "cases": len(manifest["cases"]),
                    "code_revision": revision,
                    "code_identity": _code_identity(root),
                }
            )
        )
        return 0
    except (AcceptanceError, OSError, KeyError, TypeError, ValueError) as error:
        print(
            canonical({"status": "estimated_reference_mismatch", "reason": str(error)}),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
