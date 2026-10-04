# Architecture entry point

AlphaForge has two implemented research-input paths joined by company identity
and an explicit `as_of` date, not by a model conversation:

```text
Börsdata → stored observations → deterministic calculations → ranking / readiness → JSON + CSV
MFN issuer mapping → retained PDF history → selection manifest → frozen evidence packet
                                                      └────→ ranking / readiness provenance
```

`rank` freezes and retains selected stored numerical inputs before calculation,
then retains original outputs before publication; it fetches no live market data.
[Executed-run audit replay](executed-run-replay.md) reconstructs from retained
bodies without mutable numerical lookup. `evidence` acquires
and freezes textual inputs for one company; rerun `rank` to consume the resulting
packet. Neither command currently generates a thesis.

## Contract ownership

| Contract | Authority and implementation boundary |
| --- | --- |
| Acquisition, stored identities, cutoff selection, calculation wiring, ranking and exports | [Deterministic flow](deterministic-flow.md); [`cli/main.py`](../alphaforge/cli/main.py) owns CLI composition, [`cli/kpi_sync.py`](../alphaforge/cli/kpi_sync.py) owns the synchronous per-company KPI workflow, and [`cli/ranking_loader.py`](../alphaforge/cli/ranking_loader.py) composes stored inputs with core calculators. |
| Valuation formulas, assumptions, units at the valuation seam and policy versions | [Valuation](valuation.md); [`core/valuation/`](../alphaforge/core/valuation/). The heuristic score and auditable DCF remain separate outputs. |
| MFN identity, retained artifacts, immutable observations, selection, completeness and packet hashing | [Textual-evidence flow](evidence-flow.md); [`evidence/flow.py`](../alphaforge/evidence/flow.py), [`evidence/manifest.py`](../alphaforge/evidence/manifest.py), [`evidence/manifest_store.py`](../alphaforge/evidence/manifest_store.py) and [`db/evidence_repository.py`](../alphaforge/db/evidence_repository.py). |
| Live SQLite schema and upgrades | [`db/alphaforge.sqlite.sql`](../db/alphaforge.sqlite.sql), [`db/migrations/`](../db/migrations/) and [`db/migrations.py`](../alphaforge/db/migrations.py), not the historical plan's DDL sketch. |
| Target design and deferred scope | [Consolidated MVP plan](plans/2026-09-16-alphaforge-mvp.md), especially §§3–7 and §9; it is design authority, not a claim that every listed phase is shipped. |

## Shared boundaries

- **Identity and time:** `companies.id` is the database join identity;
  `borsdata_id`, ticker and ISIN identify external inputs. MFN requires an
  independently verified issuer mapping. Observation/period dates, publication
  dates and ingestion timestamps are different facts; a cutoff is not an
  ingestion snapshot. See each flow's filtering and replay contract.
- **Integration versus calculation:** providers acquire data; persistence lives
  under `db/`; core calculators own arithmetic and typed missing results. The
  intended field-mapping seam is [`core/kpi_taxonomy.py`](../alphaforge/core/kpi_taxonomy.py).
  CLI composition owns I/O and export, not financial policy.
- **Evidence versus numbers:** a frozen textual packet's hash is carried into
  ranking provenance. It does **not** hash all financial rows or prove their
  numerical replay. Readiness is distinct from ranking eligibility, and neither
  a high score nor an arbitrary document row is a model authorization contract.
- **Future model boundary:** the pinned thesis design freezes/hashes evidence
  before calls and requires catalogued citations or explicit limitations.
  Validation and its single repair attempt belong to deterministic code; a
  second validation failure produces `insufficient_evidence`, not fallback
  prose ([plan §4.5](plans/2026-09-16-alphaforge-mvp.md#45-validation-contract--one-schema-one-citation-regime-one-repair-ceiling)).
  Transport, format and validation retries must remain separate triggers.
  Semantic recall and open-ended retrieval are outside that frozen path.

## Implemented versus planned

**Implemented:** watchlist import, Börsdata sync, stored-input ranking and DCF
exports, immutable executed-run numerical retention/audit replay, readiness
assessment, and the one-company MFN/PDF evidence lane.
Optional [Jev shadow signals](jev-shadow.md) are observational: they do not
change selection, readiness or packet contents.

**Planned, not wired as a thesis pipeline:** six specialists, the Stock
Researcher aggregator, deterministic specialist-output validation/repair,
immutable thesis revisions, incremental news updates, and the `analyze` and
standalone `export` commands. SQLite is the runtime store; the
[Postgres DDL](../db/alphaforge.postgres.sql) is not a working DSN switch today.
The broader combined financial/textual model packet in plan Phase 3 is not the
current textual `evidence_packets` contract.

Known deterministic-path gaps against that design are recorded in
[the flow's alignment notes](deterministic-flow.md#alignment-notes).
Changes are reviewed with the [contribution checklist](../CONTRIBUTING.md#contract-alignment).
Import contracts and the evidence-manifest boundary check enforce specific
structural seams; they are not universal semantic architecture verification or
proof of model citation/number fidelity.
