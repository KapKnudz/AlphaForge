## Context

Link to plan / issue:

## Scope

- [ ] ...

## Tests

- [ ] `ruff check . && ruff format --check .`
- [ ] `lint-imports` (importlinter contracts)
- [ ] `pytest -q` (integration tests excluded by `-m 'not integration'`)

## Secret hygiene

- [ ] No `BORSDATA_API_KEY` value, no 32-hex Börsdata-like literal, no `.env` contents in diff
- [ ] CI invariant job (`Repo invariants`) passes — no `BORSDATA_API_KEY` in `.github/workflows/`

## Screenshots (if renderer)

