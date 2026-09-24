"""Authoritative evidence-selection manifest and persistence adapter.

The manifest is the only selection boundary shared by report-history
completeness, frozen packet construction, cache reuse, deduplication, audit
history, and readiness fallback.  Persistence tables are read here; consumers
receive a manifest and never reconstruct selection from individual tables.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from alphaforge.evidence.mfn_taxonomy import RECOGNIZED_ATTACHMENT_TIERS
from alphaforge.evidence.report_rules import ReportHistoryWindow

MANIFEST_VERSION = "evidence-selection-manifest-v1"


@dataclass(frozen=True)
class EvidenceSelectionManifest:
    manifest_version: str
    company_id: int
    as_of: str
    report_rules_fingerprint: str
    history_window: dict[str, int]
    audit_history: tuple[dict[str, Any], ...]
    cache: tuple[dict[str, Any], ...]
    deduplication: tuple[dict[str, Any], ...]
    packet_inputs: tuple[dict[str, Any], ...]
    readiness_fallback: dict[str, Any]

    @property
    def completeness(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for group in self.deduplication:
            report_class = str(group["report_class"])
            counts = result.setdefault(report_class, {"expected": 0, "retained": 0})
            counts["expected"] += 1
            counts["retained"] += int(bool(group["packet_source_urls"]))
        return {key: result[key] for key in sorted(result)}

    def packet_contents(self) -> tuple[dict[str, Any], ...]:
        """Return exactly the sources counted as retained by completeness."""
        retained_urls = {
            url for group in self.deduplication for url in group["packet_source_urls"]
        }
        return tuple(
            source for source in self.packet_inputs if source["source_url"] in retained_urls
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["completeness"] = self.completeness
        payload["packet_source_urls"] = [source["source_url"] for source in self.packet_contents()]
        return payload


def _cutoff(as_of: str, window: ReportHistoryWindow, report_kind: str | None) -> str:
    years = window.annual_lookback_years if report_kind == "annual" else window.interim_lookback_years
    value = date.fromisoformat(as_of[:10])
    try:
        return value.replace(year=value.year - years).isoformat()
    except ValueError:
        return value.replace(year=value.year - years, day=28).isoformat()


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _kind(record: dict[str, Any]) -> str:
    return str(record.get("report_kind") or "quarterly")


def _source_url(record: dict[str, Any]) -> str:
    return str(record.get("source_url") or record.get("url") or "")


def _in_window(record: dict[str, Any], *, as_of: str, window: ReportHistoryWindow) -> bool:
    published = str(record.get("published_at") or "")[:10]
    if not published or published > as_of[:10]:
        return False
    return published >= _cutoff(as_of, window, _kind(record))


def _group_id(record: dict[str, Any]) -> str:
    explicit = record.get("bilingual_group_id") or record.get("_bilingual_group_id")
    if explicit:
        return f"variant:{explicit}"
    metadata = _metadata(record.get("raw_metadata"))
    explicit = metadata.get("bilingual_group_id")
    if explicit:
        return f"variant:{explicit}"
    period = (
        record.get("observation_date")
        or record.get("period_end")
        or record.get("report_period_end")
        or record.get("fiscal_period")
        or metadata.get("period_end")
        or metadata.get("report_period_end")
        or metadata.get("fiscal_period")
    )
    if period:
        return f"period:{_kind(record)}:{period}"
    return f"source:{_source_url(record)}"


def _report_class(record: dict[str, Any]) -> str:
    return "annual" if _kind(record) == "annual" else "quarterly"


def _audit_rows(conn: Any, company_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id AS document_id, source_url, title, published_at, ingested_lang,
               raw_metadata, report_rules_fingerprint, duplicate_of
        FROM research_documents
        WHERE company_id=? AND duplicate_of IS NULL
        ORDER BY source_url, id
        """,
        (company_id,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        metadata = _metadata(item.get("raw_metadata"))
        item["report_kind"] = metadata.get("report_kind")
        item["attachment_tier"] = metadata.get("attachment_tier")
        result.append(item)
    return result


def _packet_rows(
    conn: Any,
    *,
    company_id: int,
    as_of: str,
    publication_cutoff: str | None,
    report_rules_fingerprint: str,
    window: ReportHistoryWindow,
    excluded_source_urls: set[str],
) -> list[dict[str, Any]]:
    cutoff = min(as_of[:10], publication_cutoff[:10]) if publication_cutoff else as_of[:10]
    rows = conn.execute(
        f"""
        SELECT d.id AS document_id, d.source_url, d.title, d.published_at,
               d.fetched_at, d.ingested_lang, d.raw_metadata, d.content_text AS release_body,
               a.id AS attachment_id, a.source_url AS attachment_url, a.content_type,
               a.byte_size, a.sha256 AS attachment_sha256,
               e.id AS extraction_id, e.extractor, e.text_checksum, e.page_count,
               e.pages_included, e.page_truncated, e.scanned, e.limitations
        FROM research_documents d
        JOIN research_attachments a ON a.document_id=d.id
        JOIN document_extractions e ON e.document_id=d.id
        WHERE d.company_id=? AND d.duplicate_of IS NULL
          AND d.published_at IS NOT NULL AND substr(d.published_at, 1, 10) <= ?
          AND d.report_rules_fingerprint=?
          AND json_extract(d.raw_metadata, '$.attachment_tier') IN ({','.join('?' for _ in RECOGNIZED_ATTACHMENT_TIERS)})
          AND substr(d.published_at, 1, 10) >= CASE
              WHEN json_extract(d.raw_metadata, '$.report_kind') = 'annual'
              THEN ? ELSE ? END
          AND a.id=(SELECT MAX(active.id) FROM research_attachments active
                    WHERE active.document_id=d.id)
          AND EXISTS (SELECT 1 FROM document_pages p WHERE p.extraction_id=e.id)
        ORDER BY d.source_url, a.sha256, d.id
        """,
        (
            company_id,
            cutoff,
            report_rules_fingerprint,
            *RECOGNIZED_ATTACHMENT_TIERS,
            _cutoff(as_of, window, "annual"),
            _cutoff(as_of, window, "quarterly"),
        ),
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        source_url = str(row["source_url"])
        if source_url in excluded_source_urls:
            continue
        item = dict(row)
        metadata = _metadata(item.get("raw_metadata"))
        item["report_kind"] = metadata.get("report_kind")
        item["pages"] = [
            dict(page)
            for page in conn.execute(
                """
                SELECT page_number, anchor, text, text_checksum
                FROM document_pages WHERE extraction_id=? ORDER BY page_number
                """,
                (row["extraction_id"],),
            ).fetchall()
        ]
        item["siblings"] = [
            dict(sibling)
            for sibling in conn.execute(
                """
                SELECT source_url, title, published_at, ingested_lang, duplicate_of, raw_metadata
                FROM research_documents
                WHERE duplicate_of=? AND published_at IS NOT NULL
                  AND substr(published_at, 1, 10) <= ?
                ORDER BY source_url
                """,
                (row["document_id"], cutoff),
            ).fetchall()
        ]
        result.append(item)
    return result


def _select_one_per_group(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(_group_id(row), []).append(row)
    selected: list[dict[str, Any]] = []
    for group in groups.values():
        selected.append(
            sorted(
                group,
                key=lambda row: (
                    0 if str(row.get("ingested_lang") or "") == "en" else 1,
                    _source_url(row),
                    int(row.get("document_id") or 0),
                ),
            )[0]
        )
    return sorted(selected, key=lambda row: (_source_url(row), int(row.get("document_id") or 0)))


def load_evidence_selection_manifest(
    conn: Any,
    *,
    company_id: int,
    as_of: str,
    report_rules: dict[str, Any],
    candidate_records: list[dict[str, Any]] | None = None,
    publication_cutoff: str | None = None,
    excluded_source_urls: set[str] | None = None,
) -> EvidenceSelectionManifest:
    """Load one manifest; all downstream roles consume its derived sections."""
    fingerprint = report_rules.get("fingerprint") if isinstance(report_rules, dict) else None
    history_values = report_rules.get("history_window") if isinstance(report_rules, dict) else None
    if not isinstance(fingerprint, str) or not fingerprint or not isinstance(history_values, dict):
        raise ValueError("selection manifest requires active report rules")
    window = ReportHistoryWindow(**history_values)
    excluded = excluded_source_urls or set()
    packet_rows = _packet_rows(
        conn,
        company_id=company_id,
        as_of=as_of,
        publication_cutoff=publication_cutoff,
        report_rules_fingerprint=fingerprint,
        window=window,
        excluded_source_urls=excluded,
    )
    candidates = [dict(record) for record in (candidate_records or []) if _source_url(record)]
    group_records = [record for record in candidates if _in_window(record, as_of=as_of, window=window)]
    if not group_records:
        group_records = packet_rows
    selected_packet_rows = _select_one_per_group(packet_rows)
    packet_urls = {_source_url(row) for row in selected_packet_rows}
    groups: dict[str, dict[str, Any]] = {}
    for record in group_records:
        group_id = _group_id(record)
        group = groups.setdefault(
            group_id,
            {"group_id": group_id, "report_class": _report_class(record), "candidate_source_urls": [], "packet_source_urls": []},
        )
        source_url = _source_url(record)
        if source_url not in group["candidate_source_urls"]:
            group["candidate_source_urls"].append(source_url)
        if source_url in packet_urls and source_url not in group["packet_source_urls"]:
            group["packet_source_urls"].append(source_url)
    for row in selected_packet_rows:
        group_id = _group_id(row)
        group = groups.setdefault(
            group_id,
            {"group_id": group_id, "report_class": _report_class(row), "candidate_source_urls": [], "packet_source_urls": []},
        )
        source_url = _source_url(row)
        if source_url not in group["packet_source_urls"]:
            group["packet_source_urls"].append(source_url)
    audit = _audit_rows(conn, company_id)
    audit_by_url = {_source_url(row): row for row in audit}
    for record in candidates:
        audit_by_url.setdefault(_source_url(record), record)
    return EvidenceSelectionManifest(
        manifest_version=MANIFEST_VERSION,
        company_id=company_id,
        as_of=as_of[:10],
        report_rules_fingerprint=fingerprint,
        history_window=dict(history_values),
        audit_history=tuple(audit_by_url.values()),
        cache=tuple(selected_packet_rows),
        deduplication=tuple(groups[key] for key in sorted(groups)),
        packet_inputs=tuple(selected_packet_rows),
        readiness_fallback={
            "documents_available": bool(audit_by_url),
            "current_packet_available": bool(selected_packet_rows),
            "requires_current_packet": True,
        },
    )


def completeness(manifest: EvidenceSelectionManifest) -> dict[str, dict[str, int]]:
    return manifest.completeness


def packet_contents(manifest: EvidenceSelectionManifest) -> tuple[dict[str, Any], ...]:
    return manifest.packet_contents()
