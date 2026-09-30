# AlphaForge

A clean, simple stock-research system for the Nordic small-cap universe: one main **Stock Researcher** agent (Petter Hedborg investment philosophy) plus a small set of specialist agents that each own one report section. Deterministic ranking, valuation, and return math stay in pure, model-free code; language models only interpret evidence inside a bounded, cited packet and can never change a number.

## Status

Implemented: deterministic ranking/valuation exports and a one-company MFN/PDF evidence lane; thesis generation remains planned. Start with [`docs/architecture.md`](docs/architecture.md) for implemented paths, boundaries and contract ownership. The target design remains [`docs/plans/2026-09-16-alphaforge-mvp.md`](docs/plans/2026-09-16-alphaforge-mvp.md).

## Design principles

- Simple, clean, small over complete — understandable by one developer without tracing a large workflow graph.
- Every material claim cites a stored source or is marked as a limitation; missing data is visible, never silently zero-filled.
- All arithmetic is deterministic and reproducible from stored inputs.
- 2–3 year falsifiable investment cases with explicit sell conditions.

## Related

Predecessor with full production history: [KN-CompanyScraper](https://github.com/KapKnudz/KN-CompanyScraper) (~30k lines, PostgreSQL, 30+ CLI commands) — AlphaForge keeps its core ideas while shedding its complexity.
