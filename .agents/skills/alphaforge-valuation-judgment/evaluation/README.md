# Repeat the initial bounded evaluation

The exact initial synthetic inputs are [routing-prompt.txt](routing-prompt.txt)
and [compare-prompt.txt](compare-prompt.txt). They contain no authentic issuer
data or private receipts. These reproduce the *initial* check described in the
[parent README](../README.md), not the later independent review's six separate
prompts. Treat each execution as a new observation, not a promised baseline score.

## Preconditions and read-only commands

Use a trusted checkout, installed Pi with the flags below, and existing authorized
access to `openai-codex/gpt-6.1-sol`. Verify `pi --help` and model availability;
if access or the model is unavailable, record that limitation rather than silently
switching models, copying credentials or changing account configuration. Fresh
`--no-session` runs isolate conversation state; they use normal authentication,
not an empty replacement HOME or agent directory. Disable extensions, MCP and
context files to avoid unintended task context. The only enabled tool is `read`.

Run this Bash block from the checkout. Only evaluation receipts are written, in
a new temporary directory outside the repository; no model edit tools are enabled.
Calls are sequential and bounded to three fresh sessions, not an open-ended loop.

```bash
ROOT=$(git rev-parse --show-toplevel)
cd "$ROOT"
SKILL="$ROOT/.agents/skills/alphaforge-valuation-judgment"
OUT=$(mktemp -d "${TMPDIR:-/tmp}/alphaforge-valuation-eval.XXXXXX")
COMMON=(--provider openai-codex --model gpt-6.1-sol --thinking medium
  --no-session --no-extensions --no-mcp --no-context-files
  --no-prompt-templates --no-themes --tools read --approve --mode json --print)

git rev-parse HEAD > "$OUT/head.txt"
pi --version > "$OUT/pi-version.txt"
printf '%s\n' "Receipts: $OUT"

# Normal discovery, including competing installed skills; routing-only batch.
pi "${COMMON[@]}" @"$SKILL/evaluation/routing-prompt.txt" \
  > "$OUT/routing.jsonl" 2> "$OUT/routing.stderr"
printf '%s\n' "$?" > "$OUT/routing.exit"

# Identical financial prompt/settings; baseline has no advertised skills.
pi "${COMMON[@]}" --no-skills @"$SKILL/evaluation/compare-prompt.txt" \
  > "$OUT/without.jsonl" 2> "$OUT/without.stderr"
printf '%s\n' "$?" > "$OUT/without.exit"

# Treatment advertises only this skill; the model still chooses whether to read.
pi "${COMMON[@]}" --no-skills --skill "$SKILL" \
  @"$SKILL/evaluation/compare-prompt.txt" \
  > "$OUT/with.jsonl" 2> "$OUT/with.stderr"
printf '%s\n' "$?" > "$OUT/with.exit"
```

Check all three exit receipts and stderr before interpreting answers. Retain model,
reasoning level, Pi version, exact head, prompts, enabled tools and competing skill
set with the results. An unavailable provider or interrupted call is not a pass.
Keep any additional authentic evidence or local evaluation receipts private.

## Judge the traces, not just claimed invocation

Inspect JSONL `tool_execution_start` read calls and assistant `message_end` text
(or the equivalent event records in the installed Pi version). In the routing
batch, expect P1–P5 to select this helper and N1–N3 to select none. A single skill
read plus per-case selections is batch-routing evidence, not eight independent
natural-task invocations; a model naming a skill without reading is weaker evidence.

For both financial arms, assess these conclusions against the skill's public
principles and current contract owners:

- **A:** ROCE definition differs from after-tax operating ROIC; a historical
  ratio is not an observed future incremental return. Completeness/tests do not
  approve premises. Ask for matched reviewed operands and explicit forecast basis.
- **B:** Acquisition timing/perimeter, earn-out recurrence and currency are
  unmatched. Averaging, blanket goodwill removal or attaching today's date cannot
  repair them. Refuse this input, not valuation universally.
- **C:** Accept conditional suitability on the stipulated matched/admitted basis;
  acquisition history alone is not disqualifying. Future return remains an
  assumption, and suitability does not guarantee a numerical value or reverse root.

Look for facts/assumptions, consistency checks, disconfirmation and scoped
conclusions. In treatment, verify actual skill/reference reads and current owner
checks; in baseline, do not supply the treatment's answers or instructions.
`--no-skills` disables advertising, not file access: the model may independently
find and read the helper with `read`. If the baseline reads it, mark the paired
comparison contaminated/inconclusive rather than claiming a without-skill effect.
Record misses as misses rather than narrowing prompts or discarding responses.

The initial baseline and treatment both reasoned correctly; treatment added
implementation-context distinctions. The initial routing batch selected the
expected positives and negatives. One stochastic run per arm, bundled cases,
competing user skills, model/runtime drift and no fixed sampling seed limit
comparability. Repeating exact inputs/settings improves auditability, not
statistical power or deterministic invocation. These are development probes,
not live-model CI, financial expertise certification or analyst approval.
