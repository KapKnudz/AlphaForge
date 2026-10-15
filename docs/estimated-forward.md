# Aggregate estimated forward valuation

`alphaforge estimated-forward --input-json PACK --exports-dir DIRECTORY` evaluates
an explicitly supplied, versioned `estimated-valuation-v1` pack using
`estimated-forward-v1`. It acquires nothing and changes neither ranking/readiness
nor mature, authentic or legacy FCFF admission. This is a separate nominal WACC
lane, **not** a relabeling of financing-shield-excluded research values.

**PROVISIONAL — ESTIMATED, NOT A VERIFIED FORECAST** applies to every result,
including unavailable outputs. Arithmetic success is not financial review,
human implementation acceptance, a forecast certification or an investment
recommendation. Reference-only reviews remain reference-only.

## Ownership and executable examples

- `core/valuation/estimated_inputs.py`: evidence resolution, identity, versions.
- `core/valuation/estimated_projection.py`: optional aggregate response budgets.
- `core/valuation/estimated_fcff.py`: annual FCFF, WACC, continuation and claims.
- `cli/estimated_forward.py`: actual command/report/JSON/CSV composition.
- `tests/estimated_fixtures.py`: executable, **entirely synthetic** direct-budget
  and response-budget requests. `tests/test_estimated_forward.py` executes both
  through the evaluator and the actual CLI parser/export path.

For example, create a source-free test pack in a temporary directory:

```sh
python - <<'PY'
import json
from pathlib import Path
from tests.estimated_fixtures import pack
Path('/tmp/synthetic-estimate.json').write_text(json.dumps(pack()))
PY
alphaforge estimated-forward --input-json /tmp/synthetic-estimate.json --exports-dir /tmp/synthetic-estimates
```

This example's source records and financial numbers are fictional test inputs,
not evidence for any company. No issuer inputs or financial golden files ship.

## Pack and source contract

Amounts are original EUR or SEK **millions**, rates fractions, shares absolute
basic counts net of treasury. Forecasts cover ten full rolling annual periods
starting the day after V, plus full N+1/N+2 Gordon witnesses. Direct rows either
omit both period endpoints for deterministic generation or supply and match both;
one supplied endpoint is never silently rewritten. Endpoints follow the original
valuation anniversary, including restoration of February 29. K is a
separate UTC knowledge cutoff; later-than-V evidence requires
`retrospective_research`. Preparation and actual review dates are separate facts.

The envelope contains standard/contract, pack version/parent, entity/perimeter,
V/K/knowledge basis, preparer/date, object, denomination/lease basis, conditions,
limitations, sources, records, allocations and named scenarios (including base).
Scenario IDs must also be unique after case-folding because each scenario owns
export filenames. Malformed envelope mappings are refused before calculation;
malformed scenario or optional-stage mappings produce scoped invalid-input outputs.
Only `controlled_nonfinancial_operations` and capitalized/debt-like leases are
implemented. Acquisition/perimeter judgments are supplied, not inferred.

Sources retain content identities, owned anchors, publication, observation and
UTC ingestion facts. A separately disclosed `public_by` upper bound can prove
cutoff eligibility without inventing a first publication date; it needs a
`publication_limitation`. Same-cutoff-day publication needs `published_at` or
`public_by_at` to establish eligibility at the precise UTC cutoff; a later
same-day timestamp is not treated as known earlier. Unknown publication with no
established upper bound withholds consuming stages. Content hashes bind supplied identities; this
calculator does not authenticate documents or accounting interpretations.

Each material block has nonempty `record_ids`. Records distinguish `fact`,
`adjustment`, `estimate` and `condition`, with source/anchor references, rationale,
mechanism, counterevidence/response, alternatives, effective period and actual
preparer/date. Unresolved/invalid/unsupported records withhold their consumers;
a numerical placeholder is not a selected estimate. Facts cannot masquerade as
forecast annual rows. Root reviews are preserved actual records, never an
arithmetic-generated approval. Ordinary supported estimates do not need routine
approval tickets.

## One forward economics

Direct `annual` operations accept honestly aggregate rows, not fictional tax lots
or an asset/legal census. Rows retain recurring EBIT (after compatible D&A),
qualified noncash addbacks, aggregate **unlevered** operating cash taxes including
opening operating interactions once, cash investment, economic lease additions,
disposal cash, contiguous opening/closing operating WC and separately unembedded
operating settlements:

```text
FCFF = EBIT + noncash_addbacks + other_noncash - operating_cash_tax
       - cash_investment - lease_additions + disposal_cash
       - (WC_close - WC_open) - other_operating_cash
```

