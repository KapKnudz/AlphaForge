# Deterministic textual evidence flow

## Scope

This document owns the deterministic **textual evidence lane**: issuer mapping,
MFN discovery/admission, covered identity, PDF retention/extraction, immutable
observations/relations, manifest selection/completeness/cache, frozen packets,
and their ranking/readiness provenance contract. It does not define financial,
valuation, return or ranking arithmetic. The [architecture overview](architecture.md)
routes implemented calculation wiring to the
[deterministic-flow contract](deterministic-flow.md) and formulas and policies to
[the valuation contract](valuation.md); the
[high-level design](plans/2026-09-16-alphaforge-mvp.md) remains the target design.
Models own neither this lane's validation nor those calculations.

Issuer identity is source-of-truth data keyed by `companies.id`. MFN discovery
stores observed candidates in `mfn_issuer_candidates`; only a `mapped` row in
`mfn_issuer_mappings` with a source URL, verification timestamp, and identity
evidence can drive ingestion. Use `alphaforge mfn-map-seed` for a reviewed JSON
seed or `alphaforge mfn-map` for an explicit operator decision. Ambiguous
discovery is stored for review and is never promoted automatically. A reviewed
`ambiguous` mapping is authoritative until re-reviewed; later exact-identifier
discovery is queued as candidates, never applied. Live discovery
uses MFN's issuer/index surfaces and its `/search/companies` JSON surface. Search
records are accepted only when an explicit ticker, ISIN, or Börsdata identifier
matches the stored company identity; observed slugs are retained as evidence and
are never derived from company names. Multiple exact matches remain ambiguous.

The one-company lane is explicit:

```text
alphaforge evidence --ticker TICKER --as-of YYYY-MM-DD --diagnostic
```

It discovers MFN quarterly/interim, year-end, and annual releases. A narrative
headline is admitted only when MFN's JSON feed independently supplies both the
`sub:report` classification (with one annual or interim-quarter subtype) and an
`archive:report:pdf` attachment marker; either signal alone remains non-report.
Missing, null and malformed tag collections provide no attestation; a string
or object is never interpreted as a tag list. Explicit report headlines retain
their title-based admission path. The live AQ null-tag shape is pinned in
`tests/fixtures/mfn/aq_null_tag_feed.json` (public feed-field excerpt).
It accepts only
authoritative detail-page publication timestamps at or before both the requested
cutoff and the current wall-clock date; downloads unseen PDF attachments with
bounded retries and byte/page limits; checks content type and `%PDF-` magic
bytes; hashes the raw bytes; extracts with `pypdf`; and persists logical
documents, bilingual sibling provenance, attachments, extraction metadata, and
page anchors through repository helpers.

Each non-dry-run feed check persists a distinct audit event, including zero
delta. Event identity is independent of the SQLite wall-clock timestamp, so
equal `checked_at` values do not conflate checks. The [live SQLite
schema](../db/alphaforge.sqlite.sql) and [migration history](../db/migrations/)
own the table shape and upgrade details.

Historical retrieval is bounded: interim reports keep a 2-year lookback and
annual reports a 5-year lookback relative to `--as-of` (calendar-year
arithmetic, Feb 29 maps to Feb 28). Feed cards outside the window are skipped
before detail fetch, and authoritative detail-page timestamps are re-checked
against the same cutoffs (`pre_cutoff_release` skip). Discovery pages the MFN
`offset`/`limit` JSON feed with an HTML-fragment fallback (up to 12 offsets of
48, at most 60 detail fetches); a paginated-feed transport failure raises
`mfn_feed_fetch_failed` instead of ending the scan silently. Code-level
defaults live in `ReportHistoryWindow` (`alphaforge/evidence/report_rules.py`); the
legacy single-page plus Sunday page-2 contract applies only when paginated
discovery is unavailable (on the default paginated path the Sunday page-2
sweep survives only as a small-page HTML-fallback backstop).

### Bilingual selection and observation dates

