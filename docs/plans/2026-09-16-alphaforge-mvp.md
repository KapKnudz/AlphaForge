## Goal

Confirm shared understanding and plan the AlphaForge MVP: a clean, simple stock-research system that keeps the core idea of KN-CompanyScraper, is much less complex, and is organized as one main **Stock Researcher** agent (built on the Petter Hedborg philosophy) plus a small set of specialist agents that each own one report section, inspired by market solutions such as FinRobot and TradingAgents.

## Success Criteria

- The user can answer "which companies should I research, and why?" for a Nordic small-cap universe with one ranking, one evidence packet, and one structured thesis per company.
- Every thesis contains a falsifiable 2–3 year case (revenue driver, margin path, defensible multiple, expected return, thesis-break evidence) in Hedborg's one-sentence form.
- All arithmetic (ranking, valuation, reverse-DCF, returns) is deterministic and reproducible from stored inputs; the language model never owns a number.
- Every material claim in a thesis cites a stored source or is marked as a limitation; missing data is visible, never silently zero-filled.
- The MVP is understandable by one developer without tracing a large workflow graph (small CLI, small schema, few agents).

## Context And Current Facts

- AlphaForge repo (`firstmate/projects/AlphaForge`) is an empty git repo (no commits yet); there is no existing AlphaForge code to preserve or migrate.
- KN-CompanyScraper (`PycharmProjects/KN-CompanyScraper`) is the reference implementation: ~29,700 lines of Python, ~30 database tables, 30+ CLI commands, PostgreSQL-backed, data from Börsdata / MFN-company feeds / Nasdaq OMXS30GI, model providers via local Codex CLI, OpenAI, DeepSeek (`README.md`, `PROJECT_FUNCTIONALITY.md` §§5, 13).
- The Hedborg philosophy is already distilled in `docs/petter_hedborg_investment_philosophy.md`: case-not-company thinking, narrow Nordic circle of competence, profitable/scalable models only, the three-stage return engine (revenue growth + margin expansion + multiple expansion), gross-vs-EBIT margin spread as a clue, simple 2–3 year scenarios, management/organizational-DNA credibility ledger (8–12 quarters), ownership/flows as a parallel layer (not a buy signal), latent-case watchlist with activation triggers, concentration without leverage, explicit sell/invalidation conditions, hard gates + evidence score + 13-part required output.
- `PROJECT_FUNCTIONALITY.md` §7 already sketches the lean rewrite the user wants: Phase 1 = watchlist → one provider sync → one deterministic ranking → shortlist export → small evidence set → one structured thesis via one model provider with validated output. Phases 2–3 (sector models, cohort grace logic, comparative ranking, portfolio selection, backtest/calibration/challengers, multi-provider) come only after Phase 1 is trusted. §8 lists what to defer (three providers, six model stages, cohort grace logic, challengers, calibration, full insider/liquidity coverage, scheduler, Discord, browser scraping).
- The company-level output contract exists as `individual-thesis-card-v2` (`docs/individual_thesis_card.md`): one analyst, own evidence only, normalized business-model profile, margin-expansion mechanism, revenue resilience, catalyst calendar, sourced fact ledger (reported fact vs management claim vs analyst inference).
- Domain rules to preserve (`PROJECT_FUNCTIONALITY.md` §6): deterministic ranking from persisted inputs; model as evidence interpreter, not calculator; no look-ahead bias; missing ≠ zero; pending/ineligible instead of fabricated returns; immutability of accepted theses; evaluation-only challengers; reviewed (not silently capped) dividend anomalies; idempotent sync.
- Market evidence inspected this run:
  - FinRobot V1 (`finrobot_equity`) structures equity research as one orchestrator plus per-section agents: tagline, company overview, investment thesis, valuation, risks, competitor analysis, key takeaways, news summary — over a deterministic pipeline (fetch → metrics/forecasts → peer comparison → text generation → charts → HTML/PDF report) with separate deterministic valuation, sensitivity, catalyst, and news modules.
  - TradingAgents structures analysis as an Analyst team (Fundamentals, Sentiment, News, Technical), a Bull/Bear Researcher debate team, a Trader that synthesizes, and Risk Management + Portfolio Manager that approve/reject — a trading-firm shape aimed at timed trade decisions.
  - FinRobot's philosophy statement keeps the financial domain layer, tools, deterministic computation, and workflows as the stable core while swapping agent frameworks (AutoGen → OpenAI Agents SDK → PydanticAI) — supporting the choice to keep AlphaForge's deterministic core framework-independent.

