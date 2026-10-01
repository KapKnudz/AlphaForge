"""Retained source facts → current admission → immutable rebuild, without PDFs."""

import io
import json
from contextlib import contextmanager

import pytest
import test_post26_evidence_repairs as fixtures
from pypdf import PdfReader, PdfWriter
from test_post26_evidence_repairs import AS_OF, detail, gate, item, run, view
from test_post26_evidence_repairs import lane as lane

from alphaforge.core.frozen_packet import validate_frozen_packet
from alphaforge.db.evidence_repository import current_candidate_observations
from alphaforge.db.repositories import load_evidence_packet
from alphaforge.evidence import report_rules
from alphaforge.evidence.flow import EvidenceResourceLimits
from alphaforge.evidence.revision_flow import RevisionRecorder
from alphaforge.providers.mfn.scraper import MfnScraper


@contextmanager
def historical_rules(monkeypatch, *, omit=(), version=7):
    """Real historical records; only simulate the old metadata capture shape."""
    with monkeypatch.context() as historical:
        historical.setattr(report_rules, "REPORT_RULES_VERSION", version)
        historical.setattr("alphaforge.evidence.flow.EVIDENCE_RULES_VERSION", version + 2)
        if omit:
            # Pre-rebuild records did not capture either canonical URL or raw
            # HTML. Omit both when exercising that historical fallback shape.
            if "canonical_url" in omit:
                omit = (*omit, "mfn_detail_html")
            original = RevisionRecorder.record

            def old_capture(self, article, **kwargs):
                return original(
                    self,
                    {key: value for key, value in article.items() if key not in omit},
                    **kwargs,
                )

            historical.setattr(RevisionRecorder, "record", old_capture)
        yield


def observations(lane):
    return current_candidate_observations(lane[0], company_id=lane[1], as_of=AS_OF)


def assert_preserved(lane, before, manifest_id, manifest_json):
    for row in before:
        persisted = (
            lane[0]
            .execute(
                "SELECT * FROM evidence_candidate_observations WHERE candidate_observation_id=?",
                (row["candidate_observation_id"],),
            )
            .fetchone()
        )
        assert dict(persisted) == {key: row[key] for key in persisted.keys()}
    assert (
        lane[0]
        .execute(
            "SELECT manifest_json FROM evidence_selection_manifests WHERE manifest_id=?",
            (manifest_id,),
        )
        .fetchone()[0]
        == manifest_json
    )


def historical_manifest(lane, packet):
    manifest_id = packet["selection_manifest_id"]
    payload = (
        lane[0]
        .execute(
            "SELECT manifest_json FROM evidence_selection_manifests WHERE manifest_id=?",
            (manifest_id,),
        )
        .fetchone()[0]
    )
    return manifest_id, payload