No second reinvestment deduction, automatic loss refund, compensation addback,
financing interest/principal or APV shield is introduced. Gross cash investment
and economic lease additions are nonnegative; signed cash/FCFF deficits remain
visible with supplied continuing-capacity and funding premises. Opening operating
taxes cannot be moved to the bridge just to bypass an operating gap.

Optional `aggregate_response` supplies one bounded response mechanism: a
revenue-growth fade, margin/ongoing-D&A transition, finite aggregate legacy
amortization, positive renewal budgets plus next-year positive growth funding,
WC/revenue, and two aggregate operating-profit tax buckets. The refundable bucket
has explicit charge/refund/recovery/minimum-rate parameters; the other bucket has
an ordinary burden. New payments split current/following year, recoveries use a
supplied lag, and opening payments/recoveries/deferred adjustments follow finite
supplied schedules. These are annual modeled estimates, **not** observed legal
remittances, taxpayer identities, asset vintages or a general tax engine. Refund
impairment changes the top-up interaction; a loss creates no new refund but can
still receive an existing recovery. Growth contraction creates no asset-sale
credit. Full parameters and constituent states remain inspectable in output.

WACC is either an evidenced reporting-currency nominal annual-effective supplied
estimate, or a build from effective `exp(continuous risk-free)-1`, beta/ERP and
independent dated market-equity/debt weights with a usable financing shield only
in the debt cost. The discount block declares V, reporting currency and rate basis.
A build additionally declares the market-close date/quote currency, debt
date/reporting currency, and debt-cost and shield dates/bases. It consumes the
same dated ownership share count and quote-FX block as per-share and quote outputs;
duplicate WACC share or FX inputs are invalid. It never borrows the SEK size-bucket
hurdle or derives weights from the DCF itself. Interest and shield cash are excluded
from FCFF.

```text
ExplicitPV = sum(FCFF_t / (1+r)^t, t=1..10)
TV_10 = fully_built_FCFF_11 / (r-g)
EV = ExplicitPV + TV_10/(1+r)^10, with r>0 and -1<g<r
```

Witnesses never enter explicit PV. Finite effects must be exhausted; every
material annual witness leg, including available gross tax/investment
constituents, must grow coherently. Retained witness growth must exactly equal the
selected Gordon growth. Retained witness revenue is optional for direct rows, but
when present it must be present in both witnesses and reconcile at the annual
witness tolerance; generated response revenue is always checked. Aggregate
response recovery queues are checked
from the first witness through the first mature-regime source year, so a loss-created
or other pending transition cannot hide behind two accidentally equal recoveries.
Finite supplied settlements cannot enter or extend beyond the witnesses.
`FCFF_10*(1+g)` is not a terminal substitute.
A tail gap preserves finite explicit-PV diagnostics, not a zero-tail EV. Runoff,
stubs and alternative terminal models are not implemented. Near-pole and
terminal-dominance flags ask for financial explanation, not automatic rejection
or approval.

## Claims, FX and output dependencies

The dated bridge requires a complete category inventory: accessible cash,
nonoperating assets, nonlease/lease debt, independently nonoperating taxes,
consideration, pension/employee, legal/other and noncommon claims. Each category
needs an explicit supported estimate, including any zero. Allocation IDs are
aggregate labels assigned once to operations, bridge, rights or excluded matched
pairs, not invented underlying censuses. In every scenario, each registered
operations, bridge and rights allocation is consumed exactly once by its declared
destination; only an `excluded_pair` is intentionally unconsumed. Reusing an
embedded allocation or omitting a registered claim is invalid.

Bridge methods are supplied reporting-currency PV, explicitly dated foreign PV
converted using an explicit foreign/reporting pair and `foreign_per_reporting`,
and an explicitly selected `earnings_linked_pv` response: current anchor times
positive year-one EBIT plus later anchor times mean positive years 1–5 EBIT,
divided by supplied positive recurring baseline EBIT. It preserves any whole-stage
or consumed-row annual refusal; supplied and foreign PV claims remain independent.
Asset categories are signed
as additions; debt, tax, consideration, pension, legal and noncommon categories
are signed as deductions. Earnings-linked PV is claim-only. This is a conditional
aggregate response, not invented contractual earnout terms. Book/PV and economic
interpretation remain supplied record judgments.

Existing European-call rights can be allocated endogenously against common
value with explicit counts/strikes/expiry/ACT365, volatility, continuous risk-free
and dividend assumptions. They may use the reporting currency or the declared
quote currency with their own explicit dated currency pair. Every differing-currency
conversion identifies both sides; an explicitly supplied same-currency rate must
be one. The monotone allocation reconciles
common plus calls to pre-rights residual, counts claims once and uses **basic**
shares; no duplicate dilution or exercise-proceeds asset. This internal claims
allocation is not a revenue-growth reverse DCF. Unsupported right types refuse.

