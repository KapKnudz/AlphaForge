"""Pure supplied-schedule FCFF; shield-excluded conditional research only.

All lives, budgets, rates, lags, horizons and economic premises are caller inputs.
The evaluator neither acquires evidence nor grants issuer/ranking admission.
"""

from __future__ import annotations

import math
from datetime import timedelta

from alphaforge.core.valuation.forward_fcff_contract import (
    CONVENTION,
    METHOD,
    VALIDATION,
    ContractError,
    FrozenRequest,
    Operands,
    closed,
    freeze,
    integer,
    iso_date,
    number,
    output,
    reconcile,
    sequence,
    text,
    unique,
)

BRIDGE_CATEGORIES = (
    "excess_distributable_cash",
    "nonoperating_assets",
    "nonlease_debt",
    "existing_lease_debt",
    "opening_current_tax",
    "existing_deferred_cash",
    "acquisition_consideration",
    "pension_employee",
    "legal_other",
    "noncommon_claims",
)
RIGHTS = ("minority", "preferred", "options", "warrants")
ASSETS = {"excess_distributable_cash", "nonoperating_assets"}


def _anniversary(d):
    try:
        return d.replace(year=d.year + 1)
    except ValueError:
        return d.replace(year=d.year + 1, day=28)


def _require(condition, code, path=""):
    if not condition:
        raise ContractError(code, path)


def _amount(o, row, key, nonnegative=False):
    return o.operand(row[key], key, nonnegative)


def _movements(o, values):
    result = 0.0
    for v in sequence(values):
        closed(v, "id amount rationale")
        o.event(v["id"])
        text(v["rationale"])
        result += _amount(o, v, "amount")
    return result


def _denomination(req):
    d = closed(
        req["denomination"],
        "currency original_currency conversion_mode amount_units share_units price_basis",
    )
    _require(
        d["currency"] in {"SEK", "EUR"} and d["currency"] == d["original_currency"],
        "currency_mismatch",
    )
    _require(
        (d["conversion_mode"], d["amount_units"], d["share_units"], d["price_basis"])
        == ("original", "millions", "absolute", "nominal"),
        "denomination_unsupported",
    )
    obj = closed(
        req["object"],
        "kind perimeter_id description acquisitions accounting_basis earnings_basis lease_basis archetype archetype_basis continuing_capacity funding_access",
        "limitations",
    )
    for k in (
        "perimeter_id",
        "description",
        "accounting_basis",
        "earnings_basis",
        "archetype_basis",
    ):
        text(obj[k])
    sequence(obj["acquisitions"])
    for a in obj["acquisitions"]:
        closed(a, "id description included control_date")
        text(a["id"])
        text(a["description"])
        _require(isinstance(a["included"], bool), "acquisition_perimeter_invalid")
        if a["control_date"] is not None:
            iso_date(a["control_date"])
    _require(obj["kind"] == "existing_consolidated_operations", "object_unsupported")
    _require(obj["lease_basis"] == "capitalized_debt_like", "lease_basis_unsupported")


def _lots(schedule, o):
    lots = {}
    for lot in sequence(schedule["tax_lots"]):
        closed(
            lot,
            "id ledger_id origin leg source_period amount eligible_date prerequisites eligibility_basis accounting_date",
        )
        identity = text(lot["id"])
        _require(identity not in lots, "duplicate_id", identity)
        _require(
            lot["origin"] in {"boundary_existing", "forecast_created"}, "tax_lot_origin_unsupported"
        )
        _require(lot["leg"] in {"payable", "refund"}, "tax_lot_leg_unsupported")
        integer(lot["source_period"])
        _require(
            lot["origin"] != "boundary_existing" or lot["source_period"] == 0,
            "boundary_lot_created_after_boundary",
        )
        iso_date(lot["accounting_date"])
        if lot["eligible_date"] is not None:
            iso_date(lot["eligible_date"])
        text(lot["eligibility_basis"])
        sequence(lot["prerequisites"])
        amount = _amount(o, lot, "amount", True)
        if (
            lot["origin"] == "boundary_existing"
            and lot["amount"]["provenance"]["class"] == "reported_fact"
        ):
            _require(
                lot["amount"]["provenance"]["accounting_date"]
                == lot["accounting_date"]
                == o.request["valuation_date"],
                "boundary_fact_date_mismatch",
            )
        lots[identity] = {"record": lot, "amount": amount, "remaining": amount, "paid": 0.0}
    for lot in lots.values():
        for prerequisite in lot["record"]["prerequisites"]:
            _require(
                prerequisite in lots and prerequisite != lot["record"]["id"],
                "refund_prerequisite_missing",
            )
    return lots


