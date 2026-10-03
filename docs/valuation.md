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
* **Model version:** `RankingEngine.RANKING_MODEL_VERSION = "2026-10-03-dcf-availability-diagnostics-v18"`
  (v18 records the dated-positive-ROIC availability contract and reverse-DCF
  diagnostic output; `valuation_score` remains heuristic and DCF separate).
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
  (`VERSION = "reverse-dcf-v13-dated-roic-availability-diagnostics"`) — historical growth
  uses the latest consecutive positive-revenue suffix without bridging missing
  or nonpositive observations; this calculation is distinct from v11.
  An unresolved annual slot inside the selected fiscal span removes historical
  growth authority; the existing explicit zero-growth fallback remains recorded
  in assumption sources, never a CAGR across that uncertain span.
  5-year projection,
  `tax_rate 21%`, `terminal_growth 2%`, revenue CAGR clamped `[-5%,15%]`,
  EBIT margin revenue-weighted over 3–5 annuals, reinvestment from
  **usable dated finite positive Börsdata ROIC (KPI 37, percent)** divided by 100 internally, discount
  from `RequiredReturnPolicy` market-cap buckets in **absolute SEK**
  (market cap from `price × shares` is in MSEK — scaled ×1e6 for bucket
  selection). Without usable dated finite positive ROIC, ordinary growth-based
  FCFF is unavailable with `missing_information=("dated_positive_roic",)`;
  no zero-reinvestment value or implied roots are emitted. Negative NOPAT with
  ROIC-based reinvestment is unavailable rather than described as cash released
  by negative investment. Report `currency` at calculation is verified
  values currency; original currency, conversion mode/target and original→target
  ratio remain separate provenance. Compatible non-SEK raw multiples may be
  available, but DCF retains its SEK-only required-return refusal and does not
  convert report values or ratios.
* **Engine:** `alphaforge/core/valuation/reverse_dcf.py`
  (`ReverseDcfEngine.value/solve`) — projects `revenue → ebit → nopat → fcff`
  with linear fade of `revenue_growth` and `ebit_margin`, then
  `terminal_value = terminal_fcff / (discount - terminal_growth)`.
* **Wiring:** `alphaforge/cli/ranking_loader.py:load_results_for_company`
  builds `DcfPolicyDecision` from validated consecutive annual fiscal history
  and dated `kpi_observations` (37), then `ReverseDcfEngine` → `DcfValue`
  (enterprise/equity/value per share, terminal value, 5 `ProjectedCashFlow`
  with `fcff`/`discounted_fcff`). Inputs follow the deterministic flow's
  cutoff-filtered verified-date contract, **not historical-known-then PIT**.
  Rejected market inputs cannot drive valuation. DCF market cap, enterprise value, and
  the required-return hurdle come from the selected DCF report (latest R12, else
  latest annual); the heuristic `valuation_score` keeps the latest-report basis.
  `reverse_dcf` dict carries `dcf.available`, `assumptions`,
  `assumption_sources`, `required_return {size_bucket, required_return}`,
  `projected_cash_flows`, plus `implied` solves for
  `revenue_growth / ebit_margin / terminal_growth` within
  `SOLVE_BOUNDS (-10..30%, 0..50%, -1..4%)`. Exports include the operative
  growth-fade endpoint and label each implied result as a conditional one-variable
  solve. Endpoint prices, deterministic sampled ranges, above/below direction,
  target-denominated boundary gaps, and sampled monotonicity/interior extrema are
  reported without claiming that a finite scan proves the full range. Near-bound
  terminal-growth roots and discounted-terminal-value dependence are qualifications,
  not economic conclusions.
* **Export:** `alphaforge rank` retains original outputs, then writes
  `exports/runs/<artifact_id>/dcf.json` alongside `ranking.json/csv`; the date directory
  remains a mutable latest alias. [Executed-run replay](executed-run-replay.md)
  binds inputs and exact code/rule assumptions before consumption; `ranking_loader` also returns top-level
  `dcf` / `reverse_dcf` so callers do not need to reach into
  `candidate.full_results`. Every non-valued path emits a structured
  unavailable result (`dcf.available=false` with `missing_information` and
  top-level `status="unavailable"`), including outer DCF wiring failures.
* **Provenance:** `policy_version`, `size_bucket`, `market_cap`, `reinvestment_return`,
  `normalization {confidence, selected_window_years, reasons}`, `warnings`,
  `missing_information` are persisted; heuristic score and DCF are never merged.

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
omits them (Clas live: 42 ids, no 37/42). `alphaforge/cli/main.py:cmd_sync`
now fetches `history` for `KpiIds.ROIC` + `NET_DEBT_EBITDA` directly even when
summary omits them; genuinely unavailable KPIs (400/empty) remain missing
without error.

## Integrity

* No guessed fundamentals: absent `ebitda`, gross `total_Debt`, or KPI history
  stays `NULL` / missing, surfaced in `missing_data` and `dcf.missing_information`.
  Missing usable dated positive ROIC makes growth-based FCFF unavailable; no
  zero-reinvestment valuation or implied roots are emitted.
* Deterministic: identical selected inputs under identical supported rules →
  identical `DcfValue` and `valuation_score`. New executed ranking runs retain
  immutable numerical bodies, exact code/rules and original outputs for
  [audit replay](executed-run-replay.md); full financial vintages and arbitrary
  historical-known-then reconstruction are not implemented.
* Credentials never appear in exports or logs (`authKey` redacted in adapter).