@pytest.mark.parametrize(
    "title,tags,body,expected,kind",
    [
        (
            "Annual Report 2025/26",
            ["sub:report:annual"],
            "Annual report 2025/26",
            "2025/2026",
            "annual",
        ),
        (
            "Interim Report Q2 2026",
            ["sub:report:interim:q2"],
            "January-June 2026",
            "2026/2026-q2",
            "quarterly",
        ),
        ("Financial update", ["sub:report:annual"], "Annual report 2025/26", "2025/2026", "annual"),
        (
            "Financial update",
            ["sub:report:interim:q2"],
            "Interim report Q2 2026",
            "2026/2026-q2",
            "quarterly",
        ),
        (
            "Year-end report 2025",
            ["sub:report:interim:q4"],
            "January-December 2025",
            "2025/2025-q4",
            "quarterly",
        ),
    ],
)
@pytest.mark.parametrize("change_config", [False, True])
def test_current_rule_offfeed_rebuild_and_fully_offline_replay(
    lane, monkeypatch, title, tags, body, expected, kind, change_config
):
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
    entry = item("retained-en", title)
    entry["properties"]["tags"] = ["sub:report", *tags]
    with historical_rules(monkeypatch):
        first, _ = run(lane, [entry], bodies={entry["url"]: body})
        before = observations(lane)
        old_binding = view(lane)[1].cache[0]
    assert first.status == "complete"
    old_manifest_id, old_manifest_json = historical_manifest(lane, first.packet)
    limits = EvidenceResourceLimits(max_pages=1 if change_config else 50)
    rebuilt, requested = run(lane, [], limits=limits, fetch=False)
    assert rebuilt.status == "complete"
    assert all("?offset=" in url for url in requested), requested  # Feed only: no detail or PDF.
    assert rebuilt.pdf_fetch_attempts == rebuilt.pdf_fetch_succeeded == 0
    after = observations(lane)
    assert len(after) == 1
    assert after[0]["candidate_id"] == before[0]["candidate_id"]
    assert after[0]["candidate_observation_id"] != before[0]["candidate_observation_id"]
    assert (
        after[0]["report_rules_fingerprint"] == report_rules.report_rules_metadata()["fingerprint"]
    )
    assert after[0]["report_kind"] == kind
    assert after[0]["fiscal_period"] == expected
    assert (after[0]["extraction_id"] != before[0]["extraction_id"]) == change_config
    _, manifest = view(lane)
    binding = manifest.cache[0]
    assert binding["artifact_id"] == old_binding["artifact_id"]
    assert binding["attachment_sha256"] == old_binding["attachment_sha256"]
    assert len(binding["pages"]) == (1 if change_config else 2)
    assert binding["page_truncated"] == change_config
    assert rebuilt.completeness == {kind: {"expected": 1, "retained": 1}}
    assert rebuilt.packet_hash != first.packet_hash  # Rules + new observation projection.
    assert validate_frozen_packet(first.packet)
    assert validate_frozen_packet(rebuilt.packet)
    assert gate(rebuilt.packet, lane[1]) == "ready"
    assert load_evidence_packet(lane[0], lane[1], AS_OF)["packet_hash"] == rebuilt.packet_hash
    assert_preserved(lane, before, old_manifest_id, old_manifest_json)
    replay, requested = run(lane, [], limits=limits, fetch=False)
    assert replay.status == "complete"
    assert all("?offset=" in url for url in requested)
    assert replay.pdf_fetch_attempts == 0
    assert replay.packet_hash == rebuilt.packet_hash
    assert view(lane)[1].to_dict() == manifest.to_dict()
    assert observations(lane) == after


@pytest.mark.parametrize("provider_key", ["fiscal_period", "report_period", "period"])
@pytest.mark.parametrize("explicit,expected", [("2025/26", "2025/2026"), ("2024/25", None)])
def test_offfeed_reclassification_keeps_original_provider_input_and_conflicts(
    lane, monkeypatch, provider_key, explicit, expected
):
    entry = item("annual-en", "Annual Report 2025/2026")
    entry["properties"]["tags"] = ["sub:report", "sub:report:annual"]

    class Provider(MfnScraper):
        def scrape_details(self, *args, **kwargs):
            return [
                {**article, provider_key: explicit}
                for article in super().scrape_details(*args, **kwargs)
            ]

    with historical_rules(monkeypatch):
        first, _ = run(lane, [entry], scraper=Provider(max_articles=60))
    assert first.status == "complete"
    rebuilt, requested = run(lane, [], fetch=False)
    assert rebuilt.status == "complete"
    assert all("?offset=" in url for url in requested)
    current = observations(lane)[0]
    metadata = json.loads(current["raw_metadata"])
    assert current["fiscal_period"] == expected
    assert metadata["fiscal_period_input"] == explicit
    assert metadata["fiscal_period_input_key"] == provider_key
    if expected:
        assert metadata["fiscal_period_source"] == "provider_metadata:" + provider_key
    else:
        assert "fiscal_identity_ambiguous" in rebuilt.packet["limitations"]
    replay, _ = run(lane, [], fetch=False)
    assert replay.packet_hash == rebuilt.packet_hash
    assert observations(lane)[0] == current


