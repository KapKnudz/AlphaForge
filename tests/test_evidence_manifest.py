from __future__ import annotations

from alphaforge.evidence.manifest import EvidenceSelectionManifest, completeness, packet_contents


def test_manifest_completeness_and_packet_contents_share_retained_groups():
    source = {"source_url": "https://mfn.test/a/q1", "document_id": 1}
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
        cache=(source,),
        deduplication=(
            {
                "group_id": "period:quarterly:2026-07-31",
                "report_class": "quarterly",
                "candidate_source_urls": [source["source_url"]],
                "packet_source_urls": [source["source_url"]],
            },
        ),
        packet_inputs=(source,),
        readiness_fallback={"documents_available": True, "current_packet_available": True},
    )

    assert completeness(manifest) == {"quarterly": {"expected": 1, "retained": 1}}
    assert packet_contents(manifest) == (source,)
    assert sum(counts["retained"] for counts in completeness(manifest).values()) == len(
        packet_contents(manifest)
    )
