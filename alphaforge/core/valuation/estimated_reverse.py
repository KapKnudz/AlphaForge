"""Bounded estimated reverse analysis, always repriced by the qualified forward owner.

A scan is deliberately not a continuity, uniqueness or root-completeness proof.
Only the narrow algebraically growth-independent response can certify absence.
"""

from __future__ import annotations

import copy
from decimal import Decimal as D
from typing import NamedTuple

from alphaforge.core.valuation.estimated_inputs import Evidence, decimal, encode, require
from alphaforge.core.valuation.forward_fcff_contract import digest, iso_date, text


class ReversePolicy(NamedTuple):
    version: str
    growth_path: str
    scan_points: int
    max_bisections: int
    growth_absolute: D
    refinement_residual: D
    quote_absolute: D
    quote_relative: D


POLICY = ReversePolicy(
    version="estimated-reverse-v1",
    growth_path="linear-fade10",
    scan_points=201,
    max_bisections=200,
    growth_absolute=D("1e-10"),
    refinement_residual=D("1e-12"),
    quote_absolute=D("1e-6"),
    quote_relative=D("1e-8"),
)
SEARCH_POLICY = (
    f"{POLICY.version}; {POLICY.growth_path}; "
    f"scan{POLICY.scan_points}; bisection{POLICY.max_bisections}"
)


def _scientific(value):
    return f"{value:.0E}".lower()


NUMERICAL_POLICY = (
    f"growth A={_scientific(POLICY.growth_absolute)}; "
    f"repricing A={_scientific(POLICY.quote_absolute)},"
    f"R={_scientific(POLICY.quote_relative)}"
)


def price_matches(price, target):
    return abs(price - target) <= max(
        POLICY.quote_absolute,
        POLICY.quote_relative * max(abs(price), abs(target)),
    )


def _constant_value_response(scenario):
    # In this existing response mechanism all dependence on revenue enters value
    # through these coefficients. Opening states/settlements, fixed bridge/rate,
    # shares/FX and endogenous rights then have no x dependence. This is an exact
    # structural special case, NOT an inference from equal sampled prices.
    operations = scenario.get("operations")
    if not isinstance(operations, dict) or not isinstance(operations.get("inputs"), dict):
        return False
    p = operations["inputs"]
    coefficients = (
        "margin_first",
        "margin_terminal",
        "da_first",
        "da_terminal",
        "cash_renewal_rate",
        "cash_growth_capital",
        "lease_renewal_rate",
        "lease_growth_capital",
        "wc_ratio",
    )
    return all(key in p for key in coefficients) and all(
        decimal(p.get(key), key) == 0 for key in coefficients
    )


