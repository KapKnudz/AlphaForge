"""User-facing estimated forward research exports; no acquisition or legacy admission."""

from __future__ import annotations

import csv
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from alphaforge.core.valuation.estimated_fcff import evaluate
from alphaforge.core.valuation.estimated_inputs import (
    LABEL,
    canonical,
    content_identity,
    digest,
    load,
    revise,
    validate_envelope,
)
from alphaforge.core.valuation.forward_fcff_contract import ContractError


def export(root, pack, outputs, parent_pack=None):
    """Publish a complete write-once local bundle, with inspectable detached contexts."""
    validate_envelope(pack)
    root = Path(root) / "estimated-forward"
    root.mkdir(parents=True, exist_ok=True)
    code_root = Path(__file__).resolve().parents[2]
    paths = [
        "alphaforge/core/valuation/estimated_inputs.py",
        "alphaforge/core/valuation/estimated_projection.py",
        "alphaforge/core/valuation/estimated_fcff.py",
        "alphaforge/cli/estimated_forward.py",
    ]
    receipt = {
        "pack_identity": content_identity(pack),
        "outputs_identity": digest(outputs),
        "code_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=code_root, text=True
        ).strip(),
        "code_sources": {
            p: hashlib.sha256((code_root / p).read_bytes()).hexdigest() for p in paths
        },
        "scope": "software calculation only; not financial or human implementation acceptance",
    }
    artifact = digest(receipt)
    target = root / artifact
    context = {"identity": outputs["identity"], "assumption_pack": pack, "execution": receipt}
    if parent_pack is not None:
        context["parent_assumption_pack"] = parent_pack
    artifacts = {
        "input.json": canonical(pack),
        "outputs.json": canonical({**context, **outputs}),
        "run.json": canonical({**context, "artifact_id": artifact}),
    }
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=["scenario", "year", "status", "reasons", "row", "context"],
        lineterminator="\n",
    )
    writer.writeheader()
    lines = [
        "# Estimated forward valuation",
        "",
        f"**{LABEL}**",
        "",
        "Conditional arithmetic is not financial approval, implementation acceptance or a verified forecast.",
        f"Valuation: {pack['valuation_date']}; knowledge cutoff: {pack['knowledge_cutoff']}; basis: {pack['knowledge_basis']}.",
        f"Original {pack['currency']} millions; absolute basic shares; nominal annual end-year WACC.",
        "",
    ]
    for name, scenario in outputs["scenarios"].items():
        lines += [f"## Scenario: {name}", ""]
        for key, out in scenario.items():
            artifacts[f"{name}-{key}.json"] = canonical(
                {**context, "scenario": name, "output": key, **out}
            )
            lines.append(
                f"- {key}: {out['status']}; reasons={canonical(out['reasons'])}; value={canonical(out['value'])}"
            )
        cash = scenario["annual_fcff"]
        rows = cash["value"] or [cash]
        for year, row in enumerate(rows, 1):
            writer.writerow(
                {
                    "scenario": name,
                    "year": year,
                    "status": row["status"],
                    "reasons": canonical(row["reasons"]),
                    "row": canonical(row["value"]),
                    "context": canonical(context),
                }
            )
    artifacts["annual.csv"] = stream.getvalue()
    lines += [
        "",
        "## Conclusion",
        canonical(outputs["firm_investment_conclusion"]),
        "",
        "## Complete assumptions, provenance, conditions and actual review records",
        "",
        "```json",
        canonical(context),
        "```",
        "",
    ]
    artifacts["report.md"] = "\n".join(lines)
    if target.exists():
        if {p.name for p in target.iterdir()} != set(artifacts) or any(
            (target / name).read_bytes() != body.encode() for name, body in artifacts.items()
        ):
            raise ContractError("conflicting_immutable_export")
        return target
    staging = Path(tempfile.mkdtemp(prefix=".estimated-", dir=root))
    try:
        for name, body in artifacts.items():
            # Scenario names are identifiers, never caller-controlled file paths.
            destination = staging / name
            if destination.parent != staging:
                raise ContractError("unsafe_scenario_id")
            destination.write_text(body, encoding="utf-8")
        os.rename(staging, target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return target


def command(args):
    try:
        pack = load(Path(args.input_json).read_text(encoding="utf-8"))
        parent_pack = pack if args.override_json else None
        if args.override_json:
            pack = revise(pack, **load(Path(args.override_json).read_text(encoding="utf-8")))
        outputs = evaluate(pack)
        target = export(args.exports_dir, pack, outputs, parent_pack)
        print(
            canonical(
                {
                    "status": "estimated_research_exported",
                    "export_directory": str(target),
                    "pack_identity": content_identity(pack),
                }
            )
        )
        return 0
    except (
        ContractError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        ArithmeticError,
    ) as error:
        print(
            canonical({"status": "estimated_research_refused", "reason": str(error)}),
            file=sys.stderr,
        )
        return 1


def add_parsers(sub):
    parser = sub.add_parser(
        "estimated-forward", help="Provisional aggregate FCFF/WACC estimates; no issuer admission"
    )
    parser.add_argument("--input-json", required=True)
    parser.add_argument("--exports-dir", default="exports")
    parser.add_argument(
        "--override-json", help="Preserved child version: version/author/prepared_on/reason/changes"
    )
    parser.set_defaults(func=command)
