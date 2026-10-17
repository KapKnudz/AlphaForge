"""Executable synthetic acceptance of aggregate estimates and scoped reporting."""

import copy
import csv
import hashlib
import json
import subprocess
from decimal import Decimal as D
from decimal import localcontext
from pathlib import Path

import pytest

from alphaforge.cli.estimated_forward import (
    SOURCE_PATHS as EXPORT_SOURCE_PATHS,
)
from alphaforge.cli.estimated_forward import (
    _code_sources,
)
from alphaforge.cli.main import build_parser
from alphaforge.core.valuation.estimated_fcff import evaluate, outcome, terminal
from alphaforge.core.valuation.estimated_inputs import (
    LABEL,
    Evidence,
    Unavailable,
    canonical,
    content_identity,
    load,
    revise,
)
from tests.estimated_fixtures import block, pack, response
from tools.check_estimated_forward_acceptance import (
    SOURCE_PATHS as ACCEPTANCE_SOURCE_PATHS,
)
from tools.check_estimated_forward_acceptance import (
    AcceptanceError,
    _code_identity,
    compare_outputs,
)

SCENARIO_STAGE_NAMES = {
    "annual_fcff",
    "wacc",
    "explicit_pv",
    "terminal",
    "operating_value",
    "bridge",
    "basic_shares",
    "common_equity",
    "per_share_reporting",
    "per_share_quote",
    "reverse",
}


def base(p):
    return evaluate(p)["scenarios"]["base"]


def number(out, field):
    assert out["available"], out
    return D(out["value"][field])


def use_earnings_linked(p):
    item = next(
        x for x in p["scenarios"]["base"]["bridge"]["items"] if x["category"] == "legal_other"
    )
    item.update(
        method="earnings_linked_pv",
        inputs={
            "recurring_baseline_ebit": "20",
            "current_pv_anchor": "1",
            "later_pv_anchor": "0",
        },
    )


def test_independent_constant_perpetuity_and_every_annual_leg():
    for p in (pack(), response()):
        result = base(p)
        assert number(result["operating_value"], "ev") == pytest.approx(D(140))
        assert number(result["common_equity"], "common_residual") == pytest.approx(D(155))
        assert number(result["per_share_reporting"], "price_estimate") == pytest.approx(D("15.5"))
        assert number(result["per_share_quote"], "price_estimate") == pytest.approx(D(31))
        for t, row in enumerate(result["annual_fcff"]["value"], 1):
            v = row["value"]
            assert D(v["ebit"]) == 20 and D(v["noncash_addbacks"]) == 5
            assert D(v["operating_cash_tax"]) == 4
            assert D(v["cash_investment"]) == 6 and D(v["lease_additions"]) == 1
            assert D(v["wc_open"]) == 10 and D(v["wc_close"]) == 10
            assert D(v["other_operating_cash"]) == 0 and D(v["fcff"]) == 14
            if t <= 10:
                assert D(result["explicit_pv"]["value"]["annual_pv"][t - 1]) == pytest.approx(
                    D(14) / D("1.1") ** t
                )
        assert len(result["explicit_pv"]["value"]["annual_pv"]) == 10
        assert result["annual_fcff"]["value"][3]["value"]["end"] == "2028-02-29"
        assert number(result["terminal"], "numerator") == 14
        assert result["reverse"]["status"] == "not_requested"


@pytest.mark.parametrize(
    "stage,survive,withheld",
    [
        ("operations", [], ["annual_fcff", "explicit_pv", "operating_value", "common_equity"]),
        ("discount", ["annual_fcff"], ["explicit_pv", "operating_value", "common_equity"]),
        ("terminal", ["annual_fcff", "explicit_pv"], ["operating_value", "common_equity"]),
        ("bridge", ["annual_fcff", "operating_value"], ["common_equity", "per_share_reporting"]),
        (
            "ownership",
            ["operating_value", "common_equity"],
            ["per_share_reporting", "per_share_quote"],
        ),
        ("fx", ["operating_value", "per_share_reporting"], ["per_share_quote"]),
    ],
)
def test_optional_missing_dependencies(stage, survive, withheld):
    p = pack()
    p["scenarios"]["base"][stage] = None
    r = base(p)
    for key in survive:
        assert r[key]["available"], (key, r[key])
    for key in withheld:
        assert not r[key]["available"], key
        assert r[key]["reasons"]


def test_operating_gap_retains_unaffected_years_and_tail_gap_preserves_explicit():
    p = pack()
    rows = p["scenarios"]["base"]["operations"]["rows"]
    rows[2]["operating_cash_tax"] = None
    r = base(p)
    assert not r["annual_fcff"]["available"]
    assert r["annual_fcff"]["value"][0]["available"]
    assert not r["annual_fcff"]["value"][2]["available"]
    rows[2]["operating_cash_tax"] = "4"
    rows[10]["operating_cash_tax"] = None
    r = base(p)
    assert r["explicit_pv"]["available"] and not r["operating_value"]["available"]


@pytest.mark.parametrize("horizon", [10, 11, 12])
def test_explicit_pv_components_are_exactly_first_ten_years(horizon):
    p = pack()
    p["scenarios"]["base"]["operations"]["rows"] = p["scenarios"]["base"]["operations"]["rows"][
        :horizon
    ]
    result = base(p)
    explicit = result["explicit_pv"]
    assert explicit["available"]
    components = [D(value) for value in explicit["value"]["annual_pv"]]
    assert len(components) == 10
    with localcontext() as context:
        context.prec = 64
        expected = [D(14) / D("1.1") ** year for year in range(1, 11)]
        expected_sum = sum(expected, D(0))
    assert components == expected
    assert D(explicit["value"]["sum"]) == expected_sum
    assert result["terminal"]["available"] == (horizon == 12)


def test_explicit_pv_is_invariant_to_zero_changed_and_unavailable_witnesses():
    baseline = base(pack())["explicit_pv"]

    p = pack()
    for row in p["scenarios"]["base"]["operations"]["rows"][10:]:
        for key in (
            "ebit",
            "noncash_addbacks",
            "operating_cash_tax",
            "cash_investment",
            "lease_additions",
        ):
            row[key] = "0"
    zero = base(p)
    assert zero["explicit_pv"] == baseline
    assert number(zero["terminal"], "numerator") == 0

    p = pack()
    for row in p["scenarios"]["base"]["operations"]["rows"][10:]:
        row["ebit"] = "30"
    changed = base(p)
    assert changed["explicit_pv"] == baseline
    assert number(changed["terminal"], "numerator") == 24

    for index in (10, 11):
        p = pack()
        p["scenarios"]["base"]["operations"]["rows"][index]["operating_cash_tax"] = None
        unavailable = base(p)
        assert unavailable["explicit_pv"] == baseline
        assert not unavailable["terminal"]["available"]

    p = pack()
    p["scenarios"]["base"]["operations"]["rows"][2]["operating_cash_tax"] = None
    assert not base(p)["explicit_pv"]["available"]


@pytest.mark.parametrize(
    "value,status,code",
    [
        (None, "insufficient_evidence", "missing_operand"),
        ("abc", "invalid_input", "invalid_number"),
    ],
)
def test_first_ten_row_refusal_preserves_root_status_and_reason(value, status, code):
    p = pack()
    p["scenarios"]["base"]["operations"]["rows"][2]["operating_cash_tax"] = value
    result = base(p)
    expected = [{"code": code, "path": "annual.3.operating_cash_tax"}]
    assert result["annual_fcff"]["status"] == status
    assert result["annual_fcff"]["reasons"] == expected
    assert result["annual_fcff"]["value"][2]["reasons"] == expected
    for output in ("explicit_pv", "terminal", "operating_value", "common_equity"):
        assert result[output]["status"] == status
        assert result[output]["reasons"] == expected


def test_unsupported_row_refusal_is_not_collapsed_by_consumers():
    p = pack()
    p["records"]["unsupported-row"] = {
        **copy.deepcopy(p["records"]["budget"]),
        "status": "unsupported",
    }
    p["scenarios"]["base"]["operations"]["rows"][2]["record_ids"] = ["unsupported-row"]
    result = base(p)
    expected = [{"code": "unqualified_record", "path": "unsupported-row"}]
    assert result["annual_fcff"]["status"] == "unsupported"
    assert result["annual_fcff"]["reasons"] == expected
    assert result["operating_value"]["status"] == "unsupported"
    assert result["operating_value"]["reasons"] == expected


def test_annual_refusal_summary_is_chronological_and_structural_failures_are_distinct():
    p = pack()
    rows = p["scenarios"]["base"]["operations"]["rows"]
    rows[2]["operating_cash_tax"] = None
    rows[3]["operating_cash_tax"] = "abc"
    result = base(p)["annual_fcff"]
    assert result["status"] == "insufficient_evidence"
    assert result["reasons"] == [{"code": "missing_operand", "path": "annual.3.operating_cash_tax"}]
    assert not result["value"][2]["available"] and not result["value"][3]["available"]

    p = pack()
    p["scenarios"]["base"]["operations"]["rows"] = p["scenarios"]["base"]["operations"]["rows"][:9]
    structural = base(p)["annual_fcff"]
    assert structural["status"] == "invalid_input"
    assert structural["reasons"] == [{"code": "ten_full_years_required", "path": ""}]
    p = pack()
    p["scenarios"]["base"]["operations"]["rows"] = {}
    assert base(p)["annual_fcff"]["reasons"] == [{"code": "invalid_annual_rows", "path": ""}]


