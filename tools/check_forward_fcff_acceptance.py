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
        "transition_failure": (
            {
                "code": "terminal_regime_not_stationary",
                "path": (
                    "tax/tax/refund" if group == "late2_refund_two_year_lag" else "tax/tax/payment"
                ),
                "regime_start": 17 if group == "late2_refund_two_year_lag" else 16,
                "requested_tail_start": 16 if group == "late2_refund_two_year_lag" else 15,
            }
            if group in TRANSITION_GROUPS
            else None
        ),
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


def _require_output_mask(result, availability, case_id):
    names = ("operating_cash_flows", "operating_value", "common_equity", "per_share")
    require(
        tuple(result[name]["available"] for name in names) == availability,
        "output availability mask mismatch",
        case_id,
    )
    for name, available in zip(names, availability, strict=True):
        require(
            (result[name]["value"] is not None) == available,
            "output value mask mismatch",
            (case_id, name),
        )


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
    _require_output_mask(actual, (True, True, False, False), case_id)
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


def _operand_premise(operand):
    premise = json.loads(canonical(operand))
    premise["provenance"].pop("id", None)
    return premise


def _queue_state(request, horizon, ledger_id, leg):
    lot_leg = "payable" if leg == "payment" else "refund"
    lots = {
        lot["id"]: lot
        for lot in request["operating_schedule"]["tax_lots"]
        if lot["origin"] == "forecast_created"
        and lot["ledger_id"] == ledger_id
        and lot["leg"] == lot_leg
        and lot["source_period"] <= horizon
    }
    remaining = {identity: float(lot["amount"]["value"]) for identity, lot in lots.items()}
    for row in request["operating_schedule"]["periods"][:horizon]:
        for ledger in row["tax_ledgers"]:
            for movement in ledger.get("lot_movements", []):
                if movement["lot_id"] in remaining:
                    remaining[movement["lot_id"]] += float(movement["amount"]["value"])
            for allocation in ledger["allocations"]:
                if allocation["lot_id"] in remaining:
                    identity = allocation["lot_id"]
                    remaining[identity] = max(
                        0.0,
                        remaining[identity] - float(allocation["amount"]["value"]),
                    )
    return {
        identity: (lots[identity]["source_period"], amount)
        for identity, amount in remaining.items()
        if amount != 0
    }


def _check_terminal_coordinates(request, result, case_id):
    terminal = request["terminal"]
    horizon = terminal["horizon"]
    require(terminal["tail_start"] == horizon + 1, "transition tail coordinate mismatch", case_id)
    rows = result["operating_cash_flows"]["value"]
    require(horizon < len(rows), "transition seed row missing", case_id)
    seed_row = rows[horizon]
    for template in terminal["templates"]:
        path = template["path"]
        require(path in seed_row["legs"], "transition seed path missing", (case_id, path))
        require(
            math.isclose(
                float(template["seed"]["value"]),
                float(seed_row["legs"][path]),
                abs_tol=1e-8,
                rel_tol=1e-8,
            ),
            "transition seed advancement mismatch",
            (case_id, path),
        )
    for kernel in terminal["tax_kernels"]:
        expected = _queue_state(request, horizon, kernel["ledger_id"], kernel["leg"])
        actual = {}
        for queue in kernel["queues"]:
            identity = queue["lot_id"]
            require(identity not in actual, "transition queue duplicated", (case_id, identity))
            actual[identity] = (
                queue["source_period"],
                float(queue["remaining_at_tail"]["value"]),
            )
        require(set(actual) == set(expected), "transition queue inventory mismatch", case_id)
        for identity, (source_period, amount) in expected.items():
            require(
                actual[identity][0] == source_period,
                "transition queue period mismatch",
                identity,
            )
            require(
                math.isclose(actual[identity][1], amount, abs_tol=1e-8, rel_tol=1e-8),
                "transition queue advancement mismatch",
                identity,
            )


def _queue_premises(terminal, kernel):
    premises = []
    for queue in kernel["queues"]:
        provenance = {
            key: value
            for key, value in queue["remaining_at_tail"]["provenance"].items()
            if key != "id"
        }
        premises.append(
            (
                terminal["horizon"] - queue["source_period"],
                canonical(provenance),
            )
        )
    return sorted(premises)


def _exhausted_states(terminal, case_id):
    states = terminal["exhausted_states"]
    identities = [state["id"] for state in states]
    require(
        len(identities) == len(set(identities)),
        "transition exhausted-state identity duplicated",
        case_id,
    )
    return {state["id"]: state for state in states}


