"""Dependency-free validation for frozen evidence packets."""

from __future__ import annotations

import hashlib
import json
from typing import Any


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


def packet_hash_body(packet_without_hash: dict[str, Any]) -> dict[str, Any]:
    """Project the hashed subset: everything except run-timestamp provenance."""
    body = {key: value for key, value in packet_without_hash.items() if key != "packet_hash"}
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


def stable_packet_hash(packet_without_hash: dict[str, Any]) -> str:
    """Hash new packets over the provenance-excluded canonical subset."""
    return canonical_packet_hash(packet_hash_body(packet_without_hash))


def packet_hash_matches(packet: dict[str, Any]) -> bool:
    packet_hash = packet.get("packet_hash")
    if not isinstance(packet_hash, str) or not packet_hash:
        return False
    if stable_packet_hash(packet) == packet_hash:
        return True
    # Legacy packets hashed the full body including run timestamps; they keep
    # validating against their stored hash without being rewritten.
    without_hash = {key: value for key, value in packet.items() if key != "packet_hash"}
    return canonical_packet_hash(without_hash) == packet_hash


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
