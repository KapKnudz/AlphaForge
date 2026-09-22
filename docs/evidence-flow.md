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
group, selection-rule, and relationship audit record, surfaced in the packet as
`selection_state` / `selection_reason` (`PREFERRED_LANGUAGE` /
`FALLBACK_LANGUAGE`) / `variant_group_id`.

Document language is decided from the PDF itself — attachment-filename markers,
then word scoring over the first three extracted pages — and outranks MFN
release-language metadata; the evidence and source used are stored as
`pdf_language` / `language_evidence`. Identity dates (`period_start` /
`period_end`) are resolved once per article from provider metadata or the
release body and carried into packet sources next to `observation_date`.

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

The resulting `evidence_packets` row is canonical JSON with stable ordering,
publication/ingestion dates, source/page anchors, limitations, and a SHA-256
hash over the packet without its own `packet_hash`. Run timestamps
(`issuer.verified_at`, per-source `ingestion_date`) stay in the stored JSON
for auditability but are excluded from the hash, so identical artifacts hash
identically across databases built at different times; packets hashed before
this change keep validating against their stored hash. Readiness for the
evidence lane requires that frozen packet hash to validate; a stray document
row is not sufficient.

### Live verification

Opt-in live checks are `pytest -m integration` (fixture-only by default, and
in CI). No manual key step is needed: when integration tests are selected,
`tests/conftest.py` automatically resolves the repository-root `.env`
(worktree-safe via `tests/env_resolver.py`, which follows `git
--git-common-dir` to the primary checkout) and loads it. With no key in the
environment or that `.env`, the run fails fast and points at the resolver.