def _tax(o, row, ebit, da, lots, prior, index, start, end):
    payments = refunds = boundary_pay = boundary_ref = 0.0
    legs = {}
    ledgers = sequence(row["tax_ledgers"])
    if not ledgers:
        text(row["tax_not_applicable"])
    else:
        _require(row["tax_not_applicable"] is None, "tax_applicability_conflict")
    ids = []
    allocated_ebit = allocated_da = 0.0
    for ledger in ledgers:
        closed(
            ledger,
            "id scope taxable_basis tax_da nondeductible loss_use permanent_difference loss_basis deferred_basis payable_open payable_close arising payment payable_other receivable_open receivable_close entitlement refund writeoff receivable_other allocations",
            "net_rate net_expense allocated_ebit allocated_book_da allocation_basis lot_movements",
        )
        identity = text(ledger["id"])
        ids.append(identity)
        _require(ledger["scope"] == "operating_unlevered", "financing_tax_effect_unsupported")
        text(ledger["deferred_basis"])
        if len(ledgers) > 1 or "allocated_ebit" in ledger:
            text(ledger.get("allocation_basis"))
            ledger_ebit = _amount(o, ledger, "allocated_ebit")
            ledger_da = _amount(o, ledger, "allocated_book_da", True)
        else:
            ledger_ebit, ledger_da = ebit, da
        allocated_ebit += ledger_ebit
        allocated_da += ledger_da
        b = _amount(o, ledger, "taxable_basis")
        tax_da = _amount(o, ledger, "tax_da", True)
        nondeductible = _amount(o, ledger, "nondeductible")
        loss = _amount(o, ledger, "loss_use", True)
        permanent = _amount(o, ledger, "permanent_difference")
        reconcile(
            b,
            ledger_ebit + ledger_da - tax_da + nondeductible - loss + permanent,
            "tax_basis_reconciliation_mismatch",
            identity,
        )
        if b < 0 or loss > 0:
            text(ledger["loss_basis"])
        if "net_rate" in ledger:
            rate = _amount(o, ledger, "net_rate", True)
            _require(rate <= 1, "net_tax_rate_out_of_range")
            _require("net_expense" in ledger, "net_tax_expense_missing")
            reconcile(_amount(o, ledger, "net_expense"), b * rate, "net_tax_expense_mismatch")
        values = {
            k: _amount(o, ledger, k, True)
            for k in (
                "payable_open",
                "payable_close",
                "arising",
                "payment",
                "receivable_open",
                "receivable_close",
                "entitlement",
                "refund",
                "writeoff",
            )
        }
        if b < 0 and (values["entitlement"] or values["refund"]):
            text(ledger["loss_basis"])
        if index > 1:
            _require(identity in prior, "tax_ledger_continuity_missing")
            reconcile(values["payable_open"], prior[identity][0], "tax_state_continuity_mismatch")
            reconcile(
                values["receivable_open"], prior[identity][1], "tax_state_continuity_mismatch"
            )
        other_p = _movements(o, ledger["payable_other"])
        other_r = _movements(o, ledger["receivable_other"])
        # Reclasses/FX must be allocated to identified lots, never a residual plug.
        reconcile(
            values["payable_close"],
            values["payable_open"] + values["arising"] - values["payment"] + other_p,
            "tax_payable_rollforward_mismatch",
        )
        reconcile(
            values["receivable_close"],
            values["receivable_open"]
            + values["entitlement"]
            - values["refund"]
            - values["writeoff"]
            + other_r,
            "tax_refund_rollforward_mismatch",
        )
        relevant = [v for v in lots.values() if v["record"]["ledger_id"] == identity]
        for leg, accrual, opening in (
            ("payable", "arising", "payable_open"),
            ("refund", "entitlement", "receivable_open"),
        ):
            new = sum(
                v["amount"]
                for v in relevant
                if v["record"]["source_period"] == index and v["record"]["leg"] == leg
            )
            reconcile(new, values[accrual], "tax_lot_accrual_mismatch", identity)
            existing = sum(
                v["remaining"]
                for v in relevant
                if v["record"]["source_period"] < index and v["record"]["leg"] == leg
            )
            reconcile(existing, values[opening], "tax_lot_opening_mismatch", identity)
        movement_totals = {"payable": 0.0, "refund": 0.0}
        for movement in sequence(ledger.get("lot_movements", [])):
            closed(movement, "id lot_id amount rationale")
            text(movement["rationale"])
            o.event(movement["id"])
            _require(movement["lot_id"] in lots, "tax_movement_lot_missing")
            target = lots[movement["lot_id"]]
            _require(
                target["record"]["ledger_id"] == identity
                and target["record"]["source_period"] <= index,
                "tax_movement_lot_mismatch",
            )
            amount = _amount(o, movement, "amount")
            _require(target["remaining"] + amount >= 0, "tax_movement_negative_balance")
            target["remaining"] += amount
            movement_totals[target["record"]["leg"]] += amount
        reconcile(movement_totals["payable"], other_p, "tax_payable_lot_movements_mismatch")
        reconcile(movement_totals["refund"], other_r, "tax_refund_lot_movements_mismatch")
        totals = {"payment": 0.0, "refund": 0.0, "writeoff": 0.0}
        operating = {"payment": 0.0, "refund": 0.0}
        boundary = {"payment": 0.0, "refund": 0.0}
        allocations = sequence(ledger["allocations"])
        # Deterministic within-year execution: payable cash first; explicit order
        # premise remains necessary where no exact eligibility cash date is known.
        allocations = sorted(
            allocations, key=lambda a: (a.get("kind") != "payment", a.get("id", ""))
        )
        unique([a["id"] for a in allocations])
        for a in allocations:
            closed(a, "id kind lot_id amount cash_date eligibility_order")
            o.event(a["id"])
            kind = a["kind"]
            _require(kind in totals and a["lot_id"] in lots, "tax_allocation_invalid")
            v = lots[a["lot_id"]]
            lot = v["record"]
            _require(
                lot["ledger_id"] == identity and lot["source_period"] <= index,
                "tax_lot_period_mismatch",
            )
            _require(
                lot["leg"] == ("payable" if kind == "payment" else "refund"),
                "tax_allocation_leg_mismatch",
            )
            amount = _amount(o, a, "amount", True)
            _require(
                amount <= v["remaining"]
                or math.isclose(amount, v["remaining"], rel_tol=1e-8, abs_tol=1e-8),
                "tax_lot_overallocated",
            )
            if kind != "writeoff":
                if a["cash_date"] is not None:
                    cash_date = iso_date(a["cash_date"])
                    _require(start <= cash_date <= end, "tax_cash_date_outside_period")
                    if lot["eligible_date"] is not None:
                        _require(
                            cash_date >= iso_date(lot["eligible_date"]),
                            "refund_eligibility_unestablished",
                        )
                else:
                    text(a["eligibility_order"])
                    if lot["eligible_date"] is not None:
                        _require(
                            end >= iso_date(lot["eligible_date"]),
                            "refund_eligibility_unestablished",
                        )
                for prerequisite in lot["prerequisites"]:
                    prerequisite_lot = lots[prerequisite]
                    _require(
                        prerequisite_lot["paid"] >= prerequisite_lot["amount"],
                        "refund_eligibility_unestablished",
                    )
                    if (
                        a["cash_date"] is not None
                        and prerequisite_lot.get("last_cash_date") is not None
                    ):
                        _require(
                            iso_date(a["cash_date"])
                            >= iso_date(prerequisite_lot["last_cash_date"]),
                            "refund_eligibility_unestablished",
                        )
                    else:
                        text(a["eligibility_order"])
            v["remaining"] = max(0.0, v["remaining"] - amount)
            if kind == "payment":
                v["paid"] += amount
                if a["cash_date"] is not None:
                    v["last_cash_date"] = max(
                        v.get("last_cash_date", a["cash_date"]), a["cash_date"]
                    )
            totals[kind] += amount
            if kind != "writeoff":
                (operating if lot["origin"] == "forecast_created" else boundary)[kind] += amount
        for kind in totals:
            reconcile(totals[kind], values[kind], "tax_cash_allocations_mismatch", identity)
        for leg, closing in (("payable", "payable_close"), ("refund", "receivable_close")):
            remain = sum(
                v["remaining"]
                for v in relevant
                if v["record"]["source_period"] <= index and v["record"]["leg"] == leg
            )
            reconcile(remain, values[closing], "tax_lot_closing_mismatch", identity)
        payments += operating["payment"]
        refunds += operating["refund"]
        boundary_pay += boundary["payment"]
        boundary_ref += boundary["refund"]
        prior[identity] = (values["payable_close"], values["receivable_close"])
        for k, v in {
            "basis": b,
            "tax_da": tax_da,
            "arising": values["arising"],
            "entitlement": values["entitlement"],
            "payment": operating["payment"],
            "refund": operating["refund"],
            "payable_close": values["payable_close"],
            "receivable_close": values["receivable_close"],
            "payable_other": other_p,
            "receivable_other": other_r,
            "writeoff": values["writeoff"],
        }.items():
            legs[f"tax/{identity}/{k}"] = v
    unique(ids)
    if ledgers:
        reconcile(allocated_ebit, ebit, "tax_perimeter_earnings_mismatch")
        reconcile(allocated_da, da, "tax_perimeter_da_mismatch")
    if index > 1:
        _require(set(ids) == set(prior), "tax_ledger_continuity_missing")
    return payments, refunds, boundary_pay, boundary_ref, legs