@pytest.mark.parametrize("index", [10, 11])
@pytest.mark.parametrize(
    "value,status,code",
    [
        (None, "insufficient_evidence", "missing_operand"),
        ("abc", "invalid_input", "invalid_number"),
    ],
)
def test_terminal_witness_refusal_preserves_root_and_explicit_pv(index, value, status, code):
    p = pack()
    p["scenarios"]["base"]["operations"]["rows"][index]["operating_cash_tax"] = value
    result = base(p)
    expected = [{"code": code, "path": f"annual.{index + 1}.operating_cash_tax"}]
    assert result["annual_fcff"]["available"]
    assert result["explicit_pv"]["available"]
    assert result["terminal"]["status"] == status and result["terminal"]["reasons"] == expected
    assert result["operating_value"]["status"] == status
    assert result["operating_value"]["reasons"] == expected


@pytest.mark.parametrize("bad", ["0", "-0.1"])
def test_invalid_rate_preserves_cash(bad):
    p = pack()
    p["scenarios"]["base"]["discount"]["inputs"]["wacc"] = bad
    r = base(p)
    assert r["annual_fcff"]["available"]
    assert r["wacc"]["status"] == "invalid_input"


def test_gordon_strict_pole_and_not_final_row_shortcut():
    p = pack()
    p["scenarios"]["base"]["terminal"]["growth"] = ".1"
    r = base(p)
    assert r["explicit_pv"]["available"] and not r["operating_value"]["available"]
    p["scenarios"]["base"]["terminal"]["growth"] = "0"
    p["scenarios"]["base"]["operations"]["rows"][10]["finite_effects"]["opening"] = "1"
    assert not base(p)["operating_value"]["available"]
    p = pack()
    p["scenarios"]["base"]["operations"]["rows"][11]["cash_investment"] = "7"
    assert not base(p)["operating_value"]["available"]


def test_zero_cash_response_requires_revenue_growth_to_match_gordon_growth():
    p = response()
    inputs = p["scenarios"]["base"]["operations"]["inputs"]
    inputs.update(
        initial_growth=".05",
        terminal_growth=".05",
        margin_first="0",
        margin_terminal="0",
        da_first="0",
        da_terminal="0",
        cash_renewal_rate="0",
        cash_growth_capital="0",
        lease_renewal_rate="0",
        lease_growth_capital="0",
        wc_ratio="0",
        opening_wc="0",
        refundable_profit_share="0",
        ordinary_tax_rate="0",
    )
    p["scenarios"]["base"]["terminal"]["growth"] = ".02"
    mismatch = base(p)
    assert mismatch["explicit_pv"]["available"]
    assert mismatch["terminal"]["reasons"] == [
        {"code": "terminal_growth_mismatch", "path": "terminal.growth"}
    ]
    p["scenarios"]["base"]["terminal"]["growth"] = ".05"
    assert base(p)["terminal"]["available"]


def test_direct_terminal_drivers_are_optional_but_coherent_when_retained():
    assert base(pack())["terminal"]["available"]

    p = pack()
    rows = p["scenarios"]["base"]["operations"]["rows"]
    rows[10]["growth"] = "0.00"
    rows[11]["growth"] = "0"
    rows[10]["revenue"] = "100"
    rows[11]["revenue"] = "100.000000019"
    assert base(p)["terminal"]["available"]

    rows[11]["growth"] = ".01"
    assert base(p)["terminal"]["reasons"] == [
        {"code": "terminal_growth_mismatch", "path": "terminal.growth"}
    ]


@pytest.mark.parametrize(
    "first,second,status,code",
    [
        ("100", None, "insufficient_evidence", "terminal_revenue_pair_required"),
        (None, None, "insufficient_evidence", "missing_operand"),
        ("abc", "100", "invalid_input", "invalid_number"),
        ("100", "101", "invalid_input", "reconciliation_failed"),
    ],
)
def test_direct_terminal_revenue_refusals_are_typed(first, second, status, code):
    p = pack()
    rows = p["scenarios"]["base"]["operations"]["rows"]
    rows[10]["revenue"] = first
    if code != "terminal_revenue_pair_required":
        rows[11]["revenue"] = second
    result = base(p)["terminal"]
    assert result["status"] == status
    assert result["reasons"] == [{"code": code, "path": "terminal.revenue"}]


def test_direct_terminal_growth_null_and_malformed_keep_typed_status():
    for value, status, code in (
        (None, "insufficient_evidence", "missing_operand"),
        ("abc", "invalid_input", "invalid_number"),
    ):
        p = pack()
        p["scenarios"]["base"]["operations"]["rows"][10]["growth"] = value
        result = base(p)["terminal"]
        assert result["status"] == status
        assert result["reasons"] == [{"code": code, "path": "terminal.growth"}]


def test_terminal_optional_monetary_constituents_must_grow_coherently():
    p = pack()
    rows = p["scenarios"]["base"]["operations"]["rows"]
    rows[10]["gross_charge"] = "8"
    rows[11]["gross_charge"] = "0"
    result = base(p)["terminal"]
    assert result["reasons"] == [{"code": "reconciliation_failed", "path": "terminal.gross_charge"}]

    p = pack()
    rows = p["scenarios"]["base"]["operations"]["rows"]
    for row, gross, ordinary in zip(rows[10:], ("8", "0"), ("0", "8"), strict=True):
        row.update(
            gross_charge=gross,
            ordinary_charge=ordinary,
            topup_charge="0",
            new_charge="8",
        )
    assert base(p)["terminal"]["reasons"] == [
        {"code": "reconciliation_failed", "path": "terminal.gross_charge"}
    ]

    p = pack()
    for row in p["scenarios"]["base"]["operations"]["rows"][10:]:
        row.update(gross_charge="8", ordinary_charge="0", topup_charge="0", new_charge="8")
    assert base(p)["terminal"]["available"]

    p = pack()
    for row in p["scenarios"]["base"]["operations"]["rows"][10:]:
        row["gross_charge"] = "8"
    assert base(p)["terminal"]["available"]


def test_terminal_complete_investment_decompositions_reconcile_without_rewriting():
    p = pack()
    for row in p["scenarios"]["base"]["operations"]["rows"][10:]:
        row.update(renewal_investment="5", lead_investment="0")
    result = base(p)
    assert result["terminal"]["reasons"] == [
        {
            "code": "reconciliation_failed",
            "path": "terminal.11.cash_investment_decomposition",
        }
    ]
    assert D(result["annual_fcff"]["value"][10]["value"]["cash_investment"]) == 6

    for lead, available in (("1", True), ("1.000000019", True), ("1.000000021", False)):
        p = pack()
        for row in p["scenarios"]["base"]["operations"]["rows"][10:]:
            row.update(renewal_investment="5", lead_investment=lead)
        assert base(p)["terminal"]["available"] == available

    p = pack()
    for row in p["scenarios"]["base"]["operations"]["rows"][10:]:
        row["renewal_investment"] = "5"
    assert base(p)["terminal"]["available"]


def test_terminal_complete_tax_cash_decompositions_preserve_timing_distinctions():
    p = pack()
    for row in p["scenarios"]["base"]["operations"]["rows"][10:]:
        row.update(
            gross_charge="3",
            ordinary_charge="4",
            topup_charge="1",
            new_charge="8",
            new_entitlement="6",
            new_payment="5",
            new_recovery="1",
            opening_payment="0",
            opening_recovery="0",
            opening_deferred_cash="0",
            opening_tax="0",
            new_cash_tax="4",
        )
    assert base(p)["terminal"]["available"]

    p["scenarios"]["base"]["operations"]["rows"][10]["new_cash_tax"] = "3"
    assert base(p)["terminal"]["reasons"] == [
        {"code": "reconciliation_failed", "path": "terminal.new_cash_tax"}
    ]

    p = pack()
    for row in p["scenarios"]["base"]["operations"]["rows"][10:]:
        row.update(new_payment="5", new_recovery="1", new_cash_tax="3")
    assert base(p)["terminal"]["reasons"] == [
        {
            "code": "reconciliation_failed",
            "path": "terminal.11.new_cash_tax_decomposition",
        }
    ]

    p = pack()
    for row in p["scenarios"]["base"]["operations"]["rows"][10:]:
        row.update(
            opening_tax="0",
            new_payment="6",
            new_recovery="1",
            new_cash_tax="5",
        )
    assert base(p)["terminal"]["reasons"] == [
        {
            "code": "reconciliation_failed",
            "path": "terminal.11.operating_cash_tax_decomposition",
        }
    ]

    p = pack()
    for row in p["scenarios"]["base"]["operations"]["rows"][10:]:
        row.update(
            gross_charge="3",
            ordinary_charge="4",
            topup_charge="0",
            new_charge="8",
        )
    assert base(p)["terminal"]["reasons"] == [
        {
            "code": "reconciliation_failed",
            "path": "terminal.11.new_charge_decomposition",
        }
    ]


