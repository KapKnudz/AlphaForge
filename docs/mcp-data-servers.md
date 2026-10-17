# AlphaForge data MCP server (Börsdata + MFN)

Read-only MCP server that exposes AlphaForge's existing Börsdata adapter
and MFN scraper as tools for MCP clients (Claude Code, Codex, and similar).
It adds no new data source: Börsdata supplies structured financials and
prices, MFN supplies report and press-release discovery.

## Run

From the AlphaForge project root, using the project environment:

```sh
source .venv/bin/activate
pip install -e ".[mcp]"
alphaforge-mcp            # or: python -m alphaforge.mcp.server
```

Transport is stdio. The server reads `BORSDATA_API_KEY` from the
environment or from a `.env` file at the project root. MFN tools need no
key. Register it in an MCP client with the project `.venv` interpreter,
for example:

```json
{
  "mcpServers": {
    "alphaforge-data": {
      "command": "/absolute/path/to/AlphaForge/.venv/bin/python",
      "args": ["-m", "alphaforge.mcp.server"],
      "cwd": "/absolute/path/to/AlphaForge"
    }
  }
}
```

## Tools

Börsdata (instrument ids come from `borsdata_search_company`):

- `borsdata_search_company(query, limit)` — find instruments by ticker, name, or ISIN.
- `borsdata_reports(ins_id, period_type, limit)` — `year`, `quarter`, or `r12` financial reports.
- `borsdata_kpi_history(ins_id, kpi_id, report_type, price_type, limit)` — one KPI over time.
- `borsdata_kpi_summary(ins_id, report_type)` — KPI summary block.
- `borsdata_kpi_metadata()` — KPI id/name/format catalogue.
- `borsdata_prices(ins_id, limit)` — daily prices, most recent `limit` rows.
- `borsdata_dividends(ins_ids)` — dividend calendar rows.
- `borsdata_report_calendar(ins_ids)` — scheduled report dates.

MFN (slug form is `a/<issuer>`, e.g. `a/evolution`, `a/clas-ohlson`):

- `mfn_reports(mfn_slug, limit=480)` — annual/interim report releases from the full archive.
- `mfn_releases(mfn_slug, limit=480)` — all press releases from the full archive.

Both MFN tools page through the archive feed (`/all/a/<issuer>`, capped by
MFN at 480 items) and deduplicate by URL, so a default call returns the
complete history MFN exposes, not only the latest page.

## Response shape and reproducibility

Every response carries `source`, `fetched_at` (UTC), `params`, `count`,
`sha256` (hash of the returned payload), and `data`. Live provider data
can later be restated or adjusted by the provider, so a caller that feeds
a valuation should record the `sha256` and `fetched_at` alongside the
result — that pins which bytes the valuation used. Use the tools for live
discovery and screening; freeze snapshots for decisions.

Provider errors are returned as `{"error": ...}` results with the API key
stripped, never as raw exceptions. Known data limits still apply: Börsdata
has no tax-lot or asset-register data, and its KPI histories carry
year/period only, not observation vintages.
