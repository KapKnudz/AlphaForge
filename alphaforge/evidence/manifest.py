"""Pure evidence-selection manifest and shared selection projections.

The manifest contains the only selected/rejected view consumed by evidence
completeness, packet construction, cache reuse, and readiness. Raw persistence
facts are loaded by the repository adapter; this module has no DB or HTTP
imports.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from alphaforge.evidence.report_rules import ReportHistoryWindow

MANIFEST_VERSION = "evidence-selection-manifest-v2"


@dataclass(frozen=True)
class EvidenceSelectionManifest:
    manifest_version: str
    company_id: int
    as_of: str
    report_rules_fingerprint: str
    history_window: dict[str, int]
    audit_history: tuple[dict[str, Any], ...]
    cache: tuple[dict[str, Any], ...]
    reuse: tuple[dict[str, Any], ...]
    deduplication: tuple[dict[str, Any], ...]
    packet_inputs: tuple[dict[str, Any], ...]
    rejected: tuple[dict[str, Any], ...]
    readiness_fallback: dict[str, Any]
    source_input_fingerprint: str | None = None

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
        """Return exactly the immutable observations selected by the manifest.

        V1 manifests identify packet inputs only by release URL. V2 binds the
        exact candidate observation when it is available, while retaining the
        URL projection for historical readers.
        """
        retained_observations = {
            str(group.get("selected", {}).get("candidate_observation_id") or "")
            for group in self.deduplication
            if isinstance(group.get("selected"), dict)
        }
        retained_observations.discard("")
        retained_urls = {url for group in self.deduplication for url in group["packet_source_urls"]}
        return tuple(
            source
            for source in self.packet_inputs
            if (
                source.get("candidate_observation_id") in retained_observations
                if retained_observations
                else source["source_url"] in retained_urls
            )
        )

    @property
    def manifest_id(self) -> str:
        canonical = json.dumps(
            {
                "manifest_version": self.manifest_version,
                "company_id": self.company_id,
                "as_of": self.as_of,
                "report_rules_fingerprint": self.report_rules_fingerprint,
                "history_window": self.history_window,
                "source_input_fingerprint": self.source_input_fingerprint,
                "deduplication": self.deduplication,
                "rejected": self.rejected,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["manifest_id"] = self.manifest_id
        payload["completeness"] = self.completeness
        payload["packet_source_urls"] = [source["source_url"] for source in self.packet_contents()]
        return payload


def _cutoff(as_of: str, window: ReportHistoryWindow, report_kind: str | None) -> str:
    years = (
        window.annual_lookback_years if report_kind == "annual" else window.interim_lookback_years
    )
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
    metadata = _metadata(record.get("raw_metadata"))
    return str(record.get("report_kind") or metadata.get("report_kind") or "quarterly")


def _source_url(record: dict[str, Any]) -> str:
    return str(record.get("source_url") or record.get("url") or "")


def _in_window(record: dict[str, Any], *, as_of: str, window: ReportHistoryWindow) -> bool:
    published = str(record.get("published_at") or "")[:10]
    if not published or published > as_of[:10]:
        return False
    return published >= _cutoff(as_of, window, _kind(record))


def _group_id(record: dict[str, Any]) -> str:
    selection_group = record.get("selection_group_id")
    if selection_group:
        return f"immutable:{selection_group}"
    explicit = record.get("bilingual_group_id") or record.get("_bilingual_group_id")
    if explicit:
        return f"variant:{explicit}"
    metadata = _metadata(record.get("raw_metadata"))
    explicit = metadata.get("bilingual_group_id")
    if explicit:
        return f"variant:{explicit}"
    event_id = (
        record.get("provider_event_id")
        or record.get("mfn_event_id")
        or metadata.get("provider_event_id")
        or metadata.get("mfn_event_id")
    )
    if event_id:
        return f"event:{event_id}"
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


def _slot_key(record: dict[str, Any]) -> str:
    metadata = _metadata(record.get("raw_metadata"))
    period = (
        record.get("period_end")
        or record.get("observation_date")
        or record.get("report_period_end")
        or record.get("fiscal_period")
        or metadata.get("period_end")
        or metadata.get("observation_date")
        or metadata.get("report_period_end")
        or metadata.get("fiscal_period")
    )
    if period:
        label = str(period).strip()
        try:
            label = date.fromisoformat(label[:10]).isoformat()
        except ValueError:
            pass  # Fiscal labels are not dates; retain the entire quarter label.
        return f"{_report_class(record)}:{label}"
    return f"{_report_class(record)}:source:{_source_url(record)}"


def _stable_selection(row: dict[str, Any]) -> dict[str, Any] | None:
    observation_id = row.get("candidate_observation_id")
    artifact_id = row.get("artifact_id")
    extraction_id = row.get("immutable_extraction_id") or row.get("extraction_identity")
    attachment_observation_id = row.get("attachment_observation_id")
    if not all((observation_id, artifact_id, extraction_id, attachment_observation_id)):
        return None
    return {
        "candidate_key": row.get("candidate_key"),
        "candidate_observation_id": observation_id,
        "attachment_observation_id": attachment_observation_id,
        "artifact_id": artifact_id,
        "extraction_id": extraction_id,
        "release_source_url": _source_url(row),
        "attachment_source_url": row.get("attachment_url"),
    }


def _packet_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep every usable edition; deduplicate only repeated source identities."""
    unique: dict[str, dict[str, Any]] = {}
    for row in rows:
        source_url = _source_url(row)
        if source_url:
            unique.setdefault(source_url, row)
    return sorted(
        unique.values(), key=lambda row: (_source_url(row), int(row.get("document_id") or 0))
    )


