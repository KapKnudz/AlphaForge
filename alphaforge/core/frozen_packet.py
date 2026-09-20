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


def validate_frozen_packet(packet: dict[str, Any] | None) -> bool:
    if not isinstance(packet, dict) or packet.get("frozen") is not True:
        return False
    packet_hash = packet.get("packet_hash")
    if not isinstance(packet_hash, str) or not packet_hash:
        return False
    without_hash = {key: value for key, value in packet.items() if key != "packet_hash"}
    if canonical_packet_hash(without_hash) != packet_hash:
        return False
    sources = packet.get("sources")
    if not isinstance(sources, list) or not sources:
        return False
    for source in sources:
        if not isinstance(source, dict):
            return False
        if not source.get("source_id") or not source.get("publication_date"):
            return False
        if source.get("publication_timestamp_authoritative") is not True:
            return False
        attachment = source.get("attachment")
        pages = source.get("pages")
        if not isinstance(attachment, dict) or not attachment.get("sha256"):
            return False
        if not isinstance(pages, list) or not pages:
            return False
        if any(not isinstance(page, dict) or not page.get("anchor") for page in pages):
            return False
    return True
