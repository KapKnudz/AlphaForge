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
accepted age limits, DCF defaults, the declared solve-axis registry and growth-domain
scope, arithmetic, endpoint conversion, boundary and sampling policies, size hurdles
and scale/split assumptions. Per-run derived and requested bounds remain in the retained
eligibility/output record rather than masquerading as static registry bounds.
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

## Forward FCFF research lane

`alphaforge forward-fcff --input-json FILE --exports-dir DIRECTORY` and
`alphaforge forward-fcff-replay --run-id ID` are **separate research commands**.
[`db/forward_fcff_runs.py`](../alphaforge/db/forward_fcff_runs.py) retains tagged
`forward-fcff-retained-v1` numerical bodies using the existing immutable input
retention helper. [Migration 020](../db/migrations/020_forward_fcff_runs.sql) adds
one append-only `forward_fcff_runs` table linked to numerical identity, **not**
a ranking-run FK. No synthetic ranking, calibration or approval rows are created.
Old records remain unchanged; old rank replay retains its honest code/rule
incompatibility refusal. The v20 upgrade is additive and preserves existing
retained rows.

`freeze` canonicalizes JSON keys and ID-keyed economic arrays; annual periods
remain ordered. Catalog hashes are verified against supplied bodies before
canonical source ordering is normalized. The numerical body includes the selected
request and retained textual-context digest; catalog bodies/anchors stay in the
separate context column. Both are integrity-bound. Identity excludes filesystem
paths and collection wall time, but includes actually consumed preparation,
publication/review facts and limitations. Rules pin schema/method/economic/control
versions, executing sources and retention/CLI/schema helpers, Git revision and
Python/SQLite runtime. No live source/company lookup occurs during replay.

Input retention **commits before evaluation**. Canonical output and its hash
commit before filesystem publication. Output-retention failure leaves at most
an unused input body, no consumable research run. Triggers prohibit update,
delete and replacement. Replay verifies all retained bodies/context/output hashes,
requires compatible retained rules/runtime/code, then invokes the **same pure
evaluator** and compares canonical output bytes exactly. No tolerance, display
rounding, source reacquisition, rank evaluator or historical-code download.

Write-once `DIRECTORY/forward-fcff/runs/<artifact_id>/` contains `input.json`,
`context.json`, `outputs.json`, `run.json`, `cash-flows.csv` and `report.md`.
The artifact ID binds the lane, local run ID, numerical/context/output identities.
A completed staging directory is published only after retention; identical reuse
checks every byte and conflicting directories refuse. There is no mutable ranking
alias or score injection. Reports display date/currency/units, retrospective basis,
review/AI premise status, shield exclusion, four output statuses, exact numerator,
explicit horizon, gross tax legs and material bridge limitations.

### Independent acceptance (A1–A21)

Offline independent written equations and refusal cases live in
[`test_forward_fcff.py`](../tests/test_forward_fcff.py),
[`controls`](../tests/test_forward_fcff_controls.py) and
[`CLI/retention tests`](../tests/test_forward_fcff_runs.py). Source-free SEK and EUR
fixtures share economics without conversion or rate-policy fallback. Decimal
expectations cover full next-year numerators, separate gross-leg cancellation,
one/two-year lag horizons, finite runoff, commissioning, gross/net investment,
lease financing exclusion, boundary claims/writeoffs, WC, provenance, units,
scoped refusals, count-once bridges and exact replay. File-backed two-connection,
retention/export failure, immutable-record, corruption and migration tests
exercise the actual boundaries, not implementation-source text matching.

Authentic corrected Evolution acceptance is **private** and complementary, not
the public arithmetic oracle or issuer admission. Portable procedure:

1. Pair an explicitly supplied frozen reference root with its freeze manifest;
   verify every pinned original hash before and after. Never regenerate or edit
   that reference, its sources or manifest.
2. Independently translate preserved primitive/state CSVs into the general
   request, retaining source catalog bodies/dates/anchors, actual boundary lots,
   AI assumptions and unresolved material equity/ownership limitations. Do not
   import issuer constants into the engine. Use original nominal EUR and supplied
   financing-neutral EUR returns; no SEK hurdle or FX.
3. Independently derive expected annual gross payment/refund, asset investments,
   D&A/classes/leases, WC, FCFF and full terminal numerator from the preserved
   written/Decimal reference equations, **not new evaluator output**. Derive
   explicit and terminal PV separately and use declared written horizons. Test
   all three main paths at each supplied return, both late-inflation alternatives,
   and the late two-year refund-lag case. Also supply failing transitional tails
   and separately corrected-horizon requests; matching EV alone is insufficient.
4. Before comparisons declare absolute tolerance **2e-8 million units** and
   relative tolerance **2e-12**. These accommodate 22-year IEEE-754 accumulation
   versus 40-digit Decimal equations and are far smaller than the identified
   terminal correction. They apply only to **financial reference comparisons**;
   compatible retained replay must match canonical bytes exactly.
5. Retain private `cases.json` with `tolerance` (keys `absolute_millions`,
   `relative`, `justification`), `oracle_basis`, and `cases` (each `id`,
   `input_file`, `expected_file`, plus `transitional_input_file` where required).
   The checker's `REQUIRED_CASES` and `REQUIRED_FINANCIAL_PATHS` are the exact
   matrix and annual-path authority, including case IDs and file naming. Expected
   files bind `basis` to `oracle_basis` and contain all 22 `rows` (`index`,
   `values`: dot-separated output paths to Decimal strings), `horizon`,
   `terminal_numerator`, `explicit_pv`, `terminal_pv`, and `operating_value`.
   No expected field may be copied from the new evaluator.
6. Run the portable consumer, with all private paths explicitly paired:

   ```sh
   python -B tools/check_forward_fcff_acceptance.py \
     --cases PRIVATE/cases.json --out PRIVATE/acceptance \
     --reference-freeze PRIVATE/reference-freeze.json \
     --reference-root PRIVATE/corrected-reference
   ```

   `PRIVATE/acceptance` must not already exist, including as an empty directory;
   each failed or successful attempt permanently owns its output path. The
   completed receipt is published atomically without replacing a conflicting
   receipt. The consumer exercises the pure evaluator **and actual
   CLI/export/retained replay**, checks annual legs plus numerator/horizon/PVs,
   refuses the transitional requests in both paths and verifies exact replay for
   successful and refused runs. It
   writes a private receipt with identities, tolerances, comparisons, original
   freeze checks and explicitly unavailable equity/share outputs. Retain this
   receipt with the private authentic translations. After code/rule changes use
   a new compatible run; do not rewrite or pretend old runs remain compatible.

This milestone produces reproducible **conditional operating values**, not a
completed Evolution equity valuation, accounting review or financial sign-off.
