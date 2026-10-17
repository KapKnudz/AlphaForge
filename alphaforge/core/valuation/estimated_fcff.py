"""Conditional aggregate FCFF/WACC/common allocation and shared reverse evaluation."""

from __future__ import annotations

import copy
import re
from datetime import timedelta
from decimal import Decimal as D
from decimal import localcontext

from alphaforge.core.valuation.estimated_inputs import (
    Evidence,
    Unavailable,
    decimal,
    encode,
    identity_envelope,
    require,
    validate_envelope,
)
from alphaforge.core.valuation.estimated_projection import (
    project,
    qualify_fixed_schedules,
    qualify_response,
)
from alphaforge.core.valuation.forward_fcff import _anniversary
from alphaforge.core.valuation.forward_fcff_contract import ContractError, iso_date, text

BRIDGE_CATEGORIES = {
    "accessible_cash",
    "nonoperating_assets",
    "nonlease_debt",
    "lease_debt",
    "nonoperating_tax",
    "consideration",
    "pension_employee",
    "legal_other",
    "noncommon",
}
ADDITIVE_BRIDGE_CATEGORIES = {"accessible_cash", "nonoperating_assets"}
CASH_LEGS = (
    "ebit",
    "noncash_addbacks",
    "other_noncash",
    "operating_cash_tax",
    "cash_investment",
    "lease_additions",
    "disposal_cash",
    "wc_open",
    "wc_close",
    "other_operating_cash",
)
SCENARIO_STAGES = (
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
)


def close(a, b, path, annual=True, *, code="reconciliation_failed", status="invalid_input"):
    a, b = decimal(a, path), decimal(b, path)
    absolute, relative = (D("2e-8"), D("2e-12")) if annual else (D("1e-7"), D("2e-11"))
    require(
        abs(a - b) <= max(absolute, relative * max(abs(a), abs(b))),
        code,
        path,
        status,
    )


def outcome(value=None, error=None):
    return {
        "status": error.status if error else "conditional_computed",
        "calculation_status": "withheld" if error else "computed_not_independent_acceptance",
        "available": error is None,
        "value": encode(value) if error is None else None,
        "reasons": [{"code": error.code, "path": error.path}] if error else [],
    }


def reverse_outcome():
    return outcome(error=Unavailable("reverse_not_requested", "reverse", "not_requested"))


def scenario_refusal(error, reverse_requested=False):
    result = {stage: outcome(error=error) for stage in SCENARIO_STAGES[:-1]}
    result["reverse"] = outcome(error=error) if reverse_requested else reverse_outcome()
    return result


def first_row_error(rows):
    for row in rows:
        if not row["available"]:
            reason = row["reasons"][0]
            return Unavailable(reason["code"], reason["path"], row["status"])
    return None


def require_allocations(supplied, allocations, destination, path):
    require(isinstance(supplied, list), "invalid_allocation_consumption", path)
    for allocation in supplied:
        text(allocation, path)
    require(
        len(supplied) == len(set(supplied)),
        "duplicate_allocation_consumption",
        path,
    )
    expected = {key for key, owner in allocations.items() if owner == destination}
    require(set(supplied) == expected, "incomplete_allocation_consumption", path)


def allocation_map(pack):
    raw = pack["allocations"]
    require(len({item["id"] for item in raw}) == len(raw), "duplicate_allocation")
    allocations = {item["id"]: item["destination"] for item in raw}
    require(
        set(allocations.values()) <= {"operations", "bridge", "rights", "excluded_pair"},
        "unsupported_allocation_destination",
    )
    return allocations


def exchange_rate(block, valuation, reporting_currency, currency_key, rate_key, date_key, path):
    require(block[date_key] == valuation.isoformat(), "fx_date_mismatch", path)
    require(
        block["reporting_currency"] == reporting_currency,
        "fx_reporting_currency_mismatch",
        path,
    )
    foreign_currency = block[currency_key]
    require(
        isinstance(foreign_currency, str)
        and re.fullmatch(r"[A-Z]{3}", foreign_currency) is not None,
        "invalid_fx_currency",
        path,
    )
    q = decimal(block.get(rate_key), path)
    require(q > 0, "nonpositive_fx", path)
    require(
        foreign_currency != reporting_currency or q == 1,
        "nonunity_same_currency_fx",
        path,
    )
    return q


def quote_exchange_rate(scenario, evidence, valuation):
    fx = evidence.block(scenario.get("fx"), "quote_fx")
    require(fx["quote_currency"] == evidence.pack["quote_currency"], "fx_quote_currency_mismatch")
    return exchange_rate(
        fx,
        valuation,
        evidence.pack["currency"],
        "quote_currency",
        "quote_per_reporting",
        "valuation_date",
        "quote_fx",
    )