def search(evaluate_point, lower, upper, target, *, constant_value=False):
    """Bounded numerical policy. Point outcomes retain every failure and signed price.

    evaluate_point returns an ordinary stage outcome with price, tradable_price
    and qualified forward result. It must not floor signed diagnostic quotients.
    """
    samples, failures, evaluations = [], [], []

    def point(x):
        out = evaluate_point(x)
        item = {"x": x, **out}
        if out["available"]:
            price = decimal(out["value"]["price"])
            item = {
                "x": x,
                "status": out["status"],
                "available": True,
                "price": price,
                "residual": price - target,
                "tradable_price": out["value"]["tradable_price"],
                "reasons": [],
            }
        else:
            failures.append(item)
        evaluations.append(item)
        return item, out

    def failure_outcome():
        kinds = {failure["status"] for failure in failures}
        if "invalid_input" in kinds:
            return "invalid_input"
        if "numerical_failure" in kinds:
            return "numerical_failure"
        return "unsupported_domain"

    def summary(status, roots, brackets, regions, *, complete=False, basis=None):
        result = {
            "outcome": status,
            "roots": roots,
            "scan": samples,
            "candidate_brackets": brackets,
            "match_regions": regions,
            "failed_evaluations": failures,
            "evaluation_count": len(evaluations),
            "coverage": {
                "scan_points": len(samples),
                "declared_interval": [lower, upper],
                "full_interval_economics": "supplied_evidence_not_proved_by_scan",
                "certified_unique": False,
                "exhaustive_roots": complete,
                "absence_demonstrated": status == "no_solution",
                "basis": basis or "finite_scan_and_local_refinement_only",
                "limitations": [
                    "Tangencies and inter-sample roots may remain undiscovered.",
                    "No continuity or monotonicity is inferred from sampling.",
                ]
                if not complete
                else [],
            },
        }
        return result

    # Keep every policy point, including unsupported evaluations. Never search a
    # silently narrowed subdomain or bridge any discovered qualification hole.
    for i in range(POLICY.scan_points):
        item, _ = point(lower + (upper - lower) * D(i) / (POLICY.scan_points - 1))
        samples.append(item)
    brackets = [
        {"left": a["x"], "right": b["x"]}
        for a, b in zip(samples, samples[1:], strict=False)
        if a["available"] and b["available"] and a["residual"] * b["residual"] < 0
    ]
    regions = []
    start = None
    for i in range(POLICY.scan_points + 1):
        match = (
            i < POLICY.scan_points
            and samples[i]["available"]
            and samples[i]["tradable_price"]
            and price_matches(samples[i]["price"], target)
        )
        if match and start is None:
            start = i
        if not match and start is not None:
            regions.append(
                {
                    "left": samples[start]["x"],
                    "right": samples[i - 1]["x"],
                    "sample_count": i - start,
                    "basis": "sampled_matches_not_interval_proof",
                }
            )
            start = None
    if failures:
        return summary(failure_outcome(), [], brackets, regions)

    if constant_value:
        status = (
            "not_identifiable"
            if price_matches(samples[0]["price"], target) and samples[0]["tradable_price"]
            else "no_solution"
        )
        regions = (
            [
                {
                    "left": lower,
                    "right": upper,
                    "basis": "algebraically_growth_independent_response_at_quote_tolerance",
                }
            ]
            if status == "not_identifiable"
            else []
        )
        return summary(
            status,
            [],
            brackets,
            regions,
            complete=True,
            basis="algebraically_growth_independent_response",
        )

    roots = []

    def recover(x, left, right, iterations):
        # Re-evaluate through the SAME forward path, independently of the cached
        # numerical residual, and demand both unchanged Appendix C conditions.
        verified, forward = point(x)
        if not verified["available"]:
            return False
        if (
            right - left > POLICY.growth_absolute
            or not price_matches(verified["price"], target)
            or not verified["tradable_price"]
        ):
            return False
        if any(abs(x - root["initial_growth"]) <= POLICY.growth_absolute for root in roots):
            return True
        rows = forward["value"]["forward"]["annual_fcff"]["value"]
        revenue0 = decimal(forward["value"]["baseline_revenue"])
        roots.append(
            {
                "initial_growth": x,
                "bracket_left": left,
                "bracket_right": right,
                "growth_localization": right - left,
                "iterations": iterations,
                "repriced_quote": verified["price"],
                "quote_residual": verified["residual"],
                "horizon_cagr": (decimal(rows[9]["value"]["revenue"]) / revenue0) ** (D(1) / 10)
                - 1,
                "growth_path": [row["value"]["growth"] for row in rows[:10]],
                "revenue_path": [row["value"]["revenue"] for row in rows[:10]],
                "fcff_path": [row["value"]["fcff"] for row in rows[:10]],
                "forward": forward["value"]["forward"],
                "status": "supported_candidate_not_certified_unique",
            }
        )
        return True

    # An exact evaluated match has zero localization width. Merely near-matching
    # scan points are retained as diagnostics, not 201 falsely precise roots.
    for sample in samples:
        in_region = any(
            r["sample_count"] > 1 and r["left"] <= sample["x"] <= r["right"] for r in regions
        )
        if sample["residual"] == 0 and not in_region:
            recover(sample["x"], sample["x"], sample["x"], 0)
            if failures:
                return summary(failure_outcome(), roots, brackets, regions)
    exhausted = False
    for bracket in brackets:
        left, right = bracket["left"], bracket["right"]
        left_residual = next(s["residual"] for s in samples if s["x"] == left)
        found = False
        for iteration in range(1, POLICY.max_bisections + 1):
            x = (left + right) / 2
            candidate, _ = point(x)
            if not candidate["available"]:
                break
            residual = candidate["residual"]
            if residual == 0:
                left = right = x
            # Work more accurately than the acceptance threshold so recovered
            # constituent paths remain useful; Appendix C acceptance is unchanged.
            if right - left <= POLICY.growth_absolute and (
                abs(residual) <= POLICY.refinement_residual or iteration == POLICY.max_bisections
            ):
                found = recover(x, left, right, iteration)
                if found:
                    break
            if residual * left_residual > 0:
                left, left_residual = x, residual
            else:
                right = x
        bracket.update(
            {
                "iterations": iteration,
                "localized_left": left,
                "localized_right": right,
                "recovered": found,
            }
        )
        if failures:
            break
        exhausted |= not found
    if failures:
        status = failure_outcome()
    elif exhausted:
        status = "numerical_failure"
    elif len(roots) > 1:
        status = "multiple_solutions"
    elif any(region["sample_count"] > 1 for region in regions):
        status = "match_region"
    elif roots:
        status = "candidate_solution"
    elif regions:
        status = "sampled_match"
    else:
        status = "no_crossing_observed"
    roots.sort(key=lambda root: root["initial_growth"])
    return summary(status, roots, brackets, regions)


