"""Synthetic parent-context identity and saved-child immutable export regressions."""

import copy
import json
from pathlib import Path

import pytest

from alphaforge.cli.estimated_forward import export
from alphaforge.cli.main import build_parser
from alphaforge.core.valuation.estimated_fcff import evaluate
from alphaforge.core.valuation.estimated_inputs import content_identity, digest, revise
from alphaforge.core.valuation.forward_fcff_contract import ContractError
from tests.estimated_fixtures import pack


def bundle_bytes(directory):
    return {path.name: path.read_bytes() for path in directory.iterdir()}


def override(parent):
    scenarios = copy.deepcopy(parent["scenarios"])
    scenarios["base"]["fx"]["quote_per_reporting"] = "3"
    return {
        "version": "synthetic-parent-export-r2",
        "author": "Synthetic override",
        "prepared_on": "2024-03-17",
        "reason": "Alternative FX",
        "changes": {"scenarios": scenarios},
    }


def test_parent_context_content_is_bound_before_artifact_hashing(tmp_path):
    parent = pack()
    child = revise(parent, **override(parent))
    outputs = evaluate(child)
    original = export(tmp_path, child, outputs, parent)
    before = bundle_bytes(original)
    changed_parent = copy.deepcopy(parent)
    changed_parent["prepared_by"] = "Different synthetic preparer"
    changed = export(tmp_path, child, outputs, changed_parent)
    assert changed != original
    assert bundle_bytes(original) == before
    for context, directory in ((parent, original), (changed_parent, changed)):
        assert export(tmp_path, child, outputs, copy.deepcopy(context)) == directory
        detached = json.loads((directory / "outputs.json").read_text())
        assert detached["parent_assumption_pack"] == context
        assert detached["assumption_pack"] == child
        assert {key: detached[key] for key in outputs} == outputs
        receipt = detached["execution"]
        assert receipt["parent_pack_identity"] == content_identity(context)
        assert directory.name == digest(receipt)
    assert (original / "input.json").read_bytes() == (changed / "input.json").read_bytes()
    # Corruption still refuses; the fix must not permit overwriting immutable bundles.
    (changed / "report.md").write_text("Synthetic tamper")
    tampered = bundle_bytes(changed)
    with pytest.raises(ContractError, match="conflicting_immutable_export"):
        export(tmp_path, child, outputs, changed_parent)
    assert bundle_bytes(changed) == tampered
    assert bundle_bytes(original) == before


def test_cli_saved_child_reloads_without_parent_context_in_original_and_fresh_roots(
    tmp_path, capsys
):
    parent = pack()
    input_path = tmp_path / "parent.json"
    input_path.write_text(json.dumps(parent))
    override_path = tmp_path / "override.json"
    override_path.write_text(json.dumps(override(parent)))
    inputs_before = (input_path.read_bytes(), override_path.read_bytes())

    def run(source, root, changes=None):
        argv = ["estimated-forward", "--input-json", str(source), "--exports-dir", str(root)]
        if changes is not None:
            argv += ["--override-json", str(changes)]
        args = build_parser().parse_args(argv)
        assert args.func(args) == 0
        return Path(json.loads(capsys.readouterr().out)["export_directory"])

    root = tmp_path / "exports"
    ordinary = run(input_path, root)
    ordinary_bytes = bundle_bytes(ordinary)
    assert run(input_path, root) == ordinary
    assert bundle_bytes(ordinary) == ordinary_bytes
    original = run(input_path, root, override_path)
    original_bytes = bundle_bytes(original)
    assert run(input_path, root, override_path) == original
    assert bundle_bytes(original) == original_bytes
    saved_input = original / "input.json"
    reloaded = run(saved_input, root)
    assert reloaded != original
    reloaded_bytes = bundle_bytes(reloaded)
    assert run(saved_input, root) == reloaded
    fresh = run(saved_input, tmp_path / "fresh")
    assert fresh.name == reloaded.name
    assert run(saved_input, tmp_path / "fresh") == fresh
    assert bundle_bytes(fresh) == reloaded_bytes == bundle_bytes(reloaded)
    original_output = json.loads(original_bytes["outputs.json"])
    reloaded_output = json.loads(reloaded_bytes["outputs.json"])
    assert original_output["parent_assumption_pack"] == parent
    assert "parent_assumption_pack" not in reloaded_output
    assert "parent_pack_identity" not in reloaded_output["execution"]
    assert original_output["assumption_pack"] == reloaded_output["assumption_pack"]
    assert original_output["scenarios"] == reloaded_output["scenarios"]
    assert (
        original_output["firm_investment_conclusion"]
        == reloaded_output["firm_investment_conclusion"]
    )
    assert original_bytes["input.json"] == reloaded_bytes["input.json"]
    assert bundle_bytes(original) == original_bytes
    assert bundle_bytes(ordinary) == ordinary_bytes
    assert (input_path.read_bytes(), override_path.read_bytes()) == inputs_before
