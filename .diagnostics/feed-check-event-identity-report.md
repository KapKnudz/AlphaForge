# MFN feed-check event identity repair

## Observed diagnosis

Base: `7a7cfda46524849149dea58fc7d65a6e0289bda2` (current main at dispatch).
Read the established sibling diagnostic report, fixed-clock script and baseline
logs read-only; no yield implementation was used or modified.

The real one-company fixture flow first observes an incomplete release, then a
revocation. With SQLite `strftime` fixed to `2026-09-30T13:30:00.000Z`, the second
flow aborted at `record_mfn_feed_check` before revocation. Own retained evidence:
`baseline-fixed-clock.log` (exit 1, exact composite-primary-key collision).

Trigger: two distinct checks for one company in the same millisecond. Mask:
execution crossing a clock millisecond. Visible symptom: second evidence flow
aborts rather than processing revocation. Schema blame locates the invariant in
`fe2c705b` from September 17, not the numerical changes. The sibling unchanged
normal-clock evidence suite passed 45 tests; that timing-dependent success does
not refute the deterministic collision.

Counterfactual: retain the identical clock and repository/flow path but change
only persisted audit identity. The post-fix flow passes with two distinct ids,
two equal original timestamps, individual `(1,1)` and `(0,0)` discovery/unseen
counts, and the revocation assertions intact. Continued uniqueness failure or
missing revocation with this change would disconfirm the proposed causal
boundary; neither occurs. Both ordinary and fixed-clock flow variants now pass.

## Preservation and scope

Migration `012_mfn_feed_check_event_identity.sql`, schema version **12**, rebuilds
only the existing audit table transactionally. It copies every old column
verbatim, preserves old rowids as ids, retains the foreign key/cascade and
millisecond default, and adds `(company_id, checked_at DESC, id DESC)` indexing.
The writer remains an ordinary repository insert. The sole existing latest
reader (a fixture assertion) now orders timestamp ties by id. No production
latest consumer of this table was found.

Only disposable in-memory fixture databases were migrated. No operational DB,
financial acquisition, model call, daemon administration, yield arithmetic,
resolver, or semantic selection was touched. Yield's unlanded migration must
remain waiting and reconcile its numbering after this fix lands.

## Validation

- `focused.log`: 86 passed, including original temporal cases and new migration
  preservation/replay, same-time inserts, latest tie ordering and revocation.
- `full-suite.log`: 275 passed, 1 live integration deselected.
- Ruff lint and format, import-linter (3 contracts), evidence-manifest boundary
  check and `git diff --check` passed.
- `fixed-clock-after.log`: same real-flow standalone reproduction exits 0.

To rerun the committed standalone post-fix fixture from the worktree root:

```sh
PYTHONPATH="$PWD:$PWD/tests" .venv/bin/python .diagnostics/reproduce_feed_check_collision.py
.venv/bin/pytest -q tests/test_mfn_feed_check_identity.py tests/test_evidence_flow.py
```

The pre-fix script invoked the unchanged test without the subsequently added
`fixed_clock` parameter; its retained log is the baseline evidence, not a
passing test. Dependency setup initially used a temporary environment lacking
pytest; the retained baseline log comes from this worktree's complete local
`.venv`, not that setup failure.

Native no-mistakes/PR handoff is pending firstmate's instruction; no push or
merge has occurred.
