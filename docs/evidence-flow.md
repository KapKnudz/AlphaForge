# Deterministic textual evidence flow

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

It discovers MFN quarterly/interim, year-end, and annual releases; accepts only
authoritative detail-page publication timestamps at or before both the requested
cutoff and the current wall-clock date; downloads unseen PDF attachments with
bounded retries and byte/page limits; checks content type and `%PDF-` magic
bytes; hashes the raw bytes; extracts with `pypdf`; and persists logical
documents, bilingual sibling provenance, attachments, extraction metadata, and
page anchors through repository helpers.

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
date, publication date, or translation-neutral title). Either side carrying a
revision marker (`correct`, `revis`, `rättelse`, `uppdaterad`, `amend`) produces
a `REVISION` relation rather than a translation; other grouped cross-language
pairs are labelled `TRANSLATION`. Same-language documents merge only when a
revision marker identifies the relation; ambiguous or semantic-only pairs
remain separate and may receive an optional shadow-only review. The preferred
variant is unconditionally English when available, otherwise Swedish, so a
later English edition supersedes a previously selected
Swedish one on re-run; suppressed siblings retain a `duplicate_of`, language,
group, selection-rule, and relationship audit record (metadata only — the
demoted edition's attachment, extraction, and page rows are removed),
surfaced in the packet as
`selection_state` / `selection_reason` (`PREFERRED_LANGUAGE` /
`FALLBACK_LANGUAGE`) / `variant_group_id`.

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
dates (`period_start` / `period_end`) are resolved once per article from
provider metadata or the release body and carried into packet sources next to
`observation_date`.

An explicit `observation_date`, `period_end`, or `report_period_end` is used
first. Otherwise the flow extracts an unambiguous covered-period end date from
the release body before applying title heuristics; it does not assume calendar
quarters for companies with non-calendar fiscal periods.

Scanned or near-empty PDFs are retained with a `*_no_ocr` limitation. OCR,
semantic retrieval, general news, and model-assisted identity linking are not
part of this lane.

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
`mb.cision.com` attachments are selected by ranked identity — explicit
`mfn-primary` marker, then Cision `Main/` path, then report-like link text
with corroborating report title (`attachment_tier` in
`mfn-primary` / `main-path` / `label-score`) — and ambiguous selection
(`ambiguous_selection`) fails the lane instead of guessing. Repeated
anchors with a byte-identical PDF href count as one attachment before
ranking; distinct target URLs still refuse as ambiguous.
Invitation/presentation/webcast-titled pages never contribute evidence.
Diagnostics split into `discovered`, `filtered_before_download`,
`download_failed`, `ambiguous_selection`, and `retained` (in `diagnostic()` and
the CLI output); per-class `completeness` (annual vs quarterly over
post-dedupe groups, with no feed `group_id` pairing assumption) is a hard
gate — shortfalls return `evidence_incomplete` with no frozen packet instead
of a green `complete`.

### Evidence-selection manifest

Selection is centralized in a pure, side-effect-free manifest
(`alphaforge/evidence/manifest.py`, `MANIFEST_VERSION =
evidence-selection-manifest-v1`): `select_evidence_manifest()` derives every
evidence role — audit history, cache, reuse, deduplication groups, packet
inputs, typed rejections — from immutable facts plus one rule input set
(`alphaforge/evidence/report_rules.py`, fingerprinted). Completeness, packet
construction, cache reuse, and readiness all consume that one view, never
raw persistence; `tools/check_evidence_manifest_boundary.py` enforces the
boundary in CI, and `manifest_store.load_evidence_view` is the read path.
Groups key on explicit bilingual group, then provider event id, then fiscal
period, then source URL (attachmentless events still group by period), with
class `annual` vs `quarterly`; completeness counts retained groups over
expected groups and `packet_contents()` returns exactly the retained
sources. Rejected candidates carry typed reasons (`rejection_reason`,
`outside_history_window`, `not_selected_by_manifest`); ambiguous-selection
blocks are recorded as typed rejections so their group stays in the coverage
denominator. Evidence without a recognized `attachment_tier` is
inadmissible on first run; unchanged reruns skip feed entries that already
have complete current-fingerprint documents and rebuild the manifest with
prior considered candidates and typed dispositions when the persisted feed
fingerprint matches and the candidate remains in that feed. The normalized feed
fingerprint is part of the manifest identity. A refetched detail that disappears
cannot erase a previously blocked candidate; changed feed inputs do not inherit
prior dispositions. The shared ranking/readiness view reconstructs candidate
accounting from that persisted manifest. Re-recording
a recurring manifest identity moves it to the current end of the run chronology,
so A→B→A input transitions expose A to replay and readiness. Persisted demoted
siblings join their selected parent's group only when stored PDF checksum, explicit
translation/revision relationship and matching variant group corroborate the
link; unresolved editions remain independent expected groups. Both candidate
URLs and report-class counts therefore remain stable across cache replay.
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
hash over the packet without its own `packet_hash`. Database-local document IDs
are projected to stable source identities derived from source URL, publication
date, and attachment checksum for hashing; stored IDs remain available for
provenance and citations. Run timestamps (`issuer.verified_at`, per-source
`ingestion_date`) stay in the stored JSON for auditability but are excluded
from the hash, so identical artifacts hash identically across databases built
at different times; packets hashed before this change keep validating against
their stored hash. Every packet also stamps `evidence_rules_version` (currently
v3 in `alphaforge/core/frozen_packet.py`): the monotonic version of the
evidence/filter/completeness rule set (report/invitation taxonomy,
issuer confirmation, attachment-tier selection, completeness counting). Stale
is defined narrowly as a packet built under an older rule version — including
pre-versioning packets without the marker. Older packets stay hash-valid but
readiness rejects them with `stale_evidence_packet`; rerun the lane to rebuild
under the current rules. Readiness for the evidence lane requires that frozen packet
hash to validate; a stray document row is not sufficient. Packet rows also carry
`report_rules_fingerprint`, `usable`, and `usable_reason`. A later non-complete
run marks every prior packet for the same `(company_id, as_of)` unusable in the
same transaction as its terminal job record; rows remain queryable for audit
history, while the loader reuses only valid, current-fingerprint, usable rows.
Run diagnostics are persisted in the packet on complete runs or the job error
on terminal failures, and `describe_evidence_state` is the replay source for
CLI/result diagnostics.

### Live verification

Opt-in live checks are `pytest -m integration` (fixture-only by default, and
in CI). No manual key step is needed: when integration tests are selected,
`tests/conftest.py` automatically resolves the repository-root `.env`
(worktree-safe via `tests/env_resolver.py`, which follows `git
--git-common-dir` to the primary checkout) and loads it. With no key in the
environment or that `.env`, the run fails fast and points at the resolver.
