---
name: alphaforge-valuation-judgment
description: "Judge financial valuation premises before choosing a method or accepting a result. Use for ambiguous valuation-method suitability; ROCE versus operating ROIC versus future incremental returns; reported versus recurring earnings; accounting, fiscal-period, currency or acquisition-perimeter inconsistencies; changed margin, funding or business boundaries; document completeness versus qualified evidence; and interpreting a financial refusal despite passing tests. Applies to valuation reasoning and financial code reviews, not routine UI, formatting or mechanical code edits that leave financial meaning unchanged."
---

# AlphaForge valuation judgment

Resolve the financial premise, not just the formula. This is a small reasoning
helper, broader than DCF and narrower than general finance; it supplies neither
analyst approval nor investment recommendations.

## Decision procedure

1. **Frame the decision.** Name the valuation object (operations or equity),
   proposed method, accounting basis, period, currency and consolidation
   perimeter. If the task only changes presentation or mechanically preserves
   financial meaning, return to that task without financial review.
2. **Choose the relevant contrast.** Read the matching section of
   [examples](references/examples.md): returns, earnings, acquired capital,
   evidence, tests/refusals or method suitability. Read its cited
   [principle](references/principles.md) before treating a general claim as
   authoritative. Examples are anonymized qualitative teaching cases, not
   current issuer classifications or data to insert into a model.
3. **Reconcile facts and assumptions.** Cite supplied source anchors for facts;
   mark absent facts as limitations. Separate forecast assumptions from observed
   history. Check numerator/denominator definitions, duration, currency/FX,
   leases, cash/non-operating assets, acquisition timing and double counting.
   Ask what evidence would overturn the proposed conclusion; inspect it when
   supplied. A provider label, adjusted figure or plausible ratio is not review.
4. **Separate economics from software admission.** For AlphaForge decisions,
   read [contract owners](references/alphaforge.md) and the relevant owner at
   the checkout being reviewed. An economically reasonable method can be
   unimplemented; a supported method can lack qualified inputs. Keep the
   deterministic refusal intact and identify its layer. Request a precise
   analyst/source decision rather than inventing a route, calibration, approval,
   future return or source correction. Stop when the supported conclusion or
   smallest missing premise is identified; do not expand into acquisition or
   general financial-model construction.

## Response contract

Give a compact answer with:

- **Applicable method:** candidate and suitability; separately, implementation
  availability if relevant.
- **Facts / assumptions / limitations:** each material fact with a supplied
  source anchor, each forecast premise labeled as an assumption.
- **Consistency checks:** passed, failed or unknown on the relevant bases.
- **Disconfirming evidence:** what was checked and what would change the answer.
- **Conclusion:** supported conditional result, scoped refusal, or one precise
  analyst question specifying the missing evidence and decision it unlocks.

Use `dcf-valuation` or `quantitative-valuation` for general model construction
and `equity-research` for broader research when available and actually requested.
Their generic defaults do not authorize AlphaForge policy overrides. This skill
ends at premise judgment; it does not run tools advertised by those skills,
change deterministic validation, or replace the analyst's accounting judgment.