def select_evidence_manifest(
    *,
    company_id: int,
    as_of: str,
    report_rules: dict[str, Any],
    audit_history: list[dict[str, Any]],
    candidate_records: list[dict[str, Any]] | None,
    packet_inputs: list[dict[str, Any]],
    source_input_fingerprint: str | None = None,
) -> EvidenceSelectionManifest:
    """Derive every evidence role from immutable facts and one rule input set."""
    fingerprint = report_rules.get("fingerprint") if isinstance(report_rules, dict) else None
    history_values = report_rules.get("history_window") if isinstance(report_rules, dict) else None
    if not isinstance(fingerprint, str) or not fingerprint or not isinstance(history_values, dict):
        raise ValueError("selection manifest requires active report rules")
    window = ReportHistoryWindow(**history_values)
    candidates = [dict(record) for record in (candidate_records or []) if _source_url(record)]
    group_records = [
        record
        for record in candidates
        if (
            _in_window(record, as_of=as_of, window=window)
            or (record.get("eligibility") == "incomplete" and not record.get("published_at"))
        )
        and record.get("eligibility") != "revoked"
        and not (
            record.get("eligibility") == "rejected"
            and record.get("rejection_reason")
            in {"invitation_veto", "non_report_detail_title", "non_report_release"}
        )
    ]
    selected_packet_rows = _packet_rows(
        [record for record in packet_inputs if _in_window(record, as_of=as_of, window=window)]
    )
    packet_urls = {_source_url(row) for row in selected_packet_rows}
    # A persisted selected variant may gain a bilingual group id after the
    # discovery candidate was assembled. Its URL still denotes one edition,
    # not an additional expected group with no candidate.
    packet_groups_by_url = {_source_url(row): _group_id(row) for row in selected_packet_rows}
    packet_classes_by_group = {_group_id(row): _report_class(row) for row in selected_packet_rows}
    if not group_records:
        group_records = selected_packet_rows
    groups: dict[str, dict[str, Any]] = {}
    for record in group_records:
        group_id = packet_groups_by_url.get(_source_url(record), _group_id(record))
        group = groups.setdefault(
            group_id,
            {
                "group_id": group_id,
                "slot_key": _slot_key(record),
                "report_class": packet_classes_by_group.get(group_id, _report_class(record)),
                "candidate_source_urls": [],
                "candidate_observation_ids": [],
                "relation_observation_ids": [],
                "packet_source_urls": [],
                "rejections": [],
                "selected": None,
            },
        )
        source_url = _source_url(record)
        if source_url not in group["candidate_source_urls"]:
            group["candidate_source_urls"].append(source_url)
        observation_id = record.get("candidate_observation_id")
        if observation_id and observation_id not in group["candidate_observation_ids"]:
            group["candidate_observation_ids"].append(observation_id)
        for relation_id in record.get("relation_observation_ids") or ():
            if relation_id not in group["relation_observation_ids"]:
                group["relation_observation_ids"].append(relation_id)
        if source_url in packet_urls and source_url not in group["packet_source_urls"]:
            group["packet_source_urls"].append(source_url)
    for row in selected_packet_rows:
        group_id = _group_id(row)
        group = groups.setdefault(
            group_id,
            {
                "group_id": group_id,
                "slot_key": _slot_key(row),
                "report_class": _report_class(row),
                "candidate_source_urls": [],
                "candidate_observation_ids": [],
                "relation_observation_ids": [],
                "packet_source_urls": [],
                "rejections": [],
                "selected": None,
            },
        )
        source_url = _source_url(row)
        if source_url not in group["packet_source_urls"]:
            group["packet_source_urls"].append(source_url)
        observation_id = row.get("candidate_observation_id")
        if observation_id and observation_id not in group["candidate_observation_ids"]:
            group["candidate_observation_ids"].append(observation_id)
        for relation_id in row.get("relation_observation_ids") or ():
            if relation_id not in group["relation_observation_ids"]:
                group["relation_observation_ids"].append(relation_id)
        stable = _stable_selection(row)
        if stable is not None:
            if group["selected"] is not None and group["selected"] != stable:
                raise ValueError("one evidence slot selected multiple immutable observations")
            group["selected"] = stable

    for group in groups.values():
        for source_url in group["candidate_source_urls"]:
            candidate = next(
                (record for record in candidates if _source_url(record) == source_url),
                None,
            )
            if candidate is None or source_url in group["packet_source_urls"]:
                continue
            group["rejections"].append(
                {
                    **(
                        {"candidate_observation_id": candidate["candidate_observation_id"]}
                        if candidate.get("candidate_observation_id")
                        else {}
                    ),
                    "reason": str(candidate.get("rejection_reason") or "not_selected_by_manifest"),
                }
            )
        group["candidate_source_urls"].sort()
        group["candidate_observation_ids"].sort()
        group["relation_observation_ids"].sort()
        group["packet_source_urls"].sort()
        group["rejections"].sort(
            key=lambda rejection: (
                str(rejection.get("candidate_observation_id") or ""),
                str(rejection["reason"]),
            )
        )
    selected_urls = {url for group in groups.values() for url in group["packet_source_urls"]}
    considered_urls = {_source_url(record) for record in candidates}
    candidate_by_url = {_source_url(record): record for record in candidates}
    rejected = tuple(
        {
            "source_url": url,
            **(
                {"candidate_observation_id": candidate_by_url[url]["candidate_observation_id"]}
                if candidate_by_url.get(url, {}).get("candidate_observation_id")
                else {}
            ),
            "reason": str(
                candidate_by_url.get(url, {}).get("rejection_reason")
                or (
                    "outside_history_window"
                    if not _in_window(candidate_by_url.get(url, {}), as_of=as_of, window=window)
                    else "not_selected_by_manifest"
                )
            ),
        }
        for url in sorted(considered_urls - selected_urls)
    )
    audit_by_url = {_source_url(row): row for row in audit_history if _source_url(row)}
    for record in candidates:
        audit_by_url.setdefault(_source_url(record), record)
    selected_packet_rows = [
        row for row in selected_packet_rows if _source_url(row) in selected_urls
    ]
    reusable_rows = (
        _packet_rows(
            [
                record
                for record in group_records
                if record.get("eligibility") == "eligible"
                and not record.get("rejection_reason")
                and _stable_selection(record) is not None
            ]
        )
        or selected_packet_rows
    )
    return EvidenceSelectionManifest(
        manifest_version=MANIFEST_VERSION,
        company_id=company_id,
        as_of=as_of[:10],
        report_rules_fingerprint=fingerprint,
        history_window=dict(history_values),
        audit_history=tuple(audit_by_url.values()),
        cache=tuple(reusable_rows),
        reuse=tuple(
            {
                "attachment_sha256": row.get("attachment_sha256"),
                "byte_size": row.get("byte_size"),
                "extraction_checksum": row.get("text_checksum"),
                "valid_as_of": as_of[:10],
            }
            for row in reusable_rows
        ),
        deduplication=tuple(groups[key] for key in sorted(groups)),
        packet_inputs=tuple(selected_packet_rows),
        rejected=rejected,
        readiness_fallback={
            "documents_available": bool(audit_by_url),
            "current_packet_available": bool(selected_packet_rows),
            "requires_current_packet": True,
        },
        source_input_fingerprint=source_input_fingerprint,
    )


def completeness(manifest: EvidenceSelectionManifest) -> dict[str, dict[str, int]]:
    return manifest.completeness


def packet_contents(manifest: EvidenceSelectionManifest) -> tuple[dict[str, Any], ...]:
    return manifest.packet_contents()