def _assets(o, row, da, impairment, start, end, previous):
    investments = sequence(row["investment_items"])
    if not investments:
        text(row["zero_investment_basis"])
    spend = {}
    cash = lease = 0.0
    for item in investments:
        closed(
            item, "id asset_class kind amount spend_period in_service_date classification rationale"
        )
        o.event(item["id"])
        text(item["rationale"])
        _require(item["spend_period"] == row["index"], "investment_spend_period_mismatch")
        _require(
            item["classification"] in {"renewal", "retention", "expansion", "unallocated"},
            "investment_classification_unsupported",
        )
        _require(
            item["kind"] in {"cash_purchase", "economic_lease_addition"},
            "investment_kind_unsupported",
        )
        amount = _amount(o, item, "amount", True)
        _require(amount > 0, "investment_positive_required")
        if item["kind"] == "cash_purchase":
            cash += amount
        else:
            lease += amount
        active = item["in_service_date"] is not None and iso_date(item["in_service_date"]) <= end
        if active:
            _require(
                iso_date(item["in_service_date"]) >= start,
                "new_investment_commissioned_before_spend",
            )
        sums = spend.setdefault(text(item["asset_class"]), [0.0, 0.0])
        sums[0 if active else 1] += amount
    disposal_cash = 0.0
    disposal_nbvs = {}
    for d in sequence(row["disposals"]):
        closed(d, "id asset_class proceeds carrying_reduction gain_tax_basis")
        o.event(d["id"])
        text(d["gain_tax_basis"])
        disposal_cash += _amount(o, d, "proceeds", True)
        c = text(d["asset_class"])
        disposal_nbvs[c] = disposal_nbvs.get(c, 0) + _amount(o, d, "carrying_reduction", True)
    controls = sequence(row["asset_controls"])
    _require(bool(controls), "asset_controls_missing")
    ids = []
    total_da = total_impairment = net_book = 0.0
    legs = {}
    for a in controls:
        closed(
            a,
            "id opening commissioned_additions construction_open construction_close transfers depreciation impairment disposal_reduction other closing continuation_basis",
            "transfer_events",
        )
        identity = text(a["id"])
        ids.append(identity)
        text(a["continuation_basis"])
        values = {
            k: _amount(o, a, k, True)
            for k in (
                "opening",
                "commissioned_additions",
                "construction_open",
                "construction_close",
                "transfers",
                "depreciation",
                "impairment",
                "disposal_reduction",
                "closing",
            )
        }
        other = _movements(o, a["other"])
        if row["index"] > 1:
            _require(identity in previous, "asset_class_continuity_missing")
            reconcile(values["opening"], previous[identity][0], "asset_continuity_mismatch")
            reconcile(
                values["construction_open"], previous[identity][1], "asset_continuity_mismatch"
            )
        transfer_total = 0.0
        for event in sequence(a.get("transfer_events", [])):
            closed(event, "id in_service_date amount rationale")
            o.event(event["id"])
            text(event["rationale"])
            _require(
                start <= iso_date(event["in_service_date"]) <= end,
                "construction_commissioning_date_invalid",
            )
            transfer_total += _amount(o, event, "amount", True)
        reconcile(transfer_total, values["transfers"], "construction_transfer_events_missing")
        active, advance = spend.get(identity, [0, 0])
        reconcile(
            values["commissioned_additions"],
            active + values["transfers"],
            "investment_commissioning_mismatch",
        )
        reconcile(
            values["construction_close"],
            values["construction_open"] + advance - values["transfers"],
            "construction_rollforward_mismatch",
        )
        reconcile(
            values["disposal_reduction"],
            disposal_nbvs.get(identity, 0),
            "disposal_control_mismatch",
        )
        expected = (
            values["opening"]
            + values["commissioned_additions"]
            - values["depreciation"]
            - values["impairment"]
            - values["disposal_reduction"]
            + other
        )
        reconcile(values["closing"], expected, "asset_rollforward_mismatch", identity)
        total_da += values["depreciation"]
        total_impairment += values["impairment"]
        net_book += (
            values["commissioned_additions"]
            - values["depreciation"]
            - values["impairment"]
            - values["disposal_reduction"]
        )
        previous[identity] = (values["closing"], values["construction_close"])
        for k in ("depreciation", "commissioned_additions", "closing", "construction_close"):
            legs[f"asset/{identity}/{k}"] = values[k]
    unique(ids)
    _require(set(spend) <= set(ids) and set(disposal_nbvs) <= set(ids), "asset_class_missing")
    _require(set(ids) == set(previous), "asset_class_continuity_missing")
    reconcile(total_da, da, "aggregate_da_mismatch")
    reconcile(total_impairment, impairment, "aggregate_impairment_mismatch")
    return cash, lease, disposal_cash, net_book, legs


def _cohorts(schedule, o, rows):
    """Optional supplied finite weights are validated, never ignored or generated."""
    cohorts = sequence(schedule.get("cohorts", []))
    unique([c["id"] for c in cohorts])
    class_dep = {}
    for c in cohorts:
        closed(
            c,
            "id asset_class cost residual life_years in_service_date method weights remaining_basis",
        )
        text(c["remaining_basis"])
        cost = _amount(o, c, "cost", True)
        residual = _amount(o, c, "residual", True)
        _require(cost >= residual, "cohort_residual_exceeds_cost")
        life = _amount(o, c, "life_years")
        _require(
            life > 0 and c["method"] == "supplied_weights", "cohort_method_or_life_unsupported"
        )
        commissioned = iso_date(c["in_service_date"])
        weights = sequence(c["weights"])
        total = 0.0
        seen = []
        for w in weights:
            closed(w, "period_index depreciation")
            index = integer(w["period_index"], minimum=1)
            seen.append(index)
            dep = _amount(o, w, "depreciation", True)
            total += dep
            _require(index <= len(rows), "cohort_continuation_missing")
            _require(
                dep == 0 or commissioned <= iso_date(rows[index - 1]["end"]),
                "depreciation_before_commissioning",
            )
            class_dep[(c["asset_class"], index)] = class_dep.get((c["asset_class"], index), 0) + dep
            _require(
                total <= cost - residual
                or math.isclose(total, cost - residual, abs_tol=1e-8, rel_tol=1e-8),
                "cohort_depreciation_exceeds_cost",
            )
        unique(seen)
        _require(
            bool(seen) and max(seen) - min(seen) + 1 <= math.ceil(life) + 1,
            "cohort_weights_exceed_supplied_life",
        )
        reconcile(total, cost - residual, "cohort_finite_life_not_exhausted")
    if cohorts:
        for row in rows:
            for path, dep in row["legs"].items():
                if path.startswith("asset/") and path.endswith("/depreciation"):
                    c = path.split("/")[1]
                    reconcile(dep, class_dep.get((c, row["index"]), 0), "cohort_class_da_mismatch")


