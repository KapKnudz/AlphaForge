# Project agent memory

This file is the project's committed home for project-intrinsic agent knowledge: build, test, release, architecture, and sharp-edge notes that should travel with the code.

- Add durable project-specific notes here as they are discovered through real work.

## Maintaining this file

Keep this file for knowledge useful to almost every future agent session in this project.
Do not repeat what the codebase already shows; point to the authoritative file or command instead.
Prefer rewriting or pruning existing entries over appending new ones.
When updating this file, preserve this bar for all agents and keep entries concise.

## Pinned thesis constraints

Use `docs/plans/2026-09-16-alphaforge-mvp.md` as the design authority. Do not violate these constraints:

- Keep validation and repair in the deterministic core. A framework that can retry on a validation error may not own validation; its retry lifecycle would create a second, invisible repair path.
- Classify retries by trigger: transport retries may repeat an identical idempotent request; a format retry gets one attempt; a validation repair gets exactly one attempt. If validation fails a second time, produce `insufficient_evidence`, never fallback prose.
- Keep semantic recall and open-ended retrieval out of the pinned thesis path. Freeze and hash the evidence packet before any model call, and require every material claim either to cite a catalogued source or to be recorded as a limitation.
- Restrict agentic retrieval to evidence acquisition. Before the frozen path runs, turn its findings into a packet with canonical source ids, observation/publication/ingestion dates, and paragraph anchors.
- Use the source-level evaluation anchor `CrewAI 1.15.22 (2026-09-18)` for a future framework re-check. Revisit it only for a genuinely new open-ended-tools or persistent-conversation requirement.

## Provider contract pointers

- Börsdata dividend sync accepts the live nested calendar shape, including `excludingDate` and `dividendType=4`; adapter, fixture, and SQLite migration coverage live in `alphaforge/providers/borsdata/adapter.py`, `tests/fixtures/borsdata/live_dividend_calendar.json`, and `db/migrations/002_allow_dividend_type_4.sql`.
- Deterministic MFN issuer mapping and the one-company frozen PDF evidence lane are documented in `docs/evidence-flow.md`; implementation seams are `alphaforge/providers/mfn/issuer.py`, `alphaforge/evidence/flow.py`, and repository-owned evidence tables.
