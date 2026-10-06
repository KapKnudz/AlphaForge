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
currencies, ordered universe and cutoff; each company's explicit DCF
archetype/profile/evidence-reference input; admitted current/historical reports
and raw fiscal/publication/denomination/acquisition facts; selected current and
paired historical prices with currencies and raw date facts; selected KPI
period/price/date values and rejected/missing-selection provenance; split facts;
and consumed dividend amounts/currencies/verification and exact window assurance.
Reports/rejections examined by chronology/refusal are part of the selected input
domain. Encoding `executed-numerical-v2` also retains every analyst calibration
candidate from [migration 019](../db/migrations/019_reinvestment_calibrations.sql),
including rejected/conflicting choices, exact operand/source/accounting records
and content identities. The numerical body retains the report and rejection rows
that determine DCF input quality. Rules separately bind the calibration,
input-quality, forward-funding/convergence, explicit DCF-routing and typed DCF
result-contract policy versions. A replay whose implementation or rules are
incompatible refuses honestly rather than reinterpreting old outputs. Unselected
intervening daily prices and superseded admissible KPI rows are excluded. Raw values survive beside
canonical stored transformations; NULL
and zero remain distinct. No liquidity, realized returns or thesis inputs are
added. Provider scale, split adjustment, selection/refusals and formulas retain
the [deterministic](deterministic-flow.md) and [valuation](valuation.md) owners.

Surrogate numerical row IDs are assigned in canonical body order for stable
selection diagnostics. Natural provider/observation keys and raw payloads remain
retained; these snapshot-local IDs must not be used to join mutable source rows.
Company IDs remain actual repository identities.

For new runs, an independent immutable per-run audit map records every retained
row whose source surrogate is normalized: table, `snapshot_row_id`, original
`source_row_id` and company. Migration
[018](../db/migrations/018_numerical_source_row_maps.sql) owns
`numerical_run_source_rows`; `run.json#source_rows` and the replay envelope expose
that map, and ranking JSON explicitly labels its diagnostic row-ID namespace.
For example, `rejection:1` may map to source `financial_period_rejections.id=2`,
not to live row 1 belonging to another company. Source IDs refer only to the
originating SQLite database, not globally unique provider IDs or a current-state
join guarantee. The map is captured in the same read snapshot, retained atomically
with outputs, and independently hashed; it never enters the canonical numerical
body, numerical identity or exact output comparison. Runs predating this map are
not backfilled; replay reports `source_rows_status=not_retained` without guessing
original IDs. Generated acquisition timestamps
that are not calculation inputs are excluded; rejection and dividend verification
times consumed in diagnostics remain retained. This is not a general vintage
service and cannot recover pre-retention overwritten values.

`financial_inputs_hash` hashes only the canonical numerical body.
`numerical_identity` additionally binds the recorded rule bundle: exact Git
revision, executing source-file digests (including taxonomy, financial,
valuation, ranking, readiness and reconstruction/schema code), Python and SQLite
runtimes, selection/scoring/DCF/input-quality/required-return/dividend versions,
accepted age limits, DCF defaults and declared solve-axis registry/domains, size
hurdles and scale/split assumptions.
Actual source digests also identify dirty development execution; the Git revision
alone is not represented as sufficient code identity. A version string alone
cannot hide an unversioned formula correction.

Textual packet/manifest contents, references and fingerprints are retained as a
**separate textual context** with their own integrity digest, not included in the
financial body hash. Existing packet validation remains authoritative. DCF route
references resolve and canonicalize only against this retained frozen packet;
replay never rereads the live route file or evidence store. Numerical changes never
rewrite textual packets or manifests. Replaying a formerly usable packet does not
restore its current usability or authorize new model analysis; new `rank` always
selects text through `load_evidence_view`.

## Retention, publication and refusal

Input retention commits before calculation. The consumable `ranking_runs` row and
its retained executed-output link commit atomically before any filesystem export.
Any output-retention error rolls both records back rather than publishing an
unrepeatable run; an interrupted run can leave only an unused retained input body.
Identical retries, including an identical concurrent insertion winning after an
absence read, verify retained content; contradictory insertions refuse. Unrelated
insertion faults still abort retention rather than being treated as success.
SQLite triggers prohibit update, delete and replacement of immutable records.

`exports/runs/<artifact_id>/` contains the write-once ranking JSON/CSV, DCF JSON,
`outputs.json` and `run.json` identities. The artifact ID is a DSN-free SHA-256 over
the database-local run ID, numerical identity, textual-context hash, output hash,
and (when retained) independent source-row-map hash,
so same-local-ID runs from different databases do not collide unless their retained
artifacts are identical. Identical artifacts are verified byte-for-byte and reused;
conflicts never overwrite the original directory. The SQLite retained records are
replay authority, not editable filesystem copies. `exports/<as_of>/` remains a clearly
mutable latest convenience alias with `run.json` identifying its run and artifact and
`latest.json` explicitly marking it mutable. Ranking JSON carries run and artifact
IDs, the financial-input hash and numerical identity beside, not instead of,
textual evidence hashes. A subsequent same-cutoff rank never rewrites a different
older run artifact.

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
`invalid_textual_context`, `invalid_source_row_map`, `conflicting_immutable_insertion`,
and `output_mismatch`.
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
[`selection coverage`](../tests/test_executed_numerical_runs_selection.py)
exercise real repository → capture → loader → ranking/export → replay, single
and multi-company insertion permutations, same-key report/price/KPI corrections,
removal of live rows, retained-only reconstructed memory databases and forbidden
live-table reads. It covers missing/zero, non-calendar fiscal histories, splits,
sector branches, dividends, denominations, rule/code changes, tamper/conflicts,
legacy refusal, atomic second-reader visibility and persistence failure before
consumption. [Forward-reinvestment coverage](../tests/test_forward_reinvestment.py)
adds independent Decimal/hand bridge cases, qualification/domain refusals,
unavailable margin and not-identifiable terminal growth, immutable calibrations,
retained replay after a conflicting live review, and economic-policy mismatch refusal. Fixtures require no live provider acquisition or model call. Additional
[hosted-review regressions](../tests/test_numerical_retention_review_regressions.py)
exercise real two-connection insertion interleaving through rank/export/replay,
contradictory concurrent retention refusal, original-source/snapshot rejection-ID
mapping after live-row removal, source-map atomicity/immutability and v17 upgrade
without fabricated source IDs.