def test_terminal_ratios_and_finite_opening_components_are_distinct():
    p = pack()
    for row in p["scenarios"]["base"]["operations"]["rows"][10:]:
        row.update(revenue="100", growth="0", margin=".2")
    assert base(p)["terminal"]["available"]

    p["scenarios"]["base"]["operations"]["rows"][11]["margin"] = ".3"
    assert base(p)["terminal"]["reasons"] == [
        {"code": "terminal_ratio_mismatch", "path": "terminal.margin"}
    ]

    p = pack()
    for row in p["scenarios"]["base"]["operations"]["rows"][10:]:
        row.update(opening_payment="1", opening_recovery="1", opening_deferred_cash="0")
    assert base(p)["terminal"]["reasons"] == [
        {"code": "finite_cash_in_terminal", "path": "terminal.opening_payment"}
    ]

    p = pack()
    for row in p["scenarios"]["base"]["operations"]["rows"][10:]:
        row.update(ongoing_da="4", legacy_da="0")
    assert base(p)["terminal"]["reasons"] == [
        {
            "code": "reconciliation_failed",
            "path": "terminal.11.noncash_addbacks_decomposition",
        }
    ]


@pytest.mark.parametrize(
    "first_margin,second_margin,revenue,available,reason",
    [
        (".2", ".2", True, True, None),
        (".2", ".2", False, True, None),
        (".2", ".2000000001", True, True, None),
        (".2", ".1999999999", True, True, None),
        (".2", ".2000000001", False, True, None),
        (".2", ".20000002", False, True, None),
        (".2", ".200000020000000001", False, False, "terminal_ratio_mismatch"),
        (".2", ".3", False, False, "terminal_ratio_mismatch"),
        (None, None, True, True, None),
        (".2", None, False, False, "incomplete_witness_constituent"),
        (None, ".2", False, False, "incomplete_witness_constituent"),
    ],
)
def test_terminal_margin_annual_tolerance_boundary_and_optional_scope(
    first_margin, second_margin, revenue, available, reason
):
    p = pack()
    for row, margin in zip(
        p["scenarios"]["base"]["operations"]["rows"][10:],
        (first_margin, second_margin),
        strict=True,
    ):
        if margin is not None:
            row["margin"] = margin
        if revenue:
            row.update(revenue="100", growth="0")
            if margin is not None:
                assert abs(D(20) - D(100) * D(margin)) <= D("2e-8")
    if first_margin is not None and second_margin is not None:
        a, b = D(first_margin), D(second_margin)
        assert (abs(a - b) <= max(D("2e-8"), D("2e-12") * max(abs(a), abs(b)))) == available
    original = copy.deepcopy(p)
    result = base(p)
    assert p == original
    assert result["annual_fcff"]["available"] and result["explicit_pv"]["available"]
    rows = result["annual_fcff"]["value"]
    assert len(rows) == 12
    assert all(row["available"] and D(row["value"]["fcff"]) == 14 for row in rows)
    with localcontext() as context:
        context.prec = 64
        try:
            direct = outcome(
                terminal(p["scenarios"]["base"]["terminal"], rows, Evidence(p), D(".1"))
            )
        except Unavailable as error:
            direct = outcome(error=error)
    assert direct == result["terminal"]
    for stage in ("terminal", "operating_value", "common_equity"):
        assert result[stage]["available"] == available
    if available:
        assert number(result["terminal"], "numerator") == 14
        assert number(result["operating_value"], "ev") == pytest.approx(
            D(140), abs=D("1e-7"), rel=D("2e-11")
        )
    else:
        assert result["terminal"]["status"] == "invalid_input"
        assert result["terminal"]["reasons"] == [
            {
                "code": reason,
                "path": "margin"
                if reason == "incomplete_witness_constituent"
                else "terminal.margin",
            }
        ]


def test_loss_has_no_automatic_refund_but_opening_recovery_survives():
    p = response()
    inputs = p["scenarios"]["base"]["operations"]["inputs"]
    inputs.update(
        margin_first="-.1",
        margin_terminal=".2",
        opening_recoverable="7",
        opening_recovery_fractions=["1"],
        legacy_remaining="12",
        legacy_annual="5",
    )
    r = base(p)
    rows = r["annual_fcff"]["value"]
    assert D(rows[0]["value"]["new_charge"]) == 0
    assert D(rows[0]["value"]["new_entitlement"]) == 0
    assert D(rows[0]["value"]["opening_recovery"]) == 7
    assert D(rows[0]["value"]["operating_cash_tax"]) == -7
    assert [D(x["value"]["legacy_da"]) for x in rows[:4]] == [5, 5, 2, 0]


def test_tax_recovery_lag_topup_and_opening_once():
    p = response()
    inputs = p["scenarios"]["base"]["operations"]["inputs"]
    inputs.update(
        refundable_profit_share="1",
        opening_payable="8",
        opening_recoverable="6",
        opening_deferred="5",
        opening_payment_fractions=[".5", ".5"],
        opening_recovery_fractions=["0", "1"],
        same_year_payment=".5",
    )
    r = base(p)
    rows = [x["value"] for x in r["annual_fcff"]["value"]]
    # EBIT20: charge8 + topup1, entitlement6. Half cash now/next;
    # no new refund until t3. Finite opening payment8-refund6+deferred5=7.
    assert D(rows[0]["new_charge"]) == 9
    assert D(rows[0]["new_entitlement"]) == 6
    assert D(rows[0]["new_payment"]) == D("4.5")
    assert D(rows[0]["operating_cash_tax"]) == D("9.5")
    assert D(rows[1]["operating_cash_tax"]) == 8
    assert D(rows[2]["new_recovery"]) == 6
    assert sum(D(x["opening_tax"]) for x in rows) == 7
    assert D(rows[10]["opening_tax"]) == 0
    inputs["recovery_fraction"] = ".5"
    low = base(p)["annual_fcff"]["value"][0]["value"]
    assert D(low["topup_charge"]) == 0
    assert D(low["new_entitlement"]) == 3


def test_lag_three_breaks_terminal_transition_not_explicit_value():
    p = response()
    inputs = p["scenarios"]["base"]["operations"]["inputs"]
    inputs.update(
        initial_growth=".1", terminal_growth=".02", refundable_profit_share="1", recovery_lag=3
    )
    p["scenarios"]["base"]["terminal"]["growth"] = ".02"
    r = base(p)
    assert r["explicit_pv"]["available"] and not r["operating_value"]["available"]


def test_post_witness_recoveries_and_settlements_are_rejected():
    p = response()
    inputs = p["scenarios"]["base"]["operations"]["inputs"]
    inputs.update(refundable_profit_share="1", recovery_lag=12)
    assert base(p)["annual_fcff"]["status"] == "invalid_input"

    p = response()
    p["scenarios"]["base"]["operations"]["inputs"]["settlements"] = ["0"] * 10 + ["1"]
    assert base(p)["annual_fcff"]["status"] == "invalid_input"


def test_loss_gap_recovery_queue_withholds_terminal_after_equal_witnesses():
    p = response()
    inputs = p["scenarios"]["base"]["operations"]["inputs"]
    inputs.update(
        margin_first="-.1",
        margin_terminal=".2",
        refundable_profit_share="1",
        recovery_lag=10,
    )
    r = base(p)
    witnesses = [row["value"] for row in r["annual_fcff"]["value"][10:]]
    assert all(D(row["new_recovery"]) == 0 for row in witnesses)
    assert all(D(row["finite_effects"]["recovery_transition"]) == 1 for row in witnesses)
    assert r["explicit_pv"]["available"] and not r["terminal"]["available"]
    assert r["terminal"]["reasons"][0]["code"] == "finite_effects_not_exhausted"


def test_direct_annual_period_endpoints_must_be_supplied_together():
    for supplied, missing in (("start", "end"), ("end", "start")):
        p = pack()
        row = p["scenarios"]["base"]["operations"]["rows"][0]
        row[supplied] = "2024-03-01" if supplied == "start" else "2025-02-28"
        row.pop(missing, None)
        result = base(p)["annual_fcff"]
        assert not result["available"]
        assert result["value"][0]["reasons"] == [
            {"code": "annual_period_pair_required", "path": "1"}
        ]


def test_no_duplicate_lease_tax_compensation_claims():
    p = pack()
    p["scenarios"]["base"]["bridge"]["items"][0]["allocation_id"] = "operating_tax"
    r = base(p)
    assert r["operating_value"]["available"] and not r["common_equity"]["available"]
    p = pack()
    p["scenarios"]["base"]["operations"]["basis"] = "add_back_ordinary_compensation"
    assert base(p)["annual_fcff"]["status"] == "unsupported"


def test_every_registered_allocation_is_consumed_once_by_its_destination():
    for destination, unavailable in (
        ("operations", "annual_fcff"),
        ("bridge", "bridge"),
        ("rights", "common_equity"),
    ):
        p = pack()
        p["allocations"].append({"id": "orphan", "destination": destination})
        result = base(p)
        assert result[unavailable]["status"] == "invalid_input"

    p = pack()
    p["scenarios"]["base"]["operations"]["allocation_ids"].append("renewal")
    assert base(p)["annual_fcff"]["status"] == "invalid_input"

    p = pack()
    p["allocations"].append({"id": "documented-exclusion", "destination": "excluded_pair"})
    assert base(p)["common_equity"]["available"]


