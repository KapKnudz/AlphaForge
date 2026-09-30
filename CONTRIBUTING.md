# Contributing — AlphaForge

## Commit convention

AlphaForge uses **Conventional Commits** with a linear history and **Squash and merge** as the PR merge strategy.

Format:

```
<type>(<scope>): <short imperative summary>

[optional body]
[optional footer: Closes #123]
```

Types:

- `feat:` — new feature
- `fix:` — bug fix
- `docs:` — documentation / plan only
- `chore:` — tooling, CI, scaffolding, deps
- `refactor:`, `test:`, `perf:` — as needed

Examples:

- `docs: integrate Börsdata 33-path coverage into plan`
- `chore: add CI lint+test+invariants and .gitignore for .env`
- `feat: add Börsdata adapter batch sync (reports/kpis/prices)`

Rules:

- One logical change per commit; keep commits atomic for rebase-friendly review.
- Do not merge `main` into feature branches — rebase instead.
- PRs must be green (`Lint`, `Test`, `Repo invariants`) before merge; branch protection requires at least 1 approval, `enforce_admins: false`.

## Contract alignment

For larger fixes and changes crossing acquisition, persistence, calculation,
selection, readiness or export boundaries, include this lightweight checklist in
the PR description (or explain why an item is not applicable):

- [ ] Identify affected contracts and their owners using the
  [architecture overview](docs/architecture.md): deterministic flow, valuation,
  textual evidence, schema or target design. Keep formulas/policies in
  `docs/valuation.md` and textual selection in `docs/evidence-flow.md`.
- [ ] Trace downstream callers and consumers (including loaders, gates,
  persisted provenance and JSON/CSV exports); state identity, units/currency,
  cutoff, version or missing-value implications where relevant.
- [ ] Update the owning documentation when behavior changes. Distinguish
  implemented behavior from planned design, and report contract/implementation
  disagreements explicitly rather than silently changing policy.
- [ ] Add or extend executable cross-boundary tests for changed behavior and
  failure/replay cases, not only isolated helpers. For documentation-only work,
  verify claims against modules/callers/tests and check local links; new
  behavioral tests are unnecessary unless behavior changes.

This complements the existing green-check and approval requirements; it is not
an additional mandatory reviewer, model dependency or automated semantic
architecture audit.

## Checks

Use Python 3.11+ and install development dependencies with
`python -m pip install -e '.[dev]'`. From the repository root, run the checks in
[CI](.github/workflows/ci.yml):

```sh
ruff check .
ruff format --check .
lint-imports
python tools/check_evidence_manifest_boundary.py
pytest -q
```

Tests exclude live integrations by default (`pyproject.toml`). Opt-in live
verification and environment resolution are documented in
[the evidence contract](docs/evidence-flow.md#live-verification); do not add
credentials or model calls to CI. For documentation changes, also verify local
file links and heading anchors. Passing structural checks and fixture tests is
not proof of universally enforced semantic alignment.

## Secret handling

- The live Börsdata API key lives **only** in a local untracked `.env` (`BORSDATA_API_KEY=`). Never commit `.env` or `.env.*`.
- `.env.example` is tracked and empty.
- CI invariant job scans for generic 32-hex + `borsdata` co-occurrence and for `BORSDATA_API_KEY` in workflows; it never embeds a literal key.
- If a key is ever committed, rotate immediately at https://www.borsdata.se.

## Import isolation

Deterministic core (`alphaforge/core/`) must not import model or provider layers or DB drivers. The forbidden-import contracts in `pyproject.toml` enforce those named dependencies; `ruff` checks lint/style, not a separate core-isolation rule. Keep `alphaforge/core/kpi_taxonomy.py` as the only file that maps Börsdata raw keys. The evidence-manifest check enforces its own specific consumer boundary; neither check proves all architectural semantics.
