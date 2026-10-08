# Valuation — heuristic score vs auditable DCF

AlphaForge keeps heuristic ranking, admitted mature/reverse DCF and conditional
supplied-schedule FCFF research separate. Research results do not confer issuer
admission, calibration or readiness.

## Heuristic valuation_score (ranking)

* **Where:** `alphaforge/core/ranking/score_rules.py:score_valuation`,
  `alphaforge/core/valuation/calculator.py`, `alphaforge/core/valuation/raw_valuation.py`
* **What:** `valuation_score` in `CompanyScore` / `ranking.json` is a
  deterministic heuristic: `fcf_yield` vs required-return hurdle spread,
  `pe`/`ev_ebit` percentiles, and historical guardrails
  (`ev_ebit_guardrail_low/high` requiring ≥5 positive `ev_ebit` history).
  The margin-of-safety is a yield spread, not a DCF.
* **Model version:** `RankingEngine.RANKING_MODEL_VERSION = "2026-10-04-dcf-forward-reinvestment-v19"`
  (v19 records qualified forward reinvestment and restricted solve availability;
  `valuation_score` remains heuristic and DCF separate).
  Financial selection is `verified-dates-consecutive-annual-denomination-v1` in the loader;
  [the deterministic flow](deterministic-flow.md#3-cutoff-selection-and-calculation-wiring)
  owns its date, freshness, refusal-provenance and export contract.
* **Current dividend yield:** policy `calendar-ttm-verified-v2` in
  [`dividend_yield.py`](../alphaforge/core/valuation/dividend_yield.py) produces
  percentage points from `sum(amount) / selected_close * 100` only for an
  independently verified complete trailing calendar twelve-month `(start,end]`
  ex-date window and matching denominations from the explicit MVP allowlist:
  `CAD`, `CHF`, `DKK`, `EUR`, `GBP`, `ISK`, `NOK`, `PLN`, `SEK`, and `USD`.
  Alphabetic shape alone does not verify a code. February 29's prior-year
  anniversary clamps to February 28. Verified complete empty windows give `0`
  only when the selected close has a supported denomination; zero-valued
  observations do so only when their currencies are verified and match the
  close. Unknown/partial coverage and mismatched/unknown currency give `None`
  with a typed reason. Matching supported non-SEK distribution and close
  currencies are valid without conversion. No report FX or
  realized-return/reinvestment substitution. The v2 provenance rule admits the
  first authoritative supported denomination for an unverified, conflict-free
  row, including a valid-looking legacy tag such as assumed SEK. Only contradictory
  **verified** supported denominations establish a sticky conflict; unusable/missing
  fresh tags do not erase earlier verified denomination evidence or certify a
  window.
  General/property/bank scoring consume this same guarded value. General audit
  components distinguish `verified_dividend_window` from
  `dividend_window_unavailable`; all models retain window/coverage/selected-close
  facts in `scoring_audit.dividend_yield`. Existing missing-input weight handling
  is unchanged: unavailable yield is not a zero-valued fundamental. See
  [acquisition and selection](deterministic-flow.md#3-cutoff-selection-and-calculation-wiring).

## Auditable DCF (policy + engine)

* **Policy:** `alphaforge/core/valuation/dcf_policy.py`
  (`VERSION = "reverse-dcf-v17-admissible-growth-domain"`) — historical growth
  uses the latest consecutive positive-revenue suffix without bridging missing
  or nonpositive observations; this calculation is distinct from v11.
  An unresolved annual slot inside the selected fiscal span removes historical
  growth authority. The public DCF quality policy requires at least two
  consecutive annual periods with positive revenue and reported EBIT; a single
  latest-year/current-margin fallback cannot authorize a mature forecast. The
  zero-growth fallback remains explicit in policy diagnostics, but the policy
  returns unavailable when annual quality is insufficient.
  5-year projection,
  `tax_rate 21%`, `terminal_growth 2%`, revenue CAGR clamped `[-5%,15%]`,
  EBIT margin revenue-weighted over 3–5 annuals when available; shorter
  histories use the explicit latest-period normalization and warnings. This slice requires constant
  positive margins: the current and normalized margin must agree; it does not
  price margin expansion from revenue growth. Reinvestment requires a **qualified
  own-company operating-capital/earnings calibration**. Dated provider ROIC alone
  is insufficient. `reinvestment.py` validates fiscal coverage, source content
  hashes/anchors, publication/observation dates, denomination, normalized EBIT,
  tax and operating-capital endpoints, own-company identity, consolidation perimeter,
  matching reported IFRS16 EBIT/debt basis and explicit accounting review.
  The initial disclosure lane requires matched original currency (SEK for this
  hurdle policy); capital/earnings FX conversion is not inferred or implemented. Historical average return is NOPAT / average beginning-and-ending
  operating capital. Initial future marginal return equals that ratio but is
  explicitly a `company_history_calibrated_assumption`, never an observed future
  return. This numerical consistency check cannot authenticate an analyst's
  source interpretation. There is no automatic extraction, sector prior or backfill.
  Discount comes from `RequiredReturnPolicy` market-cap buckets in **absolute SEK**
  (market cap is MSEK, scaled ×1e6). It is labeled
  `discount_rate_proxy_for_cost_of_capital`, not measured company WACC.
  Without calibration, FCFF remains unavailable: `dated_positive_roic` is the
  retained missing-evidence reason when no usable provider return exists;
  `admissible_reinvestment_calibration` marks scalar-only or rejected calibration
  cases. Neither produces values or roots. Zero/negative profits, varying margins,
  capital contraction and unsupported external financing are refused; negative
  profits do not receive cash-tax refunds or negative-investment cash releases. Report `currency` at calculation is verified
  values currency; original currency, conversion mode/target and original→target
  ratio remain separate provenance. Compatible non-SEK raw multiples may be
  available, but DCF retains its SEK-only required-return refusal and does not
  convert report values or ratios.
* **Engine:** `alphaforge/core/valuation/reverse_dcf.py`
  (`ReverseDcfEngine.value/solve`) — convention
  `forward-funded-constant-margin-hurdle-convergence-v1`. Year t spends
  `I_t=(NOPAT_(t+1)-NOPAT_t)/q_(t+1)` at year end to fund the next year;
  `FCFF_t=NOPAT_t-I_t`. Year-one operating capital is already installed at the
  valuation boundary, a required reviewed premise. No investment cap or automatic
  capital release is applied. Replacement capacity must be evidenced in calibration;
  net growth investment does not mean replacement assets are free.
  Returns fade linearly over the five explicit funding intervals to the discount
  hurdle proxy, reaching it in the final interval and remaining equal thereafter.
  Year n funds n+1 using qT=r. Terminal year n+1 funds n+2:
  `I_(n+1)=NOPAT_(n+1)*gT/r`, `TV_n=FCFF_(n+1)/(r-gT)`.
  Thus `TV_n=NOPAT_(n+1)/r`, but still refuse gT≥r or nonfinite/nonpositive
  rates before division. Export terminal NOPAT, investment and FCFF separately,
  along with distance to the Gordon pole; guard finite outputs.
  Converging to the hurdle is deliberately conservative and can understate
  high-return companies such as Mips and Evolution.
  The direct engine's explicitly named `legacy-capped-revenue-growth-v13`
  convention remains solely for historical arithmetic controls, never the public
  loader. Current exact replay cannot reinterpret incompatible old runs.
* **Wiring:** `alphaforge/cli/ranking_loader.py:load_results_for_company`
  composes existing report selection and policy normalization into a frozen
  `DcfNormalizedFinancialView` and versioned `DcfInputQualityDecision` before
  `ReverseDcfEngine` → `DcfValue`
  (enterprise/equity/value per share, terminal value, 5 `ProjectedCashFlow`
  with `fcff`/`discounted_fcff`). Inputs follow the deterministic flow's
  cutoff-filtered verified-date contract, **not historical-known-then PIT**.
  Quality output exposes expected and selected spans, excluded and missing
  periods, dates/durations/currencies, evidenced selection anomalies and metadata
  unknowns. Current rejected annual and R12 candidates remain source-attributable
  excluded evidence; missing, malformed, or raw/stored-conflicting fiscal ends,
  publication dates, and fiscal years are emitted as unknown rather than trusted.
  Rejected R12 candidates do not expand the expected annual span. Unknown starts
  remain visible rather than becoming proven duration defects. The versioned
  sufficiency rule is
  `dcf-input-quality-v1-two-qualified-consecutive-annual-periods`, grounded in the
  existing consecutive-annual revenue and EBIT operand contracts; no three- or five-year
  minimum or guessed confidence is added. This DCF-only decision does not alter
  heuristic score calculation. Rejected market inputs cannot drive valuation.
  DCF market cap, enterprise value, and
  the required-return hurdle come from the selected DCF report (latest R12, else
  latest annual); the heuristic `valuation_score` keeps the latest-report basis.
  `reverse_dcf` dict carries `dcf.available`, consistent `status`, `reason`,
  `warnings`, `version`, and `contract_version`, plus `assumptions`, legacy
  `assumption_sources`, and typed per-field `assumption_provenance`
  (`fixed_default`, `company_history`, `report_evidence`, `market_evidence`, or
  `qualified_calibration`) with evidence references and explicit limitations.
  Report references cover only the reports consumed by each growth or margin window
  and identify company, period type, and fiscal end. Discount-rate references bind
  the selected DCF report's share operand, selected market price, and every split
  event consumed to adjust those shares. Calibration references are limited to the four
  qualified operands: normalized EBIT, tax rate, and beginning and ending capital;
  unrelated source entries are not exported. No numeric confidence is added.
  `required_return {policy_version, market_cap, size_bucket, required_return,
  source_date}` retains the hurdle decision inputs and identity.
  `projected_cash_flows` with next-year profit, profit growth, incremental return,
  investment amount/ratio, plus the terminal cash-flow bridge. Implied margin is
  **unavailable** because changing it violates the constant-margin basis. Implied
  terminal growth is **not identifiable** and is not solved: mature growth creates
  no excess-return value once q=r. A linked explicit transition endpoint can still
  change cash flows **before convergence**; this is not a claim that the whole
  forecast is mathematically invariant. Both axes export empty root lists without
  endpoint prices or implied assumptions. A direct terminal-growth scenario must
  link the explicit growth endpoint to gT; it does not re-enable solving.
  **Admissible initial-growth domain (S-M4):** `growth_domain.py` derives the
  five-year mature funding interval before observing candidate prices or roots. The
  retained `[0,.30]` registry declaration is a search-coverage scope, not a maturity
  threshold or a financing guarantee. `DcfAssumptionPolicy.SOLVE_BOUNDS` is a
  compatibility scope declaration only; replay exports it as `solve_scope_bounds`.
  Actual executable and requested bounds belong to each solve eligibility record.
  For fixed mature growth t, initial marginal-return assumption q0 and hurdle r:

  ```text
  U1 = (q0 - .25*t)/.75
  U2 = (.75*q0 + .25*r - .50*t)/.50
  U3 = (.50*q0 + .50*r - .75*t)/.25
  U = min(.30,U1,U2,U3)
  A = [0,U], provided t <= .25*q0+.75*r and 0 <= t < r
  ```

  Each constraint is the existing end-of-period funding rule
  `0 <= investment <= NOPAT`, or `0 <= next profit growth <= q`, over all six
  funding rows. Positive constant margin and tax below one let revenue scale cancel
  in those inequalities. Negative initial growth still refuses capital release;
  unfunded growth still refuses financing. No cash/debt recycling, spending cap,
  new year-zero spending, high-growth transition or margin economics are introduced.
  Certificate construction requires finite positive revenue, shares and current price,
  finite net debt, a supported branch value, the fixed five-year convention/endpoints,
  positive margin/returns and a legal tax rate. Ordinary route, history, calibration and
  provenance qualification still precedes derivation; after those prerequisites pass,
  invalid, empty or degenerate domain outcomes are reported before missing interval-wide
  coverage. An empty interval (`empty_admissible_domain`) or singleton
  (`degenerate_admissible_domain`) refuses search rather than being widened.
  Affine constraints establish a continuous real-economic interval; hypothetical
  non-affine/holey domains require a different approved contract, not sampled clipping.

  **Explicit interval-wide review is required.** The canonical evaluator assumes
  year-one capital is already installed; it does not fund R0→R1. A forward calibration's
  starting-capital premise does not automatically cover every reverse candidate.
  A trusted analyst must append a reviewed calibration with `reverse_growth_coverage`:
  `version="reverse-growth-domain-v1"`, `scope="full_derived_domain"`,
  `capital_timing="year_one_installed"`,
  `constant_margin_and_return_path="conditional_for_full_derived_domain"`,
  nonempty `starting_capacity_rationale` and `approval_id`, and `approved_on <= as_of`.
  Review must explain installed year-one scale R0*(1+x), fixed positive margin,
  maintenance, capital perimeter and return assumptions throughout A. This is a
  conditional analyst premise, not automatically verified spare capacity.
  No acquisition, migration or loader fills it in. Missing/stale coverage leaves
  forward valuation independent but reverse growth `insufficient_evidence`.

  Coverage `fixed_basis` must exactly match `growth_fixed_basis(inputs)`:
  company/currency/million units, current_revenue, ebit_margin, tax_rate,
  projection_years, terminal_growth, discount_rate, reinvestment_return,
  economic_convention, packet_hash, route_decision_identity, selected_history_identity
  and calibration_operands_identity. The latter hashes calibration JSON excluding
  coverage, avoiding a circular hash; final calibration identity includes the review.
  History binds qualified operands and quality facts without DB surrogate ids.
  Qualification checks dates, structure and exact basis, not accounting truth.
  At the latest fiscal period, genuinely different calibration operand identities remain
  ambiguous. For one shared operand identity, fixed-basis construction is independent of
  coverage and does not require an unreviewed predecessor. The sole coverage review matching
  that basis is selected; stale or malformed reviews remain retained but do not block a
  corrected append. When no review matches, qualified forward valuation survives while reverse
  coverage remains unavailable. Multiple matching current reviews remain ambiguous. No old
  record is edited; append order does not choose the review.

  Derivation uses exact rational arithmetic over captured finite float operands and
  directed inward conversion to executable float endpoints. Public rank samples the
  **full** derived interval (200 intervals/201 points), using price tolerance `1e-6`,
  assumption tolerance `1e-10`, at most 200 bisection iterations and the canonical
  `value()` for each sample and bisection candidate. These settings are certificate- and
  run-identity inputs; this policy version refuses caller overrides. An explicitly
  restricted lower-level request
  is separately recorded, identity-bound, and must lie wholly inside A. Out-of-domain
  requests refuse before sampling: never silently intersect, skip invalid samples or
  select a root-bearing subinterval. Internal brackets must remain inside that request.
  Real-economic certification is not machine-safety or root-completeness proof.
  Any actually evaluated invalid candidate discards the entire axis result, including
  earlier candidates. Closed investment=NOPAT equality is economically allowed, but
  strict canonical rounding failures at an endpoint still refuse with
  `invalid_candidate_economics`/underlying error and boundary detail: no epsilon,
  inward retry, or alternate price evaluator. Forward arithmetic remains unchanged.

  With q0=.20,r=.15,t=.02, the bounds derive to approximately [0,.26]: row one
  binds; .261 and .30 require unsupported financing. In the qualified synthetic
  fixture, price13 yields a conditional initial-growth candidate near .04215994;
  price10/30 yield honest no sampled crossing on the same interval. Bounds/certificate
  exclude target price and the forward growth estimate; full solve input identity still
  binds both. A new live price crossing a market-cap hurdle bucket legitimately changes
  fixed r and can change admissibility; it is not a target-driven domain adjustment.
  q0=.15 and .02 demonstrate strict rounded boundary refusals despite real-economic
  equality. See `tests/test_growth_admissible_domain.py` for public export/CLI replay,
  no-crossing, coverage, boundary, restricted-request and tamper acceptance.

  The shared `solve_eligibility.py` registry remains the domain/prerequisite/interpretation
  owner. Both direct solve and diagnostics require verified prerequisites, fixed-assumption
  values/provenance, the recomputed funding certificate and complete current-input binding.
  Omitting eligibility refuses; default provenance requires policy values, recorded
  limitations and no fabricated source references. Authentic issuers lacking qualified
  packet/route/history/capital/interval-wide review remain unavailable; neither numerical
  solving nor evidence-root portability grants source qualification.
  Canonical results retain conditional sign-change candidates, endpoint matches, and sampled
  tolerance regions distinctly, including sign-change candidates that coexist with a sampled
  match region. Point matches already represented by a sign-change bracket are omitted from
  `sampled_match_points`, and `sampled_match_point_count` always counts that exported collection.
  No candidate is selected as a unique answer: finite sampling cannot establish uniqueness,
  completeness, tangency, or roots/extrema between samples.
  Base and candidate results also flag
  negative modeled equity as non-tradable; limited-liability and turnaround option
  value remain outside this FCFF model.
* **Explicit router (S-M3):** `alphaforge rank --dcf-routing-json <file>` may receive a
  JSON object with canonical decimal company-id keys; every key must name a selected
  company, and duplicate or noncanonical keys are rejected. Each value supplies `archetype`,
  `forecast_profile`, and `evidence_references`. To admit an `operating_company` + `mature`
  route, every reference must resolve to a catalogued source and an anchor owned by that
  source in the valid frozen evidence packet. Returned references use the packet's URL,
  publication date, observation date, and attachment checksum; any supplied values for
  those fields must match. Only that admitted route exposes the existing FCFF
  policy/evaluator. Omitted routing stays `unknown`; `GENERAL` and sector branch
  classification never imply mature eligibility. Financial/bank, property, resource,
  holding/unusual, unknown/mixed, high-growth, cyclical and other unsupported routes refuse
  with specific reasons. Routing does not classify companies, alter sector scoring/readiness
  rules, or bypass quality, calibration, economic, price, share or net-debt requirements.
  The canonical DCF result records route method/profile/evidence/decision identities;
  the decision identity binds the resolved evidence identity as well as the supplied input
  identity and routing outcome. Capture and replay follow the
  [executed-run contract](executed-run-replay.md).
* **Export:** `alphaforge rank` retains original outputs, then writes
  `exports/runs/<artifact_id>/dcf.json` alongside `ranking.json/csv`; the date directory
  remains a mutable latest alias. [Executed-run replay](executed-run-replay.md)
  binds inputs and exact code/rule assumptions before consumption; `ranking_loader` also returns top-level
  `dcf` / `reverse_dcf` so callers do not need to reach into
  `candidate.full_results`. Every non-valued path emits a structured
  unavailable result (`dcf.available=false` with `missing_information` and
  top-level `status="unavailable"`), including outer DCF wiring failures. The
  pure `core/valuation/dcf_contract.py` serializer produces loader DCF records;
  exports and replay preserve those same records, including `input_quality` and
  `normalized_financial_view`. It rejects non-finite serialized
  values. Its status distinguishes unsupported, invalid input, insufficient
  evidence, domain unavailable, sampled matches/regions, candidate solutions, no crossing,
  and nonconvergence while preserving
  detailed reasons; the explicit non-SEK hurdle refusal is `unsupported`, not
  missing evidence.
  The `dcf-result-contract-v3-admissible-growth`,
  `reverse-dcf-solve-registry-v3-admissible-growth` and `reverse-growth-domain-v1`
  identities, scope/conversion/boundary/sampling policies are part of replay rules.
  Old outputs remain immutable; incompatible rules/code refuse replay rather than
  reinterpreting a historical refusal or injecting a coverage approval.
* **Provenance:** migration 019 adds append-only calibration records; trusted analyst
  admission uses `alphaforge.db.reinvestment.append_reinvestment_calibration`.
  There is deliberately no public acquisition command. The loader reports rejected
  candidate identities/reasons and applies the append-only current-basis review selection
  above. Capture retains **all** calibration candidates, their exact source/accounting
  record JSON and content identity, not just the selected one. Numerical encoding v2
  binds calibration/economic-policy identities and source digests; older incompatible
  runs refuse `unsupported_rules_or_code` without changing their stored artifacts.
  No actual calibration was admitted for Evolution, Clas Ohlson, Mips or Bactiguard;
  see the [bounded evidence investigation](plans/dcf-positive-profit-economic-decision.md).
  Synthetic test disclosures are not issuer evidence.
  `policy_version`, `size_bucket`, `market_cap`, `reinvestment_return`,
  `normalization {confidence, selected_window_years, reasons}`, `input_quality`,
  `normalized_financial_view`, `warnings`, and `missing_information` are persisted;
  heuristic score and DCF are never merged. The input-quality retention and rules
  identity are specified by [executed-run replay](executed-run-replay.md).

## Conditional supplied-schedule forward FCFF research

`alphaforge forward-fcff --input-json FILE --exports-dir DIRECTORY` evaluates
`forward-fcff-schedule-v1` through the pure
[`forward_fcff.py`](../alphaforge/core/valuation/forward_fcff.py) interface.
[`forward_fcff_contract.py`](../alphaforge/core/valuation/forward_fcff_contract.py)
owns the closed, canonical frozen request and provenance/source/review resolver.
The executable source-free request examples are in
[`forward_fcff_fixtures.py`](../tests/forward_fcff_fixtures.py); the separate
[retention/replay contract](executed-run-replay.md#forward-fcff-research-lane)
owns publication and private reference acceptance.

This additive lane accepts **original SEK or EUR**, one currency per request,
amounts in millions, absolute basic shares, nominal annual rolling periods and
end-year discounting. It requires a supplied same-currency annual-effective,
financing-neutral **asset return, financing shields excluded**. It never uses the
SEK market-cap hurdle as an EUR rate. Mature/reverse/ranking/readiness currency,
required-return and refusal policies above are unchanged. No FX, WACC, APV,
issuer builders, acquisition, calibration, reverse extension or model calls.

Each material numerical operand is `{value, provenance}`; null is unknown, not
zero. Provenance retains source facts separately from signed analyst adjustments
and forecast assumptions, their rationale/mechanism/effective periods,
uncertainty, actual reviews and limitations. A consumed operand marked
`unresolved` withholds its dependent output even when it carries a finite
placeholder; selected analyst estimates and hypothetical sensitivities remain
conditional. Every material limitation names at least one of the four outputs;
unresolved targets withhold that output plus its dependents, while fully specified
conditional-premise targets must be explicitly listed in `conditional_outputs`.
Source references must resolve to retained catalog bodies and owned anchors.
Catalog sources retain publication, observation (or explicit unknown) and UTC
ingestion separately. `prepared_on` is actual preparation date;
`evidence_cutoff` controls fact publication. A cutoff later than valuation
requires `retrospective_research`. A review predating a cited fact's publication
is retained as a chronology diagnostic but cannot qualify the output as human
reviewed. Empty reviews remain unreviewed; numerical consistency is never
accounting approval.

The envelope supplies company/scenario identity, object/perimeter/acquisition
boundaries, accounting/earnings/capitalized-debt-like lease basis, denomination,
evidence catalog plus hash, limitations/reviews, and an operating schedule.
Annual periods start the day after valuation and remain consecutive full rolling
years whose inclusive ends repeat the valuation boundary's month/day, clamped
for February. Thus a 2024-02-28 boundary produces 2024-02-29 through 2025-02-28,
so the leap day remains in the full annual interval instead of shortening and
shifting every later fiscal endpoint. Core imports no DB/provider/model layer.

Rows supply cash operating profit C, compatible D&A D, other noncash charge N,
identified positive cash purchases I and economic lease additions L, disposal
cash V, WC opening/closing states and otherwise-unembedded cash settlements X.
Each settlement declares its bridge category and schedule period so count-once
links reconcile category, direction, period and amount. Aggregate class
rollforwards reconcile commissioning, advances, transfers,
D&A/impairment/disposal/other and closing active/construction balances. Transfers
require dated transfer events. Optional cohorts validate supplied finite weights,
residual and life; no default life or builder. Noncash items distinguish asset
impairments from other charges; recurring stock compensation has no automatic
free-cash addback. Financing principal/interest are diagnostics, not FCFF.

Gross operating-unlevered tax ledgers supply taxable-basis reconciliation,
current arising, refund entitlement, payable/receivable states, payment/refund,
writeoff and explicitly allocated lot movements. Multiple jurisdictions/entities
require earnings/D&A allocation that sums to the operating perimeter. Lots
identify `boundary_existing` period-zero valuation-date balances versus
`forecast_created` positive-period amounts, eligible dates, prerequisites and
each annual cash allocation. Allocation chronology is evaluated across all
ledgers in a row, using actual cash dates or the supplied date-less ordering
premise; ledger identifiers are only deterministic tie-breakers. No tax expense
shortcut, legal setoff, negative-profit refund rule or deadline-to-payment
inference. Writeoff has no second cash charge; deferred cash must be explicitly
located, not deducted again as book DTL. Annual eligibility/order assumptions
remain conditional.

```text
EBIT = C - D - N
FCFF_operations = EBIT + D + N - (P - R) - I - L + V - delta_WC - X
                = C - (P - R) - I - L + V - delta_WC - X
```

P/R exclude opening boundary lots. Opening claim cash and claim-adjusted FCFF
are disclosed separately; they do not alter operating value. Gross investment,
net book investment and net cash reinvestment are separate diagnostics, never
three competing deductions. Economic lease addition is not necessarily upfront
cash payment. Deficits are retained; operating value needs explicit continuing
capacity and funding-access premises, not a cash-solvency forecast.

Optional Gordon continuation requires **full N+1 and N+2 rows**, independently
supplied geometric primitive templates, stationary active-class renewal/balance
premises, exhausted finite states and mature gross tax queues/kernels. Every
material primitive/gross leg is checked, not merely net FCFF equality. Queue
source years must be in the selected geometric accrual regime, with correct
remaining lots; payment/refund recurrences verify separately. The caller chooses
N; failed transitions refuse, never silently extend. Acceptance pairs preserve
growth, kernels, perimeter, primitive definitions and absolute economic regime
starts. Only the one-year horizon/tail coordinate and independently reconciled
seed, queue and finite-state advancement may differ. Refusal certificates retain
failures and a diagnostic full next-year row, without a terminal-PV scalar.

```text
explicit_PV = sum(F_t / (1+r)^t, t=1..N)
TV_N = full_scheduled_F_(N+1) / (r-g), with -1 < g < r
operating_value_shields_excluded = explicit_PV + TV_N / (1+r)^N
```

Witness rows do not enter explicit PV. No transitional `F_N*(1+g)`, qT=r,
NOPAT/r, exit-multiple or zero-tail substitution. Complete finite runoff with
zero residual operating/tax/WC/asset states is the alternative; an incomplete
schedule exposes explicit PV **only**, not EV. Versioned control rounding is
`1e-8 * max(1, abs(lhs), abs(rhs))`, solely for reconciliation, never signs,
rate/growth admission, eligibility or exact replay.

Four distinct outputs expose conditional/insufficient-evidence/invalid-input/
unsupported/not-requested status, reasons, missing inputs, conditions and values:
operating cash rows, operating value, common equity and basic per-share. Optional
rate/tail/bridge/ownership refusal does not poison valid upstream cash rows.
A bridge needs a dated same-currency complete category inventory, already-valued
claim PVs or separately discounted boundary cash, and count-once event allocation.
Known opening tax claims cannot be declared absent; restricted cash/obligation
pairs must be excluded together. Missing material cash/consideration/deferred/
pension/legal/noncommon claims withhold **equity and per-share**, not operating
value. A missing basic count alone withholds per-share, not equity. Externally
valued rights provide an exact, non-overlapping cover of noncommon bridge claims
and are subtracted once with basic shares; exercise dilution and option pricing
are unsupported. Negative equity is retained as nontradable, not floored.

All results are conditional supplied-premise research. In particular, reproducing
the corrected Evolution operating reference is **not a completed Evolution equity
valuation or financial sign-off**. Authentic translations, sources and receipts
remain private; only source-free independent tests and portable instructions ship.

## Börsdata field mapping (anti-leakage seam)

Only `alphaforge/core/kpi_taxonomy.py:REPORT_FIELD_MAP` may contain Börsdata
raw keys.

Live `GET /v1/instruments/reports` returns (2026-09-22, Clas Ohlson 51):

* `total_Equity` (not `book_Value`) → `equity`
* `net_Debt` (already net, not gross `total_Debt`) → dedicated `net_debt`
  column (`financial_periods.net_debt`); `total_debt` stays for legacy gross
  debt and is **not** filled from `net_Debt`.
* `cash_Flow_From_Operating_Activities` (not `operating_Cash_Flow`) →
  `operating_cash_flow` (similarly investing/financing)

`ebitda` is never populated from reports (Börsdata field absent) and
`total_Debt` (gross) is never returned — both stay explicitly `NULL`
with no synthetic guess. Net-debt/EBITDA uses **KPI 42** only.

Historical `ev_ebit` guardrails require `enterprise_value = market_cap + net_debt`
with ≥5 positive history points; before the `net_debt` fix they were always
`None` because `enterprise_value` was `NULL`.

## KPI 37 / 42 (ROIC, net-debt/EBITDA)

Live `GET /v1/instruments/{id}/kpis/{kpiId}/{reportType}/{priceType}/history`
returns 10 rows for 37/42, but `GET /v1/instruments/{id}/kpis/{reportType}/summary`
omits them (Clas live: 42 ids, no 37/42). The per-company workflow in
`alphaforge/cli/kpi_sync.py` fetches `history` for `KpiIds.ROIC` +
`NET_DEBT_EBITDA` directly even when summary omits them; genuinely unavailable
KPIs (400/empty) remain missing
without error.

## Integrity

* No guessed fundamentals: absent `ebitda`, gross `total_Debt`, or KPI history
  stays `NULL` / missing, surfaced in `missing_data` and `dcf.missing_information`.
  Missing qualified operating-return/capital calibration makes growth-based FCFF
  unavailable; no zero-reinvestment valuation or implied roots are emitted.
* Deterministic: identical selected inputs under identical supported rules →
  identical `DcfValue` and `valuation_score`. New executed ranking runs retain
  immutable numerical bodies, exact code/rules and original outputs for
  [audit replay](executed-run-replay.md); full financial vintages and arbitrary
  historical-known-then reconstruction are not implemented.
* Credentials never appear in exports or logs (`authKey` redacted in adapter).