def _schedule(req, o):
    _denomination(req)
    s = closed(
        req["operating_schedule"], "periods initial_wc tax_lots", "cohorts supporting_operands"
    )
    for operand in sequence(s.get("supporting_operands", [])):
        o.operand(operand)
    periods = sequence(s["periods"])
    _require(bool(periods), "operating_schedule_missing")
    lots = _lots(s, o)
    asset_prior = {}
    tax_prior = {}
    wc = _amount(o, s, "initial_wc")
    start = iso_date(req["valuation_date"]) + timedelta(days=1)
    result = []
    ids = []
    for index, row in enumerate(periods, 1):
        closed(
            row,
            "id index start end cash_operating_profit_before_da book_da other_noncash_operating_charge noncash_basis investment_items zero_investment_basis disposals wc_open wc_close wc_basis tax_ledgers tax_not_applicable asset_controls settlements",
            "financing_diagnostics reported_ebit ebit_reconciliation noncash_items",
        )
        ids.append(text(row["id"]))
        _require(integer(row["index"], minimum=1) == index, "unsupported_period")
        end = _anniversary(start) - timedelta(days=1)
        _require(
            iso_date(row["start"]) == start and iso_date(row["end"]) == end,
            "unsupported_period",
            row["id"],
        )
        c = _amount(o, row, "cash_operating_profit_before_da")
        da = _amount(o, row, "book_da", True)
        n = _amount(o, row, "other_noncash_operating_charge", True)
        text(row["noncash_basis"])
        text(row["wc_basis"])
        asset_impairment = n
        if "noncash_items" in row:
            noncash_total = asset_impairment = 0.0
            for charge in sequence(row["noncash_items"]):
                closed(charge, "id kind amount rationale")
                o.event(charge["id"])
                text(charge["rationale"])
                _require(
                    charge["kind"] in {"asset_impairment", "other_noncash_operating_charge"},
                    "noncash_charge_treatment_unsupported",
                )
                amount = _amount(o, charge, "amount", True)
                noncash_total += amount
                if charge["kind"] == "asset_impairment":
                    asset_impairment += amount
            reconcile(noncash_total, n, "noncash_charge_items_mismatch")
        ebit = c - da - n
        if "reported_ebit" in row:
            reported = _amount(o, row, "reported_ebit")
            adjustments = _movements(o, row.get("ebit_reconciliation"))
            reconcile(ebit, reported + adjustments, "earnings_normalization_mismatch")
        wc_open = _amount(o, row, "wc_open")
        wc_close = _amount(o, row, "wc_close")
        reconcile(wc_open, wc, "wc_continuity_mismatch")
        delta = wc_close - wc_open
        i, lease, v, book_net, asset_legs = _assets(
            o, row, da, asset_impairment, start, end, asset_prior
        )
        p, r, bp, br, tax_legs = _tax(o, row, ebit, da, lots, tax_prior, index, start, end)
        x = 0.0
        for settlement in sequence(row["settlements"]):
            closed(settlement, "id kind amount rationale")
            o.event(settlement["id"])
            text(settlement["rationale"])
            _require(settlement["kind"] in {"outflow", "recovery"}, "settlement_kind_unsupported")
            x += _amount(o, settlement, "amount", True) * (
                1 if settlement["kind"] == "outflow" else -1
            )
        for d in sequence(row.get("financing_diagnostics", [])):
            closed(d, "id kind amount rationale")
            text(d["rationale"])
            _require(
                d["kind"] in {"lease_principal", "lease_interest", "debt_issuance"},
                "financing_shield_unsupported",
            )
            _amount(o, d, "amount", True)
        fcff = ebit + da + n - (p - r) - i - lease + v - delta - x
        reconcile(fcff, c - p + r - i - lease + v - delta - x, "fcff_equations_mismatch")
        legs = {
            "cash_profit": c,
            "book_da": da,
            "noncash": n,
            "cash_investment": i,
            "economic_leases": lease,
            "disposals": v,
            "wc_open": wc_open,
            "wc_close": wc_close,
            "delta_wc": delta,
            "settlements": x,
            **asset_legs,
            **tax_legs,
        }
        result.append(
            {
                "period_id": row["id"],
                "index": index,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "ebit": ebit,
                "book_da": da,
                "cash_operating_profit_before_da": c,
                "gross_tax_payment": p,
                "gross_tax_refund": r,
                "cash_operating_tax": p - r,
                "cash_asset_purchases": i,
                "economic_lease_additions": lease,
                "gross_economic_investment": i + lease,
                "disposal_cash": v,
                "delta_wc": delta,
                "cash_settlements": x,
                "net_book_investment": book_net,
                "net_cash_reinvestment": i + lease - v - da + delta,
                "fcff_operations": fcff,
                "boundary_tax_payment": bp,
                "boundary_tax_refund": br,
                "fcff_with_boundary_tax": fcff - bp + br,
                "legs": legs,
            }
        )
        wc = wc_close
        start = end + timedelta(days=1)
    unique(ids)
    ledger_ids = set(tax_prior)
    for lot in lots.values():
        _require(
            lot["record"]["ledger_id"] in ledger_ids
            and lot["record"]["source_period"] <= len(periods),
            "tax_lot_unconsumed_source",
        )
    _cohorts(s, o, result)
    return result, lots


def _discount(req, o):
    discount = req.get("discount")
    if discount is None:
        raise ContractError("discount_missing", "discount")
    closed(discount, "rate currency price_basis compounding risk_basis timing rationale review_ids")
    text(discount["rationale"])
    sequence(discount["review_ids"])
    _require(discount["currency"] == req["denomination"]["currency"], "currency_mismatch")
    _require(
        (
            discount["price_basis"],
            discount["compounding"],
            discount["risk_basis"],
            discount["timing"],
        )
        == ("nominal", "annual_effective", "unlevered_operations_shields_excluded", "annual_end"),
        "discount_basis_unsupported",
    )
    r = _amount(o, discount, "rate")
    _require(r > 0, "discount_rate_invalid")
    return r


