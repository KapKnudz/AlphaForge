"""Annual range and off-feed extraction contracts across the immutable lane."""

import io
import json

import pytest
import test_post26_evidence_repairs as fixtures
from pypdf import PdfReader, PdfWriter
from test_post26_evidence_repairs import AS_OF, gate, item, run, view
from test_post26_evidence_repairs import lane as lane

from alphaforge.core.frozen_packet import validate_frozen_packet
from alphaforge.db.evidence_repository import current_candidate_observations
from alphaforge.db.repositories import load_evidence_packet
from alphaforge.evidence.flow import EvidenceResourceLimits
from alphaforge.providers.mfn.scraper import MfnScraper


@pytest.mark.parametrize(
    "title,body,expected,provenance,limitation",
    [
        ("Annual Report 2025/2026", "", "2025/2026", "report_title", None),
        ("Annual Report 2025/26", "", "2025/2026", "report_title", None),
        ("Årsredovisning 2025/2026", "", "2025/2026", "report_title", None),
        (
            "Annual Report 2025/2026 — compared with 2024/2025",
            "",
            "2025/2026",
            "report_title",
            None,
        ),
        ("Annual Report 2025", "", "2025", "report_title", None),
        (
            "Financial update",
            "Annual report 2025/2026",
            "2025/2026",
            "covered_report_heading",
            None,
        ),
        (
            "Financial update",
            "Forecast for 2027. Annual report 2025/26",
            "2025/2026",
            "covered_report_heading",
            None,
        ),
        (
            "Financial update",
            "Compared with annual report 2024/2025. Annual report 2025/2026",
            "2025/2026",
            "covered_report_heading",
            None,
        ),
        ("Annual report forecast 2025/2026", "", None, "unresolved", "fiscal_identity_unresolved"),
        (
            "Financial update",
            "Annual report forecast 2025/2026",
            None,
            "unresolved",
            "fiscal_identity_unresolved",
        ),
        (
            "Financial update",
            "Annual report 2024/2025. Annual report 2025/2026",
            None,
            "conflicting_covered_headings",
            "fiscal_identity_ambiguous",
        ),
    ],
)
def test_annual_range_survives_observation_manifest_packet_and_replay(
    lane, title, body, expected, provenance, limitation
):
    entry = item("annual-en", title)
    entry["properties"]["tags"] = ["sub:report", "sub:report:annual"]
    first, _ = run(lane, [entry], bodies={entry["url"]: body})
    assert first.status == "complete"
    source = first.packet["sources"][0]
    assert source["document_type"] == "ANNUAL_REPORT"
    assert source["fiscal_period"] == expected
    assert source["fiscal_period_source"] == provenance
    assert source["period_start"] is None
    assert source["period_end"] is None
    current = current_candidate_observations(lane[0], company_id=lane[1], as_of=AS_OF)[0]
    assert current["fiscal_period"] == expected
    assert json.loads(current["raw_metadata"])["fiscal_period_source"] == provenance
    if limitation:
        assert limitation in first.packet["limitations"]
    _, manifest = view(lane)
    if expected:
        assert manifest.deduplication[0]["slot_key"] == "annual:" + expected
    assert gate(first.packet, lane[1]) == "ready"
    replay, _ = run(lane, [entry], bodies={entry["url"]: body}, fetch=False)
    assert replay.status == "complete"
    assert replay.pdf_fetch_attempts == 0
    assert replay.packet_hash == first.packet_hash
    assert view(lane)[1].to_dict() == manifest.to_dict()
    assert replay.packet["sources"][0]["immutable_evidence"] == source["immutable_evidence"]


@pytest.mark.parametrize("provider_key", ["fiscal_period", "report_period", "period"])
@pytest.mark.parametrize(
    "explicit,expected,limitation",
    [
        ("2025/2026", "2025/2026", None),
        ("2025/26", "2025/26", None),
        ("2025", None, "fiscal_identity_ambiguous"),
        ("2024/2025", None, "fiscal_identity_ambiguous"),
    ],
)
def test_annual_range_provider_provenance_and_conflict_replay(
    lane, provider_key, explicit, expected, limitation
):
    entry = item("annual-en", "Annual Report 2025/2026")
    entry["properties"]["tags"] = ["sub:report", "sub:report:annual"]

    class ProviderPeriod(MfnScraper):
        def scrape_details(self, *args, **kwargs):
            return [
                {**article, provider_key: explicit}
                for article in super().scrape_details(*args, **kwargs)
            ]

    first, _ = run(
        lane, [entry], bodies={entry["url"]: ""}, scraper=ProviderPeriod(max_articles=60)
    )
    assert first.status == "complete"
    assert first.packet["sources"][0]["fiscal_period"] == expected
    current = current_candidate_observations(lane[0], company_id=lane[1], as_of=AS_OF)[0]
    metadata = json.loads(current["raw_metadata"])
    assert metadata["fiscal_period_input"] == explicit
    assert metadata["fiscal_period_input_key"] == provider_key
    if limitation:
        assert limitation in first.packet["limitations"]
    else:
        assert metadata["fiscal_period_source"] == "provider_metadata:" + provider_key
    replay, _ = run(
        lane,
        [entry],
        bodies={entry["url"]: ""},
        scraper=ProviderPeriod(max_articles=60),
        fetch=False,
    )
    assert replay.packet_hash == first.packet_hash
    assert (
        current_candidate_observations(lane[0], company_id=lane[1], as_of=AS_OF)[0][
            "candidate_observation_id"
        ]
        == current["candidate_observation_id"]
    )


