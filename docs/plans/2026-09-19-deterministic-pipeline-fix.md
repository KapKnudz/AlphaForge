# Deterministic pipeline completion plan

**Status:** implementation in progress
**Authority:** `docs/plans/2026-09-16-alphaforge-mvp.md`

## Reproduced failure modes

- SQLite URL parsing must distinguish repository-relative paths (`sqlite:///data/...`, `sqlite://./data/...`) from explicit absolute paths (`sqlite:////tmp/...`).
- Watchlist rows are intentionally importable before instruments exist; the sync must seed instruments, relink those rows, and then scope work to matched watchlist companies.
- A first `sync --company` must resolve its company after instrument seeding and fail if it cannot; no green no-op.
- Börsdata response-shape handling follows the adapter contract in the authoritative MVP plan (§1.2 and §2.4), including nested values envelopes. Unknown HTTP-200 shapes must fail the sync rather than become empty data.
- Ranking must load PIT-filtered financials, prices, KPIs, dividends, split/calendar context, and evidence from SQLite. Missing inputs are limitations, not numeric zeroes; ineligible rows are held out of the meaningful score ordering.

## Implementation sequence

1. Harden DSN/path and schema migration behavior; add clean-checkout regression tests.
2. Make import/relink/sync lifecycle deterministic, watchlist-scoped, and failure-visible.
3. Complete typed Börsdata envelope normalization and persistence for reports, KPIs, metadata, splits, calendar, dividends, shorts, and instrument anomalies.
4. Add repository loaders and split-aware financial/per-share calculations with strict `as_of` filters.
5. Integrate loaded results and readiness verdicts into ranking/export; make missing-data ordering explicit and deterministic.
6. Add fixture-backed adapter, persistence, PIT, idempotence, fairness, readiness, and split regression tests.

## Verification gate

- `.venv/bin/pytest -q`
- `.venv/bin/ruff check alphaforge tests`
- `.venv/bin/python -m compileall alphaforge`
- no live network is required for the normal suite; optional live verification remains `pytest -m integration` with `BORSDATA_API_KEY`.