def test_earnings_linked_bridge_preserves_consumed_row_refusal_scope():
    p = pack()
    use_earnings_linked(p)
    p["scenarios"]["base"]["operations"]["rows"][2]["ebit"] = "abc"
    result = base(p)
    expected = [{"code": "invalid_number", "path": "annual.3.ebit"}]
    assert result["bridge"]["status"] == "invalid_input"
    assert result["bridge"]["reasons"] == expected

    p = pack()
    use_earnings_linked(p)
    p["records"]["unsupported-earnings"] = {
        **copy.deepcopy(p["records"]["budget"]),
        "status": "unsupported",
    }
    p["scenarios"]["base"]["operations"]["rows"][2]["record_ids"] = ["unsupported-earnings"]
    result = base(p)["bridge"]
    assert result["status"] == "unsupported"
    assert result["reasons"] == [{"code": "unqualified_record", "path": "unsupported-earnings"}]

    p = pack()
    use_earnings_linked(p)
    p["scenarios"]["base"]["operations"]["rows"][5]["ebit"] = "abc"
    assert base(p)["bridge"]["available"]

    p = pack()
    p["scenarios"]["base"]["operations"]["rows"][2]["ebit"] = "abc"
    assert base(p)["bridge"]["available"]


@pytest.mark.parametrize(
    "case,status,reason",
    [
        ("missing", "not_requested", {"code": "not_supplied", "path": "operations"}),
        ("invalid_block", "invalid_input", {"code": "invalid_block", "path": "operations"}),
        (
            "missing_record",
            "insufficient_evidence",
            {"code": "record_not_catalogued", "path": "absent-operation-record"},
        ),
        (
            "unsupported_basis",
            "unsupported",
            {"code": "unsupported_earnings_basis", "path": ""},
        ),
        ("malformed_rows", "invalid_input", {"code": "invalid_annual_rows", "path": ""}),
        ("empty_rows", "invalid_input", {"code": "ten_full_years_required", "path": ""}),
        (
            "annual_limitation",
            "unsupported",
            {"code": "unresolved_limitation", "path": "Synthetic annual limitation"},
        ),
    ],
)
def test_earnings_linked_bridge_preserves_whole_annual_refusal(case, status, reason):
    p = pack()
    use_earnings_linked(p)
    operations = p["scenarios"]["base"]["operations"]
    if case == "missing":
        p["scenarios"]["base"]["operations"] = None
    elif case == "invalid_block":
        p["scenarios"]["base"]["operations"] = []
    elif case == "missing_record":
        operations["record_ids"] = ["absent-operation-record"]
    elif case == "unsupported_basis":
        operations["basis"] = "unsupported_basis"
    elif case == "malformed_rows":
        operations["rows"] = {}
    elif case == "empty_rows":
        operations["rows"] = []
    else:
        p["limitations"].append(
            {
                "reason": "Synthetic annual limitation",
                "status": "unsupported",
                "affected_outputs": ["annual_fcff"],
            }
        )
    result = base(p)
    expected = [reason]
    assert result["annual_fcff"]["status"] == status
    assert result["annual_fcff"]["reasons"] == expected
    assert result["bridge"]["status"] == status
    assert result["bridge"]["reasons"] == expected


def test_non_earnings_bridge_methods_ignore_whole_annual_refusal():
    p = pack()
    p["scenarios"]["base"]["operations"] = None
    assert base(p)["bridge"]["available"]

    p = pack()
    p["scenarios"]["base"]["operations"] = None
    item = next(
        x for x in p["scenarios"]["base"]["bridge"]["items"] if x["category"] == "legal_other"
    )
    item.update(
        method="foreign_pv",
        inputs={
            "amount": "0",
            "currency": "USD",
            "reporting_currency": "EUR",
            "foreign_per_reporting": "1",
            "fx_date": p["valuation_date"],
        },
    )
    assert base(p)["bridge"]["available"]


def test_negative_residual_is_not_tradable_or_floored():
    p = pack()
    item = next(
        x for x in p["scenarios"]["base"]["bridge"]["items"] if x["category"] == "legal_other"
    )
    item["inputs"]["amount"] = "-200"
    r = base(p)
    assert number(r["common_equity"], "common_residual") == pytest.approx(D(-45))
    assert number(r["per_share_reporting"], "signed_quotient") == pytest.approx(D("-4.5"))
    assert r["per_share_reporting"]["value"]["price_estimate"] is None
    assert not r["per_share_quote"]["value"]["tradable_price"]


def test_endogenous_calls_independently_known_atm_value():
    p = pack()
    p["valuation_date"] = "2023-02-28"
    p["allocations"].append({"id": "call", "destination": "rights"})
    s = p["scenarios"]["base"]
    for key in ("discount", "bridge", "ownership", "rights", "fx"):
        s[key]["valuation_date"] = p["valuation_date"]
    # One-year ATM call at S=100, sigma=.2, r=q=0:
    # C=100*(2*Phi(.1)-1)=7.965567455405796...
    call = D("7.965567455405796")
    target = D(1000) + call
    item = next(x for x in s["bridge"]["items"] if x["category"] == "accessible_cash")
    item["inputs"]["amount"] = str(target - 140 + 5)
    s["rights"]["items"] = [
        block(
            id="synthetic-call",
            allocation_id="call",
            count="1000000",
            strike="100",
            volatility=".2",
            risk_free_continuous="0",
            dividend_yield="0",
            expiry="2024-02-28",
        )
    ]
    r = base(p)
    assert float(number(r["per_share_reporting"], "price_estimate")) == pytest.approx(
        100, abs=1e-10
    )
    assert float(number(r["common_equity"], "rights_value")) == pytest.approx(
        float(call), abs=1e-10
    )
    c = r["common_equity"]["value"]
    assert abs(D(c["allocation_residual"])) <= D("1e-24")
    s["bridge"]["items"][0]["inputs"]["amount"] = "2000"
    assert number(base(p)["common_equity"], "rights_value") > call


def test_parented_override_recalculates_and_invalidates_review_without_mutation():
    p = pack()
    p["reviews"] = [
        {"reviewer": "Synthetic human", "scope": "reference_only", "reviewed_on": "2024-03-16"}
    ]
    original = copy.deepcopy(p)
    scenarios = copy.deepcopy(p["scenarios"])
    scenarios["base"]["fx"]["quote_per_reporting"] = "3"
    child = revise(
        p,
        version="synthetic-r2",
        author="Synthetic override",
        prepared_on="2024-03-17",
        reason="Synthetic FX alternative",
        changes={"scenarios": scenarios},
    )
    assert p == original
    assert child["parent"]["content_identity"] == content_identity(p)
    assert child["reviews"] == [] and child["revision"]["invalidated_reviews"] == p["reviews"]
    assert child["revision"]["changed_inputs"][0]["path"].endswith("quote_per_reporting")
    assert child["revision"]["affected_outputs"] == [
        "common_equity",
        "explicit_pv",
        "operating_value",
        "per_share_quote",
        "per_share_reporting",
        "conclusion",
    ]
    assert number(base(child)["per_share_quote"], "price_estimate") == pytest.approx(D("46.5"))
    assert number(base(p)["per_share_quote"], "price_estimate") == pytest.approx(D(31))


def test_revision_result_differences_cover_scenario_addition_and_removal():
    p = pack()
    p["scenarios"]["same"] = copy.deepcopy(p["scenarios"]["base"])
    scenarios = {"base": copy.deepcopy(p["scenarios"]["base"])}
    removed = revise(
        p,
        version="synthetic-remove-neutral",
        author="Synthetic override",
        prepared_on="2024-03-17",
        reason="Remove neutral scenario",
        changes={"scenarios": scenarios},
    )
    differences = removed["revision"]["result_differences"]
    assert set(differences["scenarios"]) == {"same"}
    assert set(differences["scenarios"]["same"]) == SCENARIO_STAGE_NAMES
    assert "firm_investment_conclusion" not in differences
    for difference in differences["scenarios"]["same"].values():
        assert difference["before_present"] and not difference["after_present"]
        assert difference["before"] is not None and difference["after"] is None

    p = pack()
    scenarios = copy.deepcopy(p["scenarios"])
    scenarios["same"] = copy.deepcopy(scenarios["base"])
    added = revise(
        p,
        version="synthetic-add-neutral",
        author="Synthetic override",
        prepared_on="2024-03-17",
        reason="Add neutral scenario",
        changes={"scenarios": scenarios},
    )
    addition = added["revision"]["result_differences"]["scenarios"]["same"]
    assert set(addition) == SCENARIO_STAGE_NAMES
    for difference in addition.values():
        assert not difference["before_present"] and difference["after_present"]
        assert difference["before"] is None and difference["after"] is not None