def basic_shares(block, evidence, valuation):
    block = evidence.block(block, "ownership")
    require(block["valuation_date"] == valuation.isoformat(), "ownership_date_mismatch")
    require(block["share_units"] == "absolute", "ownership_units_mismatch")
    value = decimal(block.get("basic_shares"), "basic_shares")
    require(value > 0, "nonpositive_basic_shares")
    require(value == value.to_integral_value(), "basic_shares_must_be_absolute_count")
    return value


def rate(block, evidence, scenario, valuation):
    require(block["valuation_date"] == valuation.isoformat(), "discount_date_mismatch")
    require(block["currency"] == evidence.pack["currency"], "discount_currency_mismatch")
    require(
        block["rate_basis"] == "nominal_annual_effective",
        "unsupported_discount_rate_basis",
        status="unsupported",
    )
    p = block["inputs"]
    if "wacc" in p:
        require(set(p) == {"wacc"}, "invalid_supplied_wacc_fields")
        r = decimal(p["wacc"], "wacc")
        require(r > 0, "nonpositive_wacc")
        return {"wacc": r, "basis": "supplied_nominal_annual_effective_WACC"}

    require(
        set(p)
        == {
            "risk_free_continuous",
            "beta",
            "equity_risk_premium",
            "market_close",
            "market_close_date",
            "market_close_currency",
            "debt",
            "debt_date",
            "debt_currency",
            "debt_cost",
            "debt_cost_date",
            "debt_cost_basis",
            "shield_rate",
            "shield_rate_date",
        },
        "invalid_wacc_build_fields",
    )

    def n(k):
        return decimal(p.get(k), "wacc." + k)

    valuation_date = valuation.isoformat()
    require(
        p["market_close_date"] == valuation_date
        and p["market_close_currency"] == evidence.pack["quote_currency"],
        "invalid_market_close_basis",
    )
    require(
        p["debt_date"] == valuation_date and p["debt_currency"] == evidence.pack["currency"],
        "invalid_debt_basis",
    )
    require(
        p["debt_cost_date"] == valuation_date
        and p["debt_cost_basis"] == "nominal_annual_effective_pre_tax",
        "invalid_debt_cost_basis",
    )
    require(p["shield_rate_date"] == valuation_date, "invalid_shield_rate_basis")
    shares = basic_shares(scenario.get("ownership"), evidence, valuation)
    fx = evidence.block(scenario.get("fx"), "quote_fx")
    require(fx["quote_currency"] == evidence.pack["quote_currency"], "fx_quote_currency_mismatch")
    q = exchange_rate(
        fx,
        valuation,
        evidence.pack["currency"],
        "quote_currency",
        "quote_per_reporting",
        "valuation_date",
        "wacc.fx",
    )
    require(n("market_close") > 0, "invalid_market_weight_basis")
    require(
        n("debt") >= 0 and n("debt_cost") >= 0 and 0 <= n("shield_rate") <= 1,
        "invalid_debt_basis",
    )
    rf = n("risk_free_continuous").exp() - 1
    equity = n("market_close") * shares / q / 1_000_000
    debt_weight = n("debt") / (n("debt") + equity)
    ke = rf + n("beta") * n("equity_risk_premium")
    r = (1 - debt_weight) * ke + debt_weight * n("debt_cost") * (1 - n("shield_rate"))
    require(r > 0, "nonpositive_wacc")
    return {
        "risk_free_effective": rf,
        "equity_cost": ke,
        "market_equity": equity,
        "debt_weight": debt_weight,
        "equity_weight": 1 - debt_weight,
        "wacc": r,
    }


def qualify_operations(block, evidence, allocations, *, include_initial_growth=True):
    evidence.block(block, "operations")
    require_allocations(block.get("allocation_ids"), allocations, "operations", "operations")
    require(
        block["basis"] == "recurring_ebit_after_da_no_compensation_addback",
        "unsupported_earnings_basis",
        status="unsupported",
    )
    require(
        block["tax_basis"] == "unlevered_including_opening_operating_once",
        "unsupported_tax_basis",
        status="unsupported",
    )
    for key in ("continuing_capacity", "funding_access", "noncash_compatibility"):
        text(block.get(key), key)
    require(
        block["mode"] in {"annual", "aggregate_response"},
        "unsupported_projection",
        status="unsupported",
    )
    if block["mode"] == "aggregate_response" and not include_initial_growth:
        qualify_response(block, include_initial_growth=False)
        for record_id in block["record_ids"]:
            require(
                evidence.pack["records"][record_id]["kind"] != "fact",
                "fact_is_not_forecast",
                record_id,
            )