def cis_entry(slug="q2-en", title="Interim Report Q2 2026"):
    entry = item(slug, title)
    entry["url"] = f"https://mfn.se/cis/a/flow/{slug}-aabbccdd"
    return entry


def cis_detail(entry, *, issuer="flow", body=None):
    page = detail(entry) if body is None else detail(entry, body)
    return page.replace(f'href="{entry["url"]}"', f'href="https://mfn.se/all/a/{issuer}/report"')


@pytest.mark.parametrize("change_config", [False, True])
def test_missing_immutable_canonical_uses_only_known_detail_then_offline(
    lane, monkeypatch, change_config
):
    entry = cis_entry()
    page = cis_detail(entry)
    with historical_rules(monkeypatch, omit=("canonical_url",)):
        first, _ = run(lane, [entry], pages={entry["url"]: page})
        before = observations(lane)
    assert first.status == "complete"
    old_manifest_id, old_manifest_json = historical_manifest(lane, first.packet)
    limits = EvidenceResourceLimits(max_pages=1 if change_config else 50)
    rebuilt, requested = run(lane, [], pages={entry["url"]: page}, limits=limits, fetch=False)
    assert rebuilt.status == "complete"
    assert [url for url in requested if "?offset=" not in url] == [entry["url"]]
    assert rebuilt.pdf_fetch_attempts == 0
    current = observations(lane)[0]
    assert (
        json.loads(current["raw_metadata"])["canonical_url"] == "https://mfn.se/all/a/flow/report"
    )
    feed_event = (
        lane[0]
        .execute(
            "SELECT discovered_count, unseen_count FROM mfn_feed_checks ORDER BY id DESC LIMIT 1"
        )
        .fetchone()
    )
    assert tuple(feed_event) == (0, 0)  # Known-URL revalidation is not new feed discovery.
    assert current["candidate_id"] == before[0]["candidate_id"]
    assert (current["extraction_id"] != before[0]["extraction_id"]) == change_config
    assert_preserved(lane, before, old_manifest_id, old_manifest_json)
    manifest = view(lane)[1].to_dict()
    replay, requested = run(lane, [], limits=limits, fetch=False)
    assert replay.status == "complete"
    assert all("?offset=" in url for url in requested)
    assert replay.packet_hash == rebuilt.packet_hash
    assert view(lane)[1].to_dict() == manifest
    assert observations(lane)[0] == current


@pytest.mark.parametrize(
    "refusal",
    ["foreign_canonical", "missing_canonical", "invitation", "missing_date", "missing_title"],
)
def test_fallback_failed_current_admission_stays_incomplete_without_pdf(lane, monkeypatch, refusal):
    entry = cis_entry()
    page = cis_detail(entry)
    with historical_rules(monkeypatch, omit=("canonical_url",)):
        first, _ = run(lane, [entry], pages={entry["url"]: page})
        before = observations(lane)
    assert first.status == "complete"
    if refusal == "foreign_canonical":
        page = cis_detail(entry, issuer="foreign")
    elif refusal == "missing_canonical":
        page = page.replace('<link rel="canonical" href="https://mfn.se/all/a/flow/report">', "")
    elif refusal == "invitation":
        page = page.replace(
            "Interim Report Q2 2026</h1>", "Invitation to Interim Report Q2 2026 presentation</h1>"
        )
    elif refusal == "missing_date":
        page = page.replace(
            '<meta property="article:published_time" content="2026-07-15T08:00:00Z">', ""
        )
    else:
        page = "<html><body>Missing detail evidence</body></html>"
    failed, requested = run(lane, [], pages={entry["url"]: page}, fetch=False)
    assert failed.status == "evidence_incomplete"
    assert failed.packet is None
    assert failed.pdf_fetch_attempts == 0
    assert [url for url in requested if "?offset=" not in url] == [entry["url"]]
    assert load_evidence_packet(lane[0], lane[1], AS_OF) is None
    assert gate(None, lane[1]) == "evidence_blocked"
    assert failed.completeness == {"quarterly": {"expected": 1, "retained": 0}}
    assert observations(lane)[0]["eligibility"] != "eligible"
    assert validate_frozen_packet(first.packet)
    assert (
        before[0]["candidate_observation_id"] != observations(lane)[0]["candidate_observation_id"]
    )


