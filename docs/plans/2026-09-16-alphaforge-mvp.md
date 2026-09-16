# AlphaForge v2 — Consolidated Implementation Plan

**Date:** 2026-09-16  
**Status:** AUTHORITATIVE — supersedes all prior drafts  
**Superseded:** `docs/plans/2026-09-16-alphaforge-mvp.md` draft of 2026-09-16 (same filename; now replaced — one authoritative document)  
**Sources integrated (read first):**  
- `scout-borsdata-endpoints/report.md` — live-verified Börsdata inventory, 10 endpoints, data quality, 14 gaps (2026-09-16, 1 709 instruments, 73 markets, 42 JSON payloads)  
- `scout-oss-market-research/report.md` — FinRobot / TradingAgents / FinGPT / FinRL deep source analysis, 17-item grab list, 17-item avoid list  
- `scout-alphaforge-data-foundation/report.md` — SQLite WAL + Postgres seam, full DDL for ~10 tables, 17-module deterministic core map, pipeline composition, Appendix D ready-to-drop design section (preferred core for this plan's data-layer)  
- `scout-alphaforge-evidence-sources/report.md` — deterministic scrape vs LLM-extract split, credibility-ledger and ownership pipelines, validation contract  
- `scout-ownership-free-sources/report.md` — free holder-level ownership: what exists, what stays unavailable  
- `scout-borsdata-api-coverage/report.md` — **not yet landed at plan freeze (2026-09-16 17:06 UTC status: `working`)** — official spec vs 10 known families, candidate extra endpoints, `currency_ratio` semantics. Integrate on landing; see § 2.6 open slot.  
- Reference: `PycharmProjects/KN-CompanyScraper` (`~29.7k lines`, `~37 tables`, `30+ CLI`, `PostgreSQL`, `.github/workflows/ci.yml` house reference)  
- Hedborg distillation: `docs/petter_hedborg_investment_philosophy.md` §§ 12–13  

---

## Captain's confirmed calls — honored throughout (non-negotiable)

These decisions are settled and the plan implements them verbatim; no phase revisits them without an explicit captain exception.

1. **Model provider pluggable (not decided now).** Single `LLMClient` Protocol via OpenAI-compatible endpoint; provider selected by `ALPHAFORGE_LLM_PROVIDER` env (`codex` | `openai` | `deepseek`), Hedborg skills prompt-versioned, tested under both providers via same fixture. No multi-provider registry.
2. **MVP universe Sweden-only; watchlist ~100–150 smaller Swedish companies with positive earnings, hand-curated by the captain via the Börsdata scanner.** `country_id = 1` filter on `companies`; Nordic-wide is a filter removal, not a migration. Import source is `watchlist.csv` (Id;Name;Ticker;ISIN) produced by the Börsdata scanner and curated by hand.
3. **Closing price + volume only (no OHLC columns).** `prices` stores `price_date, close, volume, currency` only. Börsdata payload `o/h/l` is ignored at adapter; deterministic ADTV uses `close*volume`. No `open/high/low` columns.
4. **Required-return v2 market-cap buckets kept verbatim.** `RequiredReturnPolicy.VERSION = "required-return-v2-market-cap-buckets"` with `SIZE_BUCKETS = (<1bn → 15%, 1–5bn → 13.5%, 5–30bn → 11.5%, ≥30bn → 10%)`, SEK-only guard. No recalibration in MVP.
5. **SEK-only gate plus small FX conversion utility at valuation seam.** Hard gate: non-SEK `report_currency`/`stock_price_currency` → `RequiredReturnDecision(available=False)` and thesis limitation, except for a narrow utility that converts only cross-currency **sums** (net debt, market cap, EV) using Börsdata's own `currency_ratio` field **once its semantics are verified** (spec or probe confirms it is SEK-per-foreign). The utility never converts **ratios** (margins, yields, multiples). The rate used is persisted per observation (`financial_periods.fx_rate_to_sek`, `kpi_observations.fx_rate_to_sek` nullable, or `valuation_inputs.fx_rate`). Probe/verification step is owned by Phase 0 data-layer.
6. **PDF page cap 50 with tail extraction.** `pypdf` extracts `pages[:50]` plus tail slice `pages[80:90]` when the PDF exceeds 50 pages and the shareholder/notes tables are known to live late (Vitec p.51 at boundary proves need). `page_truncated` + `pages_included` metadata persisted; scanned-image PDFs → `missing_information: supplemental`.
7. **Swedish-aware prompt variants (no translation layer).** Deterministic language tag at ingest (`lang: sv|en`, `lang_confidence`); per-specialist prompt variants `prompts/specialist_*_sv.md` selected when majority lang of the packet is `sv`. Directive: *"Du svarar på svenska där evidens är på svenska; citera ordagrant och översätt inte nyckeltermer."* Deterministic layer performs no translation; Börsdata fields are language-independent.
8. **MFN cadence daily delta + weekly page-2 backstop.** `MfnScraper.discover_feed()` daily page-1 delta (feed → unseen URLs → detail fetch), plus Sunday page-2 sweep to catch FY reports pushed off page 1 by interim flow. `mfn_feed_checks{checked_at, discovered_count, unseen_count}` persisted even on zero delta.
9. **Call transcripts deferred to Phase 2.** No transcript ingest in MVP (`research_documents.source_type = transcript` is a reserved enum value only). Ledger rows cite report PDFs + MFN bodies only.
10. **No paid ownership vendor — ship the free ownership stack with three permanent limitations and a capped ownership sub-score, documented plainly.** Free stack: (a) MFN annual top-10 holder tables extracted from within the 50+tail window (names+shares+%, no category), (b) flagging / placement / lockup events deterministically tagged from MFN bodies (`flaggning ≥5%`, `riktad emission`, `lock-up … dagar`), (c) FI PDMR insider windowed scrape (90-day sliced search, `marknadssok.fi.se`, `Karaktär Förvärv/Avyttring/Tilldelning` distinguished), (d) FI short register snapshot + Börsdata `holdings/shorts` snapshot. Three permanent limitations carried on every thesis's `limitations[]` and surfaced in UI/export: *`Free-float % is unavailable`* / *`Named large-holder coverage is unavailable beyond annual top 10`* / *`Ownership-change history is unavailable at quarterly granularity`*. Hedborg Stage B ownership sub-score capped (≤3/10) and thesis cannot `activated` on ownership setup alone without a rule-exception tag.
11. **MFN publishes every release twice (Swedish + English) — ingest must dedupe and process only one.** Dedupe on normalized `(mfn_slug, canonical_url_without_lang_suffix, attachment_storage_id)` and on checksum of stripped body text; when both `sv` and `en` variants present, prefer the variant matching the packet majority lang, otherwise prefer `en`. Store `duplicate_of` pointer, process one text only, and expose `ingested_lang` in packet.
12. **Pipeline cadence: full pipeline once per company initially, then incremental earnings/news when released, Börsdata sync nightly.** Initial fleet run: `import-watchlist → sync (reports+kpis+prices+dividends) → rank → gate → evidence → analyze → export` for all watchlist names. Thereafter: nightly `sync` (Börsdata batch 50, slice-locally not `maxCount`); MFN delta daily (incremental); `rank` on demand or daily; `evidence`/`analyze` only when a new qualifying document (report `interim/annual` or R12 set) or qualifying MFN event arrives for that company.
13. **Thesis update path — news agent then Hedborg aggregator.** When new earnings/news arrives for a covered company, the **News & Catalyst Calendar** agent analyses its impact; it then delegates **for that company only** to the **Hedborg aggregator (Stock Researcher)** which re-evaluates the case briefly through the lens of the existing thesis (prior packet + prior thesis carried as context). Full recalculation (rank + reverse-DCF + scenario bands) is available but rare, triggered only when the news agent flags a price/earnings surprise above threshold or the aggregator marks `reassess` disposition. This path is per-company, not a fleet re-run.
14. **Theses immutable with packet-hash revisions.** `theses{company_id, revision, as_of, packet_hash, prior_packet_hash, change_log, thesis_json, model, prompt_version, verdict}` has **`UNIQUE(company_id, revision)` as the identity key** (packet_hash is not a unique constraint — application enforces idempotence for full recalculations by checking for an existing identical `packet_hash` before inserting and returning the existing revision). Every derived value carries `packet_hash` provenance. The light-revision path (impact note without new inputs) inserts `revision+1` with the **same** `packet_hash` and a `change_log`; the previous `UNIQUE(company_id, packet_hash)` would have blocked this, so it is dropped and prose/DDL agree on `(company_id, revision)` only. Full-recalc rows still use a content-addressed `packet_hash`; light-revision rows reuse that hash intentionally (see §5.3 contract).

---

## 1. Success Criteria — carried forward from draft (unchanged, measurable)

- The user can answer *"which companies should I research, and why?"* for the **Sweden small-cap universe (~100–150 names, positive earnings)** with **one ranking, one evidence packet, and one structured thesis per company**.
- Every thesis contains a **falsifiable 2–3 year case** (revenue driver, margin path, defensible multiple, expected return, thesis-break evidence) in Hedborg's one-sentence form, plus bear/base/bull scenarios with annualized returns.
- **All arithmetic** (ranking, valuation, reverse-DCF, returns) is **deterministic and reproducible from stored inputs**; the language model never owns a number. Golden-packet byte identity holds without any model call.
- **Every material claim cites a stored source** (document `id`/URL/paragraph or price/kpi/report row) or is marked as a **limitation**; missing data is visible, never silently zero-filled. The three ownership limitations are always visible when applicable.
- The MVP is **understandable by one developer without tracing a large workflow graph** (small CLI, ~10-table schema, 6 specialists + 1 aggregator, plain `asyncio.gather` orchestration — no LangGraph, no debate loop).

Operational acceptance: 2–3 pilot companies from the curated watchlist complete `import → sync twice (idempotence) → rank → evidence → analyze → export` with green validation gate, and a second nightly `sync` after a new MFN release triggers only that company's thesis-update path.

---

## 2. Current Facts Summary — what is known at plan freeze

### Empty AlphaForge repo, mature reference, live-verified data layer

- **AlphaForge** (`firstmate/projects/AlphaForge`) is an empty git repo with one commit (`b74596e Initial commit: MVP plan and README`, 78-line draft). No code to preserve; no migration.
- **KN-CompanyScraper** (`PycharmProjects/KN-CompanyScraper`) is ~29.7k lines of Python, 37 PostgreSQL tables, 30+ CLI commands, `psycopg2`, `requests`, `playwright`+`pypdf` for MFN, `schedule`. Hedborg philosophy distilled, `individual-thesis-card-v2` output contract exists, domain rules (§6) already encoded (deterministic ranking, no look-ahead, missing≠zero, idempotent sync, immutable theses, dividend-review caps).
- **Börsdata inventory — live-verified 2026-09-16** (`scout-borsdata-endpoints`): **10 endpoint families** under `https://apiservice.borsdata.se` (single `BASE_URL`, `authKey` query param), all 200/400/403 paths observed live on 42 JSON payloads:
  1. `GET /v1/instruments` — 1 709 Nordic instruments, `instrument 0/1` (pref), `listingDate`, `stockPriceCurrency/reportCurrency`, `branchId/marketId/countryId`.
  2. `GET /v1/markets` — 73 markets.
  3. `GET /v1/instruments/{id}/kpis/{kpiId}/{calcGroup}/{calc}` — snapshot multiples, `value.n` nullable, sector mismatches → 400 (swallowed as `sector KPI unavailable`).
  4. `GET /v1/instruments/{id}/kpis/{kpiId}/{reportType}/{priceType}/history` — `year/mean|high|low` and `r12/…` supported, **`quarter/mean` → 400 not supported**, 20-year depth, `v` null-filtered client-side.
  5. `GET /v1/instruments/reports` — annual+r12+quarterly, `instList` batch ≤50 (client-enforced, backend permissive), `period 5` = annual, `r12` rolling, `report_Date` nullable (unpublished stub), ~30–40d publication lag, `currency/currency_ratio/broken_Fiscal_Year` fields, stub-zero quarantine needed (2620/2649 `revenues 0.0, report_Date null`).
  6. `GET /v1/instruments/{id}/stockprices` — daily OHLC+volume, 10-y default (2 516 rows) vs any `maxCount` → 5 027 rows (20-y) **anomaly: maxCount is ignored by backend as of probe — sync must fetch full then slice locally**.
  7. `GET /v1/instruments/dividend/calendar` — ex-date calendar, `dividendType 0/1/2`, zero-row (`amountPaid 0.0` with currency = explicit "no distribution", dropped at adapter), duplicate ex-date with two types occurs.
  8. `GET /v1/holdings/insider` — raw buys/sells, client keeps only `type 19/25` non-misc non-program → **−10 to −60% loss** per instrument after filter.
  9. `GET /v1/holdings/buyback` — treasury deltas, sparse for large caps, rich for property.
  10. `GET /v1/holdings/shorts` — global snapshot (one row per covered instrument, 427/1 709 ≈ 25% coverage, `shortsProc` negative convention, trends 1w/1m/3m/6m, **snapshot not history**).

  Quality findings: zero-volume days ~0.16%, `ebitda` never populated from reports (always `None`), dividend zero-row vs cash-flow distinction, insider filter loss variable, shorts partial universe, reports stub-zero quarantine, 8.5M-row Nordic price worst-case.

  **14 provider gaps G1–G14** (management ledger, business-model mechanics, organic split, ownership register/float, fund investability, EBITDA, quarterly KPI, dilution cause, short history, etc.) — all require MFN/PDF or are marked `limitation`.

- **OSS market research** (`scout-oss-market-research`): FinRobot V1 is a sequential report factory (deterministic `financial_data_processor`, `valuation_engine`, `sensitivity_analyzer`, `catalyst/news_integrator` plus 8 per-section LLM writers; philosophy *"deterministic core survives framework swap"* is the load-bearing idea); TradingAgents is a LangGraph trading-firm (4 analysts → bull/bear debate → trader → 3 risk debators → PM; point-in-time guards `in_window/withhold_live_profile/assert_not_stale` are the steal, the graph is the avoid); FinGPT is a LoRA fine-tuning family, FinRL is a DRL env+SB3 stack (both negative templates). **Grab list G1–G17** (typed domain layer with lint isolation, pure-function valuation operators, per-section provenance, file-based handoff, keyword taxonomy, sensitivity heatmap, typed vendor errors, date-window guards, symbol normalisation, instrument-anchored prompt header, structured-output with rendered fallback, null-coercion, deterministic post-LLM interpreters, memory log, etc.) and **Avoid list A1–A17** (no web app before CLI is trusted, no two text-generation stacks, no fallback prose fabrication, no fragmented enhanced modules, no `df.to_markdown()` typed loss, no LangGraph for a linear chain, no multi-provider router, no bull/bear debate, no trader/risk creep, etc.) are adjudicated per-boundary in §3 and §5.
- **Data foundation** (`scout-alphaforge-data-foundation`): normative recommendation **SQLite WAL as MVP system-of-record with a typed `Repository` Protocol seam to Postgres**. DDL for ~10 tables (`companies`/`watchlist`/`financial_periods`/`kpi_observations`/`prices`/`dividends`+`dividend_coverage`/`ranking_runs`/`research_documents`/`theses`/`jobs`) in §4's Appendix D is this plan's authoritative data-layer section. 17-module deterministic core map, pipeline composition `rank → gate → evidence → analyze → export` with import-isolated `alphaforge.core`, and promotion signal for Postgres are committed. Size at Sweden ~800 names: ~218 MB (10y) / ~298 MB (20y backfill); Nordic 1 709: ~458 MB / ~626 MB, both file-backup-able.
- **Evidence sources** (`scout-alphaforge-evidence-sources`): MFN scraper already polls correctly (Playwright feed `a.title-link.item-link`, detail `h1`/`.release-body`, attachment `storage.mfn.se`, `published_at` two-tier, report-prioritized 24 cap, delta via `discover_feed → unseen`), `ResearchDocumentIngestionService` deterministically ingests PDFs (`pypdf` 30→**50+tail** after this plan, report-period normalization, both URLs retained (`source_url` + `source_release_url`), `ON CONFLICT(source_url)` idempotence). Gap closure is **deterministic-scrape vs LLM-extract split** (S0–S13): scrape stays pure code; extraction emits typed `ManagementCredibilitySpecialistOutput` / `BusinessModel…` / `Margin…` / `InsiderOwnership…` / `SellConditions…` under a single validation contract (closed schema, citation traversal via `evidence_catalog`, paragraph-anchored excerpt, deterministic post-LLM interpreters, one-repair ceiling).
- **Free ownership** (`scout-ownership-free-sources`): built from live curls (Vitec 194-page PDF p.51 holder table within 50-page boundary, Acast p.27 explicitly *"based on data from Modular Finance, Monitor"*, MFN Mycronic flagging, `marknadssok.fi.se` 211 Vitec PDMR rows over 22 pages, FI blankning register, Euroclear Cloudflare-blocked, Holdings.se SPA paywalled, Avanza `numberOfOwners` single integer via `orderbookId`, Nordnet session-gated). **No free combination closes holder categories, free float, or quarterly change**; the MVP ships the four-piece free stack (annual top-10, flagging/placement/lockup, PDMR windowed, short register) and three permanent limitations with a capped sub-score.
- **Börsdata API coverage** (`scout-borsdata-api-coverage`): in flight at freeze; when it lands, reconcile official spec against the 10 families, classify *in-use / available-but-unused / stale*, and adjudicate additions (KPI-definition/metadata, splits, calendar events, per-branch KPI listings, global metrics) plus verify `currency_ratio` semantics for the FX utility. Until then, the FX utility is speculatively designed behind a verification gate (Phase 0).

### What changed from the draft MVP

The draft (`2026-09-16` 78 lines) already chose the Stock Researcher + 6 specialists, single Börsdata provider, deterministic/model/integration boundaries, lean schema/CLI, and phased plan — but left universe, model provider, and tooling as open questions and sketched the schema at ~10-table names only. This v2 **retains every adopted key decision** (1–6), **resolves the opens** per the captain's calls above, and **replaces the sketch** with a DDL-level data foundation (Appendix D), a closed evidence architecture, an incremental pipeline, a CI-first work plan, and a golden-packet validation contract — all folded from 5 scouts.

---

## 3. Architecture

### 3.1 Three boundaries — non-negotiable, lint-enforced

```
integration  ──►  deterministic  ──►  model
(Börsdata MFN FI)   (pure, typed)     (LLM, packet-in/claims-out)
```

- **Integration boundary** (`alphaforge/providers/`, `alphaforge/evidence/ingest`): the only code that touches `https://apiservice.borsdata.se`, `https://mfn.se`, `https://marknadssok.fi.se`, or `storage.mfn.se`. Adapters map provider field names → canonical names via one file (`alphaforge/core/kpi_taxonomy.py`); nothing above it imports raw keys.
- **Deterministic core** (`alphaforge/core/`): pure functions over frozen dataclasses, **MUST NOT** import `llm`, provider SDKs, DB drivers, or `requests`. Owns every number. CI-enforced via `importlinter` (see §7.3 / Phase 0).
- **Model boundary** (`alphaforge/llm/`): one `LLMClient` Protocol: `invoke(prompt, schema) → StructuredInstance | str`, `with_structured_output(Schema)` + one free-text retry + `_coerce_optional_float` nullish handling. `llm` **must not** import `core/valuation` internals. Revalidation (`llm/validate.py`) re-derives numbers and checks citations before persistence.

### 3.2 Deterministic core map — 17 pure-function modules

Carried from `scout-alphaforge-data-foundation §3` and `scout-alphaforge-evidence-sources §2.2`. Each has a typed `frozen` I/O dataclass, a `VERSION` string for provenance, and is tested without network or LLM.

| # | Module | File (proposed) | Core function | Input → Output |
|---|---|---|---|---|
| 1 | Financial mapper | `core/financial/mapper.py` | `to_current / to_historical` | `Report` → `CurrentFinancials / HistoricalFinancials` |
| 2 | Financial calculator | `core/financial/calculator.py` | `calculate / growth / per_share` | `Current × Historical × latest_q` → `FinancialResult` (margins, 3y CAGR, dilution>5%, volatility pstdev, `recent_revenue_growth_Δ`, `positive_fcf_ratio`, `share_dilution`) |
| 3 | Raw valuation | `core/valuation/raw_valuation.py` | `compute_raw_valuation` | `PriceBar × Report` → `RawValuation` (SEK currency guard, `EV = marketCap + netDebt`) |
| 4 | Historical valuation | `core/valuation/historical.py` | `percentile / history_bound` | `history: float[]` → `pe_percentile / guardrails 0.10/0.25/0.75/0.90` |
| 5 | Valuation calculator | `core/valuation/calculator.py` | `calculate` | `CurrentValuation × HistoricalValuation × RawValuation` → `ValuationResult` (`earnings_yield=1/pe`, `fcf_yield`, guardrails) |
| 6 | Reverse-DCF engine | `core/valuation/reverse_dcf.py` | `value / solve(bisect)` | `ReverseDcfInputs{price,shares,revenue,netDebt,DcfAssumptions,branchId}` → `DcfValue / implied{rev-growth, ebit-margin, terminal-growth}` |
| 7 | DCF assumption policy | `core/valuation/dcf_policy.py` | `build` | `Report × ROIC × marketCap → DcfPolicyDecision{assumptions: 5y, tax 21%, terminal 2%, growth −5..15%, solve bounds rev −10..30% / margin 0..50% / term −1..4%}` |
| 8 | Required-return policy | `core/valuation/required_return.py` | `build` | `marketCap × currency → RequiredReturnDecision` — **v2 buckets verbatim** (see §3.4) |
| 9 | Forward scenario | `core/valuation/forward_scenario.py` | `assess_readiness / recalculate` | `ScenarioBundle × terminal_multiple_range → ScenarioBandResult(bear/base/bull with annualized returns)` |
| 10 | Ranking metrics | `core/ranking/metrics.py` | `score_quality/growth/valuation/balance_sheet` | `FinancialResult × ValuationResult × fundamental_kpis → CategoryScore{score, positives, negatives, missing, flags}` |
| 11 | Ranking engine | `core/ranking/engine.py` | `rank` | `companies × {financial, valuation, kpis} → WatchlistRanking(sorted by eligible × total_score)` |
| 12 | Per-share growth | `core/financial/per_share.py` | `per_share_values / dilution_flag` | `history × shares_history → per-share CAGR` (>5% ⇒ `share_dilution`) |
| 13 | Gross→EBIT spread | `core/financial/spread.py` | `gross_to_ebit_spread / margin_runway` | `FinancialResult(gross_margin, op_margin) → spread` (peak remains analyst estimate with provenance) |
| 14 | Liquidity (ADTV) | `core/coverage/liquidity.py` | `adtv / build` | `PriceBar[120] → LiquidityEvidence(adtv_20/60/120, observed_days, zero_volume_days_120)` |
| 15 | Dividend total return | `core/returns/total_return.py` | `realized_total_return` | `start_price × target_date × dividends × coverage → RealizedReturnObservation{total, price, issue∈{missing_price,currency_mismatch,incomplete_dividends}}` |
| 16 | Readiness gate | `core/gate/readiness.py` | `assess / guard` | `candidate{ranking_model, research_evidence} → Assessment{ready | evidence_blocked | valuation_blocked | method_unsupported}` — the only paid-call gate |
| 17 | Point-in-time | `core/point_in_time.py` | `in_window / withhold_live_profile / assert_not_stale / normalize_symbol` | Adapter + packet two-layer filter `[start, end+1 day)` half-open UTC, MFN live-profile withholding, stale OHLC rejection |

Supporting: `core/kpi_taxonomy.py` (the one-file anti-leakage seam — only file that may contain Börsdata raw keys), `core/statistics.py` (`safe_div/cagr/pearson`).

### 3.3 Required-return v2 — verbatim

From `kncompanyscraper/analysis/valuation/required_return_policy.py:23` (`VERSION = "required-return-v2-market-cap-buckets"`), retained without recalibration:

```
SIZE_BUCKETS:
  < 1_000_000_000 SEK                →  below_sek_1bn          →  15.0%
    1bn – <5bn                       →  sek_1bn_to_below_5bn   →  13.5%
    5bn – <30bn                      →  sek_5bn_to_below_30bn  →  11.5%
    ≥30bn                            →  sek_30bn_and_above     →  10.0%

Guard: currency != SEK  →  available=False, missing_information="market-cap hurdle policy is defined for SEK; received {CCY}".
Guard: market_cap is None / non-finite / ≤0  →  available=False.
Input verification: probe Börsdata currency exposure on the curated watchlist (are there any SEK-listed micro caps with foreign reportCurrency despite the Sweden-only filter?) — if the FX utility is needed, it is applied at the valuation seam only (see §3.4).

All thesis valuations persist `policy_version`, `size_bucket`, `market_cap`, `required_return`, `source_date = as_of`.
```

### 3.4 Data layer — SQLite WAL with a typed Postgres seam (Appendix D is core)

**Decision:** **SQLite in WAL mode** (`data/alphaforge.db`, gitignored, `data/alphaforge.test.db` for tests) as the MVP system-of-record; **Postgres-variant DDL shipped alongside** with a one-line `ALPHAFORGE_DSN` migration path. DuckDB is not the primary (OLTP upsert/concurrency mismatch); it is an optional read-replica for whole-universe scans (`ATTACH 'alphaforge.db'`).

- **Rationale:** At ~10 tables / 6 commands / Sweden ~800 names (2.0M price rows 10y, 4.0M backfilled 20y) the hot path is index seeks per company (≤20 rows), not warehouse scans. SQLite WAL covers that with zero daemon (`PRAGMA journal_mode=WAL; synchronous=NORMAL; foreign_keys=ON; busy_timeout=5000; cache_size=-20000; temp_store=MEMORY`), file-copy backup, and a canonical `sha256(packet.json)` golden packet. Nordic 1 709 instruments (~4.3M/8.5M price rows) remains ≈0.5–0.9 GB with indexes — below the threshold where Postgres's buffer-pool and VACUUM-autovacuum buyback matters.
- **Promotion signal — switch when any holds:** (a) second concurrent writer (web UI, scheduler), (b) price rows exceed ~12M or file exceeds ~2 GB and `VACUUM` latency becomes user-visible, (c) team >1 or hosted PITR required. The flip replays `db/alphaforge.sqlite.sql`'s Postgres variant (`db/alphaforge.postgres.sql`) plus `now()`/`JSONB`/`GENERATED ALWAYS AS IDENTITY` deltas — no code above `db/` changes because no ranking/thesis code imports `sqlite3`/`psycopg2` or Börsdata raw keys.

**Appendix D is incorporated by reference.** The full DDL, indexes (including `partial WHERE is_placeholder=0`, `partial WHERE volume IS NOT NULL`, `partial WHERE report_date IS NOT NULL`, `(company_id,period_type,period_end DESC)`, and the two KPI partial uniques `uq_kpi_snapshot`/`uq_kpi_history`), `ON CONFLICT` upsert discipline (with `source_url` — not `url` — as the `research_documents` conflict target, keeping `source_release_url` distinct), stub-zero quarantine `CHECK(is_placeholder IN (0,1))`, nullable `fx_rate_to_sek` / `currency_ratio` audit columns, `dividend_coverage` completeness guard, `theses{UNIQUE(company_id,revision)}` identity (packet_hash uniqueness is dropped — idempotence is application-checked, see §5.3), and the Postgres delta are normative — see the updated **Appendix D** block reproduced as this plan's **Addendum A (Data Foundation DDL)**. Key amendments applied for the consolidated v2:

- `prices` keeps **only** `(company_id PK, price_date, close NOT NULL, volume INT nullable, currency, fetched_at)` — no `open/high/low` columns (captain's closing-price-only call). Partial index `WHERE volume IS NOT NULL` accelerates ADTV windows.
- `financial_periods` and `kpi_observations` each carry `fx_rate_to_sek REAL CHECK (fx_rate_to_sek IS NULL OR fx_rate_to_sek > 0)` + `fx_source TEXT CHECK (fx_source IN ('currency_ratio','manual','null'))` for the FX utility audit trail (see §3.5).
- `financial_periods.raw_payload` retains the Börsdata JSON verbatim including `currency_ratio`; the FX utility prefers `currency_ratio` when a `//! verified: currency_ratio is SEK per foreign` note is present (Phase 0 verification), otherwise requires captain-supplied `manual_rates.json`.

**Point-in-time discipline:** `core/point_in_time.in_window` (`[start, end+1 day)` half-open UTC) applied at **two layers** — adapter trimming (windowed prices ≤ `as_of`) and packet assembly (each `Report/Kpi/Price/Dividend/Document` row filtered by `report_date|published_at|observation_date|ex_date ≤ as_of`). `is_placeholder=1` rows never enter ranking/valuation; `report_Date null` stubs are never PIT-visible.

**Scale estimate Sweden MVP (~100–150 curated names vs ~800 theoretical Sweden):** 100 names → `financial_periods` ~10k, `kpi_observations` ~20k, `prices` ~250k (10y) / 500k (20y), `theses` ~100/y — well inside a ~50 MB WAL file; the full ~800-name estimate (218 MB / 298 MB) is the Nordic-growth ceiling.

### 3.5 FX conversion utility — SEK-only gate plus sums-only conversion

Per the captain's FX call (detailed shape from §3):

- **Gate:** `RequiredReturnPolicy` remains SEK-only (`available=False` for any non-SEK `report_currency` or `stock_price_currency`), and `raw_valuation.compute_raw_valuation` rejects currency-mismatched `PriceBar × Report` before any yield/multiple is computed. This is the correct Hedborg stance: multi-currency in MVP is a gate, not a translation.
- **Narrow exception — sums-only conversion at the valuation seam:**

  ```
  alphaforge/core/fx.py :: convert_sum(value_in_report_ccy, rate_sek_per_ccy) -> value_in_sek
  ```

  Applied **only** to **level sums** `net_debt`, `market_cap`, `enterprise_value` (and their history when denominated in foreign currency) before `EV = marketCap + netDebt` or `EV/EBIT` denominator. **Never** applied to **ratios** (`pe/pe_percentile`, `ev/ebit`, `margins`, `yields`, `roic`, `growth CAGRs`) or to `Dividend.amount` per-share (foreign dividend stays foreign; yield conversion uses price currency, not report currency).

  Rate source preference: (1) **Börsdata `currency_ratio`** from `reports.currency_Ratio` **if Phase 0 verification confirms it is SEK per unit of `currency`** (compare a known SEK-reporting-DKK-priced instrument's ratio to ECB `SEK/DKK` on the same `report_End_Date`), (2) otherwise manual `data/fx/manual_rates.json` keyed by `(currency, observation_date)`. Whatever the source, the row persists `fx_rate_to_sek` and `fx_source`; valuation derivation logs `fx_rate_to_sek` in `thesis_json.valuation.fx_rate_used`. If no rate is verifiable, the `sum` is `null`, the valuation limitation is surfaced, and the thesis stays ineligible — never guessed.

- **Owned by Phase 0:** the first `sync` over the curated watchlist must probe and log actual currency exposure (how many of the 100–150 names report in NOK/DKK/EUR despite a Stockholm venue) and confirm or reject `currency_ratio` semantics via the Börsdata api-coverage report plus a live spot-check.

### 3.6 Hedborg philosophy — how it is encoded

Six discrete Hedborg skills (not one giant prompt) plus the aggregator, each with a prompt version and a deterministic counterpart:

- Falsifiable-case author (one-sentence case), Peak-margin bridge (`gross→EBIT` spread is a clue; peak is an estimate with mechanism), Credibility ledger (8–12q pattern recognition), Reverse-DCF expectation check (implied growth/margin vs history), Ownership/flow timing read (signal + flow-effect, never mechanical), Sell-condition author (7 Hedborg invalidation conditions).

Scoring weights: `GENERAL (30/25/30/15)`, `PROPERTY (25/15/30/30)`, `BANK (30/20/25/25)` routed by `branch_id` (`68–70 bank, 75 property`). Property/bank ranking models remain **blocked** (`method_unsupported`) in MVP — `general` only — per `agent/readiness.py`.

---

## 4. Evidence Architecture

### 4.1 Deterministic scrape stays deterministic; LLM extraction stays bounded

Following `scout-alphaforge-evidence-sources §2`:

| # | Evidence step | Mode | Owner | What happens |
|---|---|---|---|---|
| S0 | MFN feed poll + detail fetch | Deterministic — scrape | `MfnScraper` (Playwright, `BASE_URL https://mfn.se`, `MAX_ARTICLES 24` report-prioritized, `a.title-link.item-link`, detail `h1`/`.release-body\|article`, attachment `storage.mfn.se/*.pdf`) | `discover_feed(mfn_slug)` daily page-1, Sunday page-2; `scrape_details(unseen)` only for unseen URLs |
| S1 | Release persistence | Deterministic | `NewsRepository` + `ResearchDocumentIngestionService._persist_articles` | `news_releases` `ON CONFLICT(url) DO NOTHING` (PK = `url`), `research_documents` `ON CONFLICT(source_url) DO NOTHING` (keeping `source_release_url` distinct), `mfn_feed_checks` upsert even on zero delta (liveness proof), `duplicate_of` pointer for sv/en bilingual dupe |
| S2 | PDF download | Deterministic | `_download` via `request_with_retry` (60 s timeout, 3 retries, `Retry-After`) | Fails single PDF without aborting batch |
| S3 | PDF text extraction — **50 + tail** | Deterministic | `_extract_pdf_text` → `pypdf` `pages[:50]` + `pages[80:90]` tail when `len>50 && holder table suspected`; `re.sub(\n{3,},\n\n)`; `page_truncated` flag; scanned-image → `missing_information` supplemental | Language tag (`lang`, `lang_confidence`) applied here; шведск/Acast shareholder tables on p.51 prove 30 was insufficient |
| S4 | Catalog + period normalization | Deterministic | `_is_report` / `_document_type` / `_normalized_report_metadata` / `_report_period` | `report_year/report_period` canonicalization, fiscal-year guard (annual shipped next year does not inherit `published_at.year`), Swedish month names, `H2→Q4`, `authoritative_source` repair seam |
| S5 | Keyword-taxonomy pre-pass | Deterministic | `mfn_taxonomy.py` (`CATEGORY_KEYWORDS_SE`, `IMPORTANCE_KEYWORDS_SE`, `REPORT_TERMS_SE = REPORT_TITLE_TERMS + ("bokslutskommuniké","flaggning","flagging","riktad emission","ägarförteckning","aktieåterköp","lock-up","listbyte")`) | `release_category ∈ {earnings,guidance,ownership_change,insider,buyback,placement,lockup,listing_change,contract_win,…}`, `importance ∈ {high,medium,low}`; gates irrelevant outside `[as_of−5y, as_of]`; included in packet so LLM refines rather than invents |
| S6 | Evidence packet assembly | Deterministic | `ResearchEvidenceBuilder` → `AgentCandidatePacket` | PIT-filtered `ResearchEvidence{documents[], news[], insider_transactions[], ownership_liquidity, missing_information[]}` + `evidence_catalog{canonical_source_ids[], aliases{}}` + `packet_hash = sha256(canonical_json without hash)` — **the only input the LLM ever sees** (typed `EvidenceDocument{source_id, title, url, published_at, text, report_year/period, body_source_id, structured_financial_values}` — not `df.to_markdown()`) |
| S7 | Management credibility ledger | LLM (typed) | `ManagementCredibilitySpecialist` | Extracts `coverage{tier,no_ledger|partial|full, quarters_covered, …}`, `ledger[ManagementLedgerRow{quarter YYYY-Qn, claim_id, claim, expected_timing, observed_outcome, result∈{kept,delayed,missed,unverifiable,too_vague_to_test,external_shock}, claim_source_ids[], outcome_source_ids[], source_ids[]}]` with paragraph-anchored excerpt; deterministic `kept/delayed/missed` accounting, `external_shock` does not penalize pattern state |
| S8 | Business-model & scalability | LLM (typed) | `BusinessModelSpecialist` | `revenue_model_types[], recurring_revenue_profile, operating_leverage∈{absent,…,demonstrated}, profitability_state, claims[]` + deterministic `gross_to_ebit_spread` guard |
| S9 | Margin / peak-margin defensibility | Hybrid | `MarginSpecialist` + deterministic solver | LLM authors `mechanism`+`defensible_peak_ebit_margin`; code clamps `defensible_peak ≤ gross_margin` and rejects `source_ids==[]`, rejects 8%→20% jumps without `fixed cost/distribution/market size/comp` mechanism |
| S10 | Ownership structure & holder-level table | Hybrid | Deterministic table-row regex (`Aktieägare/Namn — Antal — Andel % → ownership_holders_pdf_staging` when >2 rows with `%<100`) + **LLM flow read** (`InsiderOwnershipSpecialistOutput{insider_signal, flow_effect, event_claims[], data_coverage}`) | Deterministic holder rows are facts; LLM flow read is judgement — capped, source-contract validated (each `event_claim.source_ids[0] ∈ evidence_catalog.ownership/ownership_flow`) |
| S11 | Insider signal vs program/alibi filter | LLM + deterministic cross-check | `Insider*Specialist` + `insider_repository.list_for_company(since=as_of−5y, limit 50)` filtered to non-misc/non-program `type {19 buy,25 sell}` + `valuation_repository.get_stock_price_on_or_after(transaction_date)` for entry-market-price & 90/180/365 d returns | Hedborg gate: `positive` only when open-market, ≥2 independent insiders, size ≈ SEK 1M heuristic relative to pay/holding, not transfer/program/gift; validated by price gate |
| S12 | Dilution-cause & ownership-change history | LLM | `…` | Cause narrative from report notes + MFN placement releases, triggered by `share_count_growth >5% p.a.` with no buybacks ⇒ `missing_information: dilution_cause unclear` |
| S13 | Sell / invalidation conditions | LLM (downstream, after S7–S11 typed) | `SellConditionsSpecialistOutput{tests[{break_type, condition, observable_metric_or_event, threshold, status∈{not_triggered,triggered,unassessable}, response∈{reassess,reduce,sell}, causal_basis∈{fundamental_break,valuation_overshoot,…}, source_ids[], claim_ids[]}], activation_blockers[]}` | Requires exact `run_id + packet_hash` copy, qualified upstream claim IDs preserved |

**Not LLM-scoped (stay deterministic):** ranking, KPI math, reverse-DCF solves, scenario recalc, return/dividend math, readiness gates, citation traversal, ADTV math, report-period normalization, bilingual dedupe, feed delta idempotence.

### 4.2 MFN bilingual dedupe — Swedish + English, process one

Every MFN company feed carries each release twice (same body translated). The ingestion **must**:

- Compute `normalized_key = slugify(title without "Inbjudan/Invitation to") + storage_id_or_href_stem`; group `(mfn_slug, normalized_key, calendar_date)` as one logical release.
- When a group has size 2, keep deterministically one `doc.selected = {preferred}` and mark the other `duplicate_of = selected.id` and `ingest_status = superseded_by_translation`. No PDF is fetched twice; the unselected variant's text is never concatenated into the packet.
- The selection rule is recorded (`bilingual_selection_rule ∈ {sv_packet_majority, en_packet_majority, deterministic_en_fallback}`) and exposed as a diagnostic.

### 4.3 Free ownership stack — what is shipped

Built live-verified (Vitec, Acast, FI blankning) and constrained by the Sweden-only, no-paid-vendor call:

- **Level (annual):** MFN PDFs within 50+tail: `Top 10 shareholders 38.8% / 75.7%` style tables + size/geographic distribution; deterministic parse when `>2` rows, otherwise LLM `event_claim` with verbatim `excerpt ≤240 chars` that must substring-match stored text. Annual granularity only; `Top 10 → ownership_holders_pdf_staging(holder_name, shares, pct, observation_date = published_at, source_id)`.
- **Flow (events ≥5%):** MFN flagging/placement/lockup releases — `release_category ∈ {ownership_change, placement, lockup}`; deterministic regex for `Andel %/Datum`; stored as `ownership_events_staging(event_type, holder_name, pct, lockup_expiry_date, source_ids)`.
- **Flow (trade-by-trade):** **FI PDMR windowed scrape** — `GET marknadssok.fi.se/Publiceringsklient/sv-SE/Search/Search?SearchFunctionType=Insyn&Utgivare={name}&Transaktionsdatum.From/To&Page={n}`; 90-day windows (to stay <1000 export gate), ≤1 search per 2 s, cache by `(issuer, window)`, parse HTML table (`Publiceringsdatum/Emittent/Person i ledande ställning/Karaktär/Instrument/Volym/Pris/Valuta`), distinguish `Förvärv` vs `Tilldelning` vs `Avyttring`; PIT filter `Publiceringsdatum ≤ as_of`; note FI throttling quote *"FI begränsar antalet sökningar … kan stänga av …"* and never brute-force the 1 709-company universe — queue is watchlist-scoped.
- **Flow (short):** FI blankning register (`blankningsregistret`, position ≥0.5% named, sum ≥0.1% aggregated, `Aktuella/Historiska/Aggregerade positioner.xlsx` vintages) corroborated by Börsdata `holdings/shorts` snapshot (427/1 709, 25%); store `company_short_snapshots` snapshot date = vintage date; 75% `unknown` vs `0` discipline preserved.

All four feed the `ResearchEvidence` and S10/S11 typed outputs; the thesis card's **Ownership, insider, liquidity & flow** section always ships the three limitations verbatim when applicable, and the Stage B ownership sub-score is visibly capped.

### 4.4 Swedish-aware prompts — no translation layer

- Detector: `langdetect`/`fasttext` on each `news_releases.body` and `research_documents.document_text` → `metadata{lang, lang_confidence}` persisted.
- Selector: packet majority lang (`mode(doc.lang for doc in documents if doc.lang in {sv,en})`) chooses the specialist prompt variant (`_sv.md` vs `_en.md`); mixed-sv majority → Swedish prompt even if some interim is English.
- Instruction (front-matter of every `_sv.md`): *"`Du svarar på svenska där evidens är på svenska; citera ordagrant och översätt inte nyckeltermer. Varje påstående kräver source_ids-paragraf; saknas källa → missing_information.`"*
- Hedborg Swedish-hedge fidelity (`"...förutsättningarna förblir utmanande"` is challenge-preserved, not mushed to *"challenging but positive"*) is validated by the evidence-sources report's §8 Nordic-language risk.

### 4.5 Validation contract — one schema, one citation regime, one repair ceiling

Ports `analysis/agent/output_schema.py` + `result_parser.py` + `execution_boundary.py` (`VALIDATION_VERSION = "agent-boundary-v23-ownership-source-contract"`), applied identically to S7–S13:

- **Closed JSON schema** per specialist (`schema_version:"specialist-output-v1"`, `agent_name ∈ {business_model, management_credibility, margin, insider_ownership, growth_valuation, sell_conditions}`, `company_id/ticker/evidence_as_of/packet_hash/status/confidence/claims[]/missing_information[]`) with `additionalProperties: false` and `_coerce_optional_float` (OSS G12) before Pydantic so `N/A/–/15%/12,3` → `None` rather than a run failure.
- **Citations** validated by exact traversal into `evidence_catalog.canonical_source_ids[]` (`document:{id}`, `news:{id}`, `insider:{date}:{name}:{n}`, `liquidity:borsdata:…`, `buyback:…`, `short:…`, plus `full_results.*` / `deterministic:*` via `_walk_path`). Lemma: a claim lacking an allow-listed source is rejected, not warned. Paragraph anchoring via `paragraph_range` or quoted `excerpt ≤240 chars` that must substring-match stored text.
- **One-repair ceiling:** `structured_llm.invoke(prompt).with_structured_output(Schema)` → validation error bag → re-prompt once → second failure ⇒ `status=insufficient_evidence` (no fallback prose; slot suppressed, `missing_information[{item_code, limitation_class: core|supplemental, impact_code}]` recorded per `performance/BOUNDARY.md`).
- Deterministic **post-LLM interpreters** (OSS G13) after every specialist: `ledger_coherence` (kept/delayed/missed/external_shock accounting), `margin_clamp` (`defensible_peak ≤ gross_margin`), `holder_total_gate` (`Σ holder pct <100`), `ownership_source_guard`, `insider_price_gate`.

---

## 5. Pipeline — full fleet, then incremental, then per-company update

### 5.1 Rank → gate → evidence → analyze → export

Plain orchestrator, per-company failure isolation, no LangGraph, no bull/bear debate, no shared `messages` blackboard:

```
import-watchlist  (CSV ISIN→ticker via instrument.py:26; UNIQUE source_file,row_hash)
      │
      ▼
sync  (batch 50, 0.5–1s sleep, MAX_RETRIES=3, backoff 1/2/4s, http_transport.request_with_retry)
   ├─ /reports → financial_periods (year+r12+quarter, UNIQUE company_id,period_type,period_end; is_placeholder quarantine; currency+currency_ratio+raw_payload; fx_rate_to_sek nullable)
   ├─ /kpis/last/latest + /year| r12 /mean|high|low /history → kpi_observations (UNIQUE per snapshot/history variant)
   ├─ /stockprices → prices(close,volume,currency) — full fetch, slice locally (maxCount bug), ON CONFLICT(company_id,price_date)
   └─ /dividend/calendar → dividends(UNIQUE ex_date,dividend_type,amount) + dividend_coverage{covered_from,covered_through} guard
      │ ON CONFLICT DO UPDATE idempotence; report_date retained (PIT), volume kept even when 0
      ▼
rank  (RankingEngine over watchlist: per company FinancialResult+ValuationResult → WatchlistRanking + ranking_runs{model_version, as_of, packet_hash, universe_hash, scores, inputs_summary})
      │
      ▼
gate  (AgentReadinessGate: general model only → evidence_blocked if research_documents≤as_of absent → valuation_blocked if reverse-DCF unavailable/guardrail missing → the ONLY paid-call gate)
      │ ready set only (top 25 + flags up to 30 style)
      ▼
evidence  (EvidencePacketBuilder PIT-filters every source ≤as_of via point_in_time.two_layer; instrument_header "NIBE Industrier AB (NIBE B, Industrials/Capital Goods, OMX Stockholm)" anchored to Börsdata truth; packet_hash=sha256(canonical_json without hash); dual-persist DB + evidence/<ticker>/<as_of>/packet.json; S5 taxonomy included, bilingual dupe suppressed, lang majority resolved)
      │
      ▼
analyze  (fan-out 6 specialists in parallel, each a section-scoped packet slice → SectionDraft{claims,citations,limitations} via single LLMClient; one free-text retry + _coerce_optional_float; main agent Stock Researcher owns verdict-only synthesis via Hedborg skills; numbers re-derived in core and rejected if >eps)
      │
      ▼
validate  (schema+cite+number; one-repair ceiling; second failure → needs_human_review; staged outputs never fabricate)
      │
      ▼
persist  (theses{revision,packet_hash,thesis_json,model,prompt_version,verdict ∈ {reject,watch,latent,activated}} immutable; thesis.json flat file)
      │
      ▼
export  (alphaforge export --as-of YYYY-MM-DD → exports/<as_of>/ranking.json+csv + <ticker>/packet.json+thesis.json — golden-packet fixtures without DB)
```

Provenance triplet per derived value: `observation_date (=period_end)`, `publication_date (=report_Date|published_at|observation_date)`, `ingestion_date (=fetched_at)` plus `calculation_source (raw vs kpi)` and `fx_rate_used` when conversion applied.

### 5.2 Pipeline cadence (captain's incremental model)

```
T0 — Initial fleet (one-time per watchlist name):
     full pipeline above for each of the 100–150 curated names
     cost: token metering on a 5-company pilot informs the nightly budget

T1 — Nightly Börsdata sync (batch 50, slice-locally):
     prices/dividends drift; dividends window [price_oldest, today] refreshed
     short/buyback snapshot poll daily (short 427-universe, buyback per-insId) → company_short_snapshots

T daily — MFN delta:
     discover_feed(page-1) per watchlist issuer; unseen → detail + PDF 50+tail
     ingestion dedup on url+checksum; invariant: one logical release = one processed text

T Sunday — MFN page-2 backstop:
     same delta but with ?page=2 sweep to catch FY reports pushed off page-1

T on arrival — Earnings/news trigger (incremental):
     when a new qualifying document arrives (annual/interim report or qualifying MFN event
     for that issuer and the read-iness gate now passes), enqueue only that company's S5→S13
     and the Hedborg aggregator — not a fleet re-rank. Filing date is the event; market reaction
     is not separately waited.

T ad-hoc — Manual authoritative repair seam:
     `ingest_authoritative_report(pdf_url, metadata{authoritative_source:true})`
     for PDFs discovered outside MFN (company IR page).
```

### 5.3 Thesis update path — news agent then Hedborg aggregator (captain's wording)

> *"A news agent analyses new earnings/news and its impact, then delegates for that company only to the Hedborg aggregator which re-evaluates the case briefly through the lens of the existing thesis, with full recalculation available but rare."*

- **Trigger:** `news_releases` or `research_documents` row inserted with `published_at` in the new window for an already-thesised `company_id` whose `gate == ready` still holds.
- **Step 1 — News & Catalyst Calendar specialist** (news agent) runs on a minimal packet slice (new documents + new kpis/prices that moved vs previous `packet_hash` plus prior `thesis_json` as context) and emits an **Impact Assessment** (`impact ∈ {none, incremental, material, thesis_break}`, `evidence_ids[]`, `candidate sell-test triggers`, `recommended_action ∈ {no_change, reassess_briefly, full_recalc}`).
- **Step 2 — Delegation to Hedborg aggregator** (Stock Researcher) for **that company only**: re-evaluates the case briefly through the prior thesis lens — updates ledger row outcomes, re-checks `PeakMarginBridge` only if new `operating_Income` arrived, re-derives `DcfAssumptions`/`forward_scenario` only if the impact is `material` or the sell-condition author raised `needs_full_recalc`. Otherwise the aggregator writes a **light revision** (`revision +=1` with the **same** `packet_hash` and a `change_log` when only the impact note changed, or `revision +=1` with a **new** content-addressed `packet_hash` when inputs changed — the DDL's `UNIQUE(company_id, revision)` allows the former; the old `UNIQUE(company_id, packet_hash)` was removed to permit it). The row carries `prior_packet_hash` and `change_log: {updated_sections: ["risks","catalysts"], prior_verdict, new_verdict, rationale}`; application-layer dedup for full recalcs (`SELECT ... WHERE packet_hash=?` before insert) still prevents duplicate full-recalc revisions.
- **Full recalculation** (rank + reverse-DCF solve + scenario bands from scratch) is available but **rare** — gated by `recommended_action == full_recalc` or a sell-test trigger `status==triggered`.
- **Immutability preserved:** every update is a new `theses` row (`UNIQUE(company_id, revision)` only) — light revisions reuse the prior `packet_hash`, full recalcs use a new `packet_hash`; prior rows are never mutated. The flat artefact mirrors the same: `exports/<as_of>/<ticker>/thesis.v{N}.json`.

This keeps the nightly fleet cheap (Börsdata prices drift; only issuers with fresh earnings/news pay specialist tokens) and gives the captain an auditable update chain: *news → impact → aggregator re-read → light revision or full recalc*.

---

## 6. Phased Work Plan — CI setup first

### Phase 0 — Repository & CI setup (ships first, blocks nothing else that can run in parallel, but every later PR depends on it)

**Goal:** one authoritative plan file (this one), a merge-protected repo, and a CI that proves the deterministic/model/integration split and secret hygiene.

- **0.1 Branch & docs baseline (this PR).** Branch `fm/plan-alphaforge-consolidated` → PR against `main`; this plan replaces the draft in the same filename so only one document is authoritative. README `Status` line points to this plan; `docs/plans/` keeps exactly one authoritative markdown (past drafts removed or marked `> **Superseded by …**` banner at file top — in this release the superseded draft is removed by replacement).
- **0.2 CI workflow** — adapt `PycharmProjects/KN-CompanyScraper/.github/workflows/ci.yml` as the house reference (the simple `setup-python 3.11 → pip install -e ".[dev]" → pytest --deselect…` shape, not the `firstmate/.github/workflows/ci.yml` fleet shape). AlphaForge `.github/workflows/ci.yml`:

  ```yaml
  name: CI
  on:
    pull_request:
      branches: [main]
    push:
      branches: [main]
  permissions: { contents: read }
  concurrency:
    group: ci-${{ github.workflow }}-${{ github.event_name }}-${{ github.pull_request.number || github.run_id }}
    cancel-in-progress: ${{ github.event_name == 'pull_request' }}
  jobs:
    lint:
      runs-on: ubuntu-latest
      timeout-minutes: 10
      steps:
        - uses: actions/checkout@v6
        - uses: actions/setup-python@v5
          with: { python-version: "3.11", cache: pip }
        - run: pip install ruff importlinter deptry  # pinned in requirements-dev
        - run: ruff check . && ruff format --check .
        - run: lint-imports  # importlinter contract below

    test:
      runs-on: ubuntu-latest
      timeout-minutes: 15
      steps:
        - uses: actions/checkout@v6
        - uses: actions/setup-python@v5
          with: { python-version: "3.11", cache: pip }
        - run: pip install -e ".[dev]"
        - run: pytest -q  # integration-marked Börsdata/FI/MFN tests excluded by default

    invariants:
      runs-on: ubuntu-latest
      timeout-minutes: 5
      steps:
        - uses: actions/checkout@v6
        - run: |
            set -eu
            # Secret-leak tripwire: Börsdata key must not appear in any tracked file.
            # The literal Börsdata key value was previously exposed in this public repo's
            # history (branch fm/plan-alphaforge-consolidated) and that key must be
            # rotated immediately at https://www.borsdata.se — history has been rewritten
            # to scrub the literal, but rotation is still required.
            # Use a generic 32-hex pattern so the check itself does not embed a secret.
            if git ls-files | xargs grep -E -l "[a-f0-9]{32}" 2>/dev/null | xargs grep -l "borsdata\|BORSDATA" 2>/dev/null | grep -q .; then
              echo "::error::Possible Börsdata API key literal tracked in repo — rotate the key immediately"; exit 1; fi
            if grep -R "BORSDATA_API_KEY" -- .github/workflows/ | grep -q "."; then
              echo "::error::CI references BORSDATA_API_KEY as an env var — remove"; exit 1; fi
  ```

  The reference's `--deselect` list of fixture-absent tests is not ported verbatim; AlphaForge's `pyproject.toml` uses `markers: integration` and `-m 'not integration'` so live Börsdata/FI/MFN tests stay opt-in.

- **0.3 Branch protection on `main`** (via `gh api repos/{owner}/{repo}/branches/main/protection` as repo admin):

  ```json
  {
    "required_status_checks": { "strict": true, "contexts": ["Lint", "Test", "Repo invariants"] },
    "enforce_admins": false,
    "required_pull_request_reviews": { "required_approving_review_count": 1 },
    "restrictions": null,
    "allow_force_pushes": false,
    "allow_deletions": false
  }
  ```

  Result: no direct push to `main`; every change is a PR with green status checks.

- **0.4 PR template & commit conventions.**
  - `.github/pull_request_template.md`: *Context / Scope / Tests / Secret hygiene (`BORSDATA_API_KEY` not logged) / Screenshots (if renderer).*
  - Commits: Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`) with linear history; `Squash and merge` is the merge strategy.

- **0.5 Secret-handling rules — Börsdata key never reaches CI logs or the repo.**
  - The key lives in **one place**: a local `.env` (`BORSDATA_API_KEY=<placeholder>`) gitignored via `.gitignore: .env\n.env.*\n! .env.example`. An `.env.example` contains `BORSDATA_API_KEY=` (empty). **The literal key value was exposed in the public history of this PR's first push and must be rotated immediately** — treat the previously pushed value as compromised even after the history rewrite below.
  - No workflow step exports `BORSDATA_API_KEY`; `alphaforge/providers/borsdata/adapter.py` reads it only via `python-dotenv` at adapter init and **redacts** it in error messages (`client.py:460` pattern — `authKey` stripped before `raise_for_status`). CI logs are scrubbed: never `echo` the key or print env.
  - CI invariant job fails the PR if any 32-hex Börsdata-like literal or a `BORSDATA_API_KEY` workflow env is introduced. The invariant snippet uses a placeholder pattern `<BORSDATA_API_KEY>` / generic hex regex, never the literal value.
  - Manual spot-check: `git log --all --patch | grep -E "[a-f0-9]{32}" | grep -i borsdata | wc -l` must remain `0`; the previous literal was scrubbed from history and the key must be rotated (see note above). The verification checks for any 32-hex Börsdata-like literal, not a hard-coded value.
  - Audit on this fix: `git diff HEAD~1 -- docs/` and `git log -p origin/fm/plan-alphaforge-consolidated` were scanned for any 32-hex key, `gh-axi` tokens, `.env` contents, or `BORSDATA_API_KEY=<value>` assignments — zero occurrences after the scrub (verification reported in the PR).

- **0.6 Import-isolation lint as a CI gate.**
  - `pyproject.toml` contracts (from `scout-alphaforge-data-foundation §3.3`):

    ```toml
    [importlinter.contracts.core-isolated]
    name = "deterministic core is isolated"
    type = "forbidden"
    source_modules = ["alphaforge.core"]
    forbidden_modules = ["alphaforge.llm", "alphaforge.providers", "openai", "anthropic", "httpx", "requests", "sqlite3", "psycopg2"]

    [importlinter.contracts.llm-does-not-own-numbers]
    name = "LLM may not import valuation internals"
    type = "forbidden"
    source_modules = ["alphaforge.llm"]
    forbidden_modules = ["alphaforge.core.valuation.reverse_dcf", "alphaforge.core.ranking.engine"]

    [importlinter.contracts.repo-is-the-only-db-boundary]
    name = "repositories are the only DB boundary"
    type = "forbidden"
    source_modules = ["alphaforge.core", "alphaforge.evidence", "alphaforge.llm"]
    forbidden_modules = ["sqlite3", "psycopg2"]
    ```

    Plus `ruff` rule: any file under `alphaforge/core/` containing `import requests|openai|playwright` fails the PR. This is the *"domain layer survives framework swap"* gate — FinRobot's survival across AutoGen→Agents SDK→PydanticAI depended on exactly this seam.

- **0.7 Repo visibility & license — flagged as captain decisions (not chosen by this plan).**

  > **CAPTAIN DECISION REQUIRED — `repo-visibility`:** AlphaForge is currently **public** (per brief: *"Flag repo visibility (currently public)"*). Captain must confirm `public` (open sourcing the thesis logic) or flip to `private` (keep ranking/evidence proprietary). Until decided, keep `public` and do not publish the Börsdata key, MFN throttle tuning, or curated watchlist internals that would aid scraping counter-abuse.
  >
  > **CAPTAIN DECISION REQUIRED — `license`:** No `LICENSE` file is committed by this plan. Captain must choose among, e.g., `MIT`, `Apache-2.0`, or `proprietary (no license)`. Until chosen, the repo ships **unlicensed** (all rights reserved by default) and the plan notes the hold.

  Both holds are recorded as `CAPTAIN_HOLD: repo-visibility / license` and do not block Phase 1 scaffolding, but any public announcement or dependency publishing waits on them.

- **0.8 Verification of Phase 0 before proceeding:** PR `fm/plan-alphaforge-consolidated` is green (`Lint` + `Test` + `Repo invariants` pass, import-isolation contract enforces), branch protection is live (push-to-main rejected), secret-leak tripwire passes, and the plan file is the sole authoritative plan.

### Phase 1 — Lean data layer (rank-foundation)

- **1.1 Schema & migrations.** `db/alphaforge.sqlite.sql` (normative, §3.4 + Addendum A) plus `db/alphaforge.postgres.sql` delta; `PRAGMA user_version` migration shim or `dbmate`/`alembic` runner; `config.py` typed `Settings(DSN, watchlist_path, as_of)` with `ALPHAFORGE_DSN` default `sqlite:///data/alphaforge.db`.
- **1.2 Börsdata adapter** — single `BorsdataAdapter` implementing `MarketDataProvider` Protocol (`instruments, markets, kpis/{kpiId}/{calcGroup}/{calc}, kpi_history/{reportType}/{priceType}, report_bundles, stock_prices, dividends, insider/buyback/shorts-global`). `MAX_RETRIES=3` + backoff + `Retry-After`; batch 50, 0.5–1 s sleep; **`maxCount` not trusted** (fetch full, slice locally); sector-KPI 400 swallowed as `missing`; `currency/currency_ratio` audit columns plumbed.
- **1.3 FX verification probe.** During first `sync` of the curated 100–150 names, log exposed currency surface (`stockPriceCurrency` vs `reportCurrency` per `insId`) and, if any foreign, assert `currency_ratio` semantics against a spot ECB rate on matching `report_End_Date` (two-company spot). Result determines whether the FX utility is live or remains `available=False` with a limitation.
- **1.4 MFN/document fetcher** — `MfnScraper` (Playwright), `ResearchDocumentIngestionService` (50+tail, both URLs, `ON CONFLICT(source_url)` keeping `source_release_url` distinct, report-period normalization, bilingual dedupe), `NewsRepository` + `mfn_feed_checks`. Deterministic staging tables `ownership_holders_pdf_staging` + `ownership_events_staging`.
- **1.5 Free ownership free stack.** MFN top-10 parser (deterministic >2 rows), MFN event tagger (`flaggning/riktad emission/lock-up`), FI PDMR windowed scraper (90-day slices, 2 s cadence, cache, FI throttle quote documented), FI blankning snapshot — piped into `ResearchEvidence.ownership_liquidity` with the three limitations injected.
- **1.6 Watchlist import & sync CLIs.** `alphaforge import-watchlist --file imports/watchlist_2026-09-16.csv --source-file watchlist_2026-09-16` (ISIN→ticker normalized match, `UNIQUE(source_file,row_hash)`), `alphaforge sync --all --as-of YYYY-MM-DD` (per-company failure isolation, `jobs{status, attempt, error JSON}`), idempotence via `ON CONFLICT DO UPDATE`.
- **1.7 Exit gate:** idempotent `sync` twice produces identical rowcounts; `is_placeholder` quarantine visible; bilingual dedupe suppresses one variant per pair; `report_Date <= as_of` PIT filter holds for a mid-quarter `as_of`.

### Phase 2 — Deterministic ranking + readiness gate (the gate before any model spend)

- **2.1 Deterministic core — first slice.** Port `financial/{mapper,calculator,per_share,spread,helpers}`, `valuation/{raw_valuation,historical,calculator,reverse_dcf,dcf_policy,required_return,forward_scenario}`, `coverage/liquidity`, `returns/total_return`, `ranking/{metrics,engine}`, `gate/readiness`, `point_in_time`, `fx` utility, `kpi_taxonomy`. All 17 modules typed `frozen`, versioned, pure.
- **2.2 Ranking CLI + `ranking_runs`.** `alphaforge rank --as-of YYYY-MM-DD [--watchlist watchlist_2026-09-16.csv]` → `ranking_runs{model_version=2026-08-12-reverse-dcf-v10, packet_hash, universe_hash, scores, inputs_summary}` + `exports/<as_of>/ranking.json+csv`. Weights GENERAL/PROPERTY/BANK with general-only eligibility; `rank_eligible` + `eligibility_reasons`; `score_rules` floors/ceilings preserved.
- **2.3 Readiness gate.** `AgentReadinessGate.assess(candidate)` → `ready | evidence_blocked (no documents ≤ as_of) | valuation_blocked (DCF unavailable) | method_unsupported (bank/property branch)`. Only `ready` candidates enqueue for `evidence`/`analyze`. Property/bank peers are explicitly `method_unsupported` in MVP — not mis-scored.
- **2.4 Exit gate:** golden-packet for ranking (`packet_hash` round-trip), model-number fidelity (LLM cannot change a ranked number), point-in-time (`report_date>as_of` rows excluded), Börsdata-KPI vs raw cross-check, ADTV 20/60/120 for a known liquid name validated.

### Phase 3 — Evidence packet + specialist section agents + Hedborg aggregator

- **3.1 Evidence packet builder.** `EvidencePacket{company,as_of,packet_hash,instrument_header,reports( PIT-filtered, is_placeholder=0), kpis(prices window ≤as_of), dividends(coverage-validated), documents(≤as_of, bilingual-deduped), liquidity(ADTV), missing[], source_refs[]}` — bounded, dual-persisted, the only LLM input.
- **3.2 LLM boundary + 6 specialists (parallel, section-scoped packets, no debate).**
  - Business Model & Scalability (S8),
  - Financials & Valuation-math-input (prepares inputs, authors no numbers; S2-relevant),
  - Management & DNA (credibility ledger S7, 8–12q),
  - Ownership, Liquidity & Flows (S10+S11, capped, three limitations always carried),
  - Risks & Thesis-Break (disconfirming evidence, sell conditions S13),
  - News & Catalyst Calendar (timing windows, activation triggers — the news agent in §5.3).
  One `LLMClient` Protocol, `with_structured_output` + one free-text retry + `_coerce_optional_float`, Swedish-aware variants, prompt-versioned, `packet_measurement` logged.
- **3.3 Hedborg aggregator (Stock Researcher).** Owns verdict (`reject/watch/latent/activated`), one-sentence falsifiable thesis, bear/base/bull bands with annualized returns, confidence + missing data, Hedborg skills (§3.6). Orchestrates S7–S13 and the thesis-update delegation (news agent → aggregator re-read).
- **3.4 Validation gate.** `llm/validate.py` — schema + citation-traversal + number re-derivation; one-repair ceiling; `needs_human_review` on second failure. Theses persisted immutably (`UNIQUE(company_id, revision)` — `packet_hash` reuse allowed for light revisions; dedup is application-checked).

### Phase 4 — MVP hardening + export + docs

- Failure isolation per company, retryable `jobs`, structured `error{code,message,retryable}`, exportable flat JSON for every result, docs for the four-cadence workflow (initial fleet → nightly Börsdata sync → daily MFN delta → weekly page-2 → per-company update), explicit deferred list (§9), and the pilot run.

---

## 7. Validation Plan — golden-packet, model-number fidelity, point-in-time, idempotence, pilot

All checks run without a paid model when marked *deterministic*; the pilot is the only end-to-end live-model check.

### 7.1 Golden-packet (deterministic reproducibility without a model)

- **Check:** `alphaforge export --as-of 2025-08-15` for the curated watchlist → `exports/<as_of>/{ranking.json, <ticker>/packet.json}` saved as fixtures (`tests/fixtures/golden/<as_of>/`).
- **Assertion:** reloading `packet.json` and calling every `alphaforge.core.*` path (`financial/calculator`, `valuation/*`, `ranking/engine`, `coverage/liquidity`, `returns/total_return`) with no model call reproduces byte-identical `ranking.json` `packet_hash` and all deterministic valuation outputs (`RawValuation`, `ValuationResult`, `FinancialResult`, `DcfAssumptions`, `ReverseDcfResult`, `ScenarioBandResult`). `sha256(canonical_json)` equality, not diff-tolerant.
- **Fixture source:** the live Börsdata payloads from `scout-borsdata-endpoints` (42 JSON dumps) plus MFN PDF bytes already under `/tmp/borsdata_probe/` / `/tmp/vitec2025.pdf` scope — not a hand-authored pack. CI runs `pytest tests/test_golden_packet.py -q` and fails on any drift.

### 7.2 Model-number fidelity — the highest-risk check (model cannot change a number)

- **Check:** for a fixed `packet_hash` (e.g., pilot ticker `NIBE B`), inject a deliberately wrong LLM completion (reverse-DCF `implied_ebit_margin` off by 250 bps, `ev_ebit` off by 0.5×, a fabricated `free float 42%`). The `llm/validate.py` tripwire must **reject** and emit `error: number_mismatch {field, expected, got, eps}`.
- **One-repair ceiling proof:** the same wrong completion, re-prompted once with the validator error list, must either return corrected values or produce `status=insufficient_evidence` with the field suppressed — never silent zero-fill or templated fallback prose.
- **Coverage:** doubles as `fx` misuse proof — a cross-currency thesis that converted a **ratio** (e.g., `netDebt/EBITDA` FX-adjusted) must be rejected; only sums may be converted.

### 7.3 Point-in-time discipline (no future leakage)

- **Check:** build a packet at `as_of = report_Date - 1 day` for a company whose FY report is published on `report_Date`; assert the report is **absent** from the packet. Rebuild at `as_of = report_Date`; assert it is present. Same for MFN body `published_at`, `kpi_observation.observation_date`, insider `Publiceringsdatum`, short snapshot vintage.
- **Guard-two-layer proof:** inject a future-dated `research_documents` row (publication = `as_of + 7 days`) into a fixture DB and assert **both** the adapter trim and the packet re-filter exclude it — golden-packet `packet_hash` unchanged vs fixture without the row.
- **Bilingual PIT proof:** insert both `sv` and `en` variants of one release; assert packet text count increments by 1 (not 2) and `duplicate_of` is populated.

### 7.4 Idempotence & correctness invariants

- **Idempotence:** `alphaforge sync --company NIBE --as-of 2025-08-15` twice consecutively → rowcounts for `financial_periods / kpi_observations / prices / dividends / news_releases / research_documents` unchanged (second pass `INSERT … ON CONFLICT DO UPDATE` count zero inserts). Parallel invariant: two `rank --as-of` runs with identical inputs emit identical `ranking_runs.packet_hash`; a third `sync` after no new Börsdata publication leaves `ranking_runs.packet_hash` unchanged.
- **Stub-zero quarantine:** for a 2620-class newly-listed instrument (`reportsYear=[]` yet `reportsQuarter=[{revenues:0.0, report_Date:null}]`), assert `financial_periods.is_placeholder=1` and that no ranking/valuation path with `WHERE is_placeholder=0` surfaces it as revenue-zero.
- **`maxCount` bug regression:** call `BorsdataAdapter.stock_prices(insId)` with `max_count=10` and without; assert both return the **same** row set after adapter slicing (backend bug not propagated to storage).
- **MFN cadence invariant:** `mfn_feed_checks` receives a row every day per watchlist issuer even on zero delta; Sunday page-2 sweep produces at most one net new report row per issuer in tests (no duplicate page-1 re-ingest).
- **`currency_ratio` verification gate:** a unit test asserts the stored `financial_periods.currency_ratio` equals the Börsdata raw `currency_Ratio` verbatim and that `fx.convert_sum` is the only caller that reads it; a live integration step logs the SEM verification (see §3.5) without asserting financial correctness.

### 7.5 End-to-end pilot — 2–3 companies from the curated watchlist

- **Selection (captain-curated):** 2–3 Swedish small caps with **positive earnings** from the 100–150 watchlist, varied by `branch_id` but all `general` model-eligible (property/bank deliberately avoided for MVP pilot), each with ≥8 quarters of MFN history (so S7 ledger can cover 8–12q).
- **Procedure (the plan's workhorse):**

  ```
  alphaforge import-watchlist --file imports/watchlist_2026-09-16.csv
  alphaforge sync   --as-of 2026-09-16            # nightly-shape initial
  alphaforge sync   --as-of 2026-09-16            # idempotence proof (rowcounts unchanged)
  alphaforge rank   --as-of 2026-09-16            # golden packet + ranking_runs row
  alphaforge evidence --ticker <pilot> --as-of 2026-09-16   # packet.json with packet_hash
  alphaforge analyze  --ticker <pilot> --as-of 2026-09-16   # 6 specialists + aggregator, one-repair ceiling
  alphaforge export --as-of 2026-09-16
  ```

  Then simulate a new earnings release (advance `as_of` by one interim): re-run `sync → evidence → analyze` for only that issuer via the **thesis update path** (news agent → Hedborg aggregator re-read), assert a new `theses{revision}` row with `prior_packet_hash` + `change_log` rather than a fleet re-rank.

- **Checked:** falsifiable one-sentence case present and sourced; bear/base/bull bands with annualized returns present; sell/invalidation conditions (7 types) present with `observable_metric_or_event + threshold_or_direction`; three ownership limitations explicitly listed when applicable; package hash provenance round-trips; export flat files validate against `thesis.schema.json`.
- **Highest-risk check inside the pilot:** the determinism/model split — run `analyze` twice on the same packet with identical inputs and assert the deterministic valuation numbers (`RawValuation`, `ReverseDcfResult`, `ScenarioBandResult`) are byte-identical between runs, while only the narrative prose (citations excluded) is model-sourced.

### 7.6 Scope of CI enforcement

`Lint` (import isolation + ruff) + `Test` (golden-packet + point-in-time + idempotence + coverage) + `Repo invariants` (secret-leak + bilingual-dedupe smoke) are **required status checks** for every PR. The pilot live-model step and the nightly Börsdata/FI integrations are **not** CI-gated (opt-in `pytest -m integration`), but the plan's pilot artifacts (`exports/2026-09-16/<ticker>/{packet,thesis}.json`) are committed as fixtures on the pilot PR so the determinism gate stays green without a live key.

---

## 8. Risks

| Risk | Likelihood | Impact | Mitigation (what the plan does) |
|---|---|---|---|
| **Scope creep back toward CompanyScraper (30 tables/30 CLIs)** | Medium | High | Explicit **deferred list** (§9) plus branch protection + import-isolation law. Each deferred item needs a demonstrated need and a new plan PR. |
| **Thin MFN/PDF history for illiquid micro caps (false ineligibility)** | High | Medium | Blocker-vs-limitation split: missing ledger → `evidence_blocked` (gate prevents paid call), missing peak/ownership → `limitation` not blocker. 50+tail + page-2 backstop + 8–12q “partial_coverage” tolerance. |
| **Nordic-language ledge (hedge nuance lost in translation)** | High | High | Swedish-aware prompt variants, no translation layer, paragraph-anchored excerpts, majority-lang selector, §4.4 Hedborg-fidelity. |
| **Börsdata rate-limit / Cloudflare throttling (batch 50 fleet spike)** | Medium | Medium | Adapter 0.5–1 s sleep, `Retry-After`, per-company failure isolation, nightly sync not fleet-burst, `jobs` table retry semantics; FI PDMR windowed ≤1/q2s. |
| **Over-fitting Hedborg score weights early** | Medium | Low | Weights versioned (`2026-08-12-reverse-dcf-v10`), config-versioned, unevaluated in MVP (no calibration machinery — deferred). |
| **Hedborg witness gaps (earnings one-off risk, dilution cause, peak margin subjectivity)** | Medium | Medium | Deterministic guards (`earnings_growth_one_off_risk`, `share_dilution>5%`, `gross→EBIT spread` clue vs conclusion); model authors mechanism, core clamps the number. |
| **Brand risk (scraped PDFs, MFN/FI ToS)** | Low | High | Use only the company's own annual reports via `storage.mfn.se`/company IR for internal research; respect FI throttle quote and never bulk-burst the PDMR search; no Avanza/Nordnet holder-HTML scraping (blocked by SPA session and ToS). |
| **`currency_ratio` semantics misread (FX utility computes the wrong SEK direction)** | Low | High | Phase 0 verification gate: two-company spot vs ECB rate + `scout-borsdata-api-coverage` reconciliation before the utility is live. Until verified, FX port stays `available=False` with limitation. |
| **MFN bilingual double-count inflating evidence** | Medium | Low | Deterministic `(mfn_slug, canonical_url, storage_id)` group + `duplicate_of` pointer; validated by the bilingual PIT proof in §7.3. |
| **Rollback cost for any phase** | Low | Low | Each phase is additive (new tables `ADD COLUMN`, new CLI subcommands); theses are immutable — nothing rewrites persisted rows. |

---

## 9. Deferred — explicit (no surprises)

Every item here is **out of MVP** by captain call or by design choice; promoting it requires a demonstrated need and a plan addendum PR.

- **Paid ownership vendor** (`Modular Finance Holdings`, Euroclear Monitor, Holdings.se API) — weekly `HoldingsAdapter.get_holders(isin, as_of)` with `ownership_holders_api_staging` + `free_float_snapshots`. Architecture is ready (purpose-built `source_kind` enum); vendor is not wired.
- **Call transcripts** — `research_documents.source_type = transcript`, transcript-page text. Reserved enum, no scraper; ledger rows cite reports + MFN bodies only in MVP. Promote when transcript citations would exceed 20% of ledger rows in pilot (evidence-sources report criterion).
- **OHLC columns** — `open/high/low` in `prices`. Captain closed this (closing price + volume only).
- **Multi-currency full coverage** — beyond the sums-only FX utility, no full report-currency normalization into SEK; non-SEK companies remain `valuation_blocked` or limited unless the FX utility resolves their sums, and **ratios are never converted**.
- **Scheduler/daemons/Discord/browser scraping** — use stable feed/API only; cadence is via `cron`/`systemd` invoking `alphaforge sync` and `alphaforge analyze --watchlist` on the schedule in §5.2, not via in-process `schedule`.
- **Multi-provider model support / 9-provider router / capability tables** — one `LLMClient` via OpenAI-compatible endpoint; provider is a DSN string, not a registry.
- **Sector-specific ranking models** — property (branch 75) / bank (68–70) models are `method_unsupported` in MVP; sport only `general`.
- **Bull/Bear multi-round debate, Trader/Risk-Manager/Portfolio-Manager execution roles** — replaced by the lightweight `Risks & Thesis-Break` disconfirming-evidence section; ownership/flow timing read is never a mechanical signal.
- **LangGraph orchestration, RAG retrieval product, LoRA training, RL environment, DRL policy** — plain `asyncio.gather` + typed packets; RAG stays a context-window tool for S7 when the 50+tail corpus exceeds ~12k tokens, not a retrieval product.
- **Web app (FastAPI/auth/SQLAlchemy), HTML/PDF report publishing beyond JSON/CSV export** — deferred until `evidence → analyze → export` is trusted; export is `exports/<as_of>/ranking.json+csv` + `<ticker>/{packet,thesis}.json` only.
- **Full insider/buyback/short-interest coverage as a trading signal, cohort grace-period logic, comparative verdict agent, portfolio selection, benchmark/performance machinery, backtest attribution, weight calibration, ranking challengers, dividend-review UI** — per draft non-goals and `PROJECT_FUNCTIONALITY.md` §8, carried forward.

Scope discipline rule: a deferred item ships only after the pilot validation gate for the current phase is green.

---

## 10. Open Slots — `scout-borsdata-api-coverage` integration (on landing)

At freeze this scout is `working` (16 17:06 UTC). Do not block the authoritative plan on it; integrate via a small Phase 0 PR as soon as it lands:

1. Reconcile the official Börsdata spec's full endpoint list against the 10 verified families (§2). Classify each spec entry as **in-use / available-but-unused (with purpose: ranking/valuation/Hedborg evidence) / stale**.
2. Adjudicate **one** addition per availability: KPI-definition/metadata, splits, calendar events, per-branch KPI listings, global metrics — name the AlphaForge need or say `none`.
3. **Verify `currency_ratio` semantics** if the spec documents it — confirm or correct the FX utility's `SEK per foreign unit` assumption in §3.5; if the spec is silent, keep the live spot-check gate.

Until that PR, the FX utility stays behind the verification gate and no speculative extra endpoint is wired into `BorsdataAdapter`.

---

## 11. Sources

- `scout-borsdata-endpoints/report.md` — 10 endpoints / 73 markets / 1 709 instruments / stub-zero & `maxCount` anomaly / 14 gaps (standalone, 306 lines at probe basis; live 2026-09-16)
- `scout-oss-market-research/report.md` — FinRobot 13.5k lines + TradingAgents LangGraph `setup.py:GraphSetup.setup_graph` + 852-line deep analysis (G1–G17 / A1–A17 with file/line provenance)
- `scout-alphaforge-data-foundation/report.md` — 1 348-line design (SQLite WAL vs Postgres vs DuckDB, §2 DDL, §3 17-module map, §4 pipeline, §5 fold, Appendix D drop-in section — preferred for this plan's §3)
- `scout-alphaforge-evidence-sources/report.md` — 536-line evidence pipeline (MFN Playwright 24 cap, `pypdf` 30→50, `evidence_catalog` / `packet_hash`, S7–S13 typed ledger/ownership, validation contract `agent-boundary-v23`)
- `scout-ownership-free-sources/report.md` — 507-line free-holder scout (Vitec 194 pp p.51 / Acast p.27 holder tables, Mycronic flagging, `marknadssok.fi.se` 211 Vitec rows, FI blankning, Euroclear challenge, Holdings.se SPA paywall, Avanza `numberOfOwners`; three limitations + capped sub-score)
- `scout-borsdata-api-coverage/report.md` — **pending** (official spec vs 10 families, `currency_ratio` semantics — integrate on landing)
- `PycharmProjects/KN-CompanyScraper` — `src/kncompanyscraper/borsdata/{client.py:26/49/71/81/99/166/180/230/304/347, kpi_ids.py, report.py}`, `analysis/{ranking/*, valuation/*, financial/*}`, `analysis/agent/{research_document_ingestion.py:26-260, mfn_scraper.py:15-182, ownership_liquidity_evidence.py:33-176, execution_boundary.py:65-550, output_schema.py, specialist_runner.py}`, `repositories/*`, `constants.py:5-14 REPORT_TITLE_TERMS`, `.github/workflows/ci.yml` (Python 3.11 `pip install -e ".[dev]"` house reference)
- FinRobot: `https://raw.githubusercontent.com/AI4Finance-Foundation/FinRobot/master/README.md`, `finrobot_equity/README.md`; TradingAgents: `https://raw.githubusercontent.com/TauricResearch/TradingAgents/main/README.md`
- Hedborg philosophy: `docs/petter_hedborg_investment_philosophy.md` §§ 7–8, 12–13 (case-not-company, circle of competence, scalable profit, three-stage engine, gross→EBIT spread, falsifiable thesis, credibility ledger 8–12q, ownership 12-item checklist, Stage A gates, Stage B 10/100 ownership weight, Stage C 13-part output)

---

## Addendum A — Data Foundation DDL (normative, excerpt from Appendix D)

*This addendum reproduces the SQLite DDL from `scout-alphaforge-data-foundation` Appendix D with the consolidated v2 amendments (closing-price-only, `fx_rate_to_sek` audit columns, 50+tail metadata). The Postgres variant is the delta described in §3.4; the `importlinter` composition owner is §3.3 / Phase 0. File to commit: `db/alphaforge.sqlite.sql` (normative), `db/alphaforge.postgres.sql` (variant).*

```sql
-- SQLite 3.38+ (WAL, json1, ON CONFLICT DO UPDATE)
-- Enable once at connection open: PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL;
-- Files: data/alphaforge.db (gitignored) | data/alphaforge.test.db

CREATE TABLE IF NOT EXISTS companies (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    borsdata_id         INTEGER UNIQUE NOT NULL,
    name                TEXT NOT NULL,
    ticker              TEXT,
    yahoo               TEXT,
    isin                TEXT,
    instrument          INTEGER NOT NULL DEFAULT 0 CHECK (instrument IN (0,1)),
    sector_id           INTEGER,
    branch_id           INTEGER,
    market_id           INTEGER,
    country_id          INTEGER,
    listing_date        DATE,
    stock_price_currency TEXT,
    report_currency     TEXT,
    raw_payload         TEXT CHECK (raw_payload IS NULL OR json_valid(raw_payload)),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;
CREATE INDEX IF NOT EXISTS idx_companies_ticker ON companies(ticker);
CREATE INDEX IF NOT EXISTS idx_companies_branch ON companies(branch_id);

CREATE TABLE IF NOT EXISTS watchlist (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    ticker              TEXT NOT NULL,
    isin                TEXT,
    name_hint           TEXT,
    source_file         TEXT NOT NULL,
    source_row_hash     TEXT NOT NULL,
    matched_via         TEXT CHECK (matched_via IN ('isin','ticker','borsdata_id','unmatched')),
    borsdata_id         INTEGER,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (source_file, source_row_hash),
    UNIQUE (company_id)
) STRICT;

CREATE TABLE IF NOT EXISTS financial_periods (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    period_type         TEXT NOT NULL CHECK (period_type IN ('year','r12','quarter')),
    period_end          DATE NOT NULL,
    report_year         INTEGER,
    report_period       INTEGER,
    report_date         DATE,
    broken_fiscal_year  INTEGER CHECK (broken_fiscal_year IN (0,1)),
    currency            TEXT,
    currency_ratio      REAL,
    fx_rate_to_sek      REAL CHECK (fx_rate_to_sek IS NULL OR fx_rate_to_sek > 0),
    fx_source           TEXT CHECK (fx_source IN ('currency_ratio','manual','null') OR fx_source IS NULL),
    revenue             REAL,
    gross_income        REAL,
    operating_profit    REAL,
    ebit                REAL,
    ebitda              REAL,
    net_income          REAL,
    free_cash_flow      REAL,
    operating_cash_flow REAL,
    investing_cash_flow REAL,
    financing_cash_flow REAL,
    equity              REAL,
    total_assets        REAL,
    total_debt          REAL,
    cash                REAL,
    eps                 REAL,
    dividend_per_share  REAL,
    shares_outstanding  REAL,
    is_placeholder      INTEGER NOT NULL DEFAULT 0 CHECK (is_placeholder IN (0,1)),
    raw_payload         TEXT CHECK (raw_payload IS NULL OR json_valid(raw_payload)),
    fetched_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (company_id, period_type, period_end),
    CHECK (is_placeholder = 1 OR revenue IS NOT NULL OR net_income IS NOT NULL OR equity IS NOT NULL OR total_debt IS NOT NULL)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_financials_company_period_end ON financial_periods(company_id, period_type, period_end DESC);
CREATE INDEX IF NOT EXISTS idx_financials_pit ON financial_periods(company_id, period_type, report_date, period_end) WHERE is_placeholder = 0;

CREATE TABLE IF NOT EXISTS kpi_observations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    kpi_id              INTEGER NOT NULL,
    period_type         TEXT NOT NULL CHECK (period_type IN ('last','year','r12')),
    price_type          TEXT NOT NULL CHECK (price_type IN ('latest','mean','high','low')),
    observation_date    DATE,
    year                INTEGER,
    report_period       INTEGER,
    value               REAL,
    fx_rate_to_sek      REAL CHECK (fx_rate_to_sek IS NULL OR fx_rate_to_sek > 0),
    fx_source           TEXT CHECK (fx_source IN ('currency_ratio','manual','null') OR fx_source IS NULL),
    fetched_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK ((period_type='last' AND observation_date IS NOT NULL) OR (period_type IN ('year','r12') AND year IS NOT NULL))
) STRICT;
-- Partial unique indexes — conflict targets for ON CONFLICT DO UPDATE (KPI sync)
-- Snapshot: one latest value per kpi per observation_date
CREATE UNIQUE INDEX IF NOT EXISTS uq_kpi_snapshot
    ON kpi_observations(company_id, kpi_id, period_type, price_type, observation_date)
    WHERE period_type = 'last';
-- History: one value per kpi per year+report_period per period/price type
CREATE UNIQUE INDEX IF NOT EXISTS uq_kpi_history
    ON kpi_observations(company_id, kpi_id, period_type, price_type, year, report_period)
    WHERE period_type IN ('year','r12');

CREATE TABLE IF NOT EXISTS prices (
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    price_date          DATE NOT NULL,
    close               REAL NOT NULL CHECK (close > 0),
    volume              INTEGER CHECK (volume IS NULL OR volume >= 0),
    currency            TEXT,
    fetched_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (company_id, price_date)
) STRICT WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_prices_company_date ON prices(company_id, price_date DESC);
CREATE INDEX IF NOT EXISTS idx_prices_liquidity ON prices(company_id, price_date DESC) WHERE volume IS NOT NULL;

CREATE TABLE IF NOT EXISTS dividends (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    ex_date             DATE NOT NULL,
    amount              REAL NOT NULL CHECK (amount >= 0),
    currency            TEXT NOT NULL,
    dividend_type       INTEGER NOT NULL CHECK (dividend_type IN (0,1,2)),
    distribution_frequency TEXT,
    fetched_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (company_id, ex_date, dividend_type, amount)
) STRICT;

CREATE TABLE IF NOT EXISTS dividend_coverage (
    company_id          INTEGER PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
    covered_from        DATE NOT NULL,
    covered_through     DATE NOT NULL,
    source              TEXT NOT NULL DEFAULT 'borsdata',
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (covered_through >= covered_from)
) STRICT;

CREATE TABLE IF NOT EXISTS ranking_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    as_of               DATE NOT NULL,
    model_version       TEXT NOT NULL,
    packet_hash         TEXT,
    universe_hash       TEXT,
    company_count       INTEGER NOT NULL,
    eligible_count      INTEGER NOT NULL,
    scores              TEXT NOT NULL CHECK (json_valid(scores)),
    inputs_summary      TEXT CHECK (inputs_summary IS NULL OR json_valid(inputs_summary))
) STRICT;

CREATE TABLE IF NOT EXISTS research_documents (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    source_url          TEXT NOT NULL,
    source_type         TEXT NOT NULL CHECK (source_type IN ('mfn','annual_pdf','transcript','press_release','other')),
    title               TEXT,
    published_at        DATE,
    fetched_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    content_text        TEXT,
    page_count          INTEGER,
    pages_included      TEXT,
    page_truncated      INTEGER CHECK (page_truncated IN (0,1)),
    duplicate_of        INTEGER REFERENCES research_documents(id),
    ingested_lang       TEXT,
    checksum            TEXT,
    raw_metadata        TEXT CHECK (raw_metadata IS NULL OR json_valid(raw_metadata)),
    UNIQUE (checksum),
    UNIQUE (company_id, source_url)
) STRICT;

CREATE TABLE IF NOT EXISTS theses (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    revision            INTEGER NOT NULL CHECK (revision > 0),
    as_of               DATE NOT NULL,
    packet_hash         TEXT NOT NULL,                -- content-addressed for full recalcs; reused for light revisions (see §5.3)
    prior_packet_hash   TEXT,
    change_log          TEXT CHECK (change_log IS NULL OR json_valid(change_log)),
    thesis_json         TEXT NOT NULL CHECK (json_valid(thesis_json)),
    model               TEXT NOT NULL,
    prompt_version      TEXT NOT NULL,
    verdict             TEXT CHECK (verdict IN ('reject','watch','latent','activated')),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (company_id, revision)                     -- identity: revision increments per company; packet_hash uniqueness is NOT enforced in DDL (application checks for duplicate full-recalc before insert)
) STRICT;

CREATE TABLE IF NOT EXISTS jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type            TEXT NOT NULL CHECK (job_type IN ('sync_instruments','sync_reports','sync_kpis','sync_prices','sync_dividends','sync_insider','sync_buyback','sync_shorts','rank','evidence','analyze','export')),
    company_id          INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    borsdata_id         INTEGER,
    status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','success','failed','partial')),
    attempt             INTEGER NOT NULL DEFAULT 0,
    error               TEXT CHECK (error IS NULL OR json_valid(error)),
    started_at          TEXT,
    finished_at         TEXT,
    fetched_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (job_type, company_id)
) STRICT;

CREATE TABLE IF NOT EXISTS mfn_feed_checks (
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    mfn_slug            TEXT NOT NULL,
    checked_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    discovered_count    INTEGER NOT NULL,
    unseen_count        INTEGER NOT NULL,
    PRIMARY KEY (company_id, checked_at)
) STRICT;

CREATE TABLE IF NOT EXISTS news_releases (
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    slug                TEXT,
    url                 TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    body                TEXT,
    published_at        DATE,
    duplicate_of        TEXT REFERENCES news_releases(url),
    lang                TEXT,
    release_category    TEXT,
    scraped_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

-- Free-ownership deterministic staging (ship with ownership stack)
CREATE TABLE IF NOT EXISTS ownership_holders_pdf_staging (
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    observation_date    DATE NOT NULL,
    holder_name         TEXT NOT NULL,
    shares              REAL,
    pct                 REAL CHECK (pct IS NULL OR (pct >= 0 AND pct <= 100)),
    source_id           TEXT NOT NULL,
    raw_text            TEXT,
    PRIMARY KEY (company_id, observation_date, holder_name)
) STRICT;

CREATE TABLE IF NOT EXISTS ownership_events_staging (
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    event_date          DATE NOT NULL,
    event_type          TEXT NOT NULL CHECK (event_type IN ('flagging','placement','lockup','share_change')),
    holder_name         TEXT,
    shares              REAL,
    pct                 REAL,
    lockup_expiry_date  DATE,
    source_ids          TEXT NOT NULL CHECK (json_valid(source_ids)),
    PRIMARY KEY (company_id, event_date, event_type, holder_name)
) STRICT;

CREATE TABLE IF NOT EXISTS company_short_snapshots (
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    observation_date    DATE NOT NULL,
    shorts_proc         REAL,
    shorts_holders      REAL,
    shorts_milj         REAL,
    dtc_avg             REAL,
    trend_1w            REAL,
    trend_1m            REAL,
    source              TEXT NOT NULL,
    PRIMARY KEY (company_id, observation_date)
) STRICT;

PRAGMAS = [
  'PRAGMA journal_mode=WAL;',
  'PRAGMA synchronous=NORMAL;',
  'PRAGMA foreign_keys=ON;',
  'PRAGMA busy_timeout=5000;',
];
```

---

*Plan authoritative as of 2026-09-16. All investigation and design work folded; no code beyond `docs/` is changed by this PR. Next step is Phase 0 CI/branch-protection PR merge, then Phase 1 scaffold.*
