"""Dependency-free validation for frozen evidence packets."""

from __future__ import annotations

import hashlib
import json
from typing import Any

EVIDENCE_RULES_VERSION = 1
"""Monotonic version of the evidence/filter/completeness rule set.

Stamped on every frozen packet as ``evidence_rules_version``. Bump it when a
deterministic rule that decides what counts as report evidence changes: the
report/invitation taxonomy, the issuer-confirmation filter, attachment-tier
selection, or completeness counting. Packets stamped with an older version —
including packets built before versioning existed — are stale: they stay
structurally valid (hash and schema still verify) but readiness must not
trust them; rerun the evidence lane to rebuild under the current rules.
"""


def packet_rules_version(packet: dict[str, Any] | None) -> int | None:
    """Return the stamped evidence rule version, or None when absent."""
    if not isinstance(packet, dict):
        return None
    version = packet.get("evidence_rules_version")
    if isinstance(version, bool) or not isinstance(version, int):
        return None
    return version


def is_stale_evidence_packet(
    packet: dict[str, Any] | None, *, current_rules_fingerprint: str | None = None
) -> bool:
    """Return True when a packet predates the current evidence rule set.

    Fail closed: a missing version or report-rule fingerprint means the packet
    predates rule stamping.  Callers that know the active history window pass
    its fingerprint as well; this prevents a packet built with a different
    coverage horizon from being reused silently.
    """
    if packet_rules_version(packet) != EVIDENCE_RULES_VERSION:
        return True
    if not isinstance(packet, dict):
        return True
    rules = packet.get("report_rules")
    if not isinstance(rules, dict):
        return True
    fingerprint = rules.get("fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        return True
    if current_rules_fingerprint is None:
        # Import lazily to keep this dependency-free validator free of the
        # evidence-flow import cycle.
        from alphaforge.evidence.report_rules import current_report_rules_fingerprint

        current_rules_fingerprint = current_report_rules_fingerprint()
    return fingerprint != current_rules_fingerprint


def canonical_packet_hash(packet_without_hash: dict[str, Any]) -> str:
    canonical = json.dumps(
        packet_without_hash,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Run-specific provenance excluded from the stable packet hash. These fields
# stay in the stored packet JSON for auditability, but identical artifacts
# must hash identically across databases built at different times.
HASH_EXCLUDED_ISSUER_KEYS = ("verified_at",)
HASH_EXCLUDED_SOURCE_KEYS = ("ingestion_date",)


def _stable_source_id(source: dict[str, Any]) -> str:
    attachment = source.get("attachment")
    attachment_checksum = attachment.get("sha256") if isinstance(attachment, dict) else None
    identity = {
        "source_url": source.get("source_url"),
        "publication_date": source.get("publication_date"),
        "document_checksum": attachment_checksum,
    }
    return f"source:{canonical_packet_hash(identity)}"


def _replace_source_reference(value: Any, references: dict[str, str]) -> Any:
    if not isinstance(value, str):
        return value
    for old, new in references.items():
        if value == old:
            return new
        if value.startswith(f"{old}#"):
            return f"{new}{value[len(old) :]}"
    return value


def _legacy_packet_hash_body(packet_without_hash: dict[str, Any]) -> dict[str, Any]:
    # Run diagnostics are retained in packet JSON for audit/CLI replay, but
    # they describe acquisition counters rather than evidence identity.  A
    # rerun that reuses the same persisted PDFs must keep the same artifact
    # hash even when discovery's unseen count changed.
    body = {
        key: value
        for key, value in packet_without_hash.items()
        if key not in {"packet_hash", "evidence_diagnostic", "selection_manifest_id"}
    }
    issuer = body.get("issuer")
    if isinstance(issuer, dict):
        body["issuer"] = {
            key: value for key, value in issuer.items() if key not in HASH_EXCLUDED_ISSUER_KEYS
        }
    sources = body.get("sources")
    if isinstance(sources, list):
        body["sources"] = [
            {key: value for key, value in source.items() if key not in HASH_EXCLUDED_SOURCE_KEYS}
            if isinstance(source, dict)
            else source
            for source in sources
        ]
    return body


def packet_hash_body(packet_without_hash: dict[str, Any]) -> dict[str, Any]:
    """Project stable provider identity and exclude run-timestamp provenance."""
    body = _legacy_packet_hash_body(packet_without_hash)
    body.pop("company_id", None)
    sources = body.get("sources")
    if not isinstance(sources, list):
        return body

    references: dict[str, str] = {}
    projected_sources: list[Any] = []
    for source in sources:
        if not isinstance(source, dict):
            projected_sources.append(source)
            continue
        old_source_id = source.get("source_id")
        stable_source_id = _stable_source_id(source)
        if isinstance(old_source_id, str):
            references[old_source_id] = stable_source_id
        projected = dict(source)
        projected["source_id"] = stable_source_id
        projected_sources.append(projected)

    references_ready = references
    for source in projected_sources:
        if not isinstance(source, dict):
            continue
        body_value = source.get("body")
        if isinstance(body_value, dict) and isinstance(body_value.get("paragraphs"), list):
            source["body"] = {
                **body_value,
                "paragraphs": [
                    {
                        **paragraph,
                        "anchor": _replace_source_reference(
                            paragraph.get("anchor"), references_ready
                        ),
                    }
                    if isinstance(paragraph, dict)
                    else paragraph
                    for paragraph in body_value["paragraphs"]
                ],
            }
        if isinstance(source.get("pages"), list):
            source["pages"] = [
                {
                    **page,
                    "anchor": _replace_source_reference(page.get("anchor"), references_ready),
                }
                if isinstance(page, dict)
                else page
                for page in source["pages"]
            ]
        if isinstance(source.get("bilingual_siblings"), list):
            source["bilingual_siblings"] = [
                {
                    **sibling,
                    "duplicate_of": _replace_source_reference(
                        sibling.get("duplicate_of"), references_ready
                    ),
                }
                if isinstance(sibling, dict)
                else sibling
                for sibling in source["bilingual_siblings"]
            ]
    projected_sources.sort(
        key=lambda source: source.get("source_id", "") if isinstance(source, dict) else ""
    )
    body["sources"] = projected_sources
    for key in ("evidence_catalog", "coverage_facts"):
        value = body.get(key)
        if isinstance(value, dict) and isinstance(value.get("source_ids"), list):
            body[key] = {
                **value,
                "source_ids": sorted(
                    references.get(source_id, source_id) for source_id in value["source_ids"]
                ),
            }
        if key == "evidence_catalog" and isinstance(value, dict):
            body[key] = {
                **body[key],
                "canonical_source_ids": sorted(
                    references.get(source_id, source_id)
                    for source_id in value.get("canonical_source_ids", [])
                ),
            }
    return body


def stable_packet_hash(packet_without_hash: dict[str, Any]) -> str:
    """Hash new packets over the provenance-excluded canonical subset."""
    return canonical_packet_hash(packet_hash_body(packet_without_hash))


def packet_hash_matches(packet: dict[str, Any]) -> bool:
    packet_hash = packet.get("packet_hash")
    if not isinstance(packet_hash, str) or not packet_hash:
        return False
    if stable_packet_hash(packet) == packet_hash:
        return True
    # Older packets used the timestamp-only projection or the full body; both
    # remain valid without rewriting their stored hashes.
    without_hash = {key: value for key, value in packet.items() if key != "packet_hash"}
    return (
        canonical_packet_hash(_legacy_packet_hash_body(packet)) == packet_hash
        or canonical_packet_hash(without_hash) == packet_hash
    )


def validate_frozen_packet(packet: dict[str, Any] | None) -> bool:
    if not isinstance(packet, dict) or packet.get("frozen") is not True:
        return False
    if not packet_hash_matches(packet):
        return False
    if packet.get("schema_version") != "evidence-packet-v1":
        return False
    if not isinstance(packet.get("company_id"), int) or isinstance(packet.get("company_id"), bool):
        return False
    if not isinstance(packet.get("as_of"), str) or not packet["as_of"]:
        return False
    sources = packet.get("sources")
    catalog = packet.get("evidence_catalog")
    catalog_ids = catalog.get("canonical_source_ids") if isinstance(catalog, dict) else None
    if not isinstance(sources, list) or not sources or not isinstance(catalog_ids, list):
        return False
    source_ids: list[str] = []
    for source in sources:
        if not isinstance(source, dict):
            return False
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or not source_id:
            return False
        if source_id in source_ids:
            return False
        source_ids.append(source_id)
        if not isinstance(source.get("source_url"), str) or not source["source_url"]:
            return False
        if not isinstance(source.get("publication_date"), str) or not source["publication_date"]:
            return False
        if not isinstance(source.get("ingestion_date"), str) or not source["ingestion_date"]:
            return False
        if source.get("publication_timestamp_authoritative") is not True:
            return False
        attachment = source.get("attachment")
        extraction = source.get("extraction")
        pages = source.get("pages")
        if (
            not isinstance(attachment, dict)
            or not isinstance(attachment.get("source_url"), str)
            or not attachment["source_url"]
            or not isinstance(attachment.get("sha256"), str)
            or not attachment["sha256"]
        ):
            return False
        if (
            not isinstance(extraction, dict)
            or not isinstance(extraction.get("extractor"), str)
            or not extraction["extractor"]
            or not isinstance(extraction.get("text_checksum"), str)
            or not extraction["text_checksum"]
            or not isinstance(extraction.get("page_count"), int)
            or isinstance(extraction["page_count"], bool)
            or extraction["page_count"] < 1
        ):
            return False
        if not isinstance(pages, list) or not pages:
            return False
        page_numbers: list[int] = []
        for page in pages:
            if not isinstance(page, dict):
                return False
            if not isinstance(page.get("page_number"), int) or isinstance(
                page["page_number"], bool
            ):
                return False
            if page["page_number"] < 1 or page["page_number"] in page_numbers:
                return False
            page_numbers.append(page["page_number"])
            if page["page_number"] > extraction["page_count"]:
                return False
            if not isinstance(page.get("anchor"), str) or not page["anchor"]:
                return False
            if not isinstance(page.get("text"), str):
                return False
            if not isinstance(page.get("text_checksum"), str) or not page["text_checksum"]:
                return False
            expected_page_checksum = hashlib.sha256(page["text"].encode("utf-8")).hexdigest()
            if page["text_checksum"] != expected_page_checksum:
                return False
        canonical_text = "\n\n".join(
            f"[page {page['page_number']}]\n{page['text']}".rstrip() for page in pages
        )
        expected_text_checksum = hashlib.sha256(canonical_text.encode("utf-8")).hexdigest()
        if extraction["text_checksum"] != expected_text_checksum:
            return False
    return set(catalog_ids) == set(source_ids) and len(catalog_ids) == len(source_ids)
