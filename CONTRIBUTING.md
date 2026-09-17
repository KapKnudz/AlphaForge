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

## Secret handling

- The live Börsdata API key lives **only** in a local untracked `.env` (`BORSDATA_API_KEY=`). Never commit `.env` or `.env.*`.
- `.env.example` is tracked and empty.
- CI invariant job scans for generic 32-hex + `borsdata` co-occurrence and for `BORSDATA_API_KEY` in workflows; it never embeds a literal key.
- If a key is ever committed, rotate immediately at https://www.borsdata.se.

## Import isolation

Deterministic core (`alphaforge/core/`) must not import model or integration layers. Enforced by `importlinter` contracts in `pyproject.toml` and by a `ruff` rule on `alphaforge/core/`. Keep `alphaforge/core/kpi_taxonomy.py` as the only file that maps Börsdata raw keys.