def _tail(req, o, rows, lots, rate):
    t = req.get("terminal")
    if t is None:
        raise ContractError("terminal_state_missing", "terminal")
    if t.get("kind") == "finite_runoff":
        closed(t, "kind horizon residual_zero rationale")
        horizon = integer(t["horizon"], minimum=1)
        _require(horizon == len(rows) and t["residual_zero"] is True, "finite_runoff_incomplete")
        text(t["rationale"])
        last = rows[-1]
        _require(
            last["legs"]["wc_close"] == 0 and all(v["remaining"] == 0 for v in lots.values()),
            "finite_runoff_incomplete",
        )
        for k, v in last["legs"].items():
            if k.startswith("asset/") and (
                k.endswith("/closing") or k.endswith("/construction_close")
            ):
                _require(v == 0, "finite_runoff_incomplete", k)
        return horizon, 0.0, None, {"kind": "finite_runoff", "conditional": True}
    closed(
        t,
        "kind horizon growth tail_start continuing_perimeter renewal_basis survival_basis price_volume_basis growth_funding_basis templates tax_kernels exhausted_states",
    )
    _require(t["kind"] == "gordon", "terminal_basis_unsupported")
    n = integer(t["horizon"], minimum=1)
    _require(
        integer(t["tail_start"], minimum=2) == n + 1 and len(rows) >= n + 2,
        "terminal_full_next_year_or_witness_missing",
    )
    _require(
        t["continuing_perimeter"] == req["object"]["perimeter_id"], "terminal_perimeter_mismatch"
    )
    for k in ("renewal_basis", "survival_basis", "price_volume_basis", "growth_funding_basis"):
        text(t[k])
    g = _amount(o, t, "growth")
    h = 1 + g
    _require(g > -1 and g < rate, "terminal_growth_invalid")
    tail, witness = rows[n], rows[n + 1]
    templates = sequence(t["templates"])
    expected_paths = set(tail["legs"])
    paths = []
    regimes = {}
    residuals = {}
    for template in templates:
        closed(template, "id path seed multiplier regime_start derivation")
        path = text(template["path"])
        paths.append(path)
        text(template["derivation"])
        _require(path in expected_paths, "terminal_template_unknown", path)
        seed = _amount(o, template, "seed")
        multiplier = _amount(o, template, "multiplier")
        _require(multiplier == h, "terminal_multiplier_mismatch", path)
        regime = integer(template["regime_start"], minimum=1)
        _require(regime <= n + 1, "terminal_regime_missing", path)
        reconcile(seed, tail["legs"][path], "terminal_seed_mismatch", path)
        # Regime start is a supplied premise checked against all retained rows.
        for idx in range(regime - 1, len(rows)):
            _require(path in rows[idx]["legs"], "terminal_primitive_missing", path)
            reconcile(
                rows[idx]["legs"][path], seed * h ** (idx - n), "tail_primitive_nonstationary", path
            )
        residuals[path] = witness["legs"][path] - h * tail["legs"][path]
        regimes[path] = regime
    unique(paths)
    _require(set(paths) == expected_paths, "terminal_primitive_templates_missing")
    # Old finite states must be genuinely zero before the geometric continuation.
    exhausted = sequence(t["exhausted_states"])
    for state in exhausted:
        closed(state, "id path exhausted_by rationale")
        text(state["rationale"])
        by = integer(state["exhausted_by"], minimum=1)
        _require(by <= n and state["path"] in rows[by - 1]["legs"], "terminal_exhaustion_missing")
        for row in rows[by - 1 :]:
            _require(row["legs"][state["path"]] == 0, "terminal_finite_state_unexhausted")
    # Construction/disposal/one-off settlement tails are not renewable primitives.
    for path, v in tail["legs"].items():
        if path in {"disposals", "settlements", "noncash"} or path.endswith(
            ("/construction_close", "/payable_other", "/receivable_other")
        ):
            _require(v == 0, "terminal_discrete_state_unsupported", path)
    kernels = sequence(t["tax_kernels"])
    kernel_ids = []
    longest = 0
    queue_receipts = []
    ledger_ids = sorted({path.split("/")[1] for path in expected_paths if path.startswith("tax/")})
    for kernel in kernels:
        closed(kernel, "id ledger_id leg recovery_fraction max_lag weights queues rationale")
        text(kernel["rationale"])
        ledger, leg = kernel["ledger_id"], kernel["leg"]
        _require(
            ledger in ledger_ids and leg in {"payment", "refund"}, "terminal_tax_kernel_unsupported"
        )
        kernel_ids.append((ledger, leg))
        fraction = _amount(o, kernel, "recovery_fraction", True)
        _require(fraction <= 1, "terminal_recovery_fraction_invalid")
        max_lag = integer(kernel["max_lag"])
        longest = max(longest, max_lag)
        weights = sequence(kernel["weights"])
        offsets = []
        weight_sum = 0.0
        for w in weights:
            closed(w, "offset_years weight")
            offset = integer(w["offset_years"])
            offsets.append(offset)
            weight = _amount(o, w, "weight", True)
            weight_sum += weight
        unique(offsets)
        _require(bool(offsets) and max(offsets) == max_lag, "terminal_kernel_lag_mismatch")
        reconcile(weight_sum, fraction, "terminal_kernel_fraction_mismatch")
        accrual_path = f"tax/{ledger}/" + ("arising" if leg == "payment" else "entitlement")
        cash_path = f"tax/{ledger}/{leg}"
        regime = regimes[accrual_path]
        queue = sequence(kernel["queues"])
        queue_ids = []
        for q in queue:
            closed(q, "id source_period lot_id remaining_at_tail")
            lot = lots.get(q["lot_id"])
            _require(lot is not None, "terminal_queue_lot_missing")
            record = lot["record"]
            source = integer(q["source_period"], minimum=1)
            _require(
                record["origin"] == "forecast_created"
                and record["ledger_id"] == ledger
                and record["leg"] == ("payable" if leg == "payment" else "refund")
                and source == record["source_period"]
                and source <= n,
                "terminal_queue_lot_mismatch",
            )
            remaining = _amount(o, q, "remaining_at_tail", True)
            # Reconstruct residual at start of N+1, not after witness execution.
            spent = 0.0
            for rr in req["operating_schedule"]["periods"][:n]:
                for ll in rr["tax_ledgers"]:
                    for a in ll["allocations"]:
                        if a["lot_id"] == record["id"]:
                            spent += number(a["amount"]["value"])
                    for movement in ll.get("lot_movements", []):
                        if movement["lot_id"] == record["id"]:
                            spent -= number(movement["amount"]["value"])
            reconcile(remaining, lot["amount"] - spent, "terminal_queue_balance_mismatch")
            queue_ids.append(record["id"])
            queue_receipts.append(
                {"lot_id": record["id"], "source_period": source, "remaining_at_tail": remaining}
            )
        unique(queue_ids)
        for tindex in (n + 1, n + 2):
            cash_expected = 0.0
            for w in weights:
                offset = w["offset_years"]
                weight = number(w["weight"]["value"])
                if weight == 0 or all(row["legs"][accrual_path] == 0 for row in rows):
                    continue
                source = tindex - offset
                _require(source >= regime, "tail_tax_queue_nonstationary", cash_path)
                if source <= n:
                    source_lots = [
                        v
                        for v in lots.values()
                        if v["record"]["ledger_id"] == ledger
                        and v["record"]["origin"] == "forecast_created"
                        and v["record"]["leg"] == ("payable" if leg == "payment" else "refund")
                        and v["record"]["source_period"] == source
                    ]
                    _require(
                        bool(source_lots)
                        and all(v["record"]["id"] in queue_ids for v in source_lots),
                        "terminal_mature_queue_missing",
                    )
                expected_source_cash = rows[source - 1]["legs"][accrual_path] * weight
                allocated_source_cash = sum(
                    number(a["amount"]["value"])
                    for ll in req["operating_schedule"]["periods"][tindex - 1]["tax_ledgers"]
                    for a in ll["allocations"]
                    if a["kind"] == leg
                    and lots[a["lot_id"]]["record"]["origin"] == "forecast_created"
                    and lots[a["lot_id"]]["record"]["ledger_id"] == ledger
                    and lots[a["lot_id"]]["record"]["source_period"] == source
                )
                reconcile(
                    allocated_source_cash,
                    expected_source_cash,
                    "terminal_tax_lot_kernel_mismatch",
                    cash_path,
                )
                cash_expected += expected_source_cash
            reconcile(
                rows[tindex - 1]["legs"][cash_path],
                cash_expected,
                "terminal_gross_tax_recurrence_mismatch",
                cash_path,
            )
    unique(kernel_ids)
    _require(
        set(kernel_ids)
        == {(ledger, leg) for ledger in ledger_ids for leg in ("payment", "refund")},
        "terminal_tax_kernel_missing",
    )
    reconcile(
        witness["fcff_operations"], h * tail["fcff_operations"], "terminal_fcff_witness_mismatch"
    )
    numerator = tail["fcff_operations"]
    tv = numerator / (rate - g)
    return (
        n,
        tv,
        numerator,
        {
            "kind": "gordon",
            "conditional": True,
            "tail_start": n + 1,
            "growth": g,
            "regime_starts": regimes,
            "exhausted_state_ids": [s["id"] for s in exhausted],
            "longest_lag": longest,
            "per_leg_residuals": residuals,
            "queues": queue_receipts,
            "failures": [],
        },
    )


