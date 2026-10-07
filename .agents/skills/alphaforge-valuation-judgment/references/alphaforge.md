# AlphaForge contract owners

Resolve these paths relative to this reference file. Read only the owner relevant
to the decision and inspect its implementation at the checkout being reviewed.
These pointers deliberately avoid copying policy constants or proposed changes.

| Decision | Existing authority |
| --- | --- |
| Locate the implemented versus planned boundary | [Architecture](../../../../docs/architecture.md) |
| Formula, earnings/return basis, currency, reinvestment, route and solve availability | [Valuation](../../../../docs/valuation.md); [policy](../../../../alphaforge/core/valuation/dcf_policy.py), [calibration validation](../../../../alphaforge/core/valuation/reinvestment.py), [shared evaluator](../../../../alphaforge/core/valuation/reverse_dcf.py), [solve eligibility](../../../../alphaforge/core/valuation/solve_eligibility.py) |
| Stored financial dates, selection, market operands and export wiring | [Deterministic flow](../../../../docs/deterministic-flow.md); [ranking loader](../../../../alphaforge/cli/ranking_loader.py) |
| Document completeness, qualification, anchors, immutable packet and explicit artifact root | [Evidence flow](../../../../docs/evidence-flow.md) |
| Frozen numerical bodies versus textual packet identity; audit replay limits | [Executed-run replay](../../../../docs/executed-run-replay.md) |
| Pinned future thesis validation/repair/citation constraints | [MVP design](../../../../docs/plans/2026-09-16-alphaforge-mvp.md), §4.5; this is design authority, not proof that a thesis pipeline is wired |

The helper explains premises and asks for missing judgments. Deterministic code
owns admission and validation; the analyst owns actual source/accounting and
forecast review. A generated explanation is neither an approval record nor a
calibration. Keep acquisition outside the frozen valuation/thesis decision;
retain limitations instead of acquiring more evidence during a frozen run.

For any refusal, separate **evidence availability**, **input compatibility**,
**method implementation**, **candidate economics** and **numerical solving**.
Report the actual layer and relevant operands, including later prerequisites
not established by resolving the first visible reason. An approved but unlanded
specification is not current implementation. Re-check the owners after boundary
changes; do not teach snapshot refusals as permanent company classifications.