@pytest.mark.parametrize("refresh", ["unchanged", "config", "extractor"])
def test_offfeed_verified_extraction_reuse_or_refresh_preserves_history(lane, monkeypatch, refresh):
    original_pdf = fixtures.pdf

    def two_pages(text=fixtures.PDF_TEXT):
        writer = PdfWriter()
        reader = PdfReader(io.BytesIO(original_pdf(text)))
        writer.add_page(reader.pages[0])
        writer.add_page(reader.pages[0])
        output = io.BytesIO()
        writer.write(output)
        return output.getvalue()

    monkeypatch.setattr(fixtures, "pdf", two_pages)
    entries = [item(), item("q2-sv", "Flow AB Delårsrapport Q2 2026", "sv")]
    first, _ = run(lane, entries)
    old = current_candidate_observations(lane[0], company_id=lane[1], as_of=AS_OF)
    _, old_manifest = view(lane)
    historical_manifest_id = first.packet["selection_manifest_id"]
    historical = (
        lane[0]
        .execute(
            "SELECT manifest_json FROM evidence_selection_manifests WHERE manifest_id=?",
            (historical_manifest_id,),
        )
        .fetchone()[0]
    )
    change_config = refresh == "config"
    must_refresh = refresh != "unchanged"
    if refresh == "extractor":
        monkeypatch.setattr(
            "alphaforge.evidence.revision_flow._extractor_version", lambda: "fixture-upgrade"
        )
    limits = EvidenceResourceLimits(max_pages=1 if change_config else 50)
    changed, requested = run(lane, [], limits=limits, fetch=False)
    assert changed.status == "complete"
    assert changed.pdf_fetch_attempts == 0
    assert not any("storage.mfn.se" in url for url in requested)
    current = current_candidate_observations(lane[0], company_id=lane[1], as_of=AS_OF)
    assert len(current) == len(old) == 2
    for before, after in zip(old, current, strict=True):
        assert after["candidate_id"] == before["candidate_id"]
        assert (after["extraction_id"] != before["extraction_id"]) == must_refresh
        assert (
            after["candidate_observation_id"] != before["candidate_observation_id"]
        ) == must_refresh
    _, manifest = view(lane)
    for before, after in zip(
        sorted(old_manifest.cache, key=lambda x: x["source_url"]),
        sorted(manifest.cache, key=lambda x: x["source_url"]),
        strict=True,
    ):
        assert after["artifact_id"] == before["artifact_id"]
        assert len(after["pages"]) == (1 if change_config else 2)
        assert after["page_truncated"] == change_config
    assert (
        lane[0]
        .execute(
            "SELECT manifest_json FROM evidence_selection_manifests WHERE manifest_id=?",
            (historical_manifest_id,),
        )
        .fetchone()[0]
        == historical
    )
    for before in old:
        assert (
            lane[0]
            .execute(
                "SELECT extraction_id FROM evidence_candidate_observations WHERE candidate_observation_id=?",
                (before["candidate_observation_id"],),
            )
            .fetchone()[0]
            == before["extraction_id"]
        )
    assert validate_frozen_packet(first.packet)
    assert validate_frozen_packet(changed.packet)
    assert (changed.packet_hash != first.packet_hash) == must_refresh
    assert load_evidence_packet(lane[0], lane[1], AS_OF)["packet_hash"] == changed.packet_hash
    assert gate(changed.packet, lane[1]) == "ready"
    replay, _ = run(lane, [], limits=limits, fetch=False)
    assert replay.status == "complete"
    assert replay.pdf_fetch_attempts == 0
    assert replay.packet_hash == changed.packet_hash
    assert view(lane)[1].to_dict() == manifest.to_dict()


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_changed_config_offfeed_missing_bytes_refuses_without_http(lane, damage):
    first, _ = run(lane, [item()])
    binding = view(lane)[1].cache[0]
    digest = binding["attachment_sha256"]
    path = lane[2].root / "sha256" / digest[:2] / f"{digest}.pdf"
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"%PDF-corrupt")
    failed, requested = run(lane, [], limits=EvidenceResourceLimits(max_pages=1), fetch=False)
    assert failed.status == "evidence_incomplete"
    assert failed.packet is None
    assert failed.pdf_fetch_attempts == 0
    assert not any("storage.mfn.se" in url for url in requested)
    assert load_evidence_packet(lane[0], lane[1], AS_OF) is None
    assert gate(None, lane[1]) == "evidence_blocked"
    assert validate_frozen_packet(first.packet)
