# Deterministic textual evidence flow

Issuer identity is source-of-truth data keyed by `companies.id`. MFN discovery
stores observed candidates in `mfn_issuer_candidates`; only a `mapped` row in
`mfn_issuer_mappings` with a source URL, verification timestamp, and identity
evidence can drive ingestion. Use `alphaforge mfn-map-seed` for a reviewed JSON
seed or `alphaforge mfn-map` for an explicit operator decision. Ambiguous
discovery is stored for review and is never promoted automatically. Live discovery
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
candidates for the same issuer and report kind can match. A match requires one
strong corroborator (a shared provider event ID, exact PDF/attachment checksum,
or equal numeric key-figure fingerprint) plus at least two compatible derived
signals (fiscal period, resolved observation date, publication date, or
translation-neutral title). Same-language documents never merge; ambiguous or
semantic-only pairs remain separate. The preferred variant follows the packet
language majority, otherwise English, and suppressed siblings retain a
`duplicate_of`, language, group, and selection-rule audit record.

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
hash over the packet without its own `packet_hash`. Readiness for the evidence
lane requires that frozen packet hash to validate; a stray document row is not
sufficient.
