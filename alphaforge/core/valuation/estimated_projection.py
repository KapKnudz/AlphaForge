"""Optional aggregate response budgets, not tax lots, asset registers or legal censuses.

Direct annual estimates need none of these response assumptions. This builder is
one explicitly selected two-bucket charge/recovery mechanism, not a tax engine.
"""

from decimal import Decimal as D

from alphaforge.core.valuation.estimated_inputs import decimal, require


def qualify_response(block, *, include_initial_growth=True):
    p = block["inputs"]
    require(isinstance(p, dict), "invalid_projection_inputs")
    numbers = {}

    def n(key):
        numbers[key] = decimal(p.get(key), key)
        return numbers[key]

    n("terminal_growth")
    if include_initial_growth:
        n("initial_growth")
    require(
        -1 < numbers["terminal_growth"]
        and (not include_initial_growth or -1 < numbers["initial_growth"]),
        "invalid_growth",
    )
    maturity = p["mature_year"]
    lag = p["recovery_lag"]
    years = p["opening_deferred_years"]
    require(type(maturity) is int and 2 <= maturity <= 10, "invalid_maturity")
    require(type(lag) is int and 0 <= lag <= 10, "invalid_recovery_lag")
    require(type(years) is int and 1 <= years <= 12, "invalid_deferred_years")
    raw_settlements = p["settlements"]
    require(isinstance(raw_settlements, list), "invalid_settlements")
    require(len(raw_settlements) <= 10, "settlement_in_terminal_witnesses")
    for key in (
        "refundable_profit_share",
        "recovery_fraction",
        "ordinary_tax_rate",
        "gross_charge_rate",
        "refund_rate",
        "minimum_rate",
        "same_year_payment",
        "opening_recovery_fraction",
    ):
        require(0 <= n(key) <= 1, "invalid_fraction", key)
    for key in (
        "cash_renewal_rate",
        "cash_growth_capital",
        "lease_renewal_rate",
        "lease_growth_capital",
        "legacy_remaining",
        "legacy_annual",
        "opening_payable",
        "opening_recoverable",
    ):
        require(n(key) >= 0, "negative_budget", key)
    fractions = {}
    for key in ("opening_payment_fractions", "opening_recovery_fractions"):
        fractions[key] = [decimal(x, key) for x in p[key]]
        require(
            bool(fractions[key])
            and all(x >= 0 for x in fractions[key])
            and sum(fractions[key]) == 1,
            "opening_fractions_must_sum_to_one",
            key,
        )
    require(n("revenue") > 0, "invalid_baseline_revenue")
    for key in (
        "opening_wc",
        "opening_deferred",
        "initial_cash_diagnostic",
        "margin_first",
        "margin_terminal",
        "da_first",
        "da_terminal",
        "wc_ratio",
    ):
        n(key)
    settlements = [decimal(value, "settlements") for value in raw_settlements]
    return {
        "numbers": numbers,
        "mature_year": maturity,
        "recovery_lag": lag,
        "opening_deferred_years": years,
        "settlements": settlements,
        "fractions": fractions,
    }