def _bridge(req, o, operating_value, rows, rate):
    b = req.get("bridge")
    if b is None:
        raise ContractError("bridge_material_claim_unresolved", "bridge")
    closed(
        b,
        "currency amount_units perimeter_id scope_date categories items completeness",
        "boundary_cash",
    )
    _require(b["currency"] == req["denomination"]["currency"], "currency_mismatch")
    _require(
        b["amount_units"] == "millions"
        and b["perimeter_id"] == req["object"]["perimeter_id"]
        and b["scope_date"] == req["valuation_date"],
        "bridge_basis_mismatch",
    )
    c = closed(
        b["completeness"],
        "scope_date perimeter_id inventoried_categories reviewed_by_or_unreviewed basis_identity no_unresolved_material_claims",
    )
    text(c["basis_identity"])
    text(c["reviewed_by_or_unreviewed"])
    _require(
        c["scope_date"] == b["scope_date"]
        and c["perimeter_id"] == b["perimeter_id"]
        and c["no_unresolved_material_claims"] is True
        and set(c["inventoried_categories"]) == set(BRIDGE_CATEGORIES),
        "bridge_material_claim_unresolved",
    )
    for limit in req["limitations"]:
        if limit["material"] and "common_equity" in limit["affected_outputs"]:
            raise ContractError("bridge_material_claim_unresolved", limit["id"])
    categories = sequence(b["categories"])
    unique([cat["id"] for cat in categories])
    _require(
        {cat["id"] for cat in categories} == set(BRIDGE_CATEGORIES), "bridge_inventory_incomplete"
    )
    items = sequence(b["items"])
    by_category = {}
    total = 0.0
    excluded_pairs = {}
    for item in items:
        closed(
            item,
            "id category location direction value currency valuation_date classification_basis",
            "matched_pair_id excluded_amount",
        )
        identity = item["id"]
        category = item["category"]
        _require(
            category in BRIDGE_CATEGORIES and item["direction"] in {"asset", "obligation"},
            "bridge_claim_type_unsupported",
        )
        _require(
            item["currency"] == b["currency"] and item["valuation_date"] == b["scope_date"],
            "currency_or_claim_date_mismatch",
        )
        text(item["classification_basis"])
        _require(
            item["location"]
            in {"operating_schedule", "boundary_schedule", "bridge", "excluded_matched_pair"},
            "bridge_location_unsupported",
        )
        v = _amount(o, item, "value", True)
        if category in ASSETS:
            _require(item["direction"] == "asset", "bridge_direction_mismatch")
        elif category not in {"opening_current_tax", "existing_deferred_cash"}:
            _require(item["direction"] == "obligation", "bridge_direction_mismatch")
        if item["location"] == "bridge":
            o.event(identity)
            total += v * (1 if item["direction"] == "asset" else -1)
        elif item["location"] == "operating_schedule":
            _require(identity in o.events, "claim_operating_event_missing")
        elif item["location"] == "boundary_schedule":
            _require(
                identity in {r["id"] for r in b.get("boundary_cash", [])},
                "claim_boundary_event_missing",
            )
        else:
            _require(v == 0, "excluded_matched_pair_value_nonzero")
            pair_id = text(item.get("matched_pair_id"))
            excluded_pairs.setdefault(pair_id, []).append(
                (item["direction"], _amount(o, item, "excluded_amount", True))
            )
        by_category.setdefault(category, []).append(identity)
    for pair in excluded_pairs.values():
        _require(
            len(pair) == 2 and {direction for direction, _ in pair} == {"asset", "obligation"},
            "excluded_matched_pair_incomplete",
        )
        reconcile(pair[0][1], pair[1][1], "excluded_matched_pair_amount_mismatch")
    for cat in categories:
        closed(cat, "id status basis")
        text(cat["basis"])
        _require(
            cat["status"] in {"valued", "not_applicable_supported"},
            "bridge_material_claim_unresolved",
            cat["id"],
        )
        _require(
            (cat["status"] == "valued") == bool(by_category.get(cat["id"])),
            "bridge_category_value_missing",
            cat["id"],
        )
    boundary_lots = {
        lot["id"]: lot
        for lot in req["operating_schedule"]["tax_lots"]
        if lot["origin"] == "boundary_existing" and number(lot["amount"]["value"]) > 0
    }
    cash_by_lot = {}
    boundary_pv = 0.0
    for cash in sequence(b.get("boundary_cash", [])):
        closed(cash, "id lot_id period_index direction amount claim_rate rationale")
        text(cash["rationale"])
        o.event(cash["id"])
        _require(cash["direction"] in {"asset", "obligation"}, "boundary_direction_unsupported")
        idx = integer(cash["period_index"], minimum=1)
        claim_rate = _amount(o, cash, "claim_rate")
        _require(claim_rate > 0, "claim_discount_invalid")
        amount = _amount(o, cash, "amount", True)
        if cash["lot_id"] is not None:
            _require(cash["lot_id"] in boundary_lots, "boundary_lot_origin_mismatch")
            lot = boundary_lots[cash["lot_id"]]
            _require(
                cash["direction"] == ("obligation" if lot["leg"] == "payable" else "asset"),
                "boundary_direction_mismatch",
            )
            key = (cash["lot_id"], idx)
            _require(key not in cash_by_lot, "double_counted_event", cash["lot_id"])
            cash_by_lot[key] = amount
            allocations = [
                a
                for r in req["operating_schedule"]["periods"]
                for ll in r["tax_ledgers"]
                for a in ll["allocations"]
                if a["lot_id"] == cash["lot_id"] and r["index"] == idx and a["kind"] != "writeoff"
            ]
            reconcile(
                amount,
                sum(number(a["amount"]["value"]) for a in allocations),
                "boundary_tax_cash_mismatch",
            )
        boundary_pv += (
            amount / (1 + claim_rate) ** idx * (1 if cash["direction"] == "asset" else -1)
        )
    for identity, lot in boundary_lots.items():
        valued = [
            item
            for item in items
            if item["id"] == identity
            and item["category"] == "opening_current_tax"
            and item["location"] == "bridge"
        ]
        scheduled = {
            idx: amount for (lot_id, idx), amount in cash_by_lot.items() if lot_id == identity
        }
        _require(not (valued and scheduled), "double_counted_event", identity)
        _require(bool(valued) or bool(scheduled), "bridge_material_claim_unresolved", identity)
        if valued:
            _require(
                valued[0]["direction"] == ("obligation" if lot["leg"] == "payable" else "asset"),
                "boundary_direction_mismatch",
            )
        else:
            recovered = 0.0
            written_off = 0.0
            for row in req["operating_schedule"]["periods"]:
                cash_amount = sum(
                    number(a["amount"]["value"])
                    for ledger in row["tax_ledgers"]
                    for a in ledger["allocations"]
                    if a["lot_id"] == identity and a["kind"] != "writeoff"
                )
                written_off += sum(
                    number(a["amount"]["value"])
                    for ledger in row["tax_ledgers"]
                    for a in ledger["allocations"]
                    if a["lot_id"] == identity and a["kind"] == "writeoff"
                )
                reconcile(
                    scheduled.get(row["index"], 0),
                    cash_amount,
                    "boundary_cash_completeness_mismatch",
                    identity,
                )
                recovered += cash_amount
            reconcile(
                recovered + written_off,
                number(lot["amount"]["value"]),
                "boundary_cash_completeness_mismatch",
                identity,
            )
    # Rights that affect common equity are checked even if basic count is absent.
    ownership = req.get("ownership")
    if ownership is not None:
        _rights(req, ownership, b)
    return operating_value + total + boundary_pv, boundary_pv