def test_old_narrative_eligibility_is_not_a_raw_feed_attestation(lane, monkeypatch):
    entry = item("narrative-en", "Financial update")
    with historical_rules(monkeypatch, omit=("feed_report_evidence",)):
        first, _ = run(lane, [entry])
    assert first.status == "complete"
    failed, requested = run(lane, [], pages={entry["url"]: detail(entry)}, fetch=False)
    assert failed.status == "evidence_incomplete"
    assert failed.completeness == {"quarterly": {"expected": 1, "retained": 0}}
    assert failed.packet is None
    assert [url for url in requested if "?offset=" not in url] == [entry["url"]]
    assert failed.pdf_fetch_attempts == 0


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_old_fingerprint_missing_retained_bytes_never_reacquires_or_selects(
    lane, monkeypatch, damage
):
    with historical_rules(monkeypatch):
        first, _ = run(lane, [item()])
        binding = view(lane)[1].cache[0]
    digest = binding["attachment_sha256"]
    path = lane[2].root / "sha256" / digest[:2] / f"{digest}.pdf"
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"%PDF-corrupt")
    failed, requested = run(lane, [], fetch=False)
    assert failed.status == "evidence_incomplete"
    assert failed.packet is None
    assert failed.pdf_fetch_attempts == 0
    assert all("?offset=" in url for url in requested)
    assert load_evidence_packet(lane[0], lane[1], AS_OF) is None
    assert validate_frozen_packet(first.packet)


def test_exact_known_url_fallback_respects_existing_detail_budget(lane, monkeypatch):
    entries = [cis_entry("q1-en", "Interim Report Q1 2026"), cis_entry("q2-en")]
    entries[0]["content"]["publish_date"] = "2026-04-15T08:00:00Z"
    pages = {entry["url"]: cis_detail(entry) for entry in entries}
    pages[entries[0]["url"]] = cis_detail(entries[0], body="Interim report January-March 2026")
    with historical_rules(monkeypatch, omit=("canonical_url",)):
        first, _ = run(lane, entries, pages=pages)
    assert first.status == "complete"
    failed, requested = run(lane, [], pages=pages, scraper=MfnScraper(max_articles=1), fetch=False)
    assert failed.status == "evidence_incomplete"
    assert failed.pdf_fetch_attempts == 0
    assert [url for url in requested if "?offset=" not in url] == [entries[0]["url"]]
    assert failed.completeness == {"quarterly": {"expected": 2, "retained": 1}}
    assert failed.packet is None
    rows = observations(lane)
    deferred = next(row for row in rows if row["release_source_url"] == entries[1]["url"])
    assert (
        deferred["report_rules_fingerprint"] != report_rules.report_rules_metadata()["fingerprint"]
    )
    assert failed.skipped["retained_detail_revalidation_unavailable"] == 1
    # No provider fact was observed for the deferred URL; another bounded run
    # can now finish it rather than losing access to its retained binding.
    finished, requested = run(
        lane, [], pages=pages, scraper=MfnScraper(max_articles=1), fetch=False
    )
    assert finished.status == "complete"
    assert [url for url in requested if "?offset=" not in url] == [entries[1]["url"]]
    assert finished.pdf_fetch_attempts == 0
    assert finished.completeness == {"quarterly": {"expected": 2, "retained": 2}}
    manifest = view(lane)[1].to_dict()
    replay, requested = run(lane, [], scraper=MfnScraper(max_articles=1), fetch=False)
    assert all("?offset=" in url for url in requested)
    assert replay.packet_hash == finished.packet_hash
    assert view(lane)[1].to_dict() == manifest


