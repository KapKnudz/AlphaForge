# Positive-profit DCF slice: pre-implementation decision gate

Status: convention approved via task instruction 001, with the annotations below; implemented behavior is owned by [valuation.md](../valuation.md). No company valuation or newly admitted issuer calibration is produced by this investigation.

Approval annotations: implied-margin solving is unavailable for this constant-margin slice; terminal-growth solving is not identifiable and must not emit roots. Convergence to the hurdle is deliberately conservative and can understate high-return companies such as Mips and Evolution. Linked explicit-transition sensitivity before convergence is distinguished from mature terminal neutrality.

## Authority and decision-time baseline

Read the four-company recheck, staged engine plan, and source-verification reports in the supervising task's retained data. The staged plan §2 recommended forward funding, capital-driven profit growth (or evidenced sales-to-capital), return convergence, and a linked terminal-growth transition. Its §§5/8 explicitly described timing and growth choices as requiring approval, not already implemented policy. The launch intent approved the staged direction but did not resolve those exact alternatives.

At this decision gate, `alphaforge/core/valuation/reverse_dcf.py` and `dcf_policy.py` charged current projected NOPAT times current revenue growth / constant ROIC, floored contraction investment at zero and capped investment at NOPAT. Margins could change independently. Terminal investment used terminal growth and the same constant return. Policy additionally capped a descriptive revenue reinvestment rate; this was not a second operative investment formula when ROIC was present. A terminal-growth solve changed terminal growth but left the explicit fade endpoint fixed. The decision required public missing dated ROIC and negative-profit refusal to remain unavailable. These were economically different conventions from the proposed stage, not merely missing display fields. See [valuation.md](../valuation.md) for implemented behavior.

## Approved smallest convention

1. End-of-year forward funding: `I_t = (NOPAT_(t+1) - NOPAT_t) / q_(t+1)`; year-one operating capital is already present at valuation time. Require evidence for that starting-capital premise.
2. Initially admit only constant positive margin ROIC scenarios with qualified historical capital evidence and an explicitly assumed future incremental return. Historical average ROIC is not measured future marginal ROIC. Varying margins remain unsupported absent capital-driven-profit evidence; no revenue-growth shortcut or unpriced efficiency improvement. Sales-to-capital implementation can wait for qualified evidence rather than expanding this slice.
3. Linear return fade across the explicit funding intervals, starting at the admitted future-return assumption and reaching the positive discount hurdle in the final interval. The hurdle is a **proxy**, not measured company WACC. Horizon remains five years as policy, not a company-specific moat estimate.
4. Year n funds year n+1 at the terminal return. Terminal NOPAT derives from year n revenue times `(1+gT)` and stable margin/tax; terminal investment funds n+2, `I_(n+1)=NOPAT_(n+1)*gT/qT`. Export both NOPAT and FCFF, investment amounts/ratios and the return path. Still refuse `gT >= r`, nonfinite values and nonpositive returns/rates before division.
5. Link the explicit revenue fade endpoint to terminal growth in any explicitly supplied scenario. Under the approval annotation, terminal growth is **not solved**. This is not the previous frozen-path one-axis solve; linked transition scenarios may change pre-convergence value even though mature growth produces no excess-return value.
6. No silent caps, cash refunds or capital releases. Ordinary public policy refuses unsupported external financing, zero/negative profits or sign crossings, and capital contraction. Invalid candidates must be reported as invalid, not converted into endpoint prices or opportunistically narrowed bounds.

Alternative timing is same-year spending with an explicit starting-capital state and separately consistent terminal bridge. Alternative varying-margin treatment requires qualified sales-to-capital evidence. Neither alternative can be selected just from current EBIT/revenue history. The six points were confirmed subject to the approval annotations above.

## Independent arithmetic locks

Checked with a standalone 40-digit Decimal calculation importing no AlphaForge code; these are synthetic calculator controls, never company evidence.

