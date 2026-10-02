# Executed numerical run replay (Option B)

`alphaforge rank --as-of YYYY-MM-DD` retains an immutable numerical body before
calculating, and original canonical deterministic outputs before exporting.
`alphaforge replay --run-id ID` reproduces that executed run for **audit only**.
It is not a new live readiness assessment or an arbitrary historical-known-then
query. A normal `rank` is a fresh current-state rerun and may change after corrections.

## Owners and boundaries

[`db/numerical_runs.py`](../alphaforge/db/numerical_runs.py) owns bounded capture,
retention and reconstruction. It uses the existing
[`ranking_loader.py`](../alphaforge/cli/ranking_loader.py), `RankingEngine`,
readiness gate and calculators, not a second financial implementation.
[`migration 017`](../db/migrations/017_executed_numerical_runs.sql) adds
`numerical_input_bodies` and `executed_numerical_runs`; prior rows are untouched.
Operational report/price/KPI upserts remain latest-state views.

The body preserves the actual company/database/provider identities, branch and
currencies, ordered universe and cutoff; admitted current/historical reports
and raw fiscal/publication/denomination/acquisition facts; selected current and
paired historical prices with currencies and raw date facts; selected KPI
period/price/date values and rejected/missing-selection provenance; split facts;
and consumed dividend amounts/currencies/verification and exact window assurance.
Reports/rejections examined by chronology/refusal are part of the selected input
domain. Unselected intervening daily prices and superseded admissible KPI rows
are excluded. Raw values survive beside canonical stored transformations; NULL
and zero remain distinct. No liquidity, realized returns or thesis inputs are
added. Provider scale, split adjustment, selection/refusals and formulas retain
the [deterministic](deterministic-flow.md) and [valuation](valuation.md) owners.

Surrogate numerical row IDs are assigned in canonical body order for stable
selection diagnostics. Natural provider/observation keys and raw payloads remain
retained; these snapshot-local IDs must not be used to join mutable source rows.
Company IDs remain actual repository identities. Generated acquisition timestamps
that are not calculation inputs are excluded; rejection and dividend verification
times consumed in diagnostics remain retained. This is not a general vintage
service and cannot recover pre-retention overwritten values.

`financial_inputs_hash` hashes only the canonical numerical body.
`numerical_identity` additionally binds the recorded rule bundle: exact Git
revision, executing source-file digests (including taxonomy, financial,
valuation, ranking, readiness and reconstruction/schema code), Python and SQLite
runtimes, selection/scoring/DCF/required-return/dividend versions, accepted age
limits, DCF defaults/solve bounds, size hurdles and scale/split assumptions.
Actual source digests also identify dirty development execution; the Git revision
alone is not represented as sufficient code identity. A version string alone
cannot hide an unversioned formula correction.

Textual packet/manifest contents, references and fingerprints are retained as a
**separate textual context** with their own integrity digest, not included in the
financial body hash. Existing packet validation remains authoritative. Numerical
changes never rewrite textual packets or manifests. Replaying a formerly usable
packet does not restore its current usability or authorize new model analysis;
new `rank` always selects text through `load_evidence_view`.

## Retention, publication and refusal

Input retention commits before calculation. Output retention commits before any
consumable export. Any retention/serialization error aborts rather than publishing
an unrepeatable run. An interrupted run can leave an unused retained input body
or an incomplete run record, but neither is substituted with live numerical rows.
Identical retries verify retained content; contradictory insertions refuse.
SQLite triggers prohibit update, delete and replacement of immutable records.

`exports/runs/<artifact_id>/` contains the write-once ranking JSON/CSV, DCF JSON,
`outputs.json` and `run.json` identities. The artifact ID is a DSN-free SHA-256 over
the database-local run ID, numerical identity, textual-context hash and output hash,
so same-local-ID runs from different databases do not collide unless their retained
artifacts are identical. Identical artifacts are verified byte-for-byte and reused;
conflicts never overwrite the original directory. The SQLite retained body/output is
replay authority, not an editable filesystem copy. `exports/<as_of>/` remains a clearly
mutable latest convenience alias with `run.json` identifying its run and artifact and
`latest.json` explicitly marking it mutable. Ranking JSON carries run and artifact IDs
and both numerical identities beside, not instead of, textual evidence hashes. A
subsequent same-cutoff rank never rewrites a different older run artifact.

Replay reads only retained run/body records, constructs a disposable memory DB,
and runs the existing selection/calculation path without mutable numerical
lookups. It requires the recorded revision to exist locally and the executing
source/rule/runtime bundle to match. It does **not** download or execute arbitrary
historical code. Unavailable revisions and unsupported bundles refuse; future
code changes may therefore make older runs unsupported until their recorded
implementation is supplied by the ordinary supported release process.

Typed `ReplayRefusal.reason` outcomes include `missing_run`,
`legacy_not_replayable`, `missing_snapshot`, `corrupt_retained_body`,
`unavailable_code_revision`, `unsupported_rules_or_code`,
`invalid_textual_context`, `conflicting_immutable_insertion`, and `output_mismatch`.
Legacy `ranking_runs` without retained inputs remain honestly non-replayable;
no retrospective snapshots are fabricated. Hash-only records are insufficient.

## Bounded equality and acceptance

The original canonical output contains full ranking/readiness scores, financial
and valuation metrics, dividend-yield facts, DCF projections/assumptions/solves
and selection/missing reasons. Canonical encoding is sorted, compact UTF-8 JSON,
ISO dates, finite Python IEEE-754 floats, with no rounding tolerance. JSON object
key order and display whitespace are not semantic identity. CSV and indented
ranking JSON are presentations; `outputs.json` is the exact comparison surface.
Comparison includes the full output body, not just score/hash equality.

[`test_executed_numerical_runs.py`](../tests/test_executed_numerical_runs.py) and
[`selection coverage`](../tests/test_executed_numerical_runs_selection.py) exercise real repository → capture → loader → ranking/export → replay, single
and multi-company insertion permutations, same-key report/price/KPI corrections,
removal of live rows, retained-only reconstructed memory databases and forbidden
live-table reads. It covers missing/zero, non-calendar fiscal histories, splits,
sector branches, dividends, denominations, rule/code changes, tamper/conflicts,
legacy refusal and persistence failure before consumption. Fixtures require no
live provider acquisition or model call.
