"""Source-free supplied schedules. No production evaluator or issuer constants."""

from __future__ import annotations

from itertools import count

from alphaforge.core.valuation.forward_fcff import BRIDGE_CATEGORIES, RIGHTS
from alphaforge.core.valuation.forward_fcff_contract import CONVENTION, METHOD, SCHEMA, digest


class Inputs:
    def __init__(self):
        self.counter = count(1)

    def operand(self, value, rationale="Synthetic explicitly hypothetical input"):
        return {
            "value": value,
            "provenance": {
                "id": f"operand-{next(self.counter)}",
                "class": "forecast_assumption",
                "source_refs": [],
                "derived_from": [],
                "rationale": rationale,
                "accounting_date": None,
                "period_start": None,
                "period_end": None,
                "uncertainty": "hypothetical_sensitivity",
                "review_ids": [],
                "limitations": [],
                "mechanism": "Supplied independent schedule",
                "effective_periods": ["all selected periods"],
            },
        }


def request(
    currency="SEK",
    profits=(100, 100, 110, 121, 133.1, 146.41),
    tau=0.2,
    rho=0.1,
    lag=1,
    horizon=2,
    growth=0.1,
    rate=0.2,
):
    f = Inputs()
    op = f.operand
    catalog = {"sources": []}
    req = {
        "schema_version": SCHEMA,
        "method_id": METHOD,
        "economic_convention": CONVENTION,
        "company_id": 1,
        "scenario_id": "source-free",
        "valuation_date": "2026-06-30",
        "evidence_cutoff": "2026-06-30",
        "prepared_on": "2026-06-30",
        "knowledge_basis": "cutoff_filtered",
        "object": {
            "kind": "existing_consolidated_operations",
            "perimeter_id": "test-perimeter",
            "description": "Synthetic nonfinancial operations",
            "acquisitions": [],
            "accounting_basis": "hypothetical compatible books",
            "earnings_basis": "cash operating earnings before D&A",
            "lease_basis": "capitalized_debt_like",
            "archetype": "operating_company",
            "archetype_basis": "unverified synthetic premise",
            "continuing_capacity": "Explicit supplied renewal and survival premise",
            "funding_access": "Hypothetical continuing funding access",
        },
        "denomination": {
            "currency": currency,
            "original_currency": currency,
            "conversion_mode": "original",
            "amount_units": "millions",
            "share_units": "absolute",
            "price_basis": "nominal",
        },
        "evidence_context": {"catalog": catalog, "catalog_hash": digest(catalog)},
        "reviews": [],
        "limitations": [],
        "operating_schedule": {"periods": [], "initial_wc": op(0), "tax_lots": []},
        "discount": {
            "rate": op(rate),
            "currency": currency,
            "price_basis": "nominal",
            "compounding": "annual_effective",
            "risk_basis": "unlevered_operations_shields_excluded",
            "timing": "annual_end",
            "rationale": "Hypothetical financing-neutral nominal asset return",
            "review_ids": [],
        },
    }
    gross = [(tau + rho) * b for b in profits]
    accruals = [rho * b for b in profits]
    payable = receivable = 0.0
    for idx, b in enumerate(profits, 1):
        allocations = []
        for kind, leg, amounts in (("payment", "payable", gross), ("refund", "refund", accruals)):
            for source, weight in (
                [(idx, 0.5), (idx - 1, 0.5)] if kind == "payment" else [(idx - lag, 1)]
            ):
                if source >= 1 and amounts[source - 1] * weight:
                    allocations.append(
                        {
                            "id": f"cash-{kind}-{idx}-{source}",
                            "kind": kind,
                            "lot_id": f"{leg}-{source}",
                            "amount": op(amounts[source - 1] * weight),
                            "cash_date": None,
                            "eligibility_order": "Explicit annual eligibility/order assumption",
                        }
                    )
        payment = 0.5 * gross[idx - 1] + (0.5 * gross[idx - 2] if idx > 1 else 0)
        refund = accruals[idx - lag - 1] if idx > lag else 0
        ll = {
            "id": "tax",
            "scope": "operating_unlevered",
            "taxable_basis": op(b),
            "tax_da": op(0),
            "nondeductible": op(0),
            "loss_use": op(0),
            "permanent_difference": op(0),
            "loss_basis": None,
            "deferred_basis": "No old/new book-tax differences, restrictive hypothetical premise",
            "payable_open": op(payable),
            "payable_close": op(payable + gross[idx - 1] - payment),
            "arising": op(gross[idx - 1]),
            "payment": op(payment),
            "payable_other": [],
            "receivable_open": op(receivable),
            "receivable_close": op(receivable + accruals[idx - 1] - refund),
            "entitlement": op(accruals[idx - 1]),
            "refund": op(refund),
            "writeoff": op(0),
            "receivable_other": [],
            "allocations": allocations,
        }
        payable += gross[idx - 1] - payment
        receivable += accruals[idx - 1] - refund
        for leg, amount in (("payable", gross[idx - 1]), ("refund", accruals[idx - 1])):
            req["operating_schedule"]["tax_lots"].append(
                {
                    "id": f"{leg}-{idx}",
                    "ledger_id": "tax",
                    "origin": "forecast_created",
                    "leg": leg,
                    "source_period": idx,
                    "amount": op(amount),
                    "eligible_date": None,
                    "prerequisites": [],
                    "eligibility_basis": "Hypothetical full recovery after selected lag",
                    "accounting_date": f"{2026 + idx}-06-30",
                }
            )
        asset = {
            "id": "plant",
            "other": [],
            "continuation_basis": "Explicit no-asset synthetic business",
        }
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
        ):
            asset[k] = op(0)
        row = {
            "id": f"year-{idx}",
            "index": idx,
            "start": f"{2025 + idx}-07-01",
            "end": f"{2026 + idx}-06-30",
            "cash_operating_profit_before_da": op(b),
            "book_da": op(0),
            "other_noncash_operating_charge": op(0),
            "noncash_basis": "No noncash charges or automatic stock-comp addback",
            "investment_items": [],
            "zero_investment_basis": "No investment in synthetic asset-free service",
            "disposals": [],
            "wc_open": op(0),
            "wc_close": op(0),
            "wc_basis": "No non-tax operating WC in synthetic case",
            "tax_ledgers": [ll],
            "tax_not_applicable": None,
            "asset_controls": [asset],
            "settlements": [],
        }
        req["operating_schedule"]["periods"].append(row)
    set_tail(req, f, horizon, growth, {"tax/tax/arising": 2, "tax/tax/entitlement": 2}, lag)
    return req, f