Bilingual deduplication is deliberately fail-open. Only opposite-language
candidates for the same issuer and report kind can match. Fiscal-period labels
normalize two-digit years (`2026/27` → `2026/2027-q1`), and an additive
`document_type` (`INTERIM_Q1…Q3`, `YEAR_END_REPORT`, `ANNUAL_REPORT`) keeps
year-end and annual reports distinguishable without changing `report_kind`.
A match requires one strong corroborator (a shared provider event ID, shared
PDF/attachment checksum, or ≥ 0.5 Jaccard similarity over normalized numeric
key-figure tokens — similarity below threshold is neutral, never a veto) plus
at least two compatible derived signals (fiscal period, resolved observation
date, publication date, or translation-neutral title). Immutable asserted
relations persist that proof as `strong_corroborator` (`kind` and `value`) plus
a `compatible_signals` list; the repository checks identity claims and signals
against both candidate observations, applies the ingestion title normalization
to persisted issuer/title data, and recomputes numeric similarity from the
persisted extraction pages. It also enforces matching report kinds,
opposite-language translations, and revision-marker evidence for revisions;
withdrawals need only record their reason. Either side carrying a
revision marker (`correct`, `revis`, `rättelse`, `uppdaterad`, `amend`) produces
a `REVISION` relation rather than a translation; other grouped cross-language
pairs are labelled `TRANSLATION`. Same-language documents merge only when a
revision marker identifies the relation; ambiguous or semantic-only pairs
remain separate and may receive an optional shadow-only review. A
translation-only component prefers English when available, otherwise Swedish.
A revision component first selects the latest applicable publication; a
revision marker, English, and source URL are deterministic tie-breakers in that
order. On the V2 path, suppression exists only in the manifest: both candidates
retain their independent attachments, artifacts, extractions, pages, and
observations. The
mutable `duplicate_of` representation remains a legacy compatibility view and
is not a V2 selection authority.

Document language is decided from the PDF itself — attachment-filename markers,
then word scoring over the first three extracted pages — and outranks MFN
release-language metadata. PDF word scoring requires at least
`PDF_LANGUAGE_MIN_HITS = 2` hits for one language; an empty extraction records
`pdf_text_empty`, below-threshold non-ties record
`pdf_text_insufficient:sv=N,en=N`, and equal qualifying counts record
`pdf_text_tie:sv=N,en=N`. When PDF evidence is indeterminate, an MFN release
hint may remain an explicit fallback as `release_hint:<case>`, never as PDF
verification; fallback documents remain outside automatic language-based
grouping and add `pdf_language_fallback:N` to packet limitations. The evidence
and source used are stored as `pdf_language` / `language_evidence`. Identity
dates (`period_start` / `period_end`) and covered fiscal identity are resolved
before immutable observation and relation creation. The fiscal resolver in
`evidence/ingest.py` gives explicit provider fields precedence over a covered
report title, then falls back only to report-labelled body headings. It does
not scan arbitrary narrative for the first year/quarter: publication dates,
forecasts and comparator mentions are not covered identity. Covered month spans
accept separator punctuation (including a comma before the year); explicit
year-end report titles retain fiscal Q4, distinct from annual-report identity.
Covered annual fiscal ranges retain the established `YYYY/YYYY` label
(`2025/26` normalizes to `2025/2026`) from report titles or guarded body headings;
a bare provider year cannot prove a two-year annual range. Provider ranges use
the same canonical identity for observations, relation proof and manifest slots;
original spellings/input keys remain provenance. Conflicting covered ranges remain ambiguous, and no
calendar start/end dates are invented. Quarter/year-end normalization preserves textual position: a later comparator
cannot override the covered title, and a comparator Q1 cannot override an actual
year-end Q4. Forecast/comparison clauses are excluded from title inference.
Body guards examine the matched report heading and its own sentence prefix,
not an arbitrary window spanning earlier independent sentences. A forecast
inside a report-labelled match is not coverage. Bare HTML whitespace collapsed
by the provider parser does not prove a separate heading boundary; unresolved
identity remains explicit rather than inferred. Neither normalization changes
report-class horizons. Contradictory covered headings or provider/title identities remain null with
`fiscal_identity_ambiguous`; absent covered identity records
`fiscal_identity_unresolved`. These are source/packet limitations, not invented
periods or permission to group candidates. Fiscal labels never invent calendar
dates, including non-calendar years such as `2026/27`.

