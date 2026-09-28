"""Repository-backed evidence manifest view for non-persistence consumers."""

from __future__ import annotations

from typing import Any

from alphaforge.db.repositories import load_evidence_packet, load_evidence_selection_manifest
from alphaforge.evidence.artifact_store import LocalPdfArtifactStore
from alphaforge.evidence.report_rules import report_rules_metadata


def load_evidence_view(
    conn: Any,
    *,
    company_id: int,
    as_of: str,
    artifact_store: Any | None = None,
) -> tuple[dict[str, Any] | None, Any]:
    """Return the current packet and its one authoritative selection manifest."""
    rules = report_rules_metadata()
    packet = load_evidence_packet(
        conn,
        company_id,
        as_of[:10],
        current_rules_fingerprint=rules["fingerprint"],
    )
    manifest = load_evidence_selection_manifest(
        conn,
        company_id=company_id,
        as_of=as_of[:10],
        report_rules=rules,
        artifact_store=(artifact_store if artifact_store is not None else LocalPdfArtifactStore()),
    )
    return packet, manifest