def primitives(row):
    def v(x):
        return x["value"]

    result = {
        "cash_profit": v(row["cash_operating_profit_before_da"]),
        "book_da": v(row["book_da"]),
        "noncash": v(row["other_noncash_operating_charge"]),
        "cash_investment": sum(
            v(i["amount"]) for i in row["investment_items"] if i["kind"] == "cash_purchase"
        ),
        "economic_leases": sum(
            v(i["amount"])
            for i in row["investment_items"]
            if i["kind"] == "economic_lease_addition"
        ),
        "disposals": sum(v(d["proceeds"]) for d in row["disposals"]),
        "wc_open": v(row["wc_open"]),
        "wc_close": v(row["wc_close"]),
        "delta_wc": v(row["wc_close"]) - v(row["wc_open"]),
        "settlements": sum(
            v(x["amount"]) * (1 if x["kind"] == "outflow" else -1) for x in row["settlements"]
        ),
    }
    for a in row["asset_controls"]:
        for k in ("depreciation", "commissioned_additions", "closing", "construction_close"):
            result[f"asset/{a['id']}/{k}"] = v(a[k])
    for ll in row["tax_ledgers"]:
        for key, field in (
            ("basis", "taxable_basis"),
            ("tax_da", "tax_da"),
            ("arising", "arising"),
            ("entitlement", "entitlement"),
            ("payment", "payment"),
            ("refund", "refund"),
            ("payable_close", "payable_close"),
            ("receivable_close", "receivable_close"),
            ("writeoff", "writeoff"),
        ):
            result[f"tax/{ll['id']}/{key}"] = v(ll[field])
        for key in ("payable_other", "receivable_other"):
            result[f"tax/{ll['id']}/{key}"] = sum(v(item["amount"]) for item in ll[key])
    return result