Negative common residuals and signed quotients survive as nontradable diagnostics;
there is no zero-floor fair value or negative security-price estimate. Missing
shares withhold per-share only unless endogenous rights actually require that
denominator for common allocation. Missing quote FX withholds quote output only;
any separately required rights FX remains a genuine common-value dependency.

Each scenario always exports the same forward diagnostic stages plus the
independent not-requested reverse stage, including scenario-level malformed-input
fallbacks. Each stage exports availability, conditional/insufficient-evidence/
invalid-input/unsupported/not-requested/numerical-failure status and direct or
propagated reasons. An aggregate or dependent consumer preserves the first chronologically
consumed unavailable row's status and root reason/path; structural row-count or
shape failures remain distinct. Operating gaps suppress dependent complete PVs; rate gaps preserve
undiscounted cash; tail gaps preserve explicit PV; bridge gaps preserve EV;
denominator/quote-FX gaps remain scoped. Unaffected annual rows and supplied
component diagnostics remain visible. Limitations name their affected outputs.
Reverse exports **not requested/not implemented**; it does not suppress forward
estimates. A supplied recovered growth path is only another forward input, never
a newly computed inverse result. Reverse implementation and initial human
implementation acceptance are later work.

The conclusion is always withheld as an investment recommendation. A scenario
range spanning 10% of absolute base common value or a changed common sign adds an
explicit material-disagreement reason. Conditional scenarios are not probability
distributions or guaranteed bounds; smaller differences can also be material.

## Overrides, export and numerical checks

`--override-json FILE` accepts `version`, `author`, `prepared_on`, `reason` and
`changes` (replacement top-level fields). It leaves the original untouched,
creates a content-bound parented version, recomputes outputs, records changed
paths/affected outputs/actual result differences, and invalidates current reviews.
Result differences compare both versions' scenario and output-key unions with
presence flags and separately retain the full changed firm-conclusion pair.
Each changed path records before/after values and explicit presence flags, so a
missing key is distinguishable from an explicitly supplied null. Because built WACC consumes ownership and quote FX, revisions to either conservatively
invalidate every downstream output from explicit PV through both per-share values.
The export retains the parent pack as well as the child. It does not edit an
accepted reference or silently carry its approval into changed premises.

Write-once local bundles under `estimated-forward/<artifact identity>/` contain
original inputs, outputs, execution/code identities, report, annual CSV and
individual output JSON files. Code identities cover the estimated modules and the
shared supplied-schedule calculation/contract helpers they execute. Detached output files/CSV rows/report retain the
full identity and assumption pack, source anchors, conditions, limitations,
preparation/actual review facts and distinct calculation/financial/human status.
These are user-requested **local exports**, not permission to publish private
financial material. No DB, API, browser or model call is used. These bundles are
not the legacy retained-run/replay contract.

Calculations use local Decimal64, with guard precision for normal probabilities.
JSON retains unrounded decimal strings. Independent comparison uses
`abs(a-b) <= max(A, R*max(abs(a),abs(b)))`: annual legs/witnesses (including terminal
numerator) A=2e-8 million/R=2e-12; value/individual bridge/common components
A=1e-7 million/R=2e-11; per-share A=1e-8 currency/R=2e-11. Identities, dates, units,
share counts and withholding/allocation semantics are exact, never tolerance
exceptions. Tests cover components as well as totals. Presentation rounding is
not a license to change assumptions, reference expectations or tolerances.

A private reference can be checked without adding it to the repository:

```sh
python tools/check_estimated_forward_acceptance.py --manifest "$MANIFEST"
```

The externally supplied manifest uses
`estimated-forward-reference-acceptance-v1`, states a nonempty independent
`oracle_basis`, repeats the three tolerance objects above exactly, and lists
sibling `input_file`/`expected_file` pairs. Each expected file contains that same
`oracle_basis` and a complete independently prepared evaluator result. The checker
requires the complete output shape, compares only enumerated computed financial
fields by their fixed class, compares all other fields—including identifiers,
allocations, dates, units, shares, statuses and extensions—exactly, and prints only
the matched case count plus code revision and code-source identity, including the
shared supplied-schedule calculation and contract helpers. It neither generates
expected answers nor acquires, rewrites, exports or
registers reference material. Repository tests and examples remain synthetic; a
real-reference run is separate private evidence and must be repeated against the
exact delivered head.