The resolved value, `fiscal_period_source` and any original explicit
`fiscal_period_input`/limitation are retained in the immutable observation.
`fiscal_period_input_key` preserves the original provider field (`fiscal_period`,
`report_period`, or `period`) across replay, including unresolved conflicts.
Packet sources copy the value and provenance. A previously derived nonnull
value is an output, not a provider assertion. Changed interpretation rules
require new observations under the new fingerprint, even when correcting an
old nonnull identity. Verified independently retained bytes can be reused for
that reclassification; prior observations and selected historical manifests
remain unchanged. Relation corroboration uses these persisted identities and
never treats an unresolved conflict as period proof.

An explicit `observation_date`, `period_end`, or `report_period_end` is used
first. Otherwise the flow extracts an unambiguous covered-period end date from
the release body before applying title heuristics; it does not assume calendar
quarters for companies with non-calendar fiscal periods.

Scanned or near-empty PDFs are retained with a `*_no_ocr` limitation. OCR,
semantic retrieval, general news, and model-assisted identity linking are not
part of this lane.

### Immutable PDF object storage

`alphaforge.evidence.artifact_store.LocalPdfArtifactStore` is the production
retention boundary for new V2 PDF evidence. The CLI injects it into the
one-company flow, and V2 selection requires a matching immutable object record
plus successful checksum and size verification. Historical metadata without
retained verified bytes cannot qualify as new V2 evidence. By default the store writes objects at
`data/evidence/objects/sha256/<first-two-hex>/<sha256>.pdf` and returns a URI
relative to that configured object root (`file:sha256/<prefix>/<sha>.pdf`). It
streams writes through a same-filesystem temporary file, validates the byte
limit and PDF magic, fsyncs the file and directory hierarchy, and atomically
installs without replacing an existing object. Existing objects are reused only
after full checksum and size verification. Store operations reject symlinked or
otherwise non-directory internal path components and non-regular object paths
rather than following them outside the configured root.

Call `read_pdf(sha256, expected_size=...)` before use. It returns bytes only
after streaming verification and raises `artifact_unavailable` or
`artifact_checksum_mismatch` typed errors rather than falling back to a URL or
another object. The store itself performs no database writes or deletion; the
revision recorder persists its returned identity in the evidence history.
Schema version 11 stores each object's acquisition byte limit so replay verifies
it under the limit that originally admitted it. Migrated objects with no stored
limit remain unavailable until exact reacquisition verifies the object and fills
that one field.

A run with no model-ready source returns `no_evidence` with one of
`no_published_release`, `all_releases_after_cutoff`, or `no_complete_source`;
it persists a partial evidence job audit record and does not create a packet.
Successful runs persist a `success` job record. Job records include attempt and
started/finished timestamps, with structured error diagnostics for non-success
outcomes.