def annual(block, evidence, valuation, allocations):
    qualify_operations(block, evidence, allocations)
    rows = (
        project(block)
        if block["mode"] == "aggregate_response"
        else copy.deepcopy(block["rows"])
    )
    require(isinstance(rows, list), "invalid_annual_rows")
    require(10 <= len(rows) <= 12, "ten_full_years_required")
    result = []
    for i, row in enumerate(rows, 1):
        try:
            evidence.block(row, f"annual.{i}")
            for rid in row["record_ids"]:
                require(
                    evidence.pack["records"][rid]["kind"] != "fact", "fact_is_not_forecast", rid
                )
            start = (
                valuation + timedelta(days=1)
                if i == 1
                else _anniversary(valuation, i - 1) + timedelta(days=1)
            )
            end = _anniversary(valuation, i)
            has_start, has_end = "start" in row, "end" in row
            require(has_start == has_end, "annual_period_pair_required", str(i))
            if has_start:
                require(
                    row["start"] == start.isoformat() and row["end"] == end.isoformat(),
                    "annual_period_mismatch",
                    str(i),
                )
            nums = {key: decimal(row.get(key), f"annual.{i}.{key}") for key in CASH_LEGS}
            for key in ("noncash_addbacks", "cash_investment", "lease_additions", "disposal_cash"):
                require(nums[key] >= 0, "negative_gross_leg", f"annual.{i}.{key}")
            if i > 1 and result[-1]["available"]:
                close(nums["wc_open"], result[-1]["value"]["wc_close"], f"annual.{i}.wc_continuity")
            dwc = nums["wc_close"] - nums["wc_open"]
            fcff = (
                nums["ebit"]
                + nums["noncash_addbacks"]
                + nums["other_noncash"]
                - nums["operating_cash_tax"]
                - nums["cash_investment"]
                - nums["lease_additions"]
                + nums["disposal_cash"]
                - dwc
                - nums["other_operating_cash"]
            )
            result.append(
                outcome(
                    {
                        **row,
                        **nums,
                        "year": i,
                        "start": start.isoformat(),
                        "end": end.isoformat(),
                        "delta_wc": dwc,
                        "fcff": fcff,
                    }
                )
            )
        except Unavailable as error:
            refused = outcome(error=error)
            refused["supplied_components_not_qualified_fcff"] = row
            result.append(refused)
        except ContractError as error:
            refused = outcome(
                error=Unavailable("malformed_stage_input", str(error), "invalid_input")
            )
            refused["supplied_components_not_qualified_fcff"] = row
            result.append(refused)
    return result


def qualify_terminal(block, evidence, r, response_block=None):
    evidence.block(block, "terminal")
    text(block["sustainable_mechanism"])
    require(block["mode"] == "gordon", "unsupported_tail", status="unsupported")
    growth = decimal(block.get("growth"), "terminal.growth")
    require(-1 < growth < r, "invalid_terminal_growth")
    if response_block is not None:
        qualify_fixed_schedules(response_block)
    return growth


