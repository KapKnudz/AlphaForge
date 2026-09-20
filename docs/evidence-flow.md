# Deterministic textual evidence flow

Issuer identity is source-of-truth data keyed by `companies.id`. MFN discovery
stores observed candidates in `mfn_issuer_candidates`; only a `mapped` row in
`mfn_issuer_mappings` with a source URL, verification timestamp, and identity
evidence can drive ingestion. Use `alphaforge mfn-map-seed` for a reviewed JSON
seed or `alphaforge mfn-map` for an explicit operator decision. Ambiguous
discovery is stored for review and is never promoted automatically.

The one-company lane is explicit:

```text
alphaforge evidence --ticker TICKER --as-of YYYY-MM-DD --diagnostic
```

It discovers MFN quarterly/interim, year-end, and annual releases; accepts only
authoritative detail-page publication timestamps at or before the cutoff;
downloads unseen PDF attachments with bounded retries and byte/page limits;
checks content type and `%PDF-` magic bytes; hashes the raw bytes; extracts with
`pypdf`; and persists logical documents, bilingual sibling provenance,
attachments, extraction metadata, and page anchors through repository helpers.

Scanned or near-empty PDFs are retained with a `*_no_ocr` limitation. OCR,
semantic retrieval, general news, and model-assisted identity linking are not
part of this lane.

The resulting `evidence_packets` row is canonical JSON with stable ordering,
publication/ingestion dates, source/page anchors, limitations, and a SHA-256
hash over the packet without its own `packet_hash`. Readiness for the evidence
lane requires that frozen packet hash to validate; a stray document row is not
sufficient.