- NOPAT 100 to 104: return 20% gives investment 20 and FCFF 80; return 10% gives investment 40 and FCFF 60. NOPAT 100 to 130 at 20% gives investment 150 and FCFF -50, not capped zero FCFF.
- Revenue 100 to 110 with margins 10% to 20%, tax zero: profit 10 to 22 gives forward investment 60 at 20%, current FCFF -50. This checks a declared capital-driven assumption, not an admissible generic margin forecast.
- Two explicit years, revenue/profit boundary 100, growth 4%, tax zero, margin 100%, r=q=10%, shares 10, debt 5: N1=104, N2=108.16, N3=112.4864; I1=41.6, FCFF1=62.4; I2=43.264, FCFF2=64.896; terminal I3=44.99456, FCFF3=67.49184, TV2=1124.864. EV=62.4/1.1+(64.896+1124.864)/1.21=1040; price=103.5.
- First interval q=20%, last interval and perpetuity q=10%: I1=20.8, FCFF1=83.2; remaining bridge unchanged. EV=1058.90909090909. Constant 20% perpetual return would not satisfy this fixture.

These checks corroborate the staged plan's proposed bridge, not its approval or suitability for any issuer.

## One own-company investigation: Clas Ohlson

Used established issuer evidence only: Q1 2026/27, published **2026-09-03T05:00:00Z**, fiscal quarter **2026-05-01–2026-07-31**, issuer PDF:

https://storage.mfn.se/a2e0c126-5ba6-4c32-b950-37d50b68f5e6/clas-ohlson-interim-report-q1-202627-eng.pdf

Inspected retained extracted pages 9, 17, 19–21 from `current-source-documents.json` in the four-company evidence acquisition dataset. The established source-verification report identifies its observation date as **2026-10-03**; no finer ingestion timestamp is asserted. This is retrospective retained evidence, not a new acquisition or a claim of original historical-vintage coverage.

Page 17 defines ROCE as **EBIT plus financial income divided by monthly averaged capital employed**. Page 19 reports LTM August 2025–July 2026 EBIT 1642.4 MSEK, financial income 35.8, average capital 4812.1. Independent division gives 34.87458698%, reconciling issuer 34.9%. FY May 2025–April 2026 figures 1527.1, 31.0 and 4570.9 give 34.08737885%, reconciling 34.1%.

This is dated, qualified **issuer ROCE**, not admitted ROIC: pre-tax numerator includes financing income; capital includes cash. Page 21 defines capital employed as total assets less non-interest-bearing current and noncurrent liabilities. Page 9 identifies ROU assets, lease debt, goodwill, deferred taxes and receivables. Page 20 provides cash and lease debt at reporting dates, not monthly cash-normalized operating capital across the full earnings period. The acquisition perimeter also changed. Reported EBIT is not automatically normalized recurring profit. Multiplying issuer ROCE by 79% or subtracting closing cash from monthly average capital would invent mismatched definitions.

Outcome: **no qualified own-company operating-return calibration admitted**. No date is attached to the rejected provider KPI records and no company DCF availability is restored.

Exact evidence still needed for this candidate:

- Matched beginning/end operating invested-capital reconciliation for a full annual earnings period (or matched monthly series if using the issuer average), with cash/excess-cash and nonoperating asset treatment justified.
- Consistent IFRS16 earnings/capital/debt basis; explicit goodwill/acquisition, deferred-tax and operating-liability treatment. No lease normalization switch is part of this slice.
- Same-period recurring EBIT reconciliation and explicit normalized operating tax assumption versus observed tax; currency, units, consolidation perimeter and source page/hash/publication/observation provenance for each operand.
- For historical incremental returns: positive material capital changes across matched comparable periods, acquisition/FX adjustments and attributable profit changes. Otherwise label any future incremental return an approved assumption calibrated from historical average returns, never observed future ROIC.
- Supported maintenance/replacement capacity and starting operating capital premise; profitability alone does not establish either.

No paid acquisition, sector default, lease switch, share/date repair, agent or general publication extraction is proposed. Missing evidence can legitimately leave all four actual companies unavailable after a correctly implemented economic policy. A qualified calibration must be validated, retained and replay-bound before public use; synthetic tests must remain separate.