def check_transition_pair(
    corrected_request, transitional_request, corrected, transitional, case_id
):
    corrected_nonterminal = dict(corrected_request)
    transitional_nonterminal = dict(transitional_request)
    corrected_terminal = corrected_nonterminal.pop("terminal")
    transitional_terminal = transitional_nonterminal.pop("terminal")
    require(
        corrected_nonterminal == transitional_nonterminal,
        "transition pair changed nonterminal inputs",
        case_id,
    )
    require(
        corrected_terminal["horizon"] == transitional_terminal["horizon"] + 1,
        "transition pair horizon mismatch",
        case_id,
    )
    invariant_fields = {
        "kind",
        "continuing_perimeter",
        "renewal_basis",
        "survival_basis",
        "price_volume_basis",
        "growth_funding_basis",
    }
    require(
        {field: corrected_terminal[field] for field in invariant_fields}
        == {field: transitional_terminal[field] for field in invariant_fields},
        "transition terminal premise mismatch",
        case_id,
    )
    require(
        _operand_premise(corrected_terminal["growth"])
        == _operand_premise(transitional_terminal["growth"]),
        "transition growth premise mismatch",
        case_id,
    )
    _check_terminal_coordinates(corrected_request, corrected, case_id)
    _check_terminal_coordinates(transitional_request, transitional, case_id)
    corrected_templates = {template["id"]: template for template in corrected_terminal["templates"]}
    transitional_templates = {
        template["id"]: template for template in transitional_terminal["templates"]
    }
    require(
        set(corrected_templates) == set(transitional_templates),
        "transition template inventory mismatch",
        case_id,
    )
    for identity, corrected_template in corrected_templates.items():
        transitional_template = transitional_templates[identity]
        for field in ("path", "regime_start", "derivation"):
            require(
                corrected_template[field] == transitional_template[field],
                "transition template premise mismatch",
                (case_id, identity, field),
            )
        require(
            _operand_premise(corrected_template["multiplier"])
            == _operand_premise(transitional_template["multiplier"]),
            "transition multiplier premise mismatch",
            (case_id, identity),
        )
        corrected_provenance = dict(corrected_template["seed"]["provenance"])
        transitional_provenance = dict(transitional_template["seed"]["provenance"])
        corrected_provenance.pop("id", None)
        transitional_provenance.pop("id", None)
        require(
            corrected_provenance == transitional_provenance,
            "transition seed provenance mismatch",
            (case_id, identity),
        )
    corrected_kernels = {kernel["id"]: kernel for kernel in corrected_terminal["tax_kernels"]}
    transitional_kernels = {kernel["id"]: kernel for kernel in transitional_terminal["tax_kernels"]}
    require(
        set(corrected_kernels) == set(transitional_kernels),
        "transition kernel inventory mismatch",
        case_id,
    )
    for identity, corrected_kernel in corrected_kernels.items():
        transitional_kernel = transitional_kernels[identity]
        for field in ("ledger_id", "leg", "max_lag", "rationale"):
            require(
                corrected_kernel[field] == transitional_kernel[field],
                "transition kernel premise mismatch",
                (case_id, identity, field),
            )
        require(
            _operand_premise(corrected_kernel["recovery_fraction"])
            == _operand_premise(transitional_kernel["recovery_fraction"]),
            "transition recovery premise mismatch",
            (case_id, identity),
        )
        corrected_weights = {
            weight["offset_years"]: _operand_premise(weight["weight"])
            for weight in corrected_kernel["weights"]
        }
        transitional_weights = {
            weight["offset_years"]: _operand_premise(weight["weight"])
            for weight in transitional_kernel["weights"]
        }
        require(
            corrected_weights == transitional_weights,
            "transition kernel weights mismatch",
            (case_id, identity),
        )
        require(
            _queue_premises(corrected_terminal, corrected_kernel)
            == _queue_premises(transitional_terminal, transitional_kernel),
            "transition queue provenance mismatch",
            (case_id, identity),
        )
    corrected_states = _exhausted_states(corrected_terminal, case_id)
    transitional_states = _exhausted_states(transitional_terminal, case_id)
    all_states = set(corrected_states) | set(transitional_states)
    for identity in all_states:
        state = corrected_states.get(identity) or transitional_states[identity]
        for terminal, states in (
            (corrected_terminal, corrected_states),
            (transitional_terminal, transitional_states),
        ):
            should_exist = state["exhausted_by"] <= terminal["horizon"]
            require(
                (identity in states) == should_exist,
                "transition exhausted-state advancement mismatch",
                (case_id, identity),
            )
            if should_exist:
                require(
                    states[identity] == state,
                    "transition exhausted-state premise mismatch",
                    identity,
                )


def check_transition_refusal(result, case_id):
    dimensions = REQUIRED_CASES[case_id]
    failure = dimensions["transition_failure"]
    require(failure is not None, "unexpected transition case", case_id)
    _require_output_mask(result, (True, False, False, False), case_id)
    require(
        result["operating_value"]["reason_codes"] == [failure["code"]],
        "transition refusal reason mismatch",
        case_id,
    )
    require(
        result["terminal"]["horizon"] == dimensions["horizon"] - 1,
        "transition horizon mismatch",
        case_id,
    )
    require(
        result["terminal"]["certificate"]["failures"]
        == [{"code": failure["code"], "path": failure["path"]}],
        "transition refusal certificate mismatch",
        case_id,
    )
    require(
        result.get("terminal_regime_diagnostics") == [failure],
        "transition regime diagnostic mismatch",
        case_id,
    )
    require(
        result["terminal"]["value_at_horizon"] is None
        and result["terminal"]["present_value"] is None,
        "transition emitted terminal value",
        case_id,
    )
    rows = result["operating_cash_flows"]["value"]
    horizon = result["terminal"]["horizon"]
    require(
        isinstance(horizon, int)
        and not isinstance(horizon, bool)
        and horizon < len(rows)
        and rows[horizon]["index"] == horizon + 1
        and result["terminal"]["next_year_fcff"] == rows[horizon]["fcff_operations"],
        "transition diagnostic numerator mismatch",
        case_id,
    )


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
            set(expected)
            == {
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
            failed = evaluate(transitional_frozen)
            require(
                failed.get("scenario_id") == case["id"],
                "transition scenario identity mismatch",
                case["id"],
            )
            check_transition_refusal(failed, case["id"])
            check_transition_pair(
                corrected_request,
                transitional_request,
                pure,
                failed,
                case["id"],
            )
            require(
                canonical(failed["operating_cash_flows"])
                == canonical(pure["operating_cash_flows"]),
                "transition pair changed operating cash flows",
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
            check_transition_refusal(transitional_output, case["id"])
            require(
                transitional_bytes == canonical(failed).encode(),
                "pure/CLI transition output mismatch",
                case["id"],
            )
            transitional_replay = cli(
                "forward-fcff-replay", "--run-id", str(transitional_run["run_id"])
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
