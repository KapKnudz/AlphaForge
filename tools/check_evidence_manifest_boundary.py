"""Enforce that evidence selection consumers depend on the manifest boundary."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FLOW = ROOT / "alphaforge/evidence/flow.py"
MANIFEST = ROOT / "alphaforge/evidence/manifest.py"
READINESS = ROOT / "alphaforge/core/gate/readiness.py"
RANKING = ROOT / "alphaforge/cli/ranking_loader.py"


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


def _has_any_call(node: ast.AST, name: str) -> bool:
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr == name:
            return True
        if isinstance(func, ast.Name) and func.id == name:
            return True
    return False


def _imports_module(tree: ast.AST, module: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module or alias.name.startswith(module + "."):
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module == module or (node.module or "").startswith(module + "."):
                return True
    return False


def _mentions_token(tree: ast.AST, token: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and token in node.id:
            return True
        if isinstance(node, ast.Attribute) and token in node.attr:
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if token in node.value:
                return True
    return False


def main() -> None:
    manifest_text = MANIFEST.read_text(encoding="utf-8")
    manifest_tree = ast.parse(manifest_text)
    if _imports_module(manifest_tree, "sqlite3") or _mentions_token(
        manifest_tree, "research_documents"
    ):
        raise SystemExit("pure manifest must not read raw persistence")
    if not any(
        isinstance(node, ast.FunctionDef) and node.name == "select_evidence_manifest"
        for node in ast.walk(manifest_tree)
    ):
        raise SystemExit("manifest selection constructor is missing")

    flow_text = FLOW.read_text(encoding="utf-8")
    flow_tree = ast.parse(flow_text)
    packet_builder = _function(flow_tree, "build_frozen_evidence_packet")
    if _has_call(packet_builder, "execute"):
        raise SystemExit("packet construction must consume a manifest, not execute persistence SQL")
    if not _has_any_call(packet_builder, "packet_contents"):
        raise SystemExit("packet construction must consume manifest.packet_contents")
    if not _has_any_call(flow_tree, "manifest_completeness"):
        raise SystemExit("flow completeness must consume manifest_completeness")

    ranking_text = RANKING.read_text(encoding="utf-8")
    ranking_tree = ast.parse(ranking_text)
    if _imports_module(ranking_tree, "alphaforge.db.repositories") or _mentions_token(
        ranking_tree, "research_documents"
    ):
        raise SystemExit("ranking evidence fallback must consume the manifest boundary")

    readiness_text = READINESS.read_text(encoding="utf-8")
    readiness_tree = ast.parse(readiness_text)
    if _imports_module(readiness_tree, "sqlite3") or _mentions_token(
        readiness_tree, "research_documents"
    ):
        raise SystemExit("readiness must not read the raw research persistence layer")
    if _has_call(readiness_tree, "execute"):
        raise SystemExit("readiness must not execute persistence SQL")
    if not _mentions_token(readiness_tree, "evidence_manifest"):
        raise SystemExit("readiness must consume the serialized evidence manifest")


if __name__ == "__main__":
    main()