@pytest.mark.parametrize("old_version", [7, 8])
def test_old_explicit_title_without_raw_tags_is_independently_sufficient(
    lane, monkeypatch, old_version
):
    with historical_rules(monkeypatch, omit=("feed_report_evidence",), version=old_version):
        first, _ = run(lane, [item()])
    assert first.status == "complete"
    rebuilt, requested = run(lane, [], fetch=False)
    assert rebuilt.status == "complete"
    assert all("?offset=" in url for url in requested)
    assert rebuilt.completeness == {"quarterly": {"expected": 1, "retained": 1}}
    assert rebuilt.packet["sources"][0]["fiscal_period_source"] == "report_title"


def test_offfeed_rule_rebuild_independently_retains_bilingual_annual_editions(lane, monkeypatch):
    entries = [
        item("annual-en", "Annual Report 2025/2026"),
        item("annual-sv", "Årsredovisning 2025/26", "sv"),
    ]
    for entry in entries:
        entry["properties"]["tags"] = ["sub:report", "sub:report:annual"]

    class Provider(MfnScraper):
        def scrape_details(self, *args, **kwargs):
            return [
                {**article, "provider_event_id": "annual-2025-2026", "period": "2025/26"}
                for article in super().scrape_details(*args, **kwargs)
            ]

    with historical_rules(monkeypatch):
        first, _ = run(lane, entries, scraper=Provider(max_articles=60))
        before = observations(lane)
        old_cache = view(lane)[1].cache
    old_manifest_id, old_manifest_json = historical_manifest(lane, first.packet)
    rebuilt, requested = run(lane, [], fetch=False)
    assert rebuilt.status == "complete"
    assert all("?offset=" in url for url in requested)
    after = observations(lane)
    assert len(after) == len(before) == 2
    for old, new in zip(before, after, strict=True):
        assert old["candidate_id"] == new["candidate_id"]
        assert old["extraction_id"] == new["extraction_id"]
        assert old["candidate_observation_id"] != new["candidate_observation_id"]
        assert new["fiscal_period"] == "2025/2026"
        assert json.loads(new["raw_metadata"])["fiscal_period_input"] == "2025/26"
    manifest = view(lane)[1]
    assert len(old_cache) == len(manifest.cache) == 2
    assert len(manifest.deduplication) == len(rebuilt.packet["sources"]) == 1
    assert rebuilt.completeness == {"annual": {"expected": 1, "retained": 1}}
    assert gate(rebuilt.packet, lane[1]) == "ready"
    assert_preserved(lane, before, old_manifest_id, old_manifest_json)
    replay, requested = run(lane, [], fetch=False)
    assert all("?offset=" in url for url in requested)
    assert replay.packet_hash == rebuilt.packet_hash
    assert view(lane)[1].to_dict() == manifest.to_dict()
    assert observations(lane) == after


def test_current_feed_detail_work_consumes_the_same_fallback_budget(lane, monkeypatch):
    retained = cis_entry("q1-en", "Interim Report Q1 2026")
    retained["content"]["publish_date"] = "2026-04-15T08:00:00Z"
    with historical_rules(monkeypatch, omit=("canonical_url",)):
        first, _ = run(
            lane,
            [retained],
            pages={retained["url"]: cis_detail(retained, body="Interim report January-March 2026")},
        )
    assert first.status == "complete"
    current = item("q2-en", "Interim Report Q2 2026")
    failed, requested = run(lane, [current], scraper=MfnScraper(max_articles=1))
    assert failed.status == "evidence_incomplete"
    assert retained["url"] not in requested
    assert [url for url in requested if "/a/flow/" in url] == [current["url"]]
    assert (
        failed.pdf_fetch_attempts == failed.pdf_fetch_succeeded == 1
    )  # Only the genuinely new PDF.
    assert failed.completeness == {"quarterly": {"expected": 2, "retained": 1}}
    deferred = next(
        row for row in observations(lane) if row["release_source_url"] == retained["url"]
    )
    assert (
        deferred["report_rules_fingerprint"] != report_rules.report_rules_metadata()["fingerprint"]
    )
    assert failed.skipped["retained_detail_revalidation_unavailable"] == 1


