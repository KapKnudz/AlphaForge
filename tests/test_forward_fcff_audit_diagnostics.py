"""Accumulated chronology audit survives refusal without changing financial outputs."""

from __future__ import annotations

import copy

import pytest
from forward_fcff_fixtures import complete_bridge, request

from alphaforge.core.valuation.forward_fcff import evaluate
from alphaforge.core.valuation.forward_fcff_contract import digest

OUTPUTS = ("operating_cash_flows", "operating_value", "common_equity", "per_share")


def reviewed_initial_wc(publication, reviewed_on):
    req, f = request()
    complete_bridge(req, f)
    provenance = req["operating_schedule"]["initial_wc"]["provenance"]
    provenance.update(
        {
            "class": "reported_fact",
            "accounting_date": "2025-12-31",
            "uncertainty": "source_observed",
            "source_refs": [{"source_id": "source", "anchor": "p1"}],
        }
    )
    catalog = {
        "sources": [
            {
                "id": "source",
                "url": "https://example.test/retained-source",
                "sha256": "a" * 64,
                "publication_date": publication,
                "observation_date": "2025-12-31",
                "ingested_at": "2026-01-02T00:00:00Z",
                "anchors": {"p1": "Synthetic reported opening WC is zero"},
                "limitations": ["publication_unknown"] if publication is None else [],
            }
        ]
    }
    req["evidence_context"] = {"catalog": catalog, "catalog_hash": digest(catalog)}
    identities = []

    def link(tree):
        if isinstance(tree, dict):
            if set(tree) == {"value", "provenance"}:
                tree["provenance"]["review_ids"] = ["review"]
                identities.append(tree["provenance"]["id"])
            else:
                for value in tree.values():
                    link(value)
        elif isinstance(tree, list):
            for value in tree:
                link(value)

    link(req)
    req["reviews"] = [
        {
            "id": "review",
            "reviewer": "Synthetic fixture human event",
            "reviewer_kind": "human",
            "reviewed_on": reviewed_on,
            "scope_operand_ids": sorted(identities),
            "basis_identity": "Explicit synthetic supplied basis",
            "conclusion": "conditional",
            "limitations": [],
        }
    ]
    return req


@pytest.mark.parametrize(
    ("publication", "reviewed_on", "unresolved", "reason", "diagnostic"),
    [
        (
            None,
            "2026-02-01",
            False,
            "fact_not_cutoff_qualified",
            "review_source_publication_unknown",
        ),
        (
            "2026-01-01",
            "2020-01-01",
            True,
            "required_operand_unresolved",
            "review_precedes_source_publication",
        ),
    ],
)
def test_schedule_refusal_preserves_accumulated_chronology(
    publication, reviewed_on, unresolved, reason, diagnostic
):
    req = reviewed_initial_wc(publication, reviewed_on)
    if unresolved:
        req["operating_schedule"]["periods"][0]["cash_operating_profit_before_da"]["provenance"][
            "uncertainty"
        ] = "unresolved"
    refused = evaluate(req)
    expected_path = "cash_operating_profit_before_da" if unresolved else "operand-1"
    for name in OUTPUTS:
        assert refused[name]["available"] is False
        assert refused[name]["value"] is None
        assert refused[name]["reason_codes"] == [reason]
        assert refused[name]["missing_inputs"] == [expected_path]
    assert refused["review_status"] == "unreviewed"
    assert refused["review_diagnostics"] == [
        {
            "code": diagnostic,
            "operand_id": "operand-1",
            "review_id": "review",
            "source_id": "source",
        }
    ]

    valid = reviewed_initial_wc("2026-01-01", "2026-02-01")
    computed = evaluate(valid)
    assert all(computed[name]["available"] for name in OUTPUTS)
    assert computed["review_status"] == "human_reviewed"
    assert "review_diagnostics" not in computed

    early = reviewed_initial_wc("2026-01-01", "2020-01-01")
    conditional = evaluate(early)
    assert conditional["review_status"] == "unreviewed"
    for name in OUTPUTS:
        assert conditional[name] == computed[name]


def test_structural_refusal_before_resolver_still_has_no_invented_diagnostics():
    req = reviewed_initial_wc("2026-01-01", "2026-02-01")
    invalid = copy.deepcopy(req)
    invalid["schema_version"] = "unknown"
    result = evaluate(invalid)
    assert all(not result[name]["available"] and result[name]["value"] is None for name in OUTPUTS)
    assert result["review_status"] == "unreviewed"
    assert "review_diagnostics" not in result
