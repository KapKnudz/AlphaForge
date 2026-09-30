# Deterministic flow contract

This describes the **implemented** stored-market-data path. Start with the
[architecture overview](architecture.md); [valuation.md](valuation.md) owns
valuation formulas and policies, and [evidence-flow.md](evidence-flow.md) owns
textual selection and hashing. The [MVP plan](plans/2026-09-16-alphaforge-mvp.md)
remains the target design. Alignment notes below distinguish gaps from guarantees.

## 1. Acquisition and persistence

[`cli/main.py`](../alphaforge/cli/main.py) owns these executable entry points:

```sh
alphaforge import-watchlist --file imports/watchlist.csv
alphaforge sync --all
alphaforge rank --as-of YYYY-MM-DD
```

The CSV path is an operator-supplied file, not a shipped fixture. `--dsn` is a
**global** CLI option (before the subcommand); otherwise `ALPHAFORGE_DSN` selects
the store. [`config.py`](../alphaforge/config.py) defaults to the worktree-relative
`data/alphaforge.db`; [`db/connection.py`](../alphaforge/db/connection.py) opens
SQLite with WAL and foreign keys, and commands run migrations before use.

Import preserves source-file/normalized-row identity and matches ISIN, then
case-insensitive ticker, then Börsdata ID. Unmatched rows can be linked after
instrument seeding by `relink_watchlist`. `sync --all` prefers matched imported
watchlist companies; absent matches it can sync the seeded instrument universe.
`rank` uses the joined DB watchlist, or resolves tickers from `--watchlist CSV`.
Neither command enforces Sweden-only/positive-earnings membership: that remains
an operator curation requirement in the plan.

[`BorsdataAdapter`](../alphaforge/providers/borsdata/adapter.py) normalizes known
response envelopes and raises `BorsdataContractError` for unrecognized shapes.
Reports are batched at most 50 and fetched with `original=0`; prices are fetched
without trusting provider `maxCount` (optional slicing is local). Sync stores
reports, prices, annual/R12 KPI history, dividends, splits, report calendar,
reference/metadata caches and available Börsdata short snapshots. KPI 37/42
history is requested directly even if summary discovery omits it. Unsupported
KPI responses can remain missing. Integration retries belong to
[`providers/http.py`](../alphaforge/providers/http.py); sync records branch job
outcomes and isolates many per-company failures, not one all-or-nothing fleet
transaction. Each per-company dividend batch is savepoint-atomic: any date, type
or amount conversion or database-constraint failure rolls back that batch without
discarding unrelated pending caller work. `sync --as-of` does **not** trim
acquisition to that date.

### Authoritative stored input identities

[`db/alphaforge.sqlite.sql`](../db/alphaforge.sqlite.sql) plus
[migrations](../db/migrations/) own keys and constraints;
[`db/repositories.py`](../alphaforge/db/repositories.py) owns writes.

