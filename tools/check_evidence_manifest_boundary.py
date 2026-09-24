"""Enforce that evidence selection consumers depend on the manifest boundary."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FLOW = ROOT / "alphaforge/evidence/flow.py"
READINESS = ROOT / "alphaforge/core/gate/readiness.py"


def _function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise SystemExit(f"missing required function: {name}")


def _has_call(node: ast.AST, attribute: str) -> bool:
    return any(
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == attribute
        for call in ast.walk(node)
    )


def main() -> None:
    flow_text = FLOW.read_text(encoding="utf-8")
    flow_tree = ast.parse(flow_text)
    packet_builder = _function(flow_tree, "build_frozen_evidence_packet")
    if _has_call(packet_builder, "execute"):
        raise SystemExit("packet construction must consume a manifest, not execute persistence SQL")
    if "packet_contents(" not in ast.get_source_segment(flow_text, packet_builder):
        raise SystemExit("packet construction must consume manifest.packet_contents")
    if "manifest_completeness(" not in flow_text:
        raise SystemExit("flow completeness must consume manifest_completeness")

    readiness_text = READINESS.read_text(encoding="utf-8")
    readiness_tree = ast.parse(readiness_text)
    if "research_documents" in readiness_text or "sqlite3" in readiness_text:
        raise SystemExit("readiness must not read the raw research persistence layer")
    if _has_call(readiness_tree, "execute"):
        raise SystemExit("readiness must not execute persistence SQL")
    if "evidence_manifest" not in readiness_text:
        raise SystemExit("readiness must consume the serialized evidence manifest")


if __name__ == "__main__":
    main()
