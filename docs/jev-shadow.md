# Jev shadow signals

AlphaForge keeps Jev optional and observational. The deterministic frozen
packet, citation validation, readiness gate, ranking, and exports remain the
source of truth. Jev cannot create or repair citations, unblock readiness, or
change a packet.

## Enablement

The API key is read only from the server environment and is never included in
the packet, request audit, logs, or exports:

```sh
export TYPESAFE_API_KEY='server-side-secret'
export ALPHAFORGE_JEV_SHADOW_ENABLED=1
export ALPHAFORGE_JEV_CITATION_RELATIONS=1
export ALPHAFORGE_JEV_MISSING_INFORMATION=1
```

The default model is the pinned `jev-1.13.0`. Override it only as an explicit
versioned experiment with `ALPHAFORGE_JEV_MODEL`; aliases such as
`jev-latest` are intentionally not the default because they can move. Calls
use the official Python SDK, a five-second timeout, at most one identical
transport retry, and per-run limits of two calls, 12,000 input tokens, and
$0.50 input cost. These bounds can be lowered through the corresponding
`ALPHAFORGE_JEV_*` settings in `JevShadowConfig.from_env()`.

The sidecar API is deliberately narrow:

```python
from alphaforge.llm import JevShadowSidecar

sidecar = JevShadowSidecar(conn=conn)
relation = sidecar.classify_citation_relation(packet, citation, claim)
impact = sidecar.classify_missing_information(packet, missing_item, specialist_requirement)
```

Both calls first verify the frozen packet hash and perform deterministic
identity, point-in-time, catalog, anchor, and exact excerpt checks. A failed
check is audited as `not_attempted` and never reaches Jev. Model responses are
checked once; malformed responses, timeouts, rate limits, model-version
mismatches, and budget failures produce an audited error with no fallback or
repair attempt.

## Inspecting results

Every attempt appends one structured row to `jev_shadow_audit`. It contains the
packet/source or missing-item identity, checksums, claim/question versions,
pinned and returned model versions, complete probabilities, confidence, token
usage, latency, retry/error outcome, and observation time. It contains no
generated prose or secret values.

```sql
SELECT observed_at, feature, packet_hash, selected_class, probabilities,
       confidence, retry_outcome, error_code
FROM jev_shadow_audit
ORDER BY id DESC;
```

`action` is always `shadow_only`. Low-confidence outputs are marked for review
by deterministic code, but no automatic verification, unblocking, reranking,
identity, date, deduplication, arithmetic, or authorization action is taken.

With no `TYPESAFE_API_KEY`, or with the explicit flags unset, normal
deterministic behavior is unchanged and the sidecar steps aside.
