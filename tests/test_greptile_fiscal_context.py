"""Covered report clauses through immutable storage, manifest and offline replay."""

import json

import pytest
from test_post26_evidence_repairs import AS_OF, gate, item, run, view
from test_post26_evidence_repairs import lane as lane

from alphaforge.core.frozen_packet import validate_frozen_packet
from alphaforge.db.evidence_repository import current_candidate_observations
from alphaforge.db.repositories import load_evidence_packet
from alphaforge.evidence.ingest import _quarter_period, resolve_fiscal_identity


@pytest.mark.parametrize(
    "title,body,expected,provenance,limitation",
    [
        (
            "Interim report Q1 2025 — compared with year-end report 2024",
            "",
            "2025/2025-q1",
            "report_title",
            None,
        ),
        ("Year-end report 2025 — compared with Q1 2024", "", "2025/2025-q4", "report_title", None),
        (
            "Interim report 2025 Q1 — compared with year-end report 2024",
            "",
            "2025/2025-q1",
            "report_title",
            None,
        ),
        (
            "Delårsrapport Q1 2025 — jämfört med bokslutskommuniké 2024",
            "",
            "2025/2025-q1",
            "report_title",
            None,
        ),
        (
            "Financial update",
            "Forecast for 2027. Interim report January-June 2026",
            "2026/2026-q2",
            "covered_report_heading",
            None,
        ),
        (
            "Financial update",
            "Interim report forecast Q2 2027",
            None,
            "unresolved",
            "fiscal_identity_unresolved",
        ),
        (
            "Financial update",
            "Interim report forecast Q2 2027. Interim report January-December 2025",
            "2025/2025-q4",
            "covered_report_heading",
            None,
        ),
        (
            "Financial update",
            "Compared with year-end report 2024. Interim report January-March 2025",
            "2025/2025-q1",
            "covered_report_heading",
            None,
        ),
        (
            "Financial update",
            "Forecast for 2027. Interim report January-June 2026. Interim report January-September 2026",
            None,
            "conflicting_covered_headings",
            "fiscal_identity_ambiguous",
        ),
        (
            "Financial update",
            "Forecast for 2027. Interim report May-July 2026",
            "2026/2027-q1",
            "covered_report_heading",
            None,
        ),
        (
            "Financial update",
            "Prognos för 2027. Delårsrapport januari-juni 2026",
            "2026/2026-q2",
            "covered_report_heading",
            None,
        ),
        (
            "Financial update",
            "Delårsrapport prognos Q2 2027",
            None,
            "unresolved",
            "fiscal_identity_unresolved",
        ),
        # The HTML parser collapses bare whitespace, which cannot prove a heading boundary.
        (
            "Financial update",
            "Forecast for 2027\nInterim report January-June 2026",
            None,
            "unresolved",
            "fiscal_identity_unresolved",
        ),
        (
            "Financial update",
            "Forecast for 2027 — interim report Q2 2027",
            None,
            "unresolved",
            "fiscal_identity_unresolved",
        ),
        ("Interim report forecast Q2 2027", "", None, "unresolved", "fiscal_identity_unresolved"),
    ],
)
def test_covered_clause_identity_through_packet_and_offline_replay(
    lane, title, body, expected, provenance, limitation
):
    entry = item(title=title)
    if expected:
        entry["properties"]["tags"] = ["sub:report", "sub:report:interim:q" + expected[-1]]
    first, _ = run(lane, [entry], bodies={entry["url"]: body})
    assert first.status == "complete"
    source = first.packet["sources"][0]
    assert source["fiscal_period"] == expected
    assert source["fiscal_period_source"] == provenance
    assert source["period_end"] is None
    current = current_candidate_observations(lane[0], company_id=lane[1], as_of=AS_OF)[0]
    assert current["fiscal_period"] == expected
    assert json.loads(current["raw_metadata"])["fiscal_period_source"] == provenance
    if limitation:
        assert limitation in first.packet["limitations"]
    _, manifest = view(lane)
    if expected:
        assert manifest.deduplication[0]["slot_key"] == "quarterly:" + expected
    assert validate_frozen_packet(first.packet)
    assert load_evidence_packet(lane[0], lane[1], AS_OF)["packet_hash"] == first.packet_hash
    assert gate(first.packet, lane[1]) == "ready"
    replay, _ = run(lane, [entry], bodies={entry["url"]: body}, fetch=False)
    assert replay.status == "complete"
    assert replay.pdf_fetch_attempts == 0
    assert replay.packet_hash == first.packet_hash
    assert replay.packet["sources"][0]["immutable_evidence"] == source["immutable_evidence"]
    assert view(lane)[1].to_dict() == manifest.to_dict()
    assert (
        current_candidate_observations(lane[0], company_id=lane[1], as_of=AS_OF)[0][
            "candidate_observation_id"
        ]
        == current["candidate_observation_id"]
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Interim report Q1 2025 — compared with year-end report 2024", "2025/2025-q1"),
        ("Year-end report 2025 — compared with Q1 2024", "2025/2025-q4"),
        ("Interim report 2025 Q1 — compared with year-end report 2024", "2025/2025-q1"),
        ("Interim report Q2 2025 — compared with May-July 2024", "2025/2025-q2"),
    ],
)
def test_quarter_normalizer_preserves_period_location(text, expected):
    assert _quarter_period(text) == expected


def test_retained_plain_text_heading_boundary_excludes_previous_forecast():
    assert resolve_fiscal_identity(
        {
            "title": "Financial update",
            "content_text": "Forecast for 2027\nInterim report January-June 2026",
            "report_kind": "quarterly",
        }
    ) == ("2026/2026-q2", "covered_report_heading", None)