| Input | Identity / retained meaning |
| --- | --- |
| Company / watchlist | `companies.id` joins both paths; unique `borsdata_id` anchors provider identity. Watchlist retains `(source_file, source_row_hash)`, `matched_via` and a unique linked `company_id`; ticker is not the financial-row key. |
| Financial report | `(company_id, period_type, period_end)` with `year/r12/quarter`; `report_date`, `report_year/report_period`, currency/FX metadata, `is_placeholder` and `raw_payload` accompany canonical fields. Inputs without a valid explicit fiscal end are retained with their raw payload and refusal reason in `financial_period_rejections`, never keyed by publication date. |
| KPI | Company, KPI ID, period/price type, then observation date for `last`, or year/report period for `year/r12`. The writer handles missing report-period keys explicitly for idempotence. |
| Price | `(company_id, price_date)`: positive close, nullable nonnegative volume, currency; no OHLC history. |
| Dividend | `(company_id, ex_date, dividend_type, amount)`; ex-date is parsed and stored as a canonical ISO calendar date, and currency, its verification/conflict bits and distribution frequency are retained. Types `0/1/2/4` accepted; dated zeros are preserved, while missing amounts/dates and undated zero markers are ignored. Missing or unusable currency starts unverified, never assumed SEK, and the first authoritative supported observation replaces unverified conflict-free provenance, including valid-looking legacy tags. [Valuation](valuation.md#heuristic-valuation_score-ranking) owns the explicit MVP denomination allowlist. Legacy rows start unverified. Only differing verified supported currencies at an existing identity establish a sticky conflict, and repeat upserts cannot heal it. A missing/unusable fresh tag does not erase an already verified denomination; it also cannot verify a previously unknown row or establish coverage. |
| Dividend coverage | `dividend_window_coverage(company_id, window_start, window_end)` stores exact `(start,end]` status (`unknown/partial/complete`), source, independent assurance and verification time. Complete requires a nonempty assurance and verification time. Legacy `dividend_coverage` extrema are retained but never consumed as proof. |
| Split / calendar | `(borsdata_id, split_date)` / `(borsdata_id, release_date)`, with a company link when available. Ratios and calendar report types are retained, not inferred model inputs. |

Market-data upserts replace values at these keys; they are not append-only
vintages. Raw report/instrument payloads and acquisition timestamps provide
provenance, but `fetched_at` is not consistently refreshed on conflict. This
storage differs intentionally from the immutable textual-evidence history.

## 2. Units, currencies and missing values

Reports retain provider scale: monetary levels and shares are in millions;
close, EPS and dividends are per share. SEK monetary levels/derived market cap
are therefore MSEK, while the required-return bucket seam takes absolute SEK.
Börsdata KPI percentages (including ROIC) are percentage points; calculated
financial margins/growth are fractions. Valuation yield representations are
field-specific; consult [valuation.md](valuation.md) and
[`ValuationResult`](../alphaforge/core/valuation/types.py), not a blanket percent
conversion. Volume is a share count and ADTV is in price currency units.

`original=0` delivers monetary fields in stock-price currency while the report's
`currency` remains original-currency provenance. The writer retains
`currency_ratio`, copies a positive ratio into `fx_rate_to_sek`, and labels its
source `currency_ratio`; that column name does not independently verify that
the target currency is SEK. The raw valuation currency guard and SEK-only hurdle
are described in [valuation.md](valuation.md); current cross-currency wiring
limits are noted below. Ratios are never FX-converted by the ranking loader.

Missing fundamentals remain `NULL`/`None`; zero is a value, not missing.
Zero-revenue/unpublished stubs are quarantined as `is_placeholder=1`; other
unpublished reports also fail loader filtering. Absent live EBITDA and gross
debt are not invented from net debt. Missing score inputs produce diagnostics
(`missing_data`, `data_quality`, availability and eligibility), not zero-valued
fundamentals. A numeric diagnostic score can still be emitted for an ineligible
company. Yield availability/window/currency/coverage facts are returned by the
loader as `dividend_yield` and retained in every model's `scoring_audit`, including
ranking JSON and persisted scores; CSV carries the resulting score consequences,
not a standalone yield column. DCF unavailability and provisional inputs have their own structured
`missing_information`/warnings contract in [valuation.md](valuation.md).

## 3. Cutoff selection and calculation wiring

[`load_results_for_company`](../alphaforge/cli/ranking_loader.py) performs no
network fetch. Its effective date predicates are:

- Reports: `is_placeholder=0`, verified fiscal end and publication date,
  `period_end <= report_date <= as_of`. Every populated alias for publication,
  fiscal year/end/start and report period must parse and agree; contradictory
  or malformed aliases remain refusal provenance regardless of key order. ISO
  dates and datetimes are normalized to a calendar day only after the complete
  value parses successfully.
  Fiscal ends must match an explicit fiscal-end field in the retained raw
  payload; legacy publication-as-end keys and rows without source metadata are
  excluded. The writer skips rows
  without a valid explicit fiscal end rather than inventing a key from a
  publication date or year/quarter. Rows sort by fiscal end, publication date,
  then quarter/annual/R12 (R12 wins exact ties).
- Prices: verified `price_date <= as_of`; the latest eligible close must be
  **at most seven calendar days old**, inclusive. Historical valuation pairs
  each report with the last close at/before its verified fiscal end, also
  with an inclusive seven-calendar-day maximum gap. Older/missing pairs do
  not contribute to historical valuation anchors.
- KPIs: non-null value, `year <= cutoff.year`, and a verified non-null
  `observation_date <= as_of`. Undated/year-only history stays stored but is
  not numerical authority. Replacement values replace observation-date
  assurance atomically rather than inheriting an older row's date. Latest
  eligible values are collected per KPI; R12 overrides non-R12 history.
  Yearless `last` snapshots remain excluded.
- Dividend inputs: trailing calendar twelve months ending at `as_of`, with
  `(start,end]` ex-date bounds. The previous-year anniversary clamps February 29
  to February 28; e.g. `2028-02-29` uses `(2027-02-28,2028-02-29]`, and
  `2025-02-28` uses `(2024-02-28,2025-02-28]` (includes February 29).
  Out-of-window/future distributions do not participate. The loader also selects
  the exact company/window coverage assertion and latest eligible close; no
  company-currency fallback is applied. [Valuation](valuation.md) owns the
  calculation and availability policy. Börsdata currently supplies no
  trustworthy window assurance, so sync writes observations without changing
  independently owned coverage assertions. An absent assertion, HTTP success,
  and min/max dates never prove coverage. Fixture-backed independent complete
  windows are supported, not a claim of live completeness.
- Textual evidence: [`load_evidence_view`](../alphaforge/evidence/manifest_store.py)
  supplies the shared manifest/packet view. Do not replace it with raw document
  counts; [evidence-flow.md](evidence-flow.md) owns its cutoff, usability,
  fingerprint and retained-object checks.

These are **cutoff-filtered stored observations with verified applicable
dates**, not historical-known-then correctness or an ingestion-vintage query.
Generic [`point_in_time.py`](../alphaforge/core/point_in_time.py) helpers do not
replace these loader predicates. `SELECTION_VERSION` is
`verified-dates-consecutive-annual-v1`. The loader returns `selection` diagnostics
also carried by ranking JSON/CSV and `dcf.json`: rejected report/KPI provenance,
selected price date/age, historical pairings and annual fiscal ends/refusal
reasons. Refusal reasons also appear in ranking missing data and readiness
limitations. A quarter/R12 refusal is superseded only by a unique same-type row
matching its verified end or its fiscal year plus report period; otherwise only
chronology provably older than the latest same-type row becomes audit-only.
Stale prices are
retained only as diagnostic dates, not as available closes, raw multiples or DCF
inputs; current financial margins/balance facts can still be available.

The latest eligible report remains the current financial/heuristic basis.
Growth, per-share growth, dilution and consistency instead use the latest
annual anchor and the maximal latest **consecutive annual fiscal suffix**.
`period_type="year"` is authoritative; provider report-period labels do not
reclassify it. Fiscal-year labels must normalize to known, unique consecutive
integers, and verified ends must be annual anniversaries (including non-calendar
years and leap month-end). Explicit starts, when supplied, must describe
365/366-day periods. A gap, duplicate, stub, unverified date or currency mismatch
stops the suffix without bridging it; older omitted rows remain in selection
provenance and do not suppress a valid latest suffix. A defect at the latest
anchor leaves annual metrics unavailable. `broken_fiscal_year` alone is not a
stub flag and does not exclude a non-calendar fiscal series. Quarter/R12
insertions cannot change annual growth. No quarter YoY is supplied without a
matched pair.
Each total, per-share and share-count growth result carries its own actual validated
horizon; score descriptions use the horizon of the selected metric. Uncomputable
growth carries a zero-year horizon, not a fabricated year or rate.

The loader adjusts historical shares, including the annual anchor, into the
latest report's share basis using stored splits without changing raw rows
([`financial/per_share.py`](../alphaforge/core/financial/per_share.py)).
[`FinancialMapper`](../alphaforge/core/financial/mapper.py) and
[`FinancialCalculator`](../alphaforge/core/financial/calculator.py) own arithmetic;
`growth_available=False` suppresses unverified annual metrics, not current ratios.
DCF retains its separate latest-R12/else-annual current basis and receives the
same validated annual history for assumption policy. Calculation and assumption
ownership is detailed in [valuation.md](valuation.md), not redefined here.

Pure supporting APIs exist for
[liquidity](../alphaforge/core/coverage/liquidity.py),
[realized returns](../alphaforge/core/returns/total_return.py) and
[forward scenarios](../alphaforge/core/valuation/forward_scenario.py).
`rank` does not export calculated ADTV, realized returns or bear/base/bull bands;
readiness assesses forward-scenario inputs, not a generated scenario bundle.
The return helper currently uses an end-price reinvestment proxy and accepts no
dividend-coverage input. Liquidity currently counts absent volume as zero in its
proxy, despite storage preserving `NULL` versus zero.

## 4. Ranking, readiness and export consumers

[`RankingEngine`](../alphaforge/core/ranking/engine.py) emits
[`CompanyScore` / `WatchlistRanking`](../alphaforge/core/ranking/types.py).
Branch routing belongs to [`sector_rules.py`](../alphaforge/core/ranking/sector_rules.py).
Eligibility requires material inputs (including sector-specific KPIs where
applicable); it is not readiness. Eligible scores sort first by descending total;
ties/unranked sections use section, case-folded ticker/name and company ID.
Ineligible scores remain visible with reasons. General scores include a scoring
audit; a heuristic `valuation_score` is never DCF fair value.

[`AgentReadinessGate`](../alphaforge/core/gate/readiness.py) returns `ready`,
`evidence_blocked`, `valuation_blocked` or `method_unsupported` (method blockers
precede evidence, then valuation). Evidence-lane packets must be hash-valid and
current-rules; reverse DCF must be available. Missing historical EV/EBIT
anchors are a **limitation**, not a blocker. The gate supports only `general`
for future analysis, though sector scoring exists. Legacy direct callers have
a document-only fallback. `cmd_rank` copies the scored method to the gate
candidate, even for missing inputs; direct loader candidates use the same
sector-routing helper. Bank/property therefore report `method_unsupported`
before evidence/valuation blockers. FCFF DCF sector refusal is unchanged. `rank`
persists assessments but makes no paid thesis-model call.

`cmd_rank` writes these flat, operator/downstream-consumer interfaces:

| Output | Contract |
| --- | --- |
| `exports/<as_of>/ranking.json` | Full scores, model version, counts, scalar evidence hash and company-ID-keyed hash map; readiness, missing-data, input-selection provenance, scoring audit, and each computed growth value with its own horizon survive serialization. |
| `exports/<as_of>/ranking.csv` | Display subset of scores, explicit growth value/horizon columns, eligibility/readiness reasons, `missing_data`, structured `input_selection` and evidence hash; unavailable values are empty while zero values and zero-year horizons remain explicit. |
| `exports/<as_of>/dcf.json` | Company-ID-keyed auditable DCF/reverse-DCF payloads, including structured unavailable outcomes; not a score replacement. |
| `ranking_runs` | New run row with scores, `as_of`, model version, universe hash and inputs summary. Scalar `packet_hash` is populated only for a one-company run with a packet; multi-company evidence provenance lives in `inputs_summary.evidence_packet_hashes`. |

The universe hash covers sorted tickers, **not** financial inputs. Evidence
hashes cover the frozen textual packet, **not** prices/reports/KPIs. Ranking
version is owned by `RankingEngine.RANKING_MODEL_VERSION`; DCF/required-return
versions by [valuation policy modules](valuation.md), schema version by
[`config.py`](../alphaforge/config.py) and migrations, evidence versions and
fingerprints by [evidence-flow.md](evidence-flow.md).

Identical selected inputs under identical rules reproduce calculation values
and score order without a model. Full input snapshot/hash and byte-identical
combined financial/textual golden export remain planned (plan §7.1); changed
market-data upserts can change a historical rerun. Retain the DB/input snapshot
and versions for numerical replay. Date-directory exports are overwritten by a
new `rank`; they are not immutable thesis revisions or the planned standalone
`export` command.

## Alignment notes

Confirmed current gaps against the target design, not policy changes in this document:

- Verified dates/freshness do not establish historical publication knowledge
  or preserve overwritten financial vintages. Executed-run numerical replay
  snapshots remain a separate launch dependency, not supplied by these guards.
- Converted cross-currency reports keep original currency in the `Report`
  passed to the raw valuation guard, which can reject otherwise converted
  amounts. [`core/fx.py`](../alphaforge/core/fx.py) exists, but the ranking loader
  does not call it or a manual-rate fallback (plan §3.5).
- Current yield enforces verified complete currency-compatible windows, but live
  provider assurance is absent, so acquisition honestly remains unknown. The
  separate realized-return helper still accepts no coverage and uses an end-price
  proxy; this yield repair does not implement returns/reinvestment. Liquidity's
  missing-volume-as-zero supporting API is unchanged and excluded from analysis.

Report-calendar acquisition is implemented; its planned imminent-report
`pending` ranking gate is not. Broader ownership integration, thesis analysis,
validation/repair and incremental updates remain downstream design, not current
ranking/export consumers (see [architecture.md](architecture.md)).

## Verification references

Executable cross-boundary coverage lives in
[`test_deterministic_pipeline.py`](../tests/test_deterministic_pipeline.py)
(adapter → repository → loader, split adjustment and cutoff exclusion),
[`test_missing_valuation_regression.py`](../tests/test_missing_valuation_regression.py)
(live field mapping, KPI dates and DCF wiring),
[`test_phase1_exit_gate.py`](../tests/test_phase1_exit_gate.py)
(idempotence, placeholder and currency provenance),
[`test_current_dividend_yield.py`](../tests/test_current_dividend_yield.py)
(verified/unknown/partial windows, foreign-yield refusal, zero versus missing,
calendar/leap ex-date boundaries and actual ranking/export provenance), and
[`test_phase2_exit_gate.py`](../tests/test_phase2_exit_gate.py)
(packet provenance/export, sorting, ADTV and direct readiness).
These cover particular behaviors, not a complete semantic or live-model audit.
Use the checks in [CONTRIBUTING.md](../CONTRIBUTING.md#checks).