def set_tail(req, f, n, g, regimes=None, lag=1):
    op = f.operand
    regimes = regimes or {}
    rows = req["operating_schedule"]["periods"]
    values = primitives(rows[n])
    templates = [
        {
            "id": path,
            "path": path,
            "seed": op(seed),
            "multiplier": op(1 + g),
            "regime_start": regimes.get(path, n + 1),
            "derivation": "Independent geometric continuation of supplied primitives; no free volume growth",
        }
        for path, seed in values.items()
    ]
    kernels = []
    for leg, weights in (("payment", [(0, 0.5), (1, 0.5)]), ("refund", [(lag, 1)])):
        queues = []
        for lot in req["operating_schedule"]["tax_lots"]:
            if (
                lot["source_period"] > n
                or lot["leg"] != ("payable" if leg == "payment" else "refund")
                or lot["origin"] != "forecast_created"
            ):
                continue
            spent = sum(
                a["amount"]["value"]
                for r in rows[:n]
                for ll in r["tax_ledgers"]
                for a in ll["allocations"]
                if a["lot_id"] == lot["id"]
            )
            remain = lot["amount"]["value"] - spent
            if remain > 1e-8:
                queues.append(
                    {
                        "id": lot["id"],
                        "source_period": lot["source_period"],
                        "lot_id": lot["id"],
                        "remaining_at_tail": op(remain),
                    }
                )
        kernels.append(
            {
                "id": leg,
                "ledger_id": "tax",
                "leg": leg,
                "recovery_fraction": op(1),
                "max_lag": max(i for i, w in weights),
                "weights": [{"offset_years": i, "weight": op(w)} for i, w in weights],
                "queues": queues,
                "rationale": "Explicit finite payment/recovery convolution",
            }
        )
    req["terminal"] = {
        "kind": "gordon",
        "horizon": n,
        "growth": op(g),
        "tail_start": n + 1,
        "continuing_perimeter": req["object"]["perimeter_id"],
        "renewal_basis": "Supplied funded renewal economics",
        "survival_basis": "Conditional perpetual survival",
        "price_volume_basis": "Nominal prices, no free physical volume",
        "growth_funding_basis": "Supplied investment and WC support capacity",
        "templates": templates,
        "tax_kernels": kernels,
        "exhausted_states": [],
    }


def complete_bridge(req, f, items=(), shares=100_000_000):
    categories = []
    for category in BRIDGE_CATEGORIES:
        categories.append(
            {
                "id": category,
                "status": "valued"
                if any(i["category"] == category for i in items)
                else "not_applicable_supported",
                "basis": "Fully specified synthetic inventory, not real issuer facts",
            }
        )
    req["bridge"] = {
        "currency": req["denomination"]["currency"],
        "amount_units": "millions",
        "perimeter_id": req["object"]["perimeter_id"],
        "scope_date": req["valuation_date"],
        "categories": categories,
        "items": list(items),
        "completeness": {
            "scope_date": req["valuation_date"],
            "perimeter_id": req["object"]["perimeter_id"],
            "inventoried_categories": list(BRIDGE_CATEGORIES),
            "reviewed_by_or_unreviewed": "unreviewed",
            "basis_identity": "fully hypothetical inventory",
            "no_unresolved_material_claims": True,
        },
    }
    req["ownership"] = {
        "scope_date": req["valuation_date"],
        "perimeter_id": req["object"]["perimeter_id"],
        "share_basis": "basic_absolute_net_treasury_custody",
        "ordinary_shares": f.operand(shares),
        "rights_basis": "Fully hypothetical rights inventory",
        "rights": [
            {
                "id": r,
                "status": "not_applicable_supported",
                "claim_event_ids": [],
                "basis": "Synthetic no rights",
            }
            for r in RIGHTS
        ],
    }


def claim(req, f, id, category, amount, direction="obligation"):
    return {
        "id": id,
        "category": category,
        "location": "bridge",
        "direction": direction,
        "value": f.operand(amount),
        "currency": req["denomination"]["currency"],
        "valuation_date": req["valuation_date"],
        "classification_basis": "Explicit same-date claim PV, not book carrying value",
    }