def test_revision_result_differences_record_material_scenario_removal():
    p = pack()
    p["scenarios"]["downside"] = copy.deepcopy(p["scenarios"]["base"])
    for row in p["scenarios"]["downside"]["operations"]["rows"]:
        row["ebit"] = "10"
    scenarios = {"base": copy.deepcopy(p["scenarios"]["base"])}
    child = revise(
        p,
        version="synthetic-remove-material",
        author="Synthetic override",
        prepared_on="2024-03-17",
        reason="Remove material scenario",
        changes={"scenarios": scenarios},
    )
    differences = child["revision"]["result_differences"]
    assert set(differences["scenarios"]["downside"]) == SCENARIO_STAGE_NAMES
    conclusion = differences["firm_investment_conclusion"]
    assert conclusion["before"] == evaluate(p)["firm_investment_conclusion"]
    assert conclusion["after"] == evaluate(child)["firm_investment_conclusion"]
    assert "material_scenario_disagreement" in conclusion["before"]["reasons"]
    assert "material_scenario_disagreement" not in conclusion["after"]["reasons"]


@pytest.mark.parametrize(("before_ebit", "after_ebit"), [("20", "10"), ("10", "20")])
def test_revision_records_both_conclusion_directions_with_scenarios_retained(
    before_ebit, after_ebit
):
    p = pack()
    p["scenarios"]["downside"] = copy.deepcopy(p["scenarios"]["base"])
    for row in p["scenarios"]["downside"]["operations"]["rows"]:
        row["ebit"] = before_ebit
    scenarios = copy.deepcopy(p["scenarios"])
    for row in scenarios["downside"]["operations"]["rows"]:
        row["ebit"] = after_ebit
    child = revise(
        p,
        version=f"synthetic-conclusion-{before_ebit}-{after_ebit}",
        author="Synthetic override",
        prepared_on="2024-03-17",
        reason="Change scenario disagreement",
        changes={"scenarios": scenarios},
    )
    differences = child["revision"]["result_differences"]
    assert set(differences["scenarios"]) == {"downside"}
    conclusion = differences["firm_investment_conclusion"]
    assert conclusion == {
        "before": evaluate(p)["firm_investment_conclusion"],
        "after": evaluate(child)["firm_investment_conclusion"],
    }


def test_revision_result_differences_cover_malformed_transition_without_identity_churn():
    p = pack()
    malformed = revise(
        p,
        version="synthetic-malformed-result",
        author="Synthetic override",
        prepared_on="2024-03-17",
        reason="Malformed allocation alternative",
        changes={"allocations": [{}]},
    )
    outputs = malformed["revision"]["result_differences"]["scenarios"]["base"]
    assert set(outputs) == SCENARIO_STAGE_NAMES - {"reverse"}
    assert all(item["before_present"] and item["after_present"] for item in outputs.values())

    metadata = revise(
        p,
        version="synthetic-metadata-result",
        author="Synthetic override",
        prepared_on="2024-03-17",
        reason="Clarify a condition",
        changes={"conditions": p["conditions"] + ["Synthetic clarification"]},
    )
    assert metadata["revision"]["result_differences"] == {"scenarios": {}}
    with pytest.raises(Unavailable, match="override_has_no_change"):
        revise(
            p,
            version="synthetic-noop-result",
            author="Synthetic override",
            prepared_on="2024-03-17",
            reason="No effective change",
            changes={"scenarios": copy.deepcopy(p["scenarios"])},
        )


def test_revision_diff_distinguishes_missing_from_explicit_null():
    p = pack()
    scenarios = copy.deepcopy(p["scenarios"])
    scenarios["base"]["rights"]["fx_date"] = None
    added = revise(
        p,
        version="synthetic-null-added",
        author="Synthetic override",
        prepared_on="2024-03-17",
        reason="Retain explicit null",
        changes={"scenarios": scenarios},
    )
    addition = next(
        item for item in added["revision"]["changed_inputs"] if item["path"].endswith("fx_date")
    )
    assert addition == {
        "path": "scenarios.base.rights.fx_date",
        "before": None,
        "after": None,
        "before_present": False,
        "after_present": True,
    }

    original = copy.deepcopy(added)
    original.pop("revision")
    original["pack_version"] = "synthetic-null-parent"
    original["parent"] = None
    original["prepared_by"] = "Synthetic preparer"
    original["prepared_on"] = "2024-03-17"
    scenarios = copy.deepcopy(original["scenarios"])
    del scenarios["base"]["rights"]["fx_date"]
    removed = revise(
        original,
        version="synthetic-null-removed",
        author="Synthetic override",
        prepared_on="2024-03-18",
        reason="Remove explicit null",
        changes={"scenarios": scenarios},
    )
    removal = next(
        item for item in removed["revision"]["changed_inputs"] if item["path"].endswith("fx_date")
    )
    assert removal["before_present"] and not removal["after_present"]
    assert removal["before"] is None and removal["after"] is None
    assert not base(original)["common_equity"]["available"]
    assert base(removed)["common_equity"]["available"]


def test_source_anchor_and_limitation_scope():
    p = pack()
    p["limitations"].append(
        {
            "reason": "Synthetic unestimable legal claim",
            "status": "insufficient_evidence",
            "affected_outputs": ["common_equity"],
        }
    )
    r = base(p)
    assert r["operating_value"]["available"] and not r["common_equity"]["available"]
    p["limitations"][0]["affected_outputs"] = ["reverse"]
    assert base(p)["per_share_quote"]["available"]
    p["records"]["budget"]["anchors"][0]["anchor"] = "not-owned"
    assert not base(p)["annual_fcff"]["available"]


@pytest.mark.parametrize("anchor", [None, "", "   "])
def test_consumed_null_or_empty_anchor_never_qualifies(anchor):
    p = pack()
    p["sources"][0]["anchors"] = [anchor]
    p["records"]["budget"]["anchors"] = [{"source_id": "synthetic", "anchor": anchor}]
    result = base(load(json.dumps(p)))
    expected = [{"code": "anchor_not_owned", "path": "budget"}]
    assert result["annual_fcff"]["status"] == "insufficient_evidence"
    assert result["annual_fcff"]["reasons"] == expected


@pytest.mark.parametrize(
    "reference",
    [None, {}, {"source_id": "missing-source", "anchor": "budget-table"}],
)
def test_malformed_or_unowned_record_anchor_is_scoped(reference):
    p = pack()
    p["records"]["budget"]["anchors"] = [reference]
    result = base(load(json.dumps(p)))
    assert result["annual_fcff"]["reasons"] == [{"code": "anchor_not_owned", "path": "budget"}]


def test_nonempty_numeric_and_unused_invalid_source_anchors_remain_compatible():
    p = pack()
    p["sources"][0]["anchors"] = [7]
    p["records"]["budget"]["anchors"] = [{"source_id": "synthetic", "anchor": 7}]
    assert base(load(json.dumps(p)))["per_share_quote"]["available"]

    p = pack()
    unused = copy.deepcopy(p["sources"][0])
    unused.update(id="unused-source", anchors=[None, ""])
    p["sources"].append(unused)
    assert base(load(json.dumps(p)))["per_share_quote"]["available"]


def test_invalid_quote_anchor_withholds_only_quote_output():
    p = pack()
    quote_source = copy.deepcopy(p["sources"][0])
    quote_source.update(id="quote-source", anchors=[None])
    p["sources"].append(quote_source)
    quote_record = copy.deepcopy(p["records"]["budget"])
    quote_record["anchors"] = [{"source_id": "quote-source", "anchor": None}]
    p["records"]["quote"] = quote_record
    p["scenarios"]["base"]["fx"]["record_ids"] = ["quote"]
    result = base(load(json.dumps(p)))
    assert result["annual_fcff"]["available"]
    assert result["operating_value"]["available"]
    assert result["per_share_reporting"]["available"]
    assert result["per_share_quote"]["status"] == "insufficient_evidence"
    assert result["per_share_quote"]["reasons"] == [{"code": "anchor_not_owned", "path": "quote"}]


def test_scenario_disagreement_and_scaling():
    p = pack()
    p["scenarios"]["downside"] = copy.deepcopy(p["scenarios"]["base"])
    for row in p["scenarios"]["downside"]["operations"]["rows"]:
        row["ebit"] = "10"
    r = evaluate(p)
    assert "material_scenario_disagreement" in r["firm_investment_conclusion"]["reasons"]
    p = pack()
    p["scenarios"]["base"]["ownership"]["basic_shares"] = "20000000"
    assert number(base(p)["per_share_reporting"], "price_estimate") == pytest.approx(D("7.75"))


def test_equal_zero_scenarios_are_not_materially_different():
    p = pack()
    bridge = p["scenarios"]["base"]["bridge"]["items"]
    next(x for x in bridge if x["category"] == "accessible_cash")["inputs"]["amount"] = "0"
    next(x for x in bridge if x["category"] == "legal_other")["inputs"]["amount"] = "-135"
    p["scenarios"]["same"] = copy.deepcopy(p["scenarios"]["base"])
    result = evaluate(p)
    assert number(result["scenarios"]["base"]["common_equity"], "common_residual") == 0
    assert "material_scenario_disagreement" not in result["firm_investment_conclusion"]["reasons"]