def test_old_cis_issuer_cannot_be_inferred_into_changed_reviewed_mapping(lane, monkeypatch):
    entry = cis_entry()
    with historical_rules(monkeypatch):
        first, _ = run(lane, [entry], pages={entry["url"]: cis_detail(entry)})
    assert first.status == "complete"
    lane[0].execute(
        "UPDATE mfn_issuer_mappings SET mfn_slug='all/a/foreign' WHERE company_id=?", (lane[1],)
    )
    lane[0].commit()
    failed, requested = run(lane, [], fetch=False)
    assert failed.status == "evidence_incomplete"
    assert all("?offset=" in url for url in requested)
    assert failed.pdf_fetch_attempts == 0
    assert failed.packet is None
    assert failed.completeness == {"quarterly": {"expected": 1, "retained": 0}}


@pytest.mark.parametrize("title,quarter", [("Year-end report 2025", "4"), ("Interim report", "2")])
def test_missing_raw_provider_subtype_cannot_change_class_or_lose_assertion(
    lane, monkeypatch, title, quarter
):
    entry = item("legacy-en", title)
    entry["properties"]["tags"] = ["sub:report", "sub:report:interim:q" + quarter]
    with historical_rules(monkeypatch, omit=("feed_report_evidence",)):
        first, _ = run(lane, [entry])
    assert first.status == "complete"
    failed, requested = run(lane, [], pages={entry["url"]: detail(entry)}, fetch=False)
    assert failed.status == "evidence_incomplete"
    assert failed.completeness == {"quarterly": {"expected": 1, "retained": 0}}
    assert failed.packet is None
    assert [url for url in requested if "?offset=" not in url] == [entry["url"]]
    assert failed.pdf_fetch_attempts == 0
    current = observations(lane)[0]
    assert current["eligibility"] == "incomplete"
    assert current["eligibility_reason"] == "retained_provider_type_unproven"
    assert current["report_kind"] == "quarterly"


def test_missing_annual_revalidation_timestamp_keeps_known_expected_publication(lane, monkeypatch):
    entry = cis_entry("annual-en", "Annual Report 2025/26")
    entry["properties"]["tags"] = ["sub:report", "sub:report:annual"]
    page = detail(entry, "Annual report 2025/26").replace(
        f'href="{entry["url"]}"', 'href="https://mfn.se/all/a/flow/report"'
    )
    with historical_rules(monkeypatch, omit=("canonical_url",)):
        first, _ = run(lane, [entry], pages={entry["url"]: page})
    assert first.status == "complete"
    page = page.replace(
        '<meta property="article:published_time" content="2026-07-15T08:00:00Z">', ""
    )
    failed, requested = run(lane, [], pages={entry["url"]: page}, fetch=False)
    assert failed.status == "evidence_incomplete"
    assert failed.completeness == {"annual": {"expected": 1, "retained": 0}}
    assert failed.packet is None
    assert failed.pdf_fetch_attempts == 0
    assert [url for url in requested if "?offset=" not in url] == [entry["url"]]


def test_original_html_reproves_attachment_selection_not_old_decoded_tier(lane, monkeypatch):
    with historical_rules(monkeypatch, omit=("attachment_tier",)):
        first, _ = run(lane, [item()])
    assert first.status == "complete"
    assert "attachment_tier" not in json.loads(observations(lane)[0]["raw_metadata"])
    rebuilt, requested = run(lane, [], fetch=False)
    assert rebuilt.status == "complete"
    assert all("?offset=" in url for url in requested)
    metadata = json.loads(observations(lane)[0]["raw_metadata"])
    assert metadata["attachment_tier"] == "mfn-primary"
    assert metadata["mfn_detail_html"]
    assert rebuilt.pdf_fetch_attempts == 0