def _rights(req, own, bridge):
    closed(
        own, "scope_date perimeter_id share_basis ordinary_shares rights rights_basis", "currency"
    )
    _require(
        own["scope_date"] == req["valuation_date"]
        and own["perimeter_id"] == req["object"]["perimeter_id"]
        and own["share_basis"] == "basic_absolute_net_treasury_custody",
        "ownership_basis_unsupported",
    )
    text(own["rights_basis"])
    rights = sequence(own["rights"])
    unique([r["id"] for r in rights])
    _require({r["id"] for r in rights} == set(RIGHTS), "ownership_census_missing")
    claims = {
        i["id"]
        for i in bridge["items"]
        if i["category"] == "noncommon_claims"
        and i["location"] == "bridge"
        and i["direction"] == "obligation"
    }
    for right in rights:
        closed(right, "id status claim_event_ids basis")
        text(right["basis"])
        sequence(right["claim_event_ids"])
        _require(
            right["status"] in {"valued", "not_applicable_supported"}, "ownership_census_missing"
        )
        if right["status"] == "valued":
            _require(
                bool(right["claim_event_ids"]) and set(right["claim_event_ids"]) <= claims,
                "ownership_claim_unvalued",
            )
        else:
            _require(not right["claim_event_ids"], "ownership_rights_status_conflict")


def _refusal(error, optional=False):
    code = error.code
    if code.endswith("missing") or code in {
        "required_operand_missing",
        "bridge_material_claim_unresolved",
        "bridge_inventory_incomplete",
        "ownership_census_missing",
        "ownership_claim_unvalued",
        "ownership_material_input_unresolved",
        "terminal_mature_queue_missing",
    }:
        status = "insufficient_evidence"
    elif "unsupported" in code or code in {
        "currency_mismatch",
        "tail_tax_queue_nonstationary",
        "tail_primitive_nonstationary",
        "terminal_discrete_state_unsupported",
        "terminal_finite_state_unexhausted",
    }:
        status = "unsupported"
    else:
        status = "invalid_input"
    return output(status=status, reason=code, missing=error.path)