def solve(pack, scenario, valuation, forward_evaluator):
    """Consume a declared, source-qualified reverse question; no financial approval."""
    from alphaforge.core.valuation.estimated_fcff import (
        _dependent,
        _stage,
        qualify_reverse_inputs,
    )

    evidence = Evidence(pack)
    evidence.qualify("reverse")
    block = evidence.block(scenario.get("reverse"), "reverse")
    require(
        block["parameter"] == "initial_nominal_revenue_growth",
        "unsupported_reverse_parameter",
        status="unsupported",
    )
    require(
        block.get("domain") is not None, "reverse_domain_required", status="insufficient_evidence"
    )
    domain = evidence.block(block.get("domain"), "reverse.domain")
    lower, upper = decimal(domain.get("lower")), decimal(domain.get("upper"))
    require(-1 < lower < upper, "invalid_reverse_bounds")
    require(
        domain["selected_independently_of_target"] is True,
        "target_independent_bounds_required",
        status="insufficient_evidence",
    )
    for key in (
        "selection_basis",
        "full_interval_support",
        "fixed_claim_independence",
        "funding_and_distress_support",
        "thresholds_and_continuity",
    ):
        require(
            domain.get(key) is not None,
            "reverse_support_required",
            "reverse.domain." + key,
            "insufficient_evidence",
        )
        text(domain[key], "reverse.domain." + key)
    require(
        block.get("target") is not None, "reverse_target_required", status="insufficient_evidence"
    )
    target_block = evidence.block(block.get("target"), "reverse.target")
    require(
        target_block["valuation_date"] == valuation.isoformat(), "target_valuation_date_mismatch"
    )
    close_date = iso_date(target_block["close_date"])
    require(close_date <= valuation, "target_close_after_valuation")
    require(
        type(target_block["lag_days"]) is int
        and target_block["lag_days"] == (valuation - close_date).days,
        "target_lag_mismatch",
    )
    require(target_block["currency"] == pack["quote_currency"], "target_currency_mismatch")
    require(
        target_block["share_basis"] == "basic_common",
        "unsupported_target_share_basis",
        status="unsupported",
    )
    for key in (
        "exchange",
        "close_selection_basis",
        "corporate_action_basis",
        "rights_comparability",
    ):
        text(target_block.get(key), "reverse.target." + key)
    target = decimal(target_block.get("price"), "reverse.target.price")
    require(target > 0, "nonpositive_reverse_target")
    ops = evidence.block(scenario.get("operations"), "operations")
    require(
        ops.get("mode") == "aggregate_response",
        "reverse_response_required",
        status="unsupported",
    )
    inputs = ops.get("inputs")
    require(isinstance(inputs, dict), "invalid_projection_inputs")
    qualify_reverse_inputs(pack, scenario, valuation)
    fixed = copy.deepcopy(scenario)
    fixed.pop("reverse", None)
    fixed["operations"]["inputs"].pop("initial_growth", None)

    def evaluate_point(x):
        candidate = copy.deepcopy(scenario)
        candidate.pop("reverse", None)
        candidate["operations"]["inputs"]["initial_growth"] = str(x)

        def run():
            forward = forward_evaluator(pack, candidate, valuation)
            quote = _dependent(forward["per_share_quote"])
            forward.pop("reverse", None)
            return {
                "price": quote["signed_quotient"],
                "tradable_price": quote["tradable_price"],
                "forward": forward,
                "baseline_revenue": ops["inputs"]["revenue"],
            }

        return _stage(evidence, "reverse", run)

    result = search(
        evaluate_point, lower, upper, target, constant_value=_constant_value_response(scenario)
    )
    return encode(
        {
            **result,
            "parameter": block["parameter"],
            "search_policy": SEARCH_POLICY,
            "domain": domain,
            "target": target_block,
            "fixed_assumptions_identity": digest(fixed),
            "solved_field": "operations.inputs.initial_growth",
            "financial_status": "not_assessed_by_calculator",
            "human_status": "not_implementation_accepted",
            "tolerances": {
                "growth_absolute": POLICY.growth_absolute,
                "quote_absolute": POLICY.quote_absolute,
                "quote_relative": POLICY.quote_relative,
            },
        }
    )
