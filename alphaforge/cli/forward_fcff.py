"""Research I/O only: freeze, retain, evaluate, retain outputs, write-once export."""

from __future__ import annotations

import csv
import io
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

from alphaforge.config import Settings
from alphaforge.core.valuation.forward_fcff_contract import ContractError, canonical, freeze
from alphaforge.db.connection import get_connection
from alphaforge.db.forward_fcff_runs import execute, replay
from alphaforge.db.migrations import migrate
from alphaforge.db.numerical_runs import ReplayRefusal


def export(root, request, receipt, outputs):
    root = Path(root) / "forward-fcff" / "runs"
    root.mkdir(parents=True, exist_ok=True)
    target = root / receipt["artifact_id"]
    full = request.unpack()
    stream = io.StringIO(newline="")
    rows = outputs["operating_cash_flows"]["value"] or []
    fields = [k for k in rows[0] if k != "legs"] if rows else ["period_id", "fcff_operations"]
    writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows({k: v for k, v in r.items() if k != "legs"} for r in rows)
    denom = full["denomination"]
    lines = [
        "# Conditional forward FCFF research",
        "",
        "**NOT financial sign-off or completed issuer equity valuation.**",
        f"Valuation: {full['valuation_date']}; cutoff: {full['evidence_cutoff']}; prepared: {full['prepared_on']}.",
        f"Knowledge basis: {full['knowledge_basis']}. Original {denom['currency']}, millions; basic shares absolute.",
        "**Financing shields excluded. Nominal annual end-year supplied asset return; no SEK hurdle fallback.**",
        f"Review: {outputs['review_status']}. Supplied analyst/AI assumptions remain assumptions, not observed future facts.",
        "",
    ]
    for key in ("operating_cash_flows", "operating_value", "common_equity", "per_share"):
        out = outputs[key]
        lines.append(
            f"- {key}: {out['status']}; reasons={out['reason_codes']}; missing={out['missing_inputs']}"
            + (
                f"; value={out['value']}"
                if out["available"] and key != "operating_cash_flows"
                else ""
            )
        )
    lines += [
        "",
        f"Explicit PV diagnostic: {outputs['explicit_pv']}",
        f"Exact full next-year terminal numerator / explicit horizon: {canonical(outputs['terminal'])}",
        "",
        "## Conditions and limitations",
    ]
    lines.extend(f"- {v['id']}: {v['text']}" for v in full["limitations"])
    lines += [
        "",
        "## Frozen assumptions and provenance",
        "",
        "See input.json for every operand, rationale, effective period and source reference.",
        "Gross operating tax payment/refund legs, investments, D&A and economic leases are disclosed separately in cash-flows.csv.",
    ]
    artifacts = {
        "input.json": canonical(full),
        "context.json": canonical(full["evidence_context"]),
        "outputs.json": canonical(outputs),
        "run.json": canonical(receipt),
        "cash-flows.csv": stream.getvalue(),
        "report.md": "\n".join(lines) + "\n",
    }
    if target.exists():
        if (
            not target.is_dir()
            or {p.name for p in target.iterdir()} != set(artifacts)
            or any(
                (target / name).read_bytes() != body.encode() for name, body in artifacts.items()
            )
        ):
            raise ReplayRefusal("conflicting_immutable_export")
        return target
    staging = Path(tempfile.mkdtemp(prefix=".forward-", dir=root))
    try:
        for name, body in artifacts.items():
            (staging / name).write_bytes(body.encode())
        # Rename the entire completed artifact. Never expose a partial directory.
        try:
            os.rename(staging, target)
        except OSError:
            if target.exists():
                return export(root.parent.parent, request, receipt, outputs)
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return target


def command(args):
    conn = None
    try:
        settings = Settings.from_env(dsn=args.dsn) if args.dsn else Settings.from_env()
        conn = get_connection(settings)
        migrate(conn)
        if args.command == "forward-fcff-replay":
            print(canonical(replay(conn, args.run_id)))
        else:
            request = freeze(Path(args.input_json).read_text(encoding="utf-8"))
            receipt, outputs = execute(conn, request)
            target = export(args.exports_dir, request, receipt, outputs)
            print(
                canonical(
                    {
                        **receipt,
                        "export_directory": str(target),
                        "computed_outputs": outputs.get("computed_outputs", []),
                    }
                )
            )
        return 0
    except (ContractError, ReplayRefusal, OSError, ValueError, sqlite3.Error) as error:
        print(canonical({"status": "research_refused", "reason": str(error)}), file=sys.stderr)
        return 1
    finally:
        if conn is not None:
            conn.close()


def add_parsers(sub):
    run = sub.add_parser(
        "forward-fcff", help="Conditional supplied SEK/EUR FCFF research; no issuer admission"
    )
    run.add_argument("--input-json", required=True)
    run.add_argument("--exports-dir", default="exports")
    run.set_defaults(func=command)
    audit = sub.add_parser(
        "forward-fcff-replay", help="Exact retained-only forward research replay"
    )
    audit.add_argument("--run-id", type=int, required=True)
    audit.set_defaults(func=command)
