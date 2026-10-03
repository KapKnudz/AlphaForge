# Swedish fiscal regression excerpts

`varied_cohort_repairs.json` is a bounded excerpt of the retained 2026-10-03
varied-cohort scout: `diagnosis.json`, `source-inventory.csv`, `feed-CLAS B.json`,
`feed-BACTI B.json`, `source-citations.json`, and `sample-documents.json` in the
supervising investigation's `data/alphaforge-varied-cohort-e2e/` directory.

Titles, broad/fine document classes, old fiscal identities, exact release URLs,
publication values and body excerpts are observed. Corrected identities are
supported by the retained PDF headings/covers in `source_verification` (exact PDF
URL, SHA-256 and page). CLAS's Q1 and real annual report are unchanged controls.
The diagnosis's Swedish year-end siblings are unchanged parity controls.

The public CLI tests use **synthetic** HTML wrappers/timestamp markup, a single
fixture attachment and generated PDF bytes. They test discovery, title
interpretation, actual extraction, immutable storage, cache and CLI behavior;
they do not claim those generated PDFs are the issuer originals. Historical
upgrade tests pin the observed old resolver outputs under an older rule stamp,
then run the new interpreter without PDF HTTP. Synthetic financial controls
exercise a real non-calendar DCF path before/after the textual correction; they
are not an independent audit of issuer DCF inputs or model assumptions.

No live requests, paid models, OCR, foreign publisher support or raised limits
are required. Historical scout artifacts are not modified.