def qualify_fixed_schedules(block):
    qualified = qualify_response(block, include_initial_growth=False)
    n = qualified["numbers"]
    pf = qualified["fractions"]["opening_payment_fractions"]
    rf = qualified["fractions"]["opening_recovery_fractions"]
    years = qualified["opening_deferred_years"]
    customer = n["legacy_remaining"]
    pay = n["opening_payable"]
    recover = n["opening_recoverable"] * n["opening_recovery_fraction"]
    deferred = n["opening_deferred"]
    rows = []
    for year in range(1, 13):
        legacy = min(customer, n["legacy_annual"])
        customer -= legacy
        opening_payment = n["opening_payable"] * pf[year - 1] if year <= len(pf) else D(0)
        opening_recovery = (
            n["opening_recoverable"] * n["opening_recovery_fraction"] * rf[year - 1]
            if year <= len(rf)
            else D(0)
        )
        opening_deferred = n["opening_deferred"] / years if year <= years else D(0)
        pay -= opening_payment
        recover -= opening_recovery
        deferred -= opening_deferred
        if year >= len(pf):
            pay = D(0)
        if year >= len(rf):
            recover = D(0)
        if year >= years:
            deferred = D(0)
        rows.append(
            {
                "legacy_da": legacy,
                "opening_payment": opening_payment,
                "opening_recovery": opening_recovery,
                "opening_deferred_cash": opening_deferred,
                "opening_tax": opening_payment - opening_recovery + opening_deferred,
                "opening_payable_remaining": pay,
                "opening_recoverable_remaining": recover,
                "opening_deferred_remaining": deferred,
                "finite_effects": (customer, pay, recover, deferred),
            }
        )
    for row in rows[10:12]:
        require(
            all(value == 0 for value in row["finite_effects"]),
            "finite_effects_not_exhausted",
        )
        require(
            row["legacy_da"] == 0 and row["opening_tax"] == 0,
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
            require(row[leg] == 0, "finite_cash_in_terminal", "terminal." + leg)
    return qualified


def project(block, qualified=None):
    qualified = qualified or qualify_response(block)
    numbers = qualified["numbers"]

    def n(key):
        return numbers[key]

    growth = n("terminal_growth")
    initial = n("initial_growth")
    maturity = qualified["mature_year"]
    lag = qualified["recovery_lag"]
    years = qualified["opening_deferred_years"]
    settlements = qualified["settlements"]
    fractions = qualified["fractions"]
    revenues = [n("revenue")]
    growths = []
    for t in range(1, 14):
        g = growth + (initial - growth) * D(10 - t) / 9 if t <= 10 else growth
        growths.append(g)
        revenues.append(revenues[-1] * (1 + g))
    wc = n("opening_wc")
    customer = n("legacy_remaining")
    pay = n("opening_payable")
    recover = n("opening_recoverable") * n("opening_recovery_fraction")
    deferred = n("opening_deferred")
    new_pay = new_recover = D(0)
    charges, entitlements = [D(0)], [D(0)]
    rows = []
    cash = n("initial_cash_diagnostic")

    def interpolate(first, last, t):
        return n(first) + (n(last) - n(first)) * min(D(t - 1) / (maturity - 1), D(1))

    for t in range(1, 13):
        r = revenues[t]
        margin = interpolate("margin_first", "margin_terminal", t)
        ebit = r * margin
        ongoing = r * interpolate("da_first", "da_terminal", t)
        legacy = min(customer, n("legacy_annual"))
        customer -= legacy
        profit = max(D(0), ebit)
        refundable = profit * n("refundable_profit_share")
        ordinary = profit - refundable
        gross = refundable * n("gross_charge_rate")
        other = ordinary * n("ordinary_tax_rate")
        topup = refundable * max(
            D(0),
            n("minimum_rate")
            - (n("gross_charge_rate") - n("refund_rate") * n("recovery_fraction")),
        )
        arising = gross + other + topup
        entitlement = refundable * n("refund_rate") * n("recovery_fraction")
        charges.append(arising)
        entitlements.append(entitlement)
        payment = n("same_year_payment") * arising + (1 - n("same_year_payment")) * charges[t - 1]
        recovery = entitlements[t - lag] if t - lag > 0 else D(0)
        pf, rf = fractions["opening_payment_fractions"], fractions["opening_recovery_fractions"]
        op = n("opening_payable") * pf[t - 1] if t <= len(pf) else D(0)
        ore = (
            n("opening_recoverable") * n("opening_recovery_fraction") * rf[t - 1]
            if t <= len(rf)
            else D(0)
        )
        dt = n("opening_deferred") / years if t <= years else D(0)
        pay -= op
        recover -= ore
        deferred -= dt
        # Exhaustion is derived from completed schedules, not tolerance-clipped balances.
        if t >= len(pf):
            pay = D(0)
        if t >= len(rf):
            recover = D(0)
        if t >= years:
            deferred = D(0)
        new_pay += arising - payment
        new_recover += entitlement - recovery
        opening = op - ore + dt
        new = payment - recovery
        tax = opening + new
        increment = max(D(0), revenues[t + 1] - r)
        renewal = r * n("cash_renewal_rate")
        lead = increment * n("cash_growth_capital")
        lease_renewal = r * n("lease_renewal_rate")
        lease_lead = increment * n("lease_growth_capital")
        close = r * n("wc_ratio")
        settlement = settlements[t - 1] if t <= len(settlements) else D(0)
        row = {
            "revenue": r,
            "growth": growths[t - 1],
            "margin": margin,
            "ebit": ebit,
            "ongoing_da": ongoing,
            "legacy_da": legacy,
            "noncash_addbacks": ongoing + legacy,
            "other_noncash": D(0),
            "refundable_profit": refundable,
            "ordinary_profit": ordinary,
            "gross_charge": gross,
            "ordinary_charge": other,
            "topup_charge": topup,
            "new_charge": arising,
            "new_entitlement": entitlement,
            "new_payment": payment,
            "new_recovery": recovery,
            "opening_payment": op,
            "opening_recovery": ore,
            "opening_deferred_cash": dt,
            "opening_tax": opening,
            "new_cash_tax": new,
            "operating_cash_tax": tax,
            "opening_payable_remaining": pay,
            "opening_recoverable_remaining": recover,
            "opening_deferred_remaining": deferred,
            "new_payable_remaining": new_pay,
            "new_recoverable_remaining": new_recover,
            "renewal_investment": renewal,
            "lead_investment": lead,
            "cash_investment": renewal + lead,
            "lease_renewal": lease_renewal,
            "lead_lease": lease_lead,
            "lease_additions": lease_renewal + lease_lead,
            "disposal_cash": D(0),
            "wc_open": wc,
            "wc_close": close,
            "other_operating_cash": settlement,
            "finite_effects": {
                "legacy_da": customer,
                "opening_payable": pay,
                "opening_recoverable": recover,
                "opening_deferred": deferred,
            },
            "record_ids": block["record_ids"],
        }
        fcff = (
            ebit
            + ongoing
            + legacy
            - tax
            - renewal
            - lead
            - lease_renewal
            - lease_lead
            - (close - wc)
            - settlement
        )
        cash += fcff
        row["undistributed_cash_diagnostic"] = cash
        rows.append(row)
        wc = close

    recovery_transition = D(0)
    if lag > 1:
        terminal_recovery = entitlements[11 - lag]
        for t in range(12, 11 + lag):
            actual = entitlements[t - lag]
            expected = terminal_recovery * (1 + growth) ** (t - 11)
            if abs(actual - expected) > max(
                D("2e-8"), D("2e-12") * max(abs(actual), abs(expected))
            ):
                recovery_transition = D(1)
                break
    for row in rows[10:]:
        row["finite_effects"]["recovery_transition"] = recovery_transition
    return rows