def terminal(block, rows, evidence, r, response_block=None, *, reverse_candidate=False):
    g = qualify_terminal(block, evidence, r, response_block)
    candidate_status = "unsupported_domain" if reverse_candidate else "invalid_input"

    def reconcile(a, b, path, *, code="reconciliation_failed"):
        close(a, b, path, code=code, status=candidate_status)

    require(len(rows) == 12, "complete_witnesses_required")
    witness_error = first_row_error(rows[10:12])
    if witness_error is not None:
        raise witness_error
    a, b = rows[10]["value"], rows[11]["value"]
    for row in (a, b):
        require(
            isinstance(row.get("finite_effects"), dict) and bool(row["finite_effects"]),
            "finite_effect_inventory_required",
        )
        require(
            all(decimal(x) == 0 for x in row["finite_effects"].values()),
            "finite_effects_not_exhausted",
            status=candidate_status,
        )
        require(
            decimal(row.get("legacy_da", 0)) == 0 and decimal(row.get("opening_tax", 0)) == 0,
            "finite_cash_in_terminal",
        )
        for leg in (
            "opening_payment",
            "opening_recovery",
            "opening_deferred_cash",
            "opening_payable_remaining",
            "opening_recoverable_remaining",
            "opening_deferred_remaining",
        ):
            if leg in row:
                require(
                    decimal(row[leg], "terminal." + leg) == 0,
                    "finite_cash_in_terminal",
                    "terminal." + leg,
                )
    for row in (a, b):
        if "growth" in row:
            require(
                decimal(row["growth"], "terminal.growth") == g,
                "terminal_growth_mismatch",
                "terminal.growth",
            )
    revenue_present = "revenue" in a, "revenue" in b
    require(
        revenue_present[0] == revenue_present[1],
        "terminal_revenue_pair_required",
        "terminal.revenue",
        "insufficient_evidence",
    )
    if all(revenue_present):
        reconcile(
            b["revenue"],
            decimal(a["revenue"], "terminal.revenue") * (1 + g),
            "terminal.revenue",
        )
    for leg in CASH_LEGS + ("fcff",):
        reconcile(b[leg], decimal(a[leg], "terminal." + leg) * (1 + g), "terminal." + leg)
    require(("margin" in a) == ("margin" in b), "incomplete_witness_constituent", "margin")
    if "margin" in a:
        reconcile(a["margin"], b["margin"], "terminal.margin", code="terminal_ratio_mismatch")
    for leg in (
        "ongoing_da",
        "refundable_profit",
        "ordinary_profit",
        "gross_charge",
        "ordinary_charge",
        "topup_charge",
        "new_charge",
        "new_entitlement",
        "new_payment",
        "new_recovery",
        "opening_payment",
        "opening_recovery",
        "opening_deferred_cash",
        "opening_tax",
        "new_cash_tax",
        "opening_payable_remaining",
        "opening_recoverable_remaining",
        "opening_deferred_remaining",
        "new_payable_remaining",
        "new_recoverable_remaining",
        "renewal_investment",
        "lead_investment",
        "lease_renewal",
        "lead_lease",
    ):
        require((leg in a) == (leg in b), "incomplete_witness_constituent", leg)
        if leg in a:
            reconcile(b[leg], decimal(a[leg], "terminal." + leg) * (1 + g), "terminal." + leg)
    decompositions = (
        ("noncash_addbacks", (("ongoing_da", 1), ("legacy_da", 1))),
        ("new_charge", (("gross_charge", 1), ("ordinary_charge", 1), ("topup_charge", 1))),
        (
            "opening_tax",
            (("opening_payment", 1), ("opening_recovery", -1), ("opening_deferred_cash", 1)),
        ),
        ("new_cash_tax", (("new_payment", 1), ("new_recovery", -1))),
        ("operating_cash_tax", (("opening_tax", 1), ("new_cash_tax", 1))),
        ("cash_investment", (("renewal_investment", 1), ("lead_investment", 1))),
        ("lease_additions", (("lease_renewal", 1), ("lead_lease", 1))),
    )
    for year, row in ((11, a), (12, b)):
        if "revenue" in row and "margin" in row:
            reconcile(
                row["ebit"],
                decimal(row["revenue"], f"terminal.{year}.margin_decomposition")
                * decimal(row["margin"], f"terminal.{year}.margin_decomposition"),
                f"terminal.{year}.margin_decomposition",
            )
        if "refundable_profit" in row and "ordinary_profit" in row:
            reconcile(
                max(D(0), decimal(row["ebit"], f"terminal.{year}.profit_decomposition")),
                decimal(row["refundable_profit"], f"terminal.{year}.profit_decomposition")
                + decimal(row["ordinary_profit"], f"terminal.{year}.profit_decomposition"),
                f"terminal.{year}.profit_decomposition",
            )
        for aggregate, constituents in decompositions:
            if aggregate in row and all(field in row for field, _ in constituents):
                reconcile(
                    row[aggregate],
                    sum(
                        (
                            decimal(row[field], f"terminal.{year}.{aggregate}_decomposition") * sign
                            for field, sign in constituents
                        ),
                        D(0),
                    ),
                    f"terminal.{year}.{aggregate}_decomposition",
                )
    numerator = decimal(a["fcff"])
    tv = numerator / (r - g)
    pv = tv / (1 + r) ** 10
    return {
        "growth": g,
        "numerator": numerator,
        "value_end10": tv,
        "pv": pv,
        "pole_distance": r - g,
        "witnesses": [a, b],
    }


def qualify_bridge_inventory(block, evidence, valuation, allocations):
    evidence.block(block, "bridge")
    require(block["valuation_date"] == valuation.isoformat(), "bridge_date_mismatch")
    categories = block["categories"]
    require(
        len(categories) == len(set(categories)) and set(categories) == BRIDGE_CATEGORIES,
        "incomplete_bridge_inventory",
    )
    require(isinstance(block["items"], list), "invalid_bridge_items")
    require(all(isinstance(item, dict) for item in block["items"]), "invalid_bridge_item")
    require_allocations(
        [item["allocation_id"] for item in block["items"]],
        allocations,
        "bridge",
        "bridge",
    )
    return block["items"]