def test_real_cli_detached_exports_are_self_describing_and_write_once(tmp_path, capsys):
    input_path = tmp_path / "synthetic.json"
    input_path.write_text(json.dumps(pack()))
    parser = build_parser()
    args = parser.parse_args(
        ["estimated-forward", "--input-json", str(input_path), "--exports-dir", str(tmp_path)]
    )
    assert args.func(args) == 0
    directory = json.loads(capsys.readouterr().out)["export_directory"]
    root = Path(directory)
    detached = json.loads((root / "base-per_share_quote.json").read_text())
    assert detached["identity"]["label"] == LABEL
    assert detached["assumption_pack"] == pack()
    assert detached["identity"]["human_status"] == "not_implementation_accepted"
    combined = json.loads((root / "outputs.json").read_text())
    context = {key: combined[key] for key in ("identity", "assumption_pack", "execution")}
    assert all(detached[key] == value for key, value in context.items())
    assert detached["scenario"] == "base"
    assert detached["output"] == "per_share_quote"
    selected = combined["scenarios"]["base"]["per_share_quote"]
    assert all(detached[key] == value for key, value in selected.items())
    explicit = json.loads((root / "base-explicit_pv.json").read_text())
    components = [D(value) for value in explicit["value"]["annual_pv"]]
    assert len(components) == 10
    with localcontext() as decimal_context:
        decimal_context.prec = 64
        assert sum(components, D(0)) == D(explicit["value"]["sum"])
    assert explicit["value"] == combined["scenarios"]["base"]["explicit_pv"]["value"]
    run = json.loads((root / "run.json").read_text())
    assert all(run[key] == value for key, value in context.items())
    csv_rows = list(csv.DictReader((root / "annual.csv").open()))
    assert len(csv_rows) == 12
    assert json.loads(csv_rows[0]["context"]) == context
    assert canonical(context) in (root / "report.md").read_text()
    for helper in (
        "alphaforge/core/valuation/forward_fcff.py",
        "alphaforge/core/valuation/forward_fcff_contract.py",
    ):
        assert (
            context["execution"]["code_sources"][helper]
            == hashlib.sha256((Path(__file__).parents[1] / helper).read_bytes()).hexdigest()
        )
    before = {x.name: x.read_bytes() for x in root.iterdir()}
    assert args.func(args) == 0
    assert before == {x.name: x.read_bytes() for x in root.iterdir()}


def test_cli_export_is_invariant_to_scenario_insertion_order(tmp_path, capsys):
    first = pack()
    base_scenario = copy.deepcopy(first["scenarios"]["base"])
    first["scenarios"] = {
        "base": copy.deepcopy(base_scenario),
        "downside": copy.deepcopy(base_scenario),
    }
    second = copy.deepcopy(first)
    second["scenarios"] = {
        "downside": copy.deepcopy(base_scenario),
        "base": copy.deepcopy(base_scenario),
    }
    changed = copy.deepcopy(first)
    for row in changed["scenarios"]["downside"]["operations"]["rows"]:
        row["ebit"] = "10"

    parser = build_parser()

    def run(name, value):
        input_path = tmp_path / f"{name}.json"
        input_path.write_text(json.dumps(value))
        args = parser.parse_args(
            ["estimated-forward", "--input-json", str(input_path), "--exports-dir", str(tmp_path)]
        )
        assert args.func(args) == 0
        return Path(json.loads(capsys.readouterr().out)["export_directory"]), args

    first_directory, first_args = run("first-order", first)
    first_bytes = {path.name: path.read_bytes() for path in first_directory.iterdir()}
    assert len(first_bytes) == 27
    second_directory, _ = run("second-order", second)
    assert second_directory == first_directory
    assert first_bytes == {path.name: path.read_bytes() for path in second_directory.iterdir()}

    assert first_args.func(first_args) == 0
    capsys.readouterr()
    assert first_bytes == {path.name: path.read_bytes() for path in first_directory.iterdir()}

    combined = json.loads((first_directory / "outputs.json").read_text())
    execution = combined["execution"]
    repository = Path(__file__).parents[1]
    assert (
        execution["code_revision"]
        == subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
    )
    for path, identity in execution["code_sources"].items():
        assert identity == hashlib.sha256((repository / path).read_bytes()).hexdigest()
    report = (first_directory / "report.md").read_text()
    assert report.index("## Scenario: base") < report.index("## Scenario: downside")
    csv_rows = list(csv.DictReader((first_directory / "annual.csv").open()))
    assert [row["scenario"] for row in csv_rows] == ["base"] * 12 + ["downside"] * 12

    changed_directory, _ = run("changed-content", changed)
    assert changed_directory != first_directory
    assert changed_directory.name != first_directory.name


def test_cli_exports_scoped_anchor_withholding_immutably(tmp_path, capsys):
    p = pack()
    p["sources"][0]["anchors"] = ["budget-table", None]
    p["records"]["budget"]["anchors"] = [{"source_id": "synthetic", "anchor": None}]
    control = copy.deepcopy(p["records"]["budget"])
    control["anchors"] = [{"source_id": "synthetic", "anchor": "budget-table"}]
    p["records"]["control"] = control
    p["scenarios"]["base"]["operations"]["record_ids"] = ["control"]
    input_path = tmp_path / "invalid-anchor.json"
    input_path.write_text(json.dumps(p))
    args = build_parser().parse_args(
        ["estimated-forward", "--input-json", str(input_path), "--exports-dir", str(tmp_path)]
    )
    assert args.func(args) == 0
    directory = Path(json.loads(capsys.readouterr().out)["export_directory"])
    annual = json.loads((directory / "base-annual_fcff.json").read_text())
    expected = [{"code": "anchor_not_owned", "path": "budget"}]
    assert annual["status"] == "insufficient_evidence"
    assert annual["reasons"] == expected
    combined = json.loads((directory / "outputs.json").read_text())
    assert combined["scenarios"]["base"]["annual_fcff"]["reasons"] == expected
    csv_rows = list(csv.DictReader((directory / "annual.csv").open()))
    assert len(csv_rows) == 12
    assert all(json.loads(row["reasons"]) == expected for row in csv_rows)
    assert canonical(expected) in (directory / "report.md").read_text()
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    assert args.func(args) == 0
    capsys.readouterr()
    assert before == {path.name: path.read_bytes() for path in directory.iterdir()}


def test_supported_wacc_build_effective_compounding_and_shield_once():
    p = pack()
    inputs = {
        "risk_free_continuous": ".03",
        "beta": "1.2",
        "equity_risk_premium": ".05",
        "market_close": "20",
        "market_close_date": p["valuation_date"],
        "market_close_currency": "SEK",
        "debt": "25",
        "debt_date": p["valuation_date"],
        "debt_currency": "EUR",
        "debt_cost": ".06",
        "debt_cost_date": p["valuation_date"],
        "debt_cost_basis": "nominal_annual_effective_pre_tax",
        "shield_rate": ".2",
        "shield_rate_date": p["valuation_date"],
    }
    p["scenarios"]["base"]["discount"]["inputs"] = inputs
    r = base(p)
    # Market E=100m and D=25m, independently of the modeled enterprise value.
    expected = D(".8") * (D(".03").exp() - 1 + D(".06")) + D(".2") * D(".06") * D(".8")
    assert number(r["wacc"], "market_equity") == 100
    assert number(r["wacc"], "debt_weight") == D(".2")
    assert number(r["wacc"], "wacc") == pytest.approx(expected)
    assert all(D(x["value"]["operating_cash_tax"]) == 4 for x in r["annual_fcff"]["value"])
    inputs["basic_shares"] = "5000000"
    assert base(p)["wacc"]["status"] == "invalid_input"
    inputs.pop("basic_shares")
    p["scenarios"]["base"]["ownership"]["basic_shares"] = "20000000"
    shared = base(p)
    assert number(shared["wacc"], "market_equity") == 200
    assert D(shared["basic_shares"]["value"]) == 20000000


def test_built_wacc_revision_dependencies_cover_downstream_outputs():
    p = pack()
    p["scenarios"]["base"]["discount"]["inputs"] = {
        "risk_free_continuous": ".03",
        "beta": "1.2",
        "equity_risk_premium": ".05",
        "market_close": "20",
        "market_close_date": p["valuation_date"],
        "market_close_currency": "SEK",
        "debt": "25",
        "debt_date": p["valuation_date"],
        "debt_currency": "EUR",
        "debt_cost": ".06",
        "debt_cost_date": p["valuation_date"],
        "debt_cost_basis": "nominal_annual_effective_pre_tax",
        "shield_rate": ".2",
        "shield_rate_date": p["valuation_date"],
    }
    scenarios = copy.deepcopy(p["scenarios"])
    scenarios["base"]["ownership"]["basic_shares"] = "20000000"
    child = revise(
        p,
        version="synthetic-built-r2",
        author="Synthetic override",
        prepared_on="2024-03-17",
        reason="Synthetic ownership alternative",
        changes={"scenarios": scenarios},
    )
    assert child["revision"]["affected_outputs"] == [
        "common_equity",
        "explicit_pv",
        "operating_value",
        "per_share_quote",
        "per_share_reporting",
        "conclusion",
    ]
    differences = child["revision"]["result_differences"]["scenarios"]["base"]
    assert {"wacc", "explicit_pv", "operating_value", "common_equity"} <= set(differences)