MFN/Cision report history runs a guarded lane over the shared Nordic
distribution network: `/cis/a/<issuer>/<slug>-<hash>` release pages on `mfn.se`
are accepted only when the issuer segment matches the resolved mapping token
and the page canonical link (`/all/a/<issuer>/…`) confirms the same issuer;
missing, malformed, or mismatched confirmation blocks the page visibly
(`canonical_issuer_unconfirmed` / `issuer_mismatch`), never silently.
`storage.mfn.se` and `mb.cision.com` attachments are selected by ranked
identity — explicit `mfn-primary` marker, then Cision `Main/` path, then
report-like link text
with corroborating report title (`attachment_tier` in
`mfn-primary` / `main-path` / `label-score`) — and ambiguous selection
(`ambiguous_selection`) fails the lane instead of guessing. Repeated
anchors with exactly the same PDF href string count as one attachment before
ranking. Distinct target URLs remain separate candidates; unresolved ties at
the highest applicable tier refuse as ambiguous.
Invitation/presentation/webcast-titled pages never contribute evidence. V2
records terminal detail dispositions: invitations and confirmed non-reports are
rejected without reducing completeness, while missing authoritative detail
metadata is incomplete and blocks a complete result across reruns. Flow composes
one deterministic terminal state before appending it: a canonical feed veto
outranks admission rediscovered in an HTML backstop; detail terminal dispositions
outrank a conflicting returned article. There is never a provisional revocation
followed by a conflicting same-batch rejection. Unchanged intentional rejections
remain rejected and reuse their current observation. A genuinely changed feed
reclassification appends a revoked observation from eligible or incomplete
states, so a superseded incomplete classification no longer blocks completeness.
Repository conflict and append-only protections remain mandatory.
Diagnostics split into `discovered`, `filtered_before_download`,
`download_failed`, `ambiguous_selection`, and `retained` (in `diagnostic()` and
the CLI output). `pdf_fetch_attempts` counts bounded PDF acquisition calls,
including calls for subsequently suppressed candidates and failed calls;
`pdf_fetch_succeeded` counts successful PDF acquisitions before extraction or
selection. A call may include the existing bounded transport retries; these
counters are not individual transport-attempt telemetry. They are independent
of selected/retained-source counts and remain zero on verified offline byte
reuse. `downloaded` remains the compatibility retained-download count, not a
promise of zero HTTP. Persisted run diagnostics/job and packet diagnostics carry
the new counters; no download is hidden merely because its edition was suppressed.
Per-class `completeness` (annual vs quarterly over
post-dedupe groups, with no feed `group_id` pairing assumption) is a hard
gate — shortfalls return `evidence_incomplete` with no frozen packet instead
of a green `complete`.

### Immutable history and legacy backfill

Schema version 10 adds an SQLite-only, additive history layer in
`alphaforge/db/evidence_repository.py`; it does not replace the legacy tables,
acquire evidence, or select manifest slots. Each MFN release URL remains an
independent candidate. Content-addressed artifacts and verified object
locations are recorded separately from append-only attachment, extraction/page,
candidate-classification, and relation observations. Extraction identities
cover the complete extraction metadata and ordered page payload, so corrections
append a distinct snapshot while identical retries remain idempotent. Other
stable identities reject conflicting payloads, while relation withdrawals and
later candidate states append new observations rather than
mutating history.

`backfill_legacy_evidence()` explicitly snapshots current legacy MFN rows into
this history. Checksum metadata may create an artifact and existing extracted
pages may be copied with a `legacy_source_without_retained_bytes` limitation,
but the backfill creates no retained-object claim or asserted relationship;
legacy `duplicate_of` links remain unresolved audit entries. The
asserted-relation boundary rejects provider-event and checksum corroboration
from these metadata-only imports, including later observations and different
candidate URLs that reuse the same imported event or artifact; only proof
recomputed from persisted page content can qualify. Imported candidate
observations are therefore `incomplete`. Semantic snapshot and child payload
identity exclude surrogate legacy row IDs, generated row-based anchors, and
attachment or extraction processing timestamps, so replacement-generated rows
do not look like new evidence. Each snapshot at an unchanged legacy source
timestamp reuses its first immutable occurrence, so stale A-after-B replay
cannot supersede B. A changed source timestamp establishes
a new occurrence and can represent a legitimate recurrence. Historical packet and manifest rows remain untouched, and
`write_legacy_backfill_audit()` can persist the returned counts, unresolved
links, and packet and manifest digests for operator review.

### Evidence-selection manifest

