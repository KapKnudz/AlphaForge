import json

import pytest
from test_method_date_growth_selection import CUTOFF, setup

from alphaforge.cli.ranking_loader import (
    _dcf_exception_status,
    _dcf_failure_status,
    _dcf_solve_status,
    load_results_for_company,
)
from alphaforge.core.valuation.dcf_contract import (
    DCF_RESULT_CONTRACT_VERSION,
    AssumptionOrigin,
    AssumptionProvenance,
    DcfResultMetadata,
    DcfResultStatus,
    EvidenceReference,
    serialize_dcf_result,
)


def test_empty_history_early_refusal_uses_the_same_result_contract():
    conn, cid = setup(periods=[])
    result = load_results_for_company(conn, cid, CUTOFF)["reverse_dcf"]

    assert result["status"] == "unavailable"
    assert result["dcf"]["status"] == "insufficient_evidence"
    assert result["dcf"]["reason"] == "financial_period"
    assert result["dcf"]["version"] == "reverse-dcf-v17-admissible-growth-domain"
    assert result["dcf"]["contract_version"] == DCF_RESULT_CONTRACT_VERSION
    json.dumps(result["dcf"], allow_nan=False)


@pytest.mark.parametrize(
    "status",
    [
        DcfResultStatus.UNSUPPORTED,
        DcfResultStatus.INVALID_INPUT,
        DcfResultStatus.INSUFFICIENT_EVIDENCE,
        DcfResultStatus.DOMAIN_UNAVAILABLE,
        DcfResultStatus.SAMPLED_MATCH,
        DcfResultStatus.NO_CROSSING,
        DcfResultStatus.NONCONVERGENCE,
    ],
)
def test_canonical_result_contract_preserves_distinct_refusal_statuses(status):
    result = serialize_dcf_result(
        {"available": False, "detail": "original diagnostic"},
        DcfResultMetadata(status, "specific reason", ("limitation",), "policy-v1"),
    )

    assert result["status"] == status.value
    assert result["reason"] == "specific reason"
    assert result["warnings"] == ["limitation"]
    assert result["version"] == "policy-v1"
    assert result["contract_version"] == DCF_RESULT_CONTRACT_VERSION
    assert result["detail"] == "original diagnostic"
    json.dumps(result, allow_nan=False)


def test_result_status_mapping_distinguishes_input_and_solver_failures():
    assert _dcf_exception_status(ValueError("invalid DCF input")) is DcfResultStatus.INVALID_INPUT
    assert (
        _dcf_exception_status(RuntimeError("reverse DCF solver did not converge"))
        is DcfResultStatus.NONCONVERGENCE
    )
    assert _dcf_solve_status("no_candidate_solution", None, None) is DcfResultStatus.NO_CROSSING
    assert _dcf_solve_status("sampled_match", None, None) is DcfResultStatus.SAMPLED_MATCH
    assert (
        _dcf_failure_status("DCF report and stock price currencies are not both verified")
        is DcfResultStatus.INSUFFICIENT_EVIDENCE
    )
    assert (
        _dcf_failure_status("report denomination mismatch across DCF inputs")
        is DcfResultStatus.INVALID_INPUT
    )
    assert (
        _dcf_failure_status("market-cap hurdle policy is defined for SEK; received EUR")
        is DcfResultStatus.UNSUPPORTED
    )
    assert (
        _dcf_failure_status("positive market capitalization unavailable for required-return hurdle")
        is DcfResultStatus.INSUFFICIENT_EVIDENCE
    )
    assert (
        _dcf_solve_status(
            "unavailable", "invalid_candidate_economics", "unsupported_capital_release"
        )
        is DcfResultStatus.DOMAIN_UNAVAILABLE
    )
    assert (
        _dcf_solve_status(
            "unavailable", "invalid_candidate_economics", "reverse DCF solver did not converge"
        )
        is DcfResultStatus.NONCONVERGENCE
    )


def test_assumption_provenance_serializes_typed_evidence_and_limitations():
    result = serialize_dcf_result(
        {"available": True},
        DcfResultMetadata(DcfResultStatus.AVAILABLE, None, (), "policy-v1"),
        {
            "tax_rate": AssumptionProvenance(
                AssumptionOrigin.FIXED_DEFAULT,
                "fixed tax policy",
                (EvidenceReference("annual:2025", published_on="2026-05-01", anchor="tax"),),
                ("not a company-specific forecast",),
            )
        },
    )

    tax = result["assumption_provenance"]["tax_rate"]
    assert tax["origin"] == "fixed_default"
    assert tax["evidence_references"][0]["source_id"] == "annual:2025"
    assert tax["limitations"] == ["not a company-specific forecast"]
    assert "confidence" not in tax


def test_canonical_result_serializer_rejects_nonfinite_values():
    with pytest.raises(ValueError, match="non-finite"):
        serialize_dcf_result(
            {"value_per_share": float("inf")},
            DcfResultMetadata(DcfResultStatus.AVAILABLE, None, (), "policy-v1"),
        )
