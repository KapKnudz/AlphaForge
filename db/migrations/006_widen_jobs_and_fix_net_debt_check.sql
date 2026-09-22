-- 006: preserve distinct KPI endpoint diagnostics and reconcile net_debt CHECK.
-- jobs.job_type gains sync_kpis_37_year, sync_kpis_42_year, sync_kpis_37_r12,
-- sync_kpis_42_r12, sync_kpis_allowlist_37, sync_kpis_allowlist_42 so the
-- dedicated ROIC / net-debt-EBITDA fetch path can record per-endpoint
-- failures without overwriting a shared sync_kpis row.
-- financial_periods is rebuilt so migrated databases carry the same
-- non-placeholder CHECK as fresh databases (net_debt accepted alongside
-- revenue, net_income, equity, and total_debt). SQLite keeps the original
-- CHECK on ALTER TABLE ADD COLUMN, so the 005 ALTER alone leaves migrated
-- DBs rejecting net-debt-only rows that fresh DBs accept.
PRAGMA foreign_keys=OFF;

CREATE TABLE jobs_new (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type            TEXT NOT NULL CHECK (job_type IN ('sync_instruments','sync_reports','sync_kpis','sync_kpis_37_year','sync_kpis_42_year','sync_kpis_37_r12','sync_kpis_42_r12','sync_kpis_allowlist_37','sync_kpis_allowlist_42','sync_prices','sync_dividends','sync_insider','sync_buyback','sync_shorts','rank','evidence','analyze','export')),
    company_id          INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    borsdata_id         INTEGER,
    status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','success','failed','partial')),
    attempt             INTEGER NOT NULL DEFAULT 0,
    error               TEXT CHECK (error IS NULL OR json_valid(error)),
    started_at          TEXT,
    finished_at         TEXT,
    fetched_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (job_type, company_id)
) STRICT;
INSERT INTO jobs_new (id, job_type, company_id, borsdata_id, status, attempt, error, started_at, finished_at, fetched_at)
    SELECT id, job_type, company_id, borsdata_id, status, attempt, error, started_at, finished_at, fetched_at FROM jobs;
DROP TABLE jobs;
ALTER TABLE jobs_new RENAME TO jobs;

CREATE TABLE financial_periods_new (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    period_type         TEXT NOT NULL CHECK (period_type IN ('year','r12','quarter')),
    period_end          TEXT NOT NULL,
    report_year         INTEGER,
    report_period       INTEGER,
    report_date         TEXT,
    broken_fiscal_year  INTEGER CHECK (broken_fiscal_year IN (0,1)),
    currency            TEXT,
    currency_ratio      REAL,
    fx_rate_to_sek      REAL CHECK (fx_rate_to_sek IS NULL OR fx_rate_to_sek > 0),
    fx_source           TEXT CHECK (fx_source IN ('currency_ratio','manual','null') OR fx_source IS NULL),
    revenue             REAL,
    gross_income        REAL,
    operating_profit    REAL,
    ebit                REAL,
    ebitda              REAL,
    net_income          REAL,
    free_cash_flow      REAL,
    operating_cash_flow REAL,
    investing_cash_flow REAL,
    financing_cash_flow REAL,
    equity              REAL,
    total_assets        REAL,
    total_debt          REAL,
    net_debt            REAL,
    cash                REAL,
    eps                 REAL,
    dividend_per_share  REAL,
    shares_outstanding  REAL,
    is_placeholder      INTEGER NOT NULL DEFAULT 0 CHECK (is_placeholder IN (0,1)),
    raw_payload         TEXT CHECK (raw_payload IS NULL OR json_valid(raw_payload)),
    fetched_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (company_id, period_type, period_end),
    CHECK (is_placeholder = 1 OR revenue IS NOT NULL OR net_income IS NOT NULL OR equity IS NOT NULL OR total_debt IS NOT NULL OR net_debt IS NOT NULL)
) STRICT;
INSERT INTO financial_periods_new (id, company_id, period_type, period_end, report_year, report_period, report_date, broken_fiscal_year, currency, currency_ratio, fx_rate_to_sek, fx_source, revenue, gross_income, operating_profit, ebit, ebitda, net_income, free_cash_flow, operating_cash_flow, investing_cash_flow, financing_cash_flow, equity, total_assets, total_debt, net_debt, cash, eps, dividend_per_share, shares_outstanding, is_placeholder, raw_payload, fetched_at)
    SELECT id, company_id, period_type, period_end, report_year, report_period, report_date, broken_fiscal_year, currency, currency_ratio, fx_rate_to_sek, fx_source, revenue, gross_income, operating_profit, ebit, ebitda, net_income, free_cash_flow, operating_cash_flow, investing_cash_flow, financing_cash_flow, equity, total_assets, total_debt, net_debt, cash, eps, dividend_per_share, shares_outstanding, is_placeholder, raw_payload, fetched_at FROM financial_periods;
DROP TABLE financial_periods;
ALTER TABLE financial_periods_new RENAME TO financial_periods;
CREATE INDEX IF NOT EXISTS idx_financials_company_period_end ON financial_periods(company_id, period_type, period_end DESC);
CREATE INDEX IF NOT EXISTS idx_financials_pit ON financial_periods(company_id, period_type, report_date, period_end) WHERE is_placeholder = 0;

PRAGMA foreign_keys=ON;
