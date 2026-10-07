# Using and checking this helper

## Discovery and ambiguous decisions

This project-local directory uses the portable Agent Skills layout. Pi discovers
`.agents/skills/` from the working directory through repository ancestors; no
router, hook, project-memory entry or model-policy change is needed. Start a
trusted project session (or `/reload` an existing one). Pi exposes the name,
description and path in the available-skills list; only a selected skill's body
and relevant references are read. Check startup diagnostics for malformed
frontmatter, disabled resources or name collisions.

The description names observable decision situations, not the agent's subjective
feeling of confusion: a changed acquisition perimeter, differing currency or
period bases, a return being substituted for another return, a claimed recurring
profit, or a refusal being interpreted as a bug. This makes relevant ambiguity
eligible for selection even without the word “ambiguous.” Presentation-only and
meaning-preserving mechanical edits are explicit negative cases.

Selection remains **model-selected, not deterministic enforcement**. A model may
miss a trigger or read without applying it well. In Pi, explicitly request
`/skill:alphaforge-valuation-judgment <question>` when you need the instructions
loaded; this still does not guarantee financial correctness or analyst approval.
Other runtimes need their own supported discovery and explicit-invocation syntax.
The deterministic core, not this helper, enforces AlphaForge admission/refusals.

## Compact development checks

Use synthetic facts; keep authentic source data and evaluation transcripts
private. No live-model CI, evaluation platform or production changes are required.
For the exact initial synthetic prompts, three read-only Pi commands and trace
interpretation, see [repeatable evaluation](evaluation/README.md). Check discovery
with the runtime's actual loader/startup, not a grep for skill text. These are the
representative situations covered:

| Prompt situation | Expected selection / conclusion |
| --- | --- |
| Retail ROCE is proposed as observed future incremental return | Load; distinguish definitions and historical evidence from forecast assumption |
| Earn-out gain in reported EBIT versus larger adjusted EBIT | Load; reconcile recurrence rather than choosing the higher profit |
| Closing acquisition assets paired with pre-acquisition profit; EUR/SEK mismatch | Load; average capital and a ROIC date cannot fix the unmatched basis |
| Complete PDFs and passing tests coexist with a financial refusal | Load; identify missing qualification or candidate boundary, not automatic approval |
| Software supplier serves banks; annual losses but profitable quarter | Load; activities determine suitability, quarter/EBITDA is not annual positive NOPAT |
| UI color, Markdown table formatting, or mechanical variable rename preserving financial meaning | Do not select this helper |
| Unseen contrast: acquisition held for the whole year; reviewed compatible earnings/capital, funded maintenance/capacity, stable margins and actual admission; future return explicitly assumed | Load when judging premises; acquisition history alone does not warrant refusal; suitability does not promise value/root |

For a bounded with/without comparison, use identical prompts, model, reasoning
level and read-only tools in fresh sessions. Disable other skills in both arms;
add only this skill in the treatment arm. Separately probe normal discovery with
competing installed skills. Inspect actual read calls and answers against the
expected conclusions, not merely the model's stated intention to invoke.

### Initial bounded check (2026-10-07)

Pi's real skill loader accepted this frontmatter without skill diagnostics.
A normal-discovery routing probe using `openai-codex/gpt-6.1-sol`, medium reasoning,
read-only tools and no context files selected/read this helper for all five
financial situations and selected none for the three mechanical situations.
This was one batched routing-only prompt, not eight independent natural tasks.

A paired fresh-session check used the same model/settings and identical three
synthetic questions (return substitution, acquisition/earnings/currency mismatch,
and the unseen valid full-year acquisition contrast). Both arms reached the
expected refusal/conditional-suitability conclusions. The treatment actually
read the skill, examples, public-principle reference, contract-owner reference
and `docs/valuation.md`, and added the current implementation's original-currency
and reported-input distinctions. The baseline already reasoned correctly; this
small check shows disclosure and useful contract context, **not demonstrated
accuracy improvement**, general financial expertise or reliable invocation rates.
Single runs, no fixed sampling seed, a bundled prompt and supplied admission
facts limit inference. Public sources were separately fetched and checked; the
model comparison did not independently audit issuer evidence or retrieve them.