def qualify_bridge_item(item, evidence, valuation, ids, *, eager=True):
    evidence.block(item, "bridge." + item["id"])
    require(item["id"] not in ids, "duplicate_bridge_id", item["id"])
    ids.add(item["id"])
    require(item["category"] in BRIDGE_CATEGORIES, "unknown_bridge_category")
    p = item["inputs"]
    claim = None
    if item["method"] == "supplied_pv":
        amount = decimal(p.get("amount"), item["id"])
    elif item["method"] == "foreign_pv":
        require(p["currency"] != evidence.pack["currency"], "foreign_currency_required")
        q = exchange_rate(
            p,
            valuation,
            evidence.pack["currency"],
            "currency",
            "foreign_per_reporting",
            "fx_date",
            item["id"] + ".fx",
        )
        amount = decimal(p.get("amount"), item["id"]) / q
    elif item["method"] == "earnings_linked_pv":
        require(
            item["category"] not in ADDITIVE_BRIDGE_CATEGORIES,
            "bridge_method_category_mismatch",
            item["id"],
        )
        baseline = decimal(p.get("recurring_baseline_ebit"))
        require(baseline > 0, "invalid_claim_baseline")
        claim = (
            baseline,
            decimal(p.get("current_pv_anchor")) if eager else p.get("current_pv_anchor"),
            decimal(p.get("later_pv_anchor")) if eager else p.get("later_pv_anchor"),
        )
        amount = None
    else:
        raise Unavailable("unsupported_bridge_method", item["id"], "unsupported")
    if amount is not None:
        additive = item["category"] in ADDITIVE_BRIDGE_CATEGORIES
        require(
            amount >= 0 if additive else amount <= 0,
            "bridge_direction_mismatch",
            item["id"],
        )
    return item, amount, claim


def qualify_bridge(block, evidence, valuation, allocations):
    ids = set()
    prepared = [
        qualify_bridge_item(item, evidence, valuation, ids)
        for item in qualify_bridge_inventory(block, evidence, valuation, allocations)
    ]
    require(
        {item["category"] for item, _, _ in prepared} == BRIDGE_CATEGORIES,
        "unvalued_bridge_category",
    )
    return prepared


def bridge(block, evidence, annual_result, valuation, allocations, *, reverse_candidate=False):
    result, ids = [], set()
    items = qualify_bridge_inventory(block, evidence, valuation, allocations)
    for raw_item in items:
        item, amount, claim = qualify_bridge_item(
            raw_item, evidence, valuation, ids, eager=False
        )
        if claim is not None:
            rows = annual_result["value"]
            if rows is None:
                rows = _dependent(annual_result)
            require(len(rows) >= 5, "claim_earnings_missing")
            earnings_error = first_row_error(rows[:5])
            if earnings_error is not None:
                raise earnings_error
            earnings = [max(D(0), decimal(x["value"]["ebit"])) for x in rows[:5]]
            baseline, current_anchor, later_anchor = claim
            amount = -(
                decimal(current_anchor) * earnings[0]
                + decimal(later_anchor) * sum(earnings) / 5
            ) / baseline
            require(
                amount <= 0,
                "bridge_direction_mismatch",
                item["id"],
                "unsupported_domain" if reverse_candidate else "invalid_input",
            )
        result.append({**item, "amount": amount})
    require({item["category"] for item in result} == BRIDGE_CATEGORIES, "unvalued_bridge_category")
    return result


def call_value(s, strike, t, vol, rf, div):
    if t == 0 or vol == 0:
        return max(D(0), s * (-div * t).exp() - strike * (-rf * t).exp())
    sd = vol * t.sqrt()
    d1 = ((s / strike).ln() + (rf - div + vol * vol / 2) * t) / sd
    d2 = d1 - sd

    def cdf(x):
        if x > 16:
            return D(1)
        if x < -16:
            return D(0)
        # Extra guard precision covers cancellation in the integral series.
        with localcontext() as ctx:
            ctx.prec = 120
            term = total = x
            for n in range(1, 2200):
                term *= -x * x / (2 * n)
                part = term / (2 * n + 1)
                total += part
                if abs(part) < D("1e-105"):
                    pi = D(
                        "3.14159265358979323846264338327950288419716939937510582097494459230781640628620899862803482534211706798214808651"
                    )
                    return D(".5") + total / (2 * pi).sqrt()
        raise Unavailable("normal_probability_nonconvergence", status="numerical_failure")

    return max(D(0), s * (-div * t).exp() * cdf(d1) - strike * (-rf * t).exp() * cdf(d2))


