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
* **Model version:** `RankingEngine.RANKING_MODEL_VERSION = "2026-08-12-reverse-dcf-v11"`
  (v11 wires DCF but keeps `valuation_score` as the heuristic; DCF is exported
  separately).

## Auditable DCF (policy + engine)

* **Policy:** `alphaforge/core/valuation/dcf_policy.py`
  (`VERSION = "reverse-dcf-v11-market-cap-hurdle"`) — 5-year projection,
  `tax_rate 21%`, `terminal_growth 2%`, revenue CAGR clamped `[-5%,15%]`,
  EBIT margin revenue-weighted over 3–5 annuals, reinvestment from
  **Börsdata ROIC (KPI 37, percent)** divided by 100 internally, discount
  from `RequiredReturnPolicy` market-cap buckets in **absolute SEK**
  (market cap from `price × shares` is in MSEK — scaled ×1e6 for bucket
  selection). Missing ROIC (KPI 37) is provisional, not fatal: the DCF stays
  `available` with net reinvestment 0%, lowered confidence, a warning, and
  `missing_information=("roic",)`.
* **Engine:** `alphaforge/core/valuation/reverse_dcf.py`
  (`ReverseDcfEngine.value/solve`) — projects `revenue → ebit → nopat → fcff`
  with linear fade of `revenue_growth` and `ebit_margin`, then
  `terminal_value = terminal_fcff / (discount - terminal_growth)`.
* **Wiring:** `alphaforge/cli/ranking_loader.py:load_results_for_company`
  builds `DcfPolicyDecision` from PIT-filtered annuals and `kpi_observations`
  (37), then `ReverseDcfEngine` → `DcfValue` (enterprise/equity/value per
  share, terminal value, 5 `ProjectedCashFlow` with `fcff`/`discounted_fcff`).
  PIT means `year <= cutoff.year AND observation_date <= as_of`; KPI 37/42
  prefer R12 over annual explicitly. DCF market cap, enterprise value, and the
  required-return hurdle come from the selected DCF report (latest R12, else
  latest annual); the heuristic `valuation_score` keeps the latest-report basis.
  `reverse_dcf` dict carries `dcf.available`, `assumptions`,
  `assumption_sources`, `required_return {size_bucket, required_return}`,
  `projected_cash_flows`, plus `implied` solves for
  `revenue_growth / ebit_margin / terminal_growth` within
  `SOLVE_BOUNDS (-10..30%, 0..50%, -1..4%)`.
* **Export:** `alphaforge rank` writes `exports/<as_of>/dcf.json`
  alongside `ranking.json/csv`; `ranking_loader` also returns top-level
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
  stays `NULL` / missing, surfaced in `missing_data` and `dcf.missing_information`
  — except missing ROIC, which yields a provisional available DCF at 0%
  reinvestment (see policy above).
* Deterministic: identical PIT inputs → identical `DcfValue` and `valuation_score`.
* Credentials never appear in exports or logs (`authKey` redacted in adapter).