Selection is centralized in a pure, side-effect-free manifest
(`alphaforge/evidence/manifest.py`, `MANIFEST_VERSION =
evidence-selection-manifest-v2`). Immutable observation batches use canonical
`YYYY-MM-DD` cutoffs and fixed-microsecond UTC timestamps; live candidate and
relation observations may reuse older facts but cannot reference a later batch.
Deterministic legacy backfill may attach a historical observation to an unchanged
stable candidate or artifact identity first seen by a later live batch, without
rewriting that identity. Candidate rules must match their batch. Current-state
reads rank the newest eligible cutoff before acquisition chronology, so a later replay of an older
cutoff cannot regress a newer view. `select_evidence_manifest()` derives every
evidence role — audit history, cache, reuse, deduplication groups, packet
inputs, typed rejections — from immutable facts plus one rule input set
(`alphaforge/evidence/report_rules.py`, fingerprinted). Completeness, packet
construction, cache reuse, and readiness all consume that one view, never
raw persistence; `tools/check_evidence_manifest_boundary.py` enforces the
boundary in CI, and `manifest_store.load_evidence_view` is the read path.
Legacy groups retain their compatibility grouping rules. V2 groups key only on
an independently owned candidate or a currently asserted immutable relation;
shared event, period, PDF URL, or hash does not itself merge candidates. Every
group also carries its fiscal slot key: ISO dates normalize to their day, while
fiscal labels preserve the entire quarter (`2026/2026-q1` differs from
`2026/2026-q2`). Equal slots do not authorize grouping. Completeness counts retained groups over
expected groups and `packet_contents()` returns exactly the observations
selected by stable identity. Rejected candidates carry typed reasons (`rejection_reason`,
`outside_history_window`, `not_selected_by_manifest`); ambiguous-selection
blocks are recorded as typed rejections so their group stays in the coverage
denominator. Evidence without a recognized `attachment_tier` is
inadmissible on first run; unchanged reruns skip feed entries that already
have complete current-fingerprint documents and rebuild the manifest with
prior considered candidates and typed dispositions when the persisted feed
fingerprint matches and the candidate remains in that feed. The normalized feed
fingerprint is part of the manifest identity. When the repository reconstructs a
V2 manifest without an explicit fingerprint, the newest immutable batch matching
the exact company, cutoff, and report-rule fingerprint supplies it; an older
persisted manifest never supplies provenance for a changed or incomplete batch.
A caller-supplied fingerprint remains authoritative. A refetched detail that
disappears cannot erase a previously blocked candidate; changed feed inputs do
not inherit prior dispositions. The shared ranking/readiness view reconstructs
candidate accounting from that persisted manifest. Manifest JSON is immutable: recording
the same identity verifies identical content and may bind its packet hash once,
but does not delete and reinsert history. For new V2 evidence, Swedish and English release URLs are independent candidate
identities with independently retained artifacts and extractions. An append-only
asserted relation may group their observations only after deterministic
corroboration; a later explicit withdrawn relation separates them. Suppression
is a manifest decision and never deletes either candidate's children. Legacy
demoted siblings join their selected parent's group only when stored provenance,
explicit translation/revision relationship and matching variant group
corroborate the link; unresolved editions remain independent expected groups.
Both candidate URLs and report-class counts therefore remain stable across
cache replay.

On the V2 path, `cache`/`reuse` contain **all independently retained, currently
eligible in-window candidate bindings whose bytes and extractions verify**, not
only packet winners. The repository supplies these verified bindings to the
same pure manifest that selects packet sources. Acquisition never follows a
legacy `duplicate_of` parent for candidate reuse, and it does not write legacy
selection rows as V2 authority. Exact candidate ownership, attachment identity,
current feed disposition and classification fingerprint govern classification
reuse; only current-fingerprint observations can select packet sources. A stale
classification's verified artifact may support a new current-rule observation,
not a restamped old classification. Reusing an extraction additionally requires
matching extractor version and configuration. This check is shared by current-feed
and retained off-feed paths: changed configuration/version extracts again from
that candidate's exact verified bytes and appends new observation/extraction
bindings before manifest/packet selection. Unchanged configuration reuses the
existing binding; missing/corrupt bytes still refuse without PDF HTTP or sibling
substitution. Failed exact-detail refreshes also retain an explicit original
observation link. The repository can verify that exact company/candidate/URL
binding for acquisition only (older incomplete records locate their last exact
complete binding); this lookup never returns packet selection rows. A failed
current observation remains current and unselectable. A later run must obtain
fresh exact-URL admission, not restore the older healthy HTML or title/assertions.
The same fresh-proof requirement applies when a failed report returns unchanged
in the feed: its acquisition-only binding cannot authorize the feed cache skip.
Latest failed source inputs govern retry; matching bytes/extractions may then
be reused, while changed release inputs require independent acquisition.
Missing/corrupt original bytes still block before reacquisition. New/changed attachment or release inputs
acquire independently. Missing/corrupt retained bytes block without URL or
cross-candidate substitution. Window filtering and current acquisition vetoes
precede retained-object verification.

