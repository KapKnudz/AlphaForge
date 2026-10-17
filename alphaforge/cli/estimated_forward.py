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

SOURCE_PATHS = (
    "alphaforge/core/valuation/estimated_inputs.py",
    "alphaforge/core/valuation/estimated_projection.py",
    "alphaforge/core/valuation/estimated_fcff.py",
    "alphaforge/core/valuation/estimated_reverse.py",
    "alphaforge/core/valuation/forward_fcff.py",
    "alphaforge/core/valuation/forward_fcff_contract.py",
    "alphaforge/cli/estimated_forward.py",
)


def _code_sources(root):
    return {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in SOURCE_PATHS}


def export(root, pack, outputs, parent_pack=None):
    """Publish a complete write-once local bundle, with inspectable detached contexts."""
    validate_envelope(pack)
    root = Path(root) / "estimated-forward"
    root.mkdir(parents=True, exist_ok=True)
    code_root = Path(__file__).resolve().parents[2]
    receipt = {
        "pack_identity": content_identity(pack),
        "outputs_identity": digest(outputs),
        "code_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=code_root, text=True
        ).strip(),
        "code_sources": _code_sources(code_root),
        "scope": "software calculation only; not financial or human implementation acceptance",
    }
    if parent_pack is not None:
        receipt["parent_pack_identity"] = content_identity(parent_pack)
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
    reverse_stream = io.StringIO(newline="")
    reverse_writer = csv.DictWriter(
        reverse_stream,
        fieldnames=["scenario", "status", "record_type", "record", "context"],
        lineterminator="\n",
    )
    reverse_writer.writeheader()
    lines = [
        "# Estimated valuation — forward and declared reverse analysis",
        "",
        f"**{LABEL}**",
        "",
        "Conditional arithmetic is not financial approval, implementation acceptance or a verified forecast.",
        f"Valuation: {pack['valuation_date']}; knowledge cutoff: {pack['knowledge_cutoff']}; basis: {pack['knowledge_basis']}.",
        f"Original {pack['currency']} millions; absolute basic shares; nominal annual end-year WACC.",
        "",
    ]
    for name in sorted(outputs["scenarios"]):
        scenario = outputs["scenarios"][name]
        lines += [f"## Scenario: {name}", ""]
        for key, out in scenario.items():
            artifacts[f"{name}-{key}.json"] = canonical(
                {**context, "scenario": name, "output": key, **out}
            )
            lines.append(
                f"- {key}: {out['status']}; reasons={canonical(out['reasons'])}; value={canonical(out['value'])}; search_diagnostics={canonical(out.get('search_diagnostics'))}"
            )
        reverse = scenario["reverse"]
        diagnostics = reverse.get("value") or reverse.get("search_diagnostics")
        reverse_records = [("outcome", reverse)]
        if diagnostics is not None:
            reverse_records += [("scan", row) for row in diagnostics["scan"]]
            reverse_records += [("root", root) for root in diagnostics["roots"]]
            reverse_records += [("match_region", region) for region in diagnostics["match_regions"]]
        for record_type, record in reverse_records:
            reverse_writer.writerow(
                {
                    "scenario": name,
                    "status": reverse["status"],
                    "record_type": record_type,
                    "record": canonical(record),
                    "context": canonical(context),
                }
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
    artifacts["reverse.csv"] = reverse_stream.getvalue()
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