def rights_exchange_rate(block, evidence, valuation):
    currency = block["currency"]
    require(
        isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency) is not None,
        "invalid_rights_currency",
    )
    if currency != evidence.pack["currency"]:
        require(currency == evidence.pack["quote_currency"], "rights_currency_mismatch")
    if currency != evidence.pack["currency"] or any(
        key in block for key in ("fx_date", "reporting_currency", "quote_per_reporting")
    ):
        return exchange_rate(
            block,
            valuation,
            evidence.pack["currency"],
            "currency",
            "quote_per_reporting",
            "fx_date",
            "rights.fx",
        )
    return D(1)


def qualify_rights(block, evidence, valuation, allocations):
    evidence.block(block, "rights")
    require(block["valuation_date"] == valuation.isoformat(), "rights_date_mismatch")
    require(
        block["method"] == "endogenous_european_calls_basic_shares",
        "unsupported_rights",
        status="unsupported",
    )
    require(isinstance(block["items"], list), "invalid_right_items")
    require(all(isinstance(item, dict) for item in block["items"]), "invalid_right_item")
    require_allocations(
        [item["allocation_id"] for item in block["items"]],
        allocations,
        "rights",
        "rights",
    )
    if not block["items"]:
        require(block["currency"] == evidence.pack["currency"], "empty_rights_currency_mismatch")
    q = rights_exchange_rate(block, evidence, valuation)
    require(len({item["id"] for item in block["items"]}) == len(block["items"]), "duplicate_right_id")
    prepared = []
    for item in block["items"]:
        evidence.block(item, "rights." + item["id"])

        def n(key, item=item):
            return decimal(item.get(key), "rights." + item["id"] + "." + key)

        count, strike, vol = n("count"), n("strike"), n("volatility")
        rf, div = n("risk_free_continuous"), n("dividend_yield")
        t = D((iso_date(item["expiry"]) - valuation).days) / 365
        require(
            t >= 0 and count >= 0 and strike > 0 and vol >= 0 and div >= 0,
            "invalid_right_terms",
            item["id"],
        )
        prepared.append((item, count, strike, vol, rf, div, t))
    return q, prepared


def allocate(block, preclaims, shares, q, prepared):
    if preclaims <= 0:
        return {
            "common_residual": preclaims,
            "rights_value": None,
            "rights": [],
            "signed_diagnostic_only": True,
            "allocation_residual": None,
        }

    def claims(s):
        rows = []
        for item, count, strike, vol, rf, div, t in prepared:
            call = call_value(s, strike, t, vol, rf, div)
            rows.append(
                {
                    "id": item["id"],
                    "count": count,
                    "strike": strike,
                    "expiry": item["expiry"],
                    "years_act365": t,
                    "currency": block["currency"],
                    "underlying_reporting": s / q,
                    "per_right_reporting": call / q,
                    "claim_pv": call * count / q / 1_000_000,
                }
            )
        return sum((x["claim_pv"] for x in rows), D(0)), rows

    # This monotone common/rights allocation is not a revenue-growth inverse DCF.
    low, high = D(0), preclaims * 1_000_000 * q / shares
    for _ in range(180):
        s = (low + high) / 2
        rights, rr = claims(s)
        common = s * shares / q / 1_000_000
        residual = common + rights - preclaims
        if abs(residual) <= D("1e-24"):
            return {
                "common_residual": common,
                "rights_value": rights,
                "rights": rr,
                "signed_diagnostic_only": False,
                "allocation_residual": residual,
            }
        if residual > 0:
            high = s
        else:
            low = s
    raise Unavailable("rights_allocation_nonconvergence", status="numerical_failure")


def _stage(evidence, target, fn):
    try:
        evidence.qualify(target)
        return outcome(fn())
    except Unavailable as error:
        return outcome(error=error)
    except (KeyError, TypeError, AttributeError, ContractError) as error:
        return outcome(error=Unavailable("malformed_stage_input", str(error), "invalid_input"))
    except ArithmeticError as error:
        return outcome(error=Unavailable("numerical_failure", str(error), "numerical_failure"))


def _dependent(upstream):
    if not upstream["available"]:
        reason = upstream["reasons"][0]
        raise Unavailable(reason["code"], reason["path"], upstream["status"])
    return upstream["value"]


