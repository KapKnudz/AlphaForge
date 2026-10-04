# Swedish-company fiscal interpretation repair — 2026-10-03

## Scope and findings

This repair is restricted to the Swedish CLAS B, EVO, MIPS and BACTI B paths.
The original varied-cohort report and diagnosis were examined before editing;
the later Swedish-only correction supersedes foreign cohort acceptance.
Foreign publisher investigation/work is deferred, not shipped. No URL,
issuer/canonical/publisher, attachment-host guard or retrieval limit changes.
Historical observations, PDFs and manifests are not rewritten.

Retained issuer page text, not numerical replay, supports these interpretations:

| Issuer | Verified covered semantics | Repair |
| --- | --- | --- |
| CLAS B | English PDF p1 explicitly says `YEAR-END REPORT/Q4 2025/26`, with Feb 2026–Apr 2026 quarter and May 2025–Apr 2026 full year. | English `YEAR_END_REPORT` changes `2025/2026` → `2025/2026-q4`; Swedish sibling remains Q4. Real annual edition stays `2025/2026`; Q1 2026/27 stays Q1. No inferred calendar-year dates. |
| EVO | Year-end PDF p8 has Oct–Dec 2025 and Jan–Dec 2025 columns, distinct from 2024 comparators. | English year-end `2025` → `2025/2025-q4`; Swedish sibling remains Q4. |
| MIPS | Annual PDF p1 says `2025 | Annual and Sustainability Report`. | Compound annual title resolves formerly null identity to `2025`, not Q4. Existing typed Q4 reports remain Q4. |
| BACTI B | English year-end PDF p1 explicitly covers Fourth quarter 2025 (October–December); annual PDF p1 says `ANNUAL AND SUSTAINABILITY REPORT 2025`. | English year-end `2025` → `2025/2025-q4`; compound annual null → `2025`. Swedish year-end remains Q4. |

Exact observed release/PDF URLs, SHA-256s, page anchors and bounded heading
excerpts are committed in
[`varied_cohort_repairs.json`](../../tests/fixtures/mfn/varied_cohort_repairs.json).
The [fixture notes](../../tests/fixtures/mfn/varied_cohort_repairs.README.md)
distinguish source observations from generated test wrappers/PDFs.

## Numerical/DCF impact and coordination boundary

**These textual fiscal repairs directly change no numerical DCF input for any
of the four issuers.** `load_results_for_company` in
`alphaforge/cli/ranking_loader.py` selects financial periods and annual
chronology from Börsdata repository rows. Its policy receives current report,
latest annual, historical annuals, selected currency/market cap and ROIC;
`ReverseDcfInputs` receives selected price, shares, revenue, net debt and policy
assumptions. None is extracted from MFN `fiscal_period` or the PDF text here.
Textual fiscal identity changes observation/relation/manifest slots and packet
provenance; it cannot correct wrongly acquired provider financial inputs.

Cross-boundary tests execute the public rank and replay commands with available
DCF and synthetic non-calendar financial controls before/after each historical
fiscal repair. Financial selection, metrics and the entire reverse-DCF result
remain equal, while observations/fingerprints/packet hashes change. This is a
boundary regression, **not proof that the live DCF is correct**.

Retained source reconciliation identifies remaining numerical questions for
the separate DCF/source scout, without edits here:

- CLAS: source full-year operating margin is 12.2% (p1 year-end); current LTM
  is 12.7% (p1 Q1). Thus a 21.1% implied margin is not its observed margin.
  This repair does not establish why that implied result arose.
- EVO: p8 annual distinguishes net revenues 2,066.540 EURm from total
  operating revenues 2,118.207 EURm (including other income). The acquired
  revenue uses total operating revenues, converted into SEK; that is not
  issuer EUR net-sales growth. The 3.84% terminal-growth output still needs
  separate assumption/solve assessment.
- MIPS: source FY EBIT 156 MSEK differs from adjusted EBIT 160 (Q4 p2);
  current LTM EBIT 229, revenue 665 (Q2 p2). Compound title identity does not
  supply/overwrite these numerical rows or explain the no-solution result.
- BACTI: source annual total revenues 228.8 differ from net sales 215.9
  MSEK (year-end p1). Current RTM total revenue 217.9 and loss 8.8 are on
  Q2 p1. These are provider/source definition issues, not repaired here;
  no-solution direction/distance remains for the separate DCF audit.

Older provider Dec30 annual ends, exchange/share/price verification,
terminal assumptions and solve-bound diagnostics remain outside this change.
Exact replay is a determinism control, never an issuer-source or investment
correctness certificate.

## Implementation and verification

`evidence/ingest.py` uses the existing guarded document type before the broad
annual class and recognizes compound annual body headings. `mfn_taxonomy.py`
recognizes the compound English title as annual. Existing comparator,
forecast, provider/title conflict, and contradictory-heading refusal remain.
Published-head review also exposed non-covered forecast, outlook and comparator
cues in compound annual titles or headings; the fiscal-context guard rejects
year cues in each marker's sentence-local comma- or semicolon-delimited segment,
including context before the report label, while retaining covered annual years
in another segment and independent sentences.
The initial repair advanced report and evidence interpretation so prior packets
cannot confer current readiness. Current rule versions and later guards are
owned by the
[evidence-flow contract](../evidence-flow.md#immutable-history-and-legacy-backfill).
Changed interpretation creates new immutable observations, reusing verified
bytes/extraction, never restamping old history.

`tests/test_varied_cohort_evidence_repairs.py` exercises the public
`alphaforge evidence --ticker ... --as-of 2026-10-03 --diagnostic` parser/command,
actual retained title cases, stable unchanged replay without PDF requests,
historical correction with byte-identical old observations/manifests, ordinary
annual/Q1 controls and forecast/comparator/conflict negatives. Tests replace
HTTP only on the normal path; upgrade controls explicitly pin the scout's old
resolver outputs to model historical interpretation. No paid models or new
bulk source downloads are involved.

Local validation covered the full `pytest -q` suite, `ruff check .`,
`ruff format --check .`, all three `lint-imports` contracts,
`tools/check_evidence_manifest_boundary.py` and the focused new module. These
checks protect behavior; they do not certify source accuracy or the separate
DCF audit.
