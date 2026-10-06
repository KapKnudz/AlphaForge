# Valuation — heuristic score vs auditable DCF

AlphaForge keeps two valuation surfaces deliberately separate so a heuristic
ranking never masquerades as a discounted-cash-flow.

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
  (`VERSION = "reverse-dcf-v16-explicit-input-quality"`) — historical growth
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
  Original bounds remain recorded `(-10..30%, 0..50%, -1..4%)`. The revenue-growth
  range contains unsupported contraction/financing candidates, so automatic range
  diagnostics refuse with `invalid_candidate_economics`, no endpoint prices or roots.
  Bounds are not narrowed and invalid candidates are not skipped to manufacture a root.
  Direct arithmetic calls on a fully admissible positive-growth range retain deterministic
  sampled diagnostics; revenue growth means initial growth fading to the fixed mature
  endpoint, not constant CAGR. The shared `solve_eligibility.py` registry declares each
  axis domain, evidence prerequisites, refusal reason, and root interpretation. Both direct
  solve and range diagnostics refuse before sampling unless supplied eligibility has every
  prerequisite verified and its fixed-assumption values and provenance match the current
  inputs. The eligibility identity binds numerical inputs to company, frozen packet, route,
  and the exact exported evidence record; omitting eligibility creates an unverified decision
  and therefore refuses. Provenance validation accepts policy-approved defaults only with
  their recorded limitations and no fabricated source references.
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
  The v2 result-contract and `reverse-dcf-solve-registry-v2` identities are part of the
  replay rules identity, so old incompatible outputs are refused rather than reinterpreted.
* **Provenance:** migration 019 adds append-only calibration records; trusted analyst
  admission uses `alphaforge.db.reinvestment.append_reinvestment_calibration`.
  There is deliberately no public acquisition command. The loader reports rejected
  candidate identities/reasons and refuses conflicting reviews for the same latest
  period. Capture retains **all** calibration candidates, their exact source/accounting
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