def qualify_reverse_inputs(pack, scenario, valuation):
    evidence = Evidence(pack)
    allocations = allocation_map(pack)
    for target in (
        "per_share_quote",
        "per_share_reporting",
        "common_equity",
        "operating_value",
        "explicit_pv",
    ):
        evidence.qualify(target)

    def operations():
        block = scenario.get("operations")
        qualify_operations(block, evidence, allocations, include_initial_growth=False)
        return block

    operation_block = _dependent(_stage(evidence, "annual_fcff", operations))
    rate_result = _dependent(
        _stage(
            evidence,
            "explicit_pv",
            lambda: rate(
                evidence.block(scenario.get("discount"), "discount"),
                evidence,
                scenario,
                valuation,
            ),
        )
    )
    discount_rate = decimal(rate_result["wacc"])

    def terminal_inputs():
        growth = qualify_terminal(
            scenario.get("terminal"),
            evidence,
            discount_rate,
            operation_block,
        )
        require(
            decimal(operation_block["inputs"].get("terminal_growth"), "terminal_growth")
            == growth,
            "reverse_terminal_growth_mismatch",
        )
        return growth

    _dependent(_stage(evidence, "operating_value", terminal_inputs))
    _dependent(
        _stage(
            evidence,
            "common_equity",
            lambda: qualify_bridge(
                scenario.get("bridge"), evidence, valuation, allocations
            ),
        )
    )
    _dependent(
        _stage(
            evidence,
            "common_equity",
            lambda: qualify_rights(
                scenario.get("rights"), evidence, valuation, allocations
            ),
        )
    )
    _dependent(
        _stage(
            evidence,
            "per_share_reporting",
            lambda: basic_shares(scenario.get("ownership"), evidence, valuation),
        )
    )
    _dependent(
        _stage(
            evidence,
            "per_share_quote",
            lambda: quote_exchange_rate(scenario, evidence, valuation),
        )
    )


def _scenario(pack, scenario, valuation, *, reverse_candidate=False):
    evidence = Evidence(pack)
    allocations = allocation_map(pack)
    result = {}
    result["annual_fcff"] = _stage(
        evidence,
        "annual_fcff",
        lambda: annual(scenario.get("operations"), evidence, valuation, allocations),
    )
    cash = result["annual_fcff"]
    if cash["available"]:
        consumed_error = first_row_error(cash["value"][:10])
        if consumed_error is not None:
            cash["status"] = consumed_error.status
            cash["calculation_status"] = "incomplete_annual_rows"
            cash["available"] = False
            cash["reasons"] = [{"code": consumed_error.code, "path": consumed_error.path}]
    rate_result = _stage(
        evidence,
        "explicit_pv",
        lambda: rate(
            evidence.block(scenario.get("discount"), "discount"),
            evidence,
            scenario,
            valuation,
        ),
    )
    result["wacc"] = rate_result

    def explicit():
        rows = _dependent(cash)
        r = decimal(_dependent(rate_result)["wacc"])
        return {
            "sum": sum(
                (
                    decimal(row["value"]["fcff"]) / (1 + r) ** t
                    for t, row in enumerate(rows[:10], 1)
                ),
                D(0),
            ),
            "annual_pv": [
                decimal(row["value"]["fcff"]) / (1 + r) ** t if row["available"] else None
                for t, row in enumerate(rows[:10], 1)
            ],
        }

    result["explicit_pv"] = _stage(evidence, "explicit_pv", explicit)

    def tail():
        rows = _dependent(cash)
        r = decimal(_dependent(rate_result)["wacc"])
        response_block = (
            scenario.get("operations")
            if scenario.get("operations", {}).get("mode") == "aggregate_response"
            else None
        )
        return terminal(
            scenario.get("terminal"),
            rows,
            evidence,
            r,
            response_block,
            reverse_candidate=reverse_candidate,
        )

    result["terminal"] = _stage(evidence, "operating_value", tail)

    def ev():
        explicit_value = decimal(_dependent(result["explicit_pv"])["sum"])
        tail_value = _dependent(result["terminal"])
        value = explicit_value + decimal(tail_value["pv"])
        fraction = decimal(tail_value["pv"]) / value if value > 0 else None
        return {
            "ev": value,
            "explicit_pv": explicit_value,
            "terminal_pv": tail_value["pv"],
            "terminal_fraction": fraction,
            "financial_review_flags": (
                ["terminal_exceeds_80pct"] if fraction is not None and fraction > D(".8") else []
            )
            + (
                ["gordon_pole_within_2pp"]
                if decimal(tail_value["pole_distance"]) < D(".02")
                else []
            ),
        }

    result["operating_value"] = _stage(evidence, "operating_value", ev)
    result["bridge"] = _stage(
        evidence,
        "common_equity",
        lambda: bridge(
            scenario.get("bridge"),
            evidence,
            cash,
            valuation,
            allocations,
            reverse_candidate=reverse_candidate,
        ),
    )

    shares = _stage(
        evidence,
        "per_share_reporting",
        lambda: basic_shares(scenario.get("ownership"), evidence, valuation),
    )
    result["basic_shares"] = shares

    def common():
        value = decimal(_dependent(result["operating_value"])["ev"])
        items = _dependent(result["bridge"])
        total = sum((decimal(x["amount"]) for x in items), D(0))
        pre = value + total
        rights = scenario.get("rights")
        q, prepared_rights = qualify_rights(rights, evidence, valuation, allocations)
        # Empty, explicitly evidenced rights do not make common total depend on shares.
        if not prepared_rights:
            allocated = {
                "common_residual": pre,
                "rights_value": D(0),
                "rights": [],
                "signed_diagnostic_only": pre <= 0,
                "allocation_residual": D(0),
            }
        else:
            allocated = allocate(
                rights,
                pre,
                decimal(_dependent(shares)),
                q,
                prepared_rights,
            )
        return {"bridge_total": total, "residual_before_rights": pre, **allocated}

    result["common_equity"] = _stage(evidence, "common_equity", common)

    def per_share():
        c = _dependent(result["common_equity"])
        quotient = decimal(c["common_residual"]) * 1_000_000 / decimal(_dependent(shares))
        return {
            "signed_quotient": quotient,
            "price_estimate": quotient if quotient > 0 else None,
            "tradable_price": quotient > 0,
        }

    result["per_share_reporting"] = _stage(evidence, "per_share_reporting", per_share)

    def quote():
        value = _dependent(result["per_share_reporting"])
        q = quote_exchange_rate(scenario, evidence, valuation)
        return {
            "signed_quotient": decimal(value["signed_quotient"]) * q,
            "price_estimate": decimal(value["price_estimate"]) * q
            if value["price_estimate"] is not None
            else None,
            "tradable_price": value["tradable_price"],
        }

    result["per_share_quote"] = _stage(evidence, "per_share_quote", quote)
    result["reverse"] = reverse_outcome()
    return result