Manifest v2 groups also carry a deterministic fiscal `slot_key` and, for new
immutable observations, bind the exact `candidate_observation_id`,
`attachment_observation_id`, content-addressed `artifact_id`, and
`extraction_id`. A later observation at the same release or PDF URL therefore
cannot change what an older manifest selected. Historical V1 manifests remain
readable as historical records. Cutover is strict per company and requested
window: once any current V2 observation exists, projection uses only current
append-only candidate and relation observations. Missing retained objects or
untouched candidates make the run incomplete rather than mixing V1 rows into a
V2 packet. Latest revoked, rejected, or incomplete observations block fallback
to an older eligible observation. History-window filtering precedes retained
object lookup and verification, so an expired artifact cannot block a current
read.

New PDF bytes are retained by `LocalPdfArtifactStore` below
`data/evidence/objects/sha256/` under their lowercase SHA-256. Writes use a
same-filesystem temporary file and install without replacement; reads verify
hash, size, PDF magic, and resource limits. Missing or corrupt retained objects
fail with typed `artifact_unavailable` / `artifact_checksum_mismatch` outcomes
and never trigger URL or cross-release substitution. The immutable DB record
stores a relative `file:` object URI; legacy attachment hashes without retained
bytes remain audit-only until exact bytes are reacquired and verified.

Packets stamp the consumed `selection_manifest_id`. The bounded AQ inventory
and defective-run provenance live authoritatively in
`tests/fixtures/mfn/aq_replay_inventory.json`; repository/cache replay coverage
is in `tests/test_aq_replay_inventory.py`. The fixture contains public
release/PDF identity metadata but no raw PDFs or extracted page text; tests seed
a schema-valid title placeholder page and do not assess PDF extraction fidelity.
The regression requires corrected first-run and unchanged-replay completeness
when every stored link remains corroborated. An uncorroborated link must instead
leave an independent incomplete group.