## Constraints And Non-goals

- Constraints: Nordic-first universe (Sweden first); 2–3 year case horizon; simple-clean-small over complete; deterministic/model/integration boundaries from `PROJECT_FUNCTIONALITY.md` §10; idempotent sync; explicit observation/publication/ingestion timestamps.
- Non-goals for the MVP: scheduler/daemons, Discord notifications, browser scraping (use stable feed/API), multi-provider model support, cohort grace-period logic, comparative verdict agent, portfolio selection, benchmark/performance machinery, backtest attribution, weight calibration, ranking challengers, dividend-review UI, full insider/buyback/short-interest coverage, sector-specific ranking models, PDF/HTML report publishing.

## Key Decisions

1. **Main agent = Stock Researcher with Hedborg skills (adopt).** The orchestrator owns the case: hard gates, evidence score, one-sentence falsifiable thesis, bear/base/bull scenarios, verdict (reject/watch/latent/activated), confidence + missing data. Hedborg principles become discrete skills (falsifiable-case author, peak-margin bridge, credibility ledger, reverse-DCF expectation check, ownership/flow timing read, sell-condition author) rather than one giant prompt — this is the "skills according to Petter Hedborg philosophy" the user asked for, grounded in `docs/petter_hedborg_investment_philosophy.md` §§12–13.
2. **Specialist agents own sections, not decisions (adopt).** Following FinRobot's per-section agents, each specialist returns sourced section content only; the main agent assembles and owns the verdict. Rejected alternative: TradingAgents-style autonomous Trader + Risk Manager that place/approve trades — wrong for a 2–3 year case investor and adds execution/risk machinery the MVP must not carry.
3. **Six specialists for the MVP (adopt).** Business Model & Scalability; Financials & Valuation-math-input (prepares deterministic inputs, authors no numbers); Management & DNA (credibility ledger); Ownership, Liquidity & Flows (signal + flow-effect read, never a mechanical signal); Risks & Thesis-Break (disconfirming evidence, sell conditions); News & Catalyst Calendar (timing windows, activation triggers). Rejected for MVP: Technical-analysis/MACD-RSI trader, Sentiment-chatter scorer, Bull/Bear multi-round debate — replaced by a lightweight confirm-vs-disconfirm evidence split inside the main agent.
4. **One market-data provider: Börsdata (adopt).** Already integrated in-house and Nordic-native. Rejected: FMP (FinRobot's provider, US-centric) and parallel multi-provider sync — one adapter interface, provider field names never leak into ranking/thesis code.
5. **Deterministic core stays model-free (adopt).** Ranking, KPI math, reverse-DCF solves, forward-scenario recalculation, return/dividend math, readiness gates, and citation validation are pure functions with unit tests. The model receives a bounded cited packet and returns structured claims; the application revalidates schema, citations, and all numbers, persisting prompt/model/version. This preserves KN-CompanyScraper rules 1–3, 6–7.
6. **Lean schema (~10 tables) and tiny CLI (adopt).** `companies, watchlist, financial_periods, kpi_observations, prices, dividends, ranking_runs, research_documents, theses, jobs` per `PROJECT_FUNCTIONALITY.md` §9; commands limited to `import-watchlist, sync, rank, evidence, analyze, export`. Benchmark/insider/challenge/challenger tables arrive only with their workflows.

## Recommended Approach

Build a Python service with three boundaries: deterministic core (pure functions, no model calls), model boundary (one provider, strict packet-in/claims-out contract with revalidation), integration boundary (Börsdata adapter, MFN/document adapter). The Stock Researcher orchestrates: rank → gate → evidence packet → specialist sections (parallel, section-scoped packets) → assemble Hedborg thesis → validate → persist immutable thesis revision → export JSON/CSV. Framework choice for agents stays minimal (plain orchestrated calls with JSON schemas, no heavy agent framework) so the deterministic core survives any later framework swap, mirroring FinRobot's framework-independent domain layer.

## Work Plan

1. **Repo scaffold + contracts.** Initialize AlphaForge repo; write the Hedborg skill specs (six skills + scoring/gating rules) and the v1 thesis JSON schema (adapted from `individual-thesis-card-v2`, single-model scope). No provider code yet.
2. **Lean data layer.** Create the ~10-table schema with observation/publication/ingestion timestamps; Börsdata adapter (instruments, reports, KPIs, prices, dividends); MFN/document fetcher with page cap and source-URL retention; idempotent sync commands (`import-watchlist, sync`).
3. **Deterministic ranking + readiness gate.** One general ranking model with eligibility flags; persisted `ranking_runs`; deterministic readiness gate (hard blockers vs valuation limitations); `rank` and `export` commands with JSON/CSV output.
4. **Main Stock Researcher agent.** Orchestrator + Hedborg skills + one model provider; bounded cited packet; one-repair validation ceiling; immutable thesis persistence; `evidence` and `analyze` commands.
5. **Specialist section agents.** Six specialists behind the same packet/claims/validation boundary; section outputs feed fixed thesis slots; main agent retains verdict ownership.
6. **MVP hardening.** Failure isolation per company, retryable jobs, structured errors, exportable JSON for every result, docs for the six-phase workflow; explicit deferral list for everything in Non-goals.

## Validation Plan

- Unit tests for pure ranking math, reverse-DCF solves, scenario recalculation, dividend reinvestment, and timestamp/point-in-time discipline (no future leakage).
- Golden-packet test: identical stored inputs always reproduce the same ranking and the same deterministic valuation outputs without any model call.
- Schema + citation tests: malformed model output and unsupported citations are rejected or marked uncertain; missing valuation inputs suppress only the affected output and force portfolio-ineligibility.
- End-to-end dry run on 2–3 known Nordic companies: import → sync twice (idempotence) → rank → evidence → analyze → export, checking the falsifiable one-sentence case and sell conditions are present and sourced.
- Highest-risk check: the determinism/model separation test — proving the model cannot change a number — because the whole Hedborg credibility design rests on it.

## Risks / Rollback

- Scope creep back toward CompanyScraper complexity (sector models, challengers, scheduler) — controlled by the Non-goals list and phased plan; each deferred item needs a demonstrated need.
- Nordic data gaps (thin MFN history, illiquid prices) producing false ineligibility — mitigated by the blocker-vs-limitation distinction and visible missing-data flags.
- Over-fitting the Hedborg score weights early — mitigated by keeping weights config-versioned and unevaluated in the MVP (no calibration machinery).
- Rollback: each phase is additive (schema additions, new commands); nothing in Phase 1 rewrites persisted theses, which are immutable by design.

## Open Questions

- Which single model provider should the MVP use (local Codex CLI vs one paid API), given cost and Nordic-language evidence quality?
- Should the MVP universe be Sweden-only or all-Nordics from day one, and what is the initial watchlist source file?
- Is a web UI or JSON/CSV export sufficient for reading theses in the MVP?

## Sources

- https://raw.githubusercontent.com/AI4Finance-Foundation/FinRobot/master/README.md
- https://raw.githubusercontent.com/AI4Finance-Foundation/FinRobot/master/finrobot_equity/README.md
- https://raw.githubusercontent.com/TauricResearch/TradingAgents/main/README.md