@pytest.mark.parametrize("change_config", [False, True])
@pytest.mark.parametrize("legacy_link", [False, True])
def test_failed_offfeed_revalidation_can_recover_without_feed_or_pdf(
    lane, monkeypatch, change_config, legacy_link
):
    entry = cis_entry()
    page = cis_detail(entry)
    with historical_rules(monkeypatch, omit=("canonical_url",)):
        first, _ = run(lane, [entry], pages={entry["url"]: page})
        before = observations(lane)
    assert first.status == "complete"
    failed_page = page.replace(
        '<meta property="article:published_time" content="2026-07-15T08:00:00Z">', ""
    )
    if legacy_link:
        with historical_rules(monkeypatch, version=9, omit=("retained_source_observation_id",)):
            failed, _ = run(lane, [], pages={entry["url"]: failed_page}, fetch=False)
    else:
        failed, _ = run(lane, [], pages={entry["url"]: failed_page}, fetch=False)
    assert failed.status == "evidence_incomplete"
    incomplete = observations(lane)
    assert incomplete[0]["eligibility"] == "incomplete"
    limits = EvidenceResourceLimits(max_pages=1 if change_config else 50)
    recovered, requested = run(lane, [], pages={entry["url"]: page}, limits=limits, fetch=False)
    assert recovered.status == "complete"
    assert [url for url in requested if "?offset=" not in url] == [entry["url"]]
    assert recovered.pdf_fetch_attempts == 0
    after = observations(lane)
    assert after[0]["candidate_id"] == before[0]["candidate_id"]
    assert after[0]["candidate_observation_id"] != incomplete[0]["candidate_observation_id"]
    assert (after[0]["extraction_id"] != before[0]["extraction_id"]) == change_config
    assert recovered.completeness == {"quarterly": {"expected": 1, "retained": 1}}
    assert gate(recovered.packet, lane[1]) == "ready"
    assert_preserved(lane, incomplete, *historical_manifest(lane, first.packet))
    manifest = view(lane)[1].to_dict()
    replay, requested = run(lane, [], limits=limits, fetch=False)
    assert all("?offset=" in url for url in requested)
    assert replay.packet_hash == recovered.packet_hash
    assert view(lane)[1].to_dict() == manifest
    assert observations(lane) == after


def test_failed_retry_cannot_select_old_good_evidence_and_missing_bytes_block(lane, monkeypatch):
    entry = cis_entry()
    page = cis_detail(entry)
    with historical_rules(monkeypatch, omit=("canonical_url",)):
        first, _ = run(lane, [entry], pages={entry["url"]: page})
        binding = view(lane)[1].cache[0]
    bad = cis_detail(entry, issuer="foreign")
    failed, _ = run(lane, [], pages={entry["url"]: bad}, fetch=False)
    assert failed.status == "evidence_incomplete"
    again, requested = run(lane, [], pages={entry["url"]: bad}, fetch=False)
    assert again.status == "evidence_incomplete"
    assert again.packet is None
    assert [url for url in requested if "?offset=" not in url] == [entry["url"]]
    assert again.pdf_fetch_attempts == 0
    assert gate(None, lane[1]) == "evidence_blocked"
    digest = binding["attachment_sha256"]
    (lane[2].root / "sha256" / digest[:2] / f"{digest}.pdf").unlink()
    missing, requested = run(lane, [], pages={entry["url"]: page}, fetch=False)
    assert missing.status == "evidence_incomplete"
    assert missing.packet is None
    assert all(
        "?offset=" in url for url in requested
    )  # No metadata/PDF substitution for lost bytes.
    assert validate_frozen_packet(first.packet)