def evaluate(request: FrozenRequest | dict | str) -> dict:
    """Four independent conditional outputs; explicit PV never masquerades as EV."""
    result = {
        "schema_version": "forward-fcff-result-v1",
        "method_id": METHOD,
        "economic_convention": CONVENTION,
        "validation_version": VALIDATION,
        "shield_basis": "financing shields excluded",
        "review_status": "unreviewed",
        "operating_cash_flows": output(status="not_requested"),
        "operating_value": output(status="not_requested"),
        "common_equity": output(status="not_requested"),
        "per_share": output(status="not_requested"),
        "explicit_pv": None,
        "terminal": None,
        "boundary_tax_pv": None,
    }
    try:
        frozen = freeze(request.body if isinstance(request, FrozenRequest) else request)
        req = frozen.unpack()
        o = Operands(req)
        result.update(
            {
                k: req[k]
                for k in (
                    "company_id",
                    "scenario_id",
                    "valuation_date",
                    "evidence_cutoff",
                    "prepared_on",
                    "knowledge_basis",
                    "object",
                    "denomination",
                    "limitations",
                    "reviews",
                )
            }
        )
        rows, lots = _schedule(req, o)
        conditions = [
            "Supplied schedules and continuing capacity are assumptions, not issuer admission.",
            "No financial sign-off; financing shields excluded.",
        ]
        result["operating_cash_flows"] = output(
            rows, conditions=conditions, limitations=req["limitations"]
        )
    except (ContractError, ValueError, TypeError, KeyError, OverflowError) as error:
        e = (
            error
            if isinstance(error, ContractError)
            else ContractError("invalid_shape", str(error))
        )
        for key in ("operating_cash_flows", "operating_value", "common_equity", "per_share"):
            result[key] = _refusal(e)
        return result
    tail_attempted = False
    try:
        r = _discount(req, o)
        # Before terminal validation, an explicit-period diagnostic remains useful.
        horizon = len(rows)
        if isinstance(req.get("terminal"), dict):
            candidate = req["terminal"].get("horizon")
            if (
                isinstance(candidate, int)
                and not isinstance(candidate, bool)
                and 1 <= candidate <= len(rows)
            ):
                horizon = candidate
        result["explicit_pv"] = sum(
            row["fcff_operations"] / (1 + r) ** row["index"] for row in rows[:horizon]
        )
        result["boundary_tax_pv"] = sum(
            (row["boundary_tax_payment"] - row["boundary_tax_refund"]) / (1 + r) ** row["index"]
            for row in rows
        )
        _require(req["object"]["archetype"] == "operating_company", "operating_object_unsupported")
        _require(
            isinstance(req["object"]["continuing_capacity"], str)
            and bool(req["object"]["continuing_capacity"].strip()),
            "continuing_capacity_missing",
            "object.continuing_capacity",
        )
        if any(row["fcff_operations"] < 0 for row in rows):
            _require(
                isinstance(req["object"]["funding_access"], str)
                and bool(req["object"]["funding_access"].strip()),
                "funding_access_missing",
                "object.funding_access",
            )
        tail_attempted = True
        n, tv, numerator, certificate = _tail(req, o, rows, lots, r)
        explicit = sum(row["fcff_operations"] / (1 + r) ** row["index"] for row in rows[:n])
        terminal_pv = tv / (1 + r) ** n
        value = explicit + terminal_pv
        _require(math.isfinite(value), "nonfinite_derived_value")
        result["explicit_pv"] = explicit
        result["terminal"] = {
            "horizon": n,
            "next_year_fcff": numerator,
            "value_at_horizon": tv,
            "present_value": terminal_pv,
            "certificate": certificate,
        }
        result["operating_value"] = output(
            value, conditions=conditions, limitations=req["limitations"]
        )
    except (ContractError, ValueError, TypeError, KeyError, OverflowError) as error:
        e = (
            error
            if isinstance(error, ContractError)
            else ContractError("invalid_optional_module", str(error))
        )
        result["operating_value"] = _refusal(e, True)
        if tail_attempted and isinstance(req.get("terminal"), dict):
            candidate = req["terminal"].get("horizon")
            candidate_numerator = None
            if (
                isinstance(candidate, int)
                and not isinstance(candidate, bool)
                and 1 <= candidate < len(rows)
                and req["terminal"].get("kind") == "gordon"
            ):
                candidate_numerator = rows[candidate]["fcff_operations"]
            result["terminal"] = {
                "horizon": candidate,
                "next_year_fcff": candidate_numerator,
                "value_at_horizon": None,
                "present_value": None,
                "certificate": {
                    "conditional": True,
                    "failures": [{"code": e.code, "path": e.path}],
                },
            }
        for key in ("common_equity", "per_share"):
            result[key] = output(
                status="insufficient_evidence", reason="operating_value_unavailable"
            )
        return _finish(result, o)
    try:
        equity, boundary_pv = _bridge(req, o, value, rows, r)
        _require(math.isfinite(equity), "nonfinite_derived_value")
        result["common_equity"] = output(
            equity, conditions=conditions, limitations=req["limitations"]
        )
        result["operations_plus_boundary_value"] = value + boundary_pv
        if equity < 0:
            result["common_equity"]["conditions"].append("negative_equity_nontradable")
    except (ContractError, ValueError, TypeError, KeyError, OverflowError) as error:
        e = (
            error
            if isinstance(error, ContractError)
            else ContractError("invalid_optional_module", str(error))
        )
        result["common_equity"] = _refusal(e, True)
        result["per_share"] = output(
            status="insufficient_evidence", reason="common_equity_unavailable"
        )
        return _finish(result, o)
    try:
        own = req.get("ownership")
        _require(own is not None, "ownership_census_missing")
        _rights(req, own, req["bridge"])
        for limitation in req["limitations"]:
            _require(
                not (limitation["material"] and "per_share" in limitation["affected_outputs"]),
                "ownership_material_input_unresolved",
                limitation["id"],
            )
        shares = _amount(o, own, "ordinary_shares")
        provenance = own["ordinary_shares"]["provenance"]
        if provenance["class"] == "reported_fact":
            _require(
                provenance["accounting_date"] == req["valuation_date"]
                and provenance["period_start"] is None
                and provenance["period_end"] is None,
                "ownership_count_date_or_period_mismatch",
            )
        _require(shares > 0, "ownership_count_invalid")
        _require(equity >= 0, "negative_equity_nontradable")
        per_share = equity * 1_000_000 / shares
        _require(math.isfinite(per_share), "nonfinite_derived_value")
        result["per_share"] = output(
            per_share, conditions=conditions, limitations=req["limitations"]
        )
    except (ContractError, ValueError, TypeError, KeyError, OverflowError) as error:
        e = (
            error
            if isinstance(error, ContractError)
            else ContractError("invalid_optional_module", str(error))
        )
        result["per_share"] = _refusal(e, True)
    return _finish(result, o)


def _finish(result, o):
    keys = ("operating_cash_flows", "operating_value", "common_equity", "per_share")
    result["computed_outputs"] = [k for k in keys if result[k]["available"]]
    result["unavailable_outputs"] = [k for k in keys if not result[k]["available"]]
    result["equations"] = {
        "version": VALIDATION,
        "operand_ids": sorted(o.used),
        "fcff": "C-(P-R)-I-L+V-dWC-X",
        "terminal": "full F(N+1)/(r-g)",
        "per_share": "common_equity_millions*1000000/basic_absolute_shares",
    }
    human = {
        operand
        for review in o.reviews.values()
        if review["reviewer_kind"] == "human"
        for operand in review["scope_operand_ids"]
    }
    if o.used and o.used <= human:
        result["review_status"] = "human_reviewed"
    return result