def evaluate(pack):
    """Evaluate local copies at fixed precision; no global decimal context mutation."""
    pack = copy.deepcopy(pack)
    valuation, _, _ = validate_envelope(pack)
    envelope = identity_envelope(pack)
    with localcontext() as ctx:
        ctx.prec = 64
        scenarios = {}
        for name, scenario in pack["scenarios"].items():
            try:
                require(isinstance(scenario, dict), "invalid_scenario", name)
                scenarios[name] = _scenario(pack, scenario, valuation)
                if scenario.get("reverse") is not None:
                    from alphaforge.core.valuation.estimated_reverse import solve

                    reverse = _stage(
                        Evidence(pack),
                        "reverse",
                        lambda scenario=scenario: solve(
                            pack,
                            scenario,
                            valuation,
                            lambda candidate_pack, candidate, candidate_valuation: _scenario(
                                candidate_pack,
                                candidate,
                                candidate_valuation,
                                reverse_candidate=True,
                            ),
                        ),
                    )
                    if reverse["available"]:
                        result_kind = reverse["value"]["outcome"]
                        reverse["status"] = result_kind
                        if result_kind not in {
                            "candidate_solution",
                            "multiple_solutions",
                            "match_region",
                            "not_identifiable",
                            "no_solution",
                        }:
                            diagnostics = reverse["value"]
                            reverse = outcome(
                                error=Unavailable(result_kind, "reverse", result_kind)
                            )
                            reverse["search_diagnostics"] = diagnostics
                    scenarios[name]["reverse"] = reverse
            except Unavailable as error:
                scenarios[name] = scenario_refusal(
                    error, isinstance(scenario, dict) and scenario.get("reverse") is not None
                )
            except (KeyError, TypeError, AttributeError, ContractError) as error:
                malformed = Unavailable("malformed_scenario_input", str(error), "invalid_input")
                scenarios[name] = scenario_refusal(
                    malformed, isinstance(scenario, dict) and scenario.get("reverse") is not None
                )
        conclusion = {"status": "withheld", "reasons": ["not_an_investment_recommendation"]}
        values = []
        for s in scenarios.values():
            out = s["common_equity"]
            if out["available"]:
                values.append(decimal(out["value"]["common_residual"]))
        base = scenarios["base"]["common_equity"]
        if base["available"] and len(values) > 1:
            b = decimal(base["value"]["common_residual"])
            spread = max(values) - min(values)
            if spread > 0 and (spread >= abs(b) * D(".1") or min(values) <= 0 < max(values)):
                conclusion["reasons"].append("material_scenario_disagreement")
        return encode(
            {
                "identity": envelope,
                "calculation_status": "executed_not_financial_acceptance",
                "scenarios": scenarios,
                "firm_investment_conclusion": conclusion,
            }
        )