The resulting `evidence_packets` row is canonical JSON with stable ordering,
publication/ingestion dates, source/page anchors, limitations, and a SHA-256
hash over the packet without its own `packet_hash`. V2 packet sources copy the
exact candidate, candidate-observation, attachment-observation, artifact,
extraction, relation, and object identities selected by the manifest. Legacy
database-local document IDs are projected to stable source identities derived
from source URL, publication date, and attachment checksum for hashing; stored
IDs remain available for provenance and citations. The V2 stable projection
retains artifact, extraction, and object identities, the stored
`acquisition_max_pdf_bytes`, and relation type/key plus both endpoint URLs;
database-local observation and relation IDs are normalized. Run timestamps
(`issuer.verified_at`, per-source `ingestion_date`) stay in the stored JSON for
auditability but are excluded from the hash, so identical artifacts hash
identically across databases built at different times; packets hashed before
this change keep validating against their stored hash. Every packet also stamps
`evidence_rules_version`; `EVIDENCE_RULES_VERSION` in
`alphaforge/core/frozen_packet.py` is the source of the current monotonic
readiness version for packet-affecting evidence interpretation. Stale is defined
narrowly as a packet built under an older rule version — including pre-versioning
packets without the marker. Older packets stay hash-valid but readiness rejects
them with `stale_evidence_packet`; rerun the lane to rebuild under the current
rules. Readiness for the evidence lane requires that frozen packet
hash to validate; a stray document row is not sufficient. Packet rows also carry
`report_rules_fingerprint`, `usable`, and `usable_reason`. A later non-complete
run marks every prior packet for the same `(company_id, as_of)` unusable in the
same transaction as its terminal job record; rows remain queryable for audit
history, while the loader reuses only valid, current-fingerprint, usable rows.
Run diagnostics are persisted in the packet on complete runs or the job error
on terminal failures, and `describe_evidence_state` is the replay source for
CLI/result diagnostics. The current report-rule fingerprint deliberately
invalidates the prior provider/state/fiscal/slot/cache interpretations, and the
current evidence-rule version invalidates prior packet readiness. Historical
packets still validate against their original hashes, but old rules cannot
confer current readiness. Report rules v11 and evidence rules v13 deliberately
invalidate earlier annual range/fiscal interpretation and off-feed rebuild
behavior. A retained candidate with an older fingerprint is never selected or
restamped: the flow re-runs current admission and covered identity from its
immutable source facts: original public detail HTML is re-parsed by the normal
MFN parser to re-prove title, publication, attachment selection and (for Cision)
canonical issuer confirmation. Narrative admission also requires independently
captured raw MFN report tags and archive-PDF metadata. Old eligibility flags,
decoded report kind or selection tier cannot supply admission; a historical
provider subtype assertion cannot silently be discarded when its raw inputs
are unavailable. Original HTML, raw feed facts and the parsed canonical URL are
retained in observation metadata.
If original facts cannot re-prove the guards, the normal provider may revalidate
only the exact catalogued detail URL, sharing the unchanged detail-fetch budget
with current-feed work. Operational budget/scope deferrals observe no new
provider fact: the old binding stays available for the next bounded attempt,
but remains unselectable under the new fingerprint. Missing/failed evidence
remains incomplete, including the previously expected report; it cannot
disappear to improve completeness.
No discovery or issuer inference is added. Verified exact-candidate PDFs and
matching extractions are reused; changed extraction configuration re-extracts
retained bytes. Corrected observations/bindings append without rewriting history.
After a successful refresh, identical replay needs neither detail nor PDF HTTP
(ordinary bounded feed checks still run).

### Cross-boundary correction coverage

`tests/test_post26_evidence_repairs.py` executes raw provider JSON/detail parsing,
real PDF extraction and CAS verification, immutable recording, shared manifest
cache/selection, packet/citation identities and readiness. It covers null-tag
admission/windowing, fallback/detail terminal-state collision and genuine
revocation, covered periods versus forecasts/comparators, ambiguous and
non-calendar identity, full quarter slots, independent bilingual offline reuse,
withdrawals/revisions, unavailable suppressed artifacts, configuration refresh,
old nonnull fiscal correction without historical mutation, loader/export replay
identity, and current-feed provenance after an incomplete batch. These behavioral
regressions complement the structural manifest checker: a green checker alone
is not evidence that the runtime contract holds. `tests/test_greptile_fiscal_context.py`
pins covered/comparator title ordering, sentence-scoped and inside-heading
forecast guards, typed ambiguity, persisted identity/provenance, full quarter
slots and exact offline immutable-binding/packet/manifest reuse.
`tests/test_offfeed_rule_reclassification.py` executes old-rule/empty-feed
annual, quarterly and narrative rebuilds, provider assertions/conflicts, exact
known-detail fallback with issuer/admission refusal and shared budget limits,
retained-byte/extraction reuse, immutable history and subsequent offline replay.
`tests/test_replacement_annual_and_offfeed.py` adds two-year annual range/provider
provenance and conflicts, canonical equivalent bilingual annual ranges,
off-feed unchanged/config/version extraction paths,
independent bilingual retained children, historical bindings/manifests,
packet/readiness/hash consumers and missing/corrupt-object refusal. Normal
fixture-only tests, import isolation and the checker all remain required. Live two-company replay
is separate bounded acceptance evidence; provider incompleteness never permits
loosening resource, completeness or readiness guards.

### Live verification

Opt-in live checks are `pytest -m integration` (fixture-only by default, and
in CI). No manual key step is needed: when integration tests are selected,
`tests/conftest.py` automatically resolves the repository-root `.env`
(worktree-safe via `tests/env_resolver.py`, which follows `git
--git-common-dir` to the primary checkout) and loads it. With no key in the
environment or that `.env`, the run fails fast and points at the resolver.
