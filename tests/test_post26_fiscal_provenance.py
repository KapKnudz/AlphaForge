"""Fiscal precedence through acquisition, immutable storage and offline replay."""

import json

import pytest
from test_post26_evidence_repairs import AS_OF, item, run, view
from test_post26_evidence_repairs import lane as lane  # Re-export the real lane fixture.

from alphaforge.db.evidence_repository import current_candidate_observations
from alphaforge.evidence.ingest import _fiscal_period
from alphaforge.providers.mfn.scraper import MfnScraper


@pytest.mark.parametrize(
    "explicit,expected,limitation",
    [
        ("Q2-2026", "Q2-2026", None),
        ("2026", "2026/2026-q2", None),
        ("Q1-2025", None, "fiscal_identity_ambiguous"),
    ],
)
def test_provider_identity_is_preserved_or_explicitly_conflicted(
    lane, explicit, expected, limitation
):
    entry = item()

    class ProviderPeriod(MfnScraper):
        def scrape_details(self, *args, **kwargs):
            return [
                {**article, "fiscal_period": explicit}
                for article in super().scrape_details(*args, **kwargs)
            ]

    first, _ = run(lane, [entry], scraper=ProviderPeriod(max_articles=60))
    assert first.status == "complete"
    current = current_candidate_observations(lane[0], company_id=lane[1], as_of=AS_OF)[0]
    assert current["fiscal_period"] == expected
    metadata = json.loads(current["raw_metadata"])
    assert metadata["fiscal_period_input"] == explicit
    assert first.packet["sources"][0]["fiscal_period"] == expected
    assert first.packet["sources"][0]["fiscal_period_source"] == metadata["fiscal_period_source"]
    if limitation:
        assert limitation in first.packet["limitations"]
        assert _fiscal_period({**metadata, **view(lane)[1].cache[0]}) == ""
    replay, _ = run(lane, [entry], scraper=ProviderPeriod(max_articles=60), fetch=False)
    assert replay.status == "complete"
    assert replay.packet_hash == first.packet_hash
    assert (
        current_candidate_observations(lane[0], company_id=lane[1], as_of=AS_OF)[0][
            "candidate_observation_id"
        ]
        == current["candidate_observation_id"]
    )


@pytest.mark.parametrize(
    "title,expected",
    [
        ("AQ Group AB (publ), interim report January-March, 2026", "2026/2026-q1"),
        ("AQ Group AB (publ), interim report January-June, 2025", "2025/2025-q2"),
        ("AQ Group AB (publ), interim report January - September, 2024", "2024/2024-q3"),
        ("AQ Group AB (publ): Year-end report 2024", "2024/2024-q4"),
        ("AQ Group AB (publ): Bokslutskommuniké 2025", "2025/2025-q4"),
        ("Flow AB Interim report January—June, 2026", "2026/2026-q2"),
    ],
)
def test_public_aq_punctuation_and_year_end_keep_covered_quarter(lane, title, expected):
    entry = item(title=title)
    entry["properties"]["tags"] = ["sub:report", f"sub:report:interim:q{expected[-1]}"]
    first, _ = run(lane, [entry])
    assert first.status == "complete"
    assert first.packet["sources"][0]["fiscal_period"] == expected
    assert view(lane)[1].deduplication[0]["slot_key"] == "quarterly:" + expected
    old = current_candidate_observations(lane[0], company_id=lane[1], as_of=AS_OF)[0]
    replay, _ = run(lane, [entry], fetch=False)
    assert replay.status == "complete"
    assert replay.packet_hash == first.packet_hash
    assert (
        current_candidate_observations(lane[0], company_id=lane[1], as_of=AS_OF)[0][
            "candidate_observation_id"
        ]
        == old["candidate_observation_id"]
    )


def test_annual_and_year_end_identity_remain_distinct(lane):
    annual = item("annual-en", "Flow AB Annual Report 2025")
    annual["properties"]["tags"] = ["sub:report", "sub:report:annual"]
    year_end = item("q4-en", "Flow AB Year-end report January-December 2025")
    year_end["properties"]["tags"] = ["sub:report", "sub:report:interim:q4"]
    result, _ = run(lane, [annual, year_end])
    assert result.status == "complete"
    sources = {source["source_url"]: source for source in result.packet["sources"]}
    assert sources[annual["url"]]["document_type"] == "ANNUAL_REPORT"
    assert sources[year_end["url"]]["document_type"] == "YEAR_END_REPORT"
    assert sources[annual["url"]]["fiscal_period"] == "2025"
    assert sources[year_end["url"]]["fiscal_period"] == "2025/2025-q4"
    assert len(result.packet["sources"]) == 2