def test_latest_failed_feed_title_requires_fresh_proof_and_independent_acquisition(
    lane, monkeypatch
):
    entry = cis_entry()
    page = cis_detail(entry)
    with historical_rules(monkeypatch, version=9):
        first, _ = run(lane, [entry], pages={entry["url"]: page})
    original = observations(lane)[0]
    revised = {**entry, "content": {**entry["content"], "title": "Interim report Q2 2026 revised"}}
    bad = page.replace(
        '<meta property="article:published_time" content="2026-07-15T08:00:00Z">', ""
    )
    bad = bad.replace(entry["content"]["title"], revised["content"]["title"])
    failed, _ = run(lane, [revised], pages={entry["url"]: bad}, fetch=False)
    assert failed.status == "evidence_incomplete"
    assert "retained_source_observation_id" not in json.loads(observations(lane)[0]["raw_metadata"])
    again, requested = run(lane, [], pages={entry["url"]: bad}, fetch=False)
    assert again.status == "evidence_incomplete"
    assert again.packet is None  # Old healthy HTML is not current admission.
    assert [url for url in requested if "?offset=" not in url] == [entry["url"]]
    new_page = cis_detail(
        revised, body="Interim report April-June 2026. Revised operating discussion."
    )
    recovered, requested = run(lane, [], pages={entry["url"]: new_page})
    assert recovered.status == "complete"
    assert recovered.pdf_fetch_attempts == 1  # Changed source body cannot borrow old binding.
    current = observations(lane)[0]
    assert current["authoritative_feed_title"] == revised["content"]["title"]
    assert current["attachment_observation_id"] != original["attachment_observation_id"]
    assert current["candidate_id"] == original["candidate_id"]
    assert validate_frozen_packet(first.packet)


@pytest.mark.parametrize("healthy", [False, True])
def test_unchanged_feed_return_does_not_skip_current_failed_admission(lane, healthy):
    entry = cis_entry()
    page = cis_detail(entry)
    first, _ = run(lane, [entry], pages={entry["url"]: page})
    original = observations(lane)
    revised = {**entry, "content": {**entry["content"], "title": "Interim report Q2 2026 revised"}}
    bad = cis_detail(revised).replace(
        '<meta property="article:published_time" content="2026-07-15T08:00:00Z">', ""
    )
    failed, _ = run(lane, [revised], pages={entry["url"]: bad}, fetch=False)
    assert failed.status == "evidence_incomplete"
    incomplete = observations(lane)
    returned, requested = run(
        lane,
        [entry],
        pages={entry["url"]: page if healthy else cis_detail(entry, issuer="foreign")},
        fetch=False,
    )
    assert [url for url in requested if "?offset=" not in url] == [entry["url"]]
    assert returned.pdf_fetch_attempts == 0
    assert_preserved(lane, original, *historical_manifest(lane, first.packet))
    assert_preserved(lane, incomplete, *historical_manifest(lane, first.packet))
    if healthy:
        assert returned.status == "complete"
        assert gate(returned.packet, lane[1]) == "ready"
        current = observations(lane)
        assert current[0]["extraction_id"] == original[0]["extraction_id"]
        assert current[0]["candidate_observation_id"] != incomplete[0]["candidate_observation_id"]
        manifest = view(lane)[1].to_dict()
        replay, requested = run(lane, [entry], fetch=False)
        assert all("?offset=" in url for url in requested)
        assert replay.packet_hash == returned.packet_hash
        assert view(lane)[1].to_dict() == manifest
        assert observations(lane) == current
    else:
        assert returned.status == "evidence_incomplete"
        assert returned.packet is None
        assert gate(None, lane[1]) == "evidence_blocked"


@pytest.mark.parametrize("wrong", ["company", "candidate", "url"])
def test_retention_lookup_is_exact_and_never_returns_packet_selection(lane, wrong):
    from alphaforge.db.evidence_repository import retained_candidate_binding

    first, _ = run(lane, [item()])
    current = observations(lane)[0]
    params = {
        "company_id": lane[1],
        "as_of": AS_OF,
        "source_url": current["release_source_url"],
        "candidate_id": current["candidate_id"],
        "observation_id": current["candidate_observation_id"],
        "publication_cutoffs": {"annual": "2021-09-29", "quarterly": "2024-09-29"},
        "artifact_store": lane[2],
    }
    params[{"company": "company_id", "candidate": "candidate_id", "url": "source_url"}[wrong]] = (
        "https://mfn.se/a/flow/other" if wrong == "url" else 9999
    )
    assert retained_candidate_binding(lane[0], **params) is None
    assert observations(lane)[0] == current
    assert load_evidence_packet(lane[0], lane[1], AS_OF)["packet_hash"] == first.packet_hash
