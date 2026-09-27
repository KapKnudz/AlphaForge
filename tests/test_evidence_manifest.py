from __future__ import annotations

from alphaforge.evidence.manifest import (
    EvidenceSelectionManifest,
    completeness,
    packet_contents,
    select_evidence_manifest,
)


def test_manifest_completeness_and_packet_contents_share_retained_groups():
    source = {"source_url": "https://mfn.test/a/q1", "document_id": 1}
    sibling = {"source_url": "https://mfn.test/cis/q1", "document_id": 2}
    manifest = EvidenceSelectionManifest(
        manifest_version="evidence-selection-manifest-v1",
        company_id=1,
        as_of="2026-09-20",
        report_rules_fingerprint="current",
        history_window={
            "interim_lookback_years": 2,
            "annual_lookback_years": 5,
            "max_offsets": 12,
            "max_detail_fetches": 60,
            "limit_per_offset": 48,
        },
        audit_history=(source,),
        cache=(source, sibling),
        reuse=(
            {"attachment_sha256": "sha", "valid_as_of": "2026-09-20"},
            {"attachment_sha256": "sha2", "valid_as_of": "2026-09-20"},
        ),
        deduplication=(
            {
                "group_id": "period:quarterly:2026-07-31",
                "report_class": "quarterly",
                "candidate_source_urls": [source["source_url"], sibling["source_url"]],
                "packet_source_urls": [source["source_url"], sibling["source_url"]],
            },
        ),
        packet_inputs=(source, sibling),
        rejected=(),
        readiness_fallback={"documents_available": True, "current_packet_available": True},
    )

    assert completeness(manifest) == {"quarterly": {"expected": 1, "retained": 1}}
    assert packet_contents(manifest) == (source, sibling)
    assert sum(counts["retained"] for counts in completeness(manifest).values()) == 1
    assert len(packet_contents(manifest)) == 2


def test_v2_manifest_binds_exact_immutable_selection_and_slot():
    rules = {
        "fingerprint": "rules-v2",
        "history_window": {
            "interim_lookback_years": 2,
            "annual_lookback_years": 5,
            "max_offsets": 12,
            "max_detail_fetches": 60,
            "limit_per_offset": 48,
        },
    }
    source = {
        "source_url": "https://mfn.test/a/q2-en",
        "published_at": "2026-07-15T05:45:00Z",
        "report_kind": "quarterly",
        "period_end": "2026-06-30",
        "candidate_key": "candidate-en",
        "candidate_observation_id": "observation-en-v2",
        "attachment_observation_id": "attachment-en-v2",
        "artifact_id": "sha256:abc",
        "immutable_extraction_id": "extraction-abc",
        "attachment_url": "https://storage.mfn.test/q2-en.pdf",
        "relation_observation_ids": ["relation-en-sv-v2"],
    }
    manifest = select_evidence_manifest(
        company_id=1,
        as_of="2026-09-20",
        report_rules=rules,
        audit_history=[source],
        candidate_records=[source],
        packet_inputs=[source],
        source_input_fingerprint="feed-v2",
    )

    assert manifest.manifest_version == "evidence-selection-manifest-v2"
    assert packet_contents(manifest) == (source,)
    assert completeness(manifest) == {"quarterly": {"expected": 1, "retained": 1}}
    group = manifest.deduplication[0]
    assert group["slot_key"] == "quarterly:2026-06-30"
    assert group["candidate_observation_ids"] == ["observation-en-v2"]
    assert group["relation_observation_ids"] == ["relation-en-sv-v2"]
    assert group["selected"] == {
        "candidate_key": "candidate-en",
        "candidate_observation_id": "observation-en-v2",
        "attachment_observation_id": "attachment-en-v2",
        "artifact_id": "sha256:abc",
        "extraction_id": "extraction-abc",
        "release_source_url": "https://mfn.test/a/q2-en",
        "attachment_source_url": "https://storage.mfn.test/q2-en.pdf",
    }


def test_v2_packet_contents_does_not_substitute_same_url_old_observation():
    current = {
        "source_url": "https://mfn.test/a/q2-en",
        "candidate_observation_id": "current",
    }
    old = {
        "source_url": "https://mfn.test/a/q2-en",
        "candidate_observation_id": "old",
    }
    manifest = EvidenceSelectionManifest(
        manifest_version="evidence-selection-manifest-v2",
        company_id=1,
        as_of="2026-09-20",
        report_rules_fingerprint="current",
        history_window={},
        audit_history=(old, current),
        cache=(current,),
        reuse=(),
        deduplication=(
            {
                "group_id": "period:quarterly:2026-06-30",
                "report_class": "quarterly",
                "candidate_source_urls": [current["source_url"]],
                "packet_source_urls": [current["source_url"]],
                "selected": {"candidate_observation_id": "current"},
            },
        ),
        packet_inputs=(old, current),
        rejected=(),
        readiness_fallback={"documents_available": True, "current_packet_available": True},
    )

    assert packet_contents(manifest) == (current,)