def test_wacc_and_ownership_bases_are_dated_and_shared():
    p = pack()
    p["scenarios"]["base"]["discount"]["valuation_date"] = "2024-02-28"
    assert base(p)["wacc"]["status"] == "invalid_input"
    p = pack()
    p["scenarios"]["base"]["ownership"]["valuation_date"] = "2024-02-28"
    r = base(p)
    assert r["wacc"]["available"] and r["basic_shares"]["status"] == "invalid_input"


def test_foreign_claims_and_rights_fx_are_scoped_and_dated():
    p = pack()
    s = p["scenarios"]["base"]
    claim = next(x for x in s["bridge"]["items"] if x["category"] == "legal_other")
    claim.update(
        method="foreign_pv",
        inputs={
            "amount": "-12",
            "currency": "USD",
            "reporting_currency": "EUR",
            "foreign_per_reporting": "1.2",
            "fx_date": p["valuation_date"],
        },
    )
    assert number(base(p)["common_equity"], "common_residual") == pytest.approx(D(145))
    claim["inputs"]["foreign_per_reporting"] = None
    r = base(p)
    assert r["operating_value"]["available"] and not r["common_equity"]["available"]
    claim["inputs"]["foreign_per_reporting"] = "1.2"
    p["allocations"].append({"id": "call", "destination": "rights"})
    s["rights"].update(
        currency="SEK",
        reporting_currency="EUR",
        fx_date=p["valuation_date"],
        quote_per_reporting="2",
    )
    s["rights"]["items"] = [
        block(
            id="synthetic-call",
            allocation_id="call",
            count="1000000",
            strike="20",
            volatility=".2",
            risk_free_continuous="0",
            dividend_yield="0",
            expiry="2025-02-28",
        )
    ]
    with_fx = base(p)
    s["fx"] = None
    no_quote = base(p)
    assert no_quote["common_equity"] == with_fx["common_equity"]
    assert (
        no_quote["per_share_reporting"]["available"]
        and not no_quote["per_share_quote"]["available"]
    )
    s["rights"]["quote_per_reporting"] = None
    assert not base(p)["common_equity"]["available"]


def test_fx_pairs_reject_same_currency_conversion_and_missing_pair_side():
    p = pack()
    p["quote_currency"] = "EUR"
    fx = p["scenarios"]["base"]["fx"]
    fx.update(quote_currency="EUR", quote_per_reporting="2")
    assert base(p)["per_share_quote"]["status"] == "invalid_input"
    fx["quote_per_reporting"] = "1"
    assert number(base(p)["per_share_quote"], "price_estimate") == D("15.5")

    p = pack()
    del p["scenarios"]["base"]["fx"]["reporting_currency"]
    assert base(p)["per_share_quote"]["status"] == "invalid_input"

    p = pack()
    p["scenarios"]["base"]["rights"].update(
        reporting_currency="EUR",
        fx_date=p["valuation_date"],
        quote_per_reporting="2",
    )
    assert base(p)["common_equity"]["status"] == "invalid_input"


def test_bridge_category_controls_method_and_sign():
    p = pack()
    debt = next(
        x for x in p["scenarios"]["base"]["bridge"]["items"] if x["category"] == "nonlease_debt"
    )
    debt["inputs"]["amount"] = "10"
    assert base(p)["common_equity"]["status"] == "invalid_input"
    debt.update(
        method="foreign_pv",
        inputs={
            "amount": "10",
            "currency": "USD",
            "reporting_currency": "EUR",
            "foreign_per_reporting": "1.2",
            "fx_date": p["valuation_date"],
        },
    )
    assert base(p)["common_equity"]["status"] == "invalid_input"

    p = pack()
    asset = next(
        x
        for x in p["scenarios"]["base"]["bridge"]["items"]
        if x["category"] == "nonoperating_assets"
    )
    asset.update(
        method="earnings_linked_pv",
        inputs={
            "recurring_baseline_ebit": "20",
            "current_pv_anchor": "1",
            "later_pv_anchor": "0",
        },
    )
    assert base(p)["common_equity"]["status"] == "invalid_input"


@pytest.mark.parametrize("value", ["0", "0.0000000000000000000001", "-1"])
def test_no_sign_tolerance_on_shares(value):
    p = pack()
    p["scenarios"]["base"]["ownership"]["basic_shares"] = value
    r = base(p)
    assert r["common_equity"]["available"] and not r["per_share_reporting"]["available"]


def test_selected_zero_fcff_and_negative_cash_are_not_missing():
    p = response()
    inputs = p["scenarios"]["base"]["operations"]["inputs"]
    inputs["cash_renewal_rate"] = ".2"
    r = base(p)
    assert all(D(x["value"]["fcff"]) == 0 for x in r["annual_fcff"]["value"])
    assert number(r["operating_value"], "ev") == 0
    inputs["initial_cash_diagnostic"] = "-10"
    inputs["cash_renewal_rate"] = ".21"
    r = base(p)
    assert number(r["operating_value"], "ev") == pytest.approx(D(-10))
    assert all(
        D(x["value"]["undistributed_cash_diagnostic"]) < 0 for x in r["annual_fcff"]["value"]
    )


def test_publication_upper_bound_and_unknown_source_scope():
    p = pack()
    source = p["sources"][0]
    source["published_on"] = None
    assert not base(p)["annual_fcff"]["available"]
    source.update(
        public_by="2024-03-10",
        publication_limitation="Exact first posting unknown; public by retained capture",
    )
    assert base(p)["annual_fcff"]["available"]
    source["observed_on"] = None
    assert base(p)["annual_fcff"]["available"]
    source["public_by"] = "2024-03-17"
    assert base(p)["annual_fcff"]["status"] == "invalid_input"

    p = pack()
    source = p["sources"][0]
    source.update(published_on="", public_by="2024-03-10")
    refused = base(load(json.dumps(p)))["annual_fcff"]
    assert refused["status"] == "invalid_input"
    assert refused["reasons"] == [
        {"code": "malformed_stage_input", "path": "nonempty_text_required: synthetic"}
    ]
    source["publication_limitation"] = "First publication omitted; public bound retained"
    assert base(load(json.dumps(p)))["annual_fcff"]["available"]


def test_override_cli_retains_both_versions_and_actual_differences(tmp_path, capsys):
    p = pack()
    (tmp_path / "pack.json").write_text(json.dumps(p))
    scenarios = copy.deepcopy(p["scenarios"])
    scenarios["base"]["fx"]["quote_per_reporting"] = "3"
    change = {
        "version": "synthetic-r2",
        "author": "Synthetic override",
        "prepared_on": "2024-03-17",
        "reason": "Alternative FX",
        "changes": {"scenarios": scenarios},
    }
    (tmp_path / "override.json").write_text(json.dumps(change))
    args = build_parser().parse_args(
        [
            "estimated-forward",
            "--input-json",
            str(tmp_path / "pack.json"),
            "--override-json",
            str(tmp_path / "override.json"),
            "--exports-dir",
            str(tmp_path),
        ]
    )
    assert args.func(args) == 0
    directory = Path(json.loads(capsys.readouterr().out)["export_directory"])
    exported = json.loads((directory / "outputs.json").read_text())
    assert exported["parent_assumption_pack"] == p
    revision = exported["assumption_pack"]["revision"]
    assert revision["affected_outputs"] == [
        "common_equity",
        "explicit_pv",
        "operating_value",
        "per_share_quote",
        "per_share_reporting",
        "conclusion",
    ]
    assert set(revision["result_differences"]["scenarios"]["base"]) == {"per_share_quote"}
    detached = json.loads((directory / "base-per_share_quote.json").read_text())
    assert detached["assumption_pack"]["revision"] == revision
    csv_context = json.loads(next(csv.DictReader((directory / "annual.csv").open()))["context"])
    assert csv_context["assumption_pack"]["revision"] == revision
    assert canonical(revision) in (directory / "report.md").read_text()
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    assert args.func(args) == 0
    capsys.readouterr()
    assert before == {path.name: path.read_bytes() for path in directory.iterdir()}
    assert json.loads((tmp_path / "pack.json").read_text()) == p


def test_override_cli_records_null_membership_with_ordinary_edit(tmp_path, capsys):
    p = pack()
    p["scenarios"]["base"]["rights"]["fx_date"] = None
    (tmp_path / "null-pack.json").write_text(json.dumps(p))
    scenarios = copy.deepcopy(p["scenarios"])
    del scenarios["base"]["rights"]["fx_date"]
    scenarios["base"]["fx"]["quote_per_reporting"] = "3"
    change = {
        "version": "synthetic-null-r2",
        "author": "Synthetic override",
        "prepared_on": "2024-03-17",
        "reason": "Remove null and revise FX",
        "changes": {"scenarios": scenarios},
    }
    (tmp_path / "null-override.json").write_text(json.dumps(change))
    args = build_parser().parse_args(
        [
            "estimated-forward",
            "--input-json",
            str(tmp_path / "null-pack.json"),
            "--override-json",
            str(tmp_path / "null-override.json"),
            "--exports-dir",
            str(tmp_path),
        ]
    )
    assert args.func(args) == 0
    directory = Path(json.loads(capsys.readouterr().out)["export_directory"])
    revision = json.loads((directory / "outputs.json").read_text())["assumption_pack"]["revision"]
    membership = next(
        item for item in revision["changed_inputs"] if item["path"].endswith("rights.fx_date")
    )
    assert membership["before_present"] and not membership["after_present"]
    assert membership["before"] is None and membership["after"] is None
    assert any(
        item["path"].endswith("fx.quote_per_reporting") for item in revision["changed_inputs"]
    )
    assert revision["result_differences"]["scenarios"]["base"]


def test_casefolded_scenario_export_names_are_refused(tmp_path, capsys):
    p = pack()
    p["scenarios"]["BASE"] = copy.deepcopy(p["scenarios"]["base"])
    input_path = tmp_path / "casefold-collision.json"
    input_path.write_text(json.dumps(p))
    args = build_parser().parse_args(
        ["estimated-forward", "--input-json", str(input_path), "--exports-dir", str(tmp_path)]
    )
    assert args.func(args) == 1
    refusal = json.loads(capsys.readouterr().err)
    assert refusal["status"] == "estimated_research_refused"
    assert "duplicate_scenario_export_id" in refusal["reason"]
    assert not (tmp_path / "estimated-forward").exists()


def test_malformed_mapping_shapes_have_structured_refusals(tmp_path, capsys):
    malformed_records = pack()
    malformed_records["records"] = []
    for index, payload in enumerate(([], malformed_records)):
        input_path = tmp_path / f"malformed-{index}.json"
        input_path.write_text(json.dumps(payload))
        args = build_parser().parse_args(
            ["estimated-forward", "--input-json", str(input_path), "--exports-dir", str(tmp_path)]
        )
        assert args.func(args) == 1
        refusal = json.loads(capsys.readouterr().err)
        assert refusal["status"] == "estimated_research_refused"

    malformed_scenario = pack()
    malformed_scenario["scenarios"]["base"] = []
    scenario = evaluate(malformed_scenario)["scenarios"]["base"]
    assert set(scenario) == SCENARIO_STAGE_NAMES
    expected = [{"code": "invalid_scenario", "path": "base"}]
    for name in SCENARIO_STAGE_NAMES - {"reverse"}:
        assert scenario[name]["status"] == "invalid_input"
        assert scenario[name]["reasons"] == expected
    assert scenario["reverse"]["status"] == "not_requested"
    assert scenario["reverse"]["reasons"] == [
        {"code": "reverse_not_implemented_stage4", "path": "reverse"}
    ]

    malformed_projection = response()
    malformed_projection["scenarios"]["base"]["operations"]["inputs"] = []
    assert base(malformed_projection)["annual_fcff"]["status"] == "invalid_input"


def test_scenario_fallback_schema_preserves_healthy_siblings_and_reverse():
    p = pack()
    p["scenarios"]["broken"] = []
    result = evaluate(p)["scenarios"]
    assert set(result["base"]) == SCENARIO_STAGE_NAMES
    assert result["base"]["common_equity"]["available"]
    assert set(result["broken"]) == SCENARIO_STAGE_NAMES
    assert result["broken"]["common_equity"]["reasons"] == [
        {"code": "invalid_scenario", "path": "broken"}
    ]
    assert result["broken"]["reverse"]["status"] == "not_requested"

    p = pack()
    p["allocations"] = [{}]
    fallback = base(p)
    assert set(fallback) == SCENARIO_STAGE_NAMES
    for name in SCENARIO_STAGE_NAMES - {"reverse"}:
        assert fallback[name]["status"] == "invalid_input"
        assert fallback[name]["reasons"] == [{"code": "malformed_scenario_input", "path": "'id'"}]
    assert fallback["reverse"]["status"] == "not_requested"


def test_unavailable_export_keeps_full_context_and_propagated_reasons(tmp_path, capsys):
    p = pack()
    p["scenarios"]["base"]["discount"] = None
    (tmp_path / "pack.json").write_text(json.dumps(p))
    args = build_parser().parse_args(
        [
            "estimated-forward",
            "--input-json",
            str(tmp_path / "pack.json"),
            "--exports-dir",
            str(tmp_path),
        ]
    )
    assert args.func(args) == 0
    directory = Path(json.loads(capsys.readouterr().out)["export_directory"])
    output = json.loads((directory / "base-operating_value.json").read_text())
    assert output["value"] is None and output["reasons"]
    assert output["identity"]["label"] == LABEL and output["assumption_pack"] == p


def test_precise_cutoff_does_not_treat_later_same_day_as_eligible():
    p = pack()
    source = p["sources"][0]
    source["published_on"] = "2024-03-15"
    assert not base(p)["annual_fcff"]["available"]
    source["published_at"] = "2024-03-15T13:00:00Z"
    assert base(p)["annual_fcff"]["status"] == "invalid_input"
    source["published_at"] = "2024-03-15T11:00:00Z"
    assert base(p)["annual_fcff"]["available"]


def test_malformed_optional_bridge_does_not_poison_operations():
    p = pack()
    p["scenarios"]["base"]["bridge"]["items"] = "not-an-array"
    r = base(p)
    assert set(r) == SCENARIO_STAGE_NAMES
    assert r["annual_fcff"]["available"] and r["operating_value"]["available"]
    assert r["bridge"]["status"] == "invalid_input"
    assert r["common_equity"]["status"] == "invalid_input"
    assert r["reverse"]["status"] == "not_requested"


def test_runtime_helper_identity_is_owned_by_exports_and_acceptance(tmp_path):
    repository = Path(__file__).parents[1]
    export_sources = _code_sources(repository)
    helpers = (
        "alphaforge/core/valuation/forward_fcff.py",
        "alphaforge/core/valuation/forward_fcff_contract.py",
    )
    for helper in helpers:
        expected = hashlib.sha256((repository / helper).read_bytes()).hexdigest()
        assert export_sources[helper] == expected
        assert helper in ACCEPTANCE_SOURCE_PATHS

    expected_acceptance = hashlib.sha256(
        b"".join(
            path.encode() + b"\0" + (repository / path).read_bytes()
            for path in ACCEPTANCE_SOURCE_PATHS
        )
    ).hexdigest()
    assert _code_identity(repository) == expected_acceptance

    for path in set(EXPORT_SOURCE_PATHS) | set(ACCEPTANCE_SOURCE_PATHS):
        destination = tmp_path / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((repository / path).read_bytes())
    for helper in helpers:
        export_before = _code_sources(tmp_path)
        acceptance_before = _code_identity(tmp_path)
        (tmp_path / helper).write_bytes(
            (tmp_path / helper).read_bytes() + b"\n# synthetic identity change\n"
        )
        assert _code_sources(tmp_path)[helper] != export_before[helper]
        assert _code_identity(tmp_path) != acceptance_before


def test_reference_comparison_preserves_component_tolerances_and_exact_metadata():
    expected = {
        "identity": {"valuation_date": "2024-02-29", "currency": "EUR"},
        "scenarios": {
            "base": {
                "annual_fcff": {
                    "value": [
                        {
                            "value": {
                                "fcff": "14.000000000",
                                "record_ids": ["1"],
                                "extension_code": "1",
                            }
                        }
                    ],
                },
                "bridge": {"value": [{"id": "1", "allocation_id": "1", "amount": "5.00000000"}]},
                "operating_value": {"value": {"ev": "140.00000000"}},
                "per_share_reporting": {"value": {"price_estimate": "15.50000000"}},
                "basic_shares": {"value": "10000000"},
            }
        },
    }
    actual = copy.deepcopy(expected)
    actual["scenarios"]["base"]["annual_fcff"]["value"][0]["value"]["fcff"] = "14.000000019"
    actual["scenarios"]["base"]["bridge"]["value"][0]["amount"] = "5.00000009"
    actual["scenarios"]["base"]["operating_value"]["value"]["ev"] = "140.00000009"
    actual["scenarios"]["base"]["per_share_reporting"]["value"]["price_estimate"] = "15.500000009"
    compare_outputs(actual, expected)

    outside = copy.deepcopy(expected)
    outside["scenarios"]["base"]["annual_fcff"]["value"][0]["value"]["fcff"] = "14.000000021"
    with pytest.raises(AcceptanceError, match="annual comparison mismatch"):
        compare_outputs(outside, expected)

    for mutate in (
        lambda value: value["scenarios"]["base"]["annual_fcff"]["value"][0]["value"].update(
            record_ids=["1.000000001"]
        ),
        lambda value: value["scenarios"]["base"]["annual_fcff"]["value"][0]["value"].update(
            extension_code="1.000000001"
        ),
        lambda value: value["scenarios"]["base"]["bridge"]["value"][0].update(
            allocation_id="1.000000001"
        ),
    ):
        semantic = copy.deepcopy(expected)
        mutate(semantic)
        with pytest.raises(AcceptanceError, match="exact comparison mismatch"):
            compare_outputs(semantic, expected)

    actual["identity"]["valuation_date"] = "2024-03-01"
    with pytest.raises(AcceptanceError, match="exact comparison mismatch"):
        compare_outputs(actual, expected)
    actual = copy.deepcopy(expected)
    actual["scenarios"]["base"]["basic_shares"]["value"] = "10000001"
    with pytest.raises(AcceptanceError, match="exact comparison mismatch"):
        compare_outputs(actual, expected)
