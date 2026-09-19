-- SQLite 3.38+ (WAL, json1, ON CONFLICT DO UPDATE)
-- Enable once at connection open: PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL;
-- Files: data/alphaforge.db (gitignored) | data/alphaforge.test.db

CREATE TABLE IF NOT EXISTS companies (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    borsdata_id         INTEGER UNIQUE NOT NULL,
    name                TEXT NOT NULL,
    ticker              TEXT,
    yahoo               TEXT,
    isin                TEXT,
    instrument          INTEGER NOT NULL DEFAULT 0 CHECK (instrument IN (0,1)),
    sector_id           INTEGER,
    branch_id           INTEGER,
    market_id           INTEGER,
    country_id          INTEGER,
    listing_date        TEXT,
    stock_price_currency TEXT,
    report_currency     TEXT,
    raw_payload         TEXT CHECK (raw_payload IS NULL OR json_valid(raw_payload)),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;
CREATE INDEX IF NOT EXISTS idx_companies_ticker ON companies(ticker);
CREATE INDEX IF NOT EXISTS idx_companies_branch ON companies(branch_id);

CREATE TABLE IF NOT EXISTS watchlist (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    ticker              TEXT NOT NULL,
    isin                TEXT,
    name_hint           TEXT,
    source_file         TEXT NOT NULL,
    source_row_hash     TEXT NOT NULL,
    matched_via         TEXT CHECK (matched_via IN ('isin','ticker','borsdata_id','unmatched')),
    borsdata_id         INTEGER,
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (source_file, source_row_hash),
    UNIQUE (company_id)
) STRICT;

CREATE TABLE IF NOT EXISTS financial_periods (
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
    cash                REAL,
    eps                 REAL,
    dividend_per_share  REAL,
    shares_outstanding  REAL,
    is_placeholder      INTEGER NOT NULL DEFAULT 0 CHECK (is_placeholder IN (0,1)),
    raw_payload         TEXT CHECK (raw_payload IS NULL OR json_valid(raw_payload)),
    fetched_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (company_id, period_type, period_end),
    CHECK (is_placeholder = 1 OR revenue IS NOT NULL OR net_income IS NOT NULL OR equity IS NOT NULL OR total_debt IS NOT NULL)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_financials_company_period_end ON financial_periods(company_id, period_type, period_end DESC);
CREATE INDEX IF NOT EXISTS idx_financials_pit ON financial_periods(company_id, period_type, report_date, period_end) WHERE is_placeholder = 0;

CREATE TABLE IF NOT EXISTS kpi_observations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    kpi_id              INTEGER NOT NULL,
    period_type         TEXT NOT NULL CHECK (period_type IN ('last','year','r12')),
    price_type          TEXT NOT NULL CHECK (price_type IN ('latest','mean','high','low')),
    observation_date    TEXT,
    year                INTEGER,
    report_period       INTEGER,
    value               REAL,
    fx_rate_to_sek      REAL CHECK (fx_rate_to_sek IS NULL OR fx_rate_to_sek > 0),
    fx_source           TEXT CHECK (fx_source IN ('currency_ratio','manual','null') OR fx_source IS NULL),
    fetched_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK ((period_type='last' AND observation_date IS NOT NULL) OR (period_type IN ('year','r12') AND year IS NOT NULL))
) STRICT;
-- Partial unique indexes — conflict targets for ON CONFLICT DO UPDATE (KPI sync)
-- Snapshot: one latest value per kpi per observation_date
CREATE UNIQUE INDEX IF NOT EXISTS uq_kpi_snapshot
    ON kpi_observations(company_id, kpi_id, period_type, price_type, observation_date)
    WHERE period_type = 'last';
-- History: one value per kpi per year+report_period per period/price type
CREATE UNIQUE INDEX IF NOT EXISTS uq_kpi_history
    ON kpi_observations(company_id, kpi_id, period_type, price_type, year, report_period)
    WHERE period_type IN ('year','r12');

CREATE TABLE IF NOT EXISTS prices (
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    price_date          TEXT NOT NULL,
    close               REAL NOT NULL CHECK (close > 0),
    volume              INTEGER CHECK (volume IS NULL OR volume >= 0),
    currency            TEXT,
    fetched_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (company_id, price_date)
) WITHOUT ROWID, STRICT;
CREATE INDEX IF NOT EXISTS idx_prices_company_date ON prices(company_id, price_date DESC);
CREATE INDEX IF NOT EXISTS idx_prices_liquidity ON prices(company_id, price_date DESC) WHERE volume IS NOT NULL;

CREATE TABLE IF NOT EXISTS dividends (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    ex_date             TEXT NOT NULL,
    amount              REAL NOT NULL CHECK (amount >= 0),
    currency            TEXT NOT NULL,
    dividend_type       INTEGER NOT NULL CHECK (dividend_type IN (0,1,2)),
    distribution_frequency TEXT,
    fetched_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (company_id, ex_date, dividend_type, amount)
) STRICT;

CREATE TABLE IF NOT EXISTS dividend_coverage (
    company_id          INTEGER PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
    covered_from        TEXT NOT NULL,
    covered_through     TEXT NOT NULL,
    source              TEXT NOT NULL DEFAULT 'borsdata',
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (covered_through >= covered_from)
) STRICT;

CREATE TABLE IF NOT EXISTS ranking_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    as_of               TEXT NOT NULL,
    model_version       TEXT NOT NULL,
    packet_hash         TEXT,
    universe_hash       TEXT,
    company_count       INTEGER NOT NULL,
    eligible_count      INTEGER NOT NULL,
    scores              TEXT NOT NULL CHECK (json_valid(scores)),
    inputs_summary      TEXT CHECK (inputs_summary IS NULL OR json_valid(inputs_summary))
) STRICT;

CREATE TABLE IF NOT EXISTS research_documents (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    source_url          TEXT NOT NULL,
    source_type         TEXT NOT NULL CHECK (source_type IN ('mfn','annual_pdf','transcript','press_release','other')),
    title               TEXT,
    published_at        TEXT,
    fetched_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    content_text        TEXT,
    page_count          INTEGER,
    pages_included      TEXT,
    page_truncated      INTEGER CHECK (page_truncated IN (0,1)),
    duplicate_of        INTEGER REFERENCES research_documents(id),
    ingested_lang       TEXT,
    checksum            TEXT,
    raw_metadata        TEXT CHECK (raw_metadata IS NULL OR json_valid(raw_metadata)),
    UNIQUE (checksum),
    UNIQUE (company_id, source_url)
) STRICT;

CREATE TABLE IF NOT EXISTS theses (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    revision            INTEGER NOT NULL CHECK (revision > 0),
    as_of               TEXT NOT NULL,
    packet_hash         TEXT NOT NULL,
    prior_packet_hash   TEXT,
    change_log          TEXT CHECK (change_log IS NULL OR json_valid(change_log)),
    thesis_json         TEXT NOT NULL CHECK (json_valid(thesis_json)),
    model               TEXT NOT NULL,
    prompt_version      TEXT NOT NULL,
    verdict             TEXT CHECK (verdict IN ('reject','watch','latent','activated')),
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (company_id, revision)
) STRICT;

CREATE TABLE IF NOT EXISTS jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type            TEXT NOT NULL CHECK (job_type IN ('sync_instruments','sync_reports','sync_kpis','sync_prices','sync_dividends','sync_insider','sync_buyback','sync_shorts','rank','evidence','analyze','export')),
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

CREATE TABLE IF NOT EXISTS mfn_feed_checks (
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    mfn_slug            TEXT NOT NULL,
    checked_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    discovered_count    INTEGER NOT NULL,
    unseen_count        INTEGER NOT NULL,
    PRIMARY KEY (company_id, checked_at)
) STRICT;

CREATE TABLE IF NOT EXISTS news_releases (
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    slug                TEXT,
    url                 TEXT PRIMARY KEY,
    title               TEXT NOT NULL,
    body                TEXT,
    published_at        TEXT,
    duplicate_of        TEXT REFERENCES news_releases(url),
    lang                TEXT,
    release_category    TEXT,
    scraped_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

-- Free-ownership deterministic staging (ship with ownership stack)
CREATE TABLE IF NOT EXISTS ownership_holders_pdf_staging (
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    observation_date    TEXT NOT NULL,
    holder_name         TEXT NOT NULL,
    shares              REAL,
    pct                 REAL CHECK (pct IS NULL OR (pct >= 0 AND pct <= 100)),
    source_id           TEXT NOT NULL,
    raw_text            TEXT,
    PRIMARY KEY (company_id, observation_date, holder_name)
) STRICT;

CREATE TABLE IF NOT EXISTS ownership_events_staging (
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    event_date          TEXT NOT NULL,
    event_type          TEXT NOT NULL CHECK (event_type IN ('flagging','placement','lockup','share_change')),
    holder_name         TEXT,
    shares              REAL,
    pct                 REAL,
    lockup_expiry_date  TEXT,
    source_ids          TEXT NOT NULL CHECK (json_valid(source_ids)),
    PRIMARY KEY (company_id, event_date, event_type, holder_name)
) STRICT;

CREATE TABLE IF NOT EXISTS company_short_snapshots (
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    observation_date    TEXT NOT NULL,
    shorts_proc         REAL,
    shorts_holders      REAL,
    shorts_milj         REAL,
    dtc_avg             REAL,
    trend_1w            REAL,
    trend_1m            REAL,
    source              TEXT NOT NULL,
    PRIMARY KEY (company_id, observation_date)
) STRICT;

-- Phase 0 must-adds — per-share correctness & catalyst calendar (Börsdata 33-path integration)
CREATE TABLE IF NOT EXISTS stock_splits (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER REFERENCES companies(id) ON DELETE CASCADE,
    borsdata_id         INTEGER NOT NULL,
    split_type          TEXT NOT NULL CHECK (split_type IN ('S','RS')),
    ratio               TEXT NOT NULL,
    split_date          TEXT NOT NULL,
    fetched_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (borsdata_id, split_date)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_stock_splits_company_date ON stock_splits(company_id, split_date);

CREATE TABLE IF NOT EXISTS report_calendar (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER REFERENCES companies(id) ON DELETE CASCADE,
    borsdata_id         INTEGER NOT NULL,
    release_date        TEXT NOT NULL,
    report_type         TEXT NOT NULL CHECK (report_type IN ('Q1','Q2','Q3','Q4')),
    fetched_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (borsdata_id, release_date)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_report_calendar_company_date ON report_calendar(company_id, release_date);

-- Phase 0 should-adds — reference dictionaries & metadata caches (seeded once)
CREATE TABLE IF NOT EXISTS branches (
    branch_id           INTEGER PRIMARY KEY,
    name_sv             TEXT NOT NULL,
    name_en             TEXT,
    sector_id           INTEGER REFERENCES sectors(sector_id)
) STRICT;

CREATE TABLE IF NOT EXISTS sectors (
    sector_id           INTEGER PRIMARY KEY,
    name_sv             TEXT NOT NULL,
    name_en             TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS countries (
    country_id          INTEGER PRIMARY KEY,
    name_sv             TEXT NOT NULL,
    name_en             TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS translation_metadata (
    translation_key     TEXT PRIMARY KEY,
    name_sv             TEXT,
    name_en             TEXT,
    fetched_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

CREATE TABLE IF NOT EXISTS kpi_metadata (
    kpi_id              INTEGER PRIMARY KEY,
    name_sv             TEXT NOT NULL,
    name_en             TEXT NOT NULL,
    format              TEXT CHECK (format IN ('%','CURR','MCURR',NULL)),
    is_string           INTEGER NOT NULL CHECK (is_string IN (0,1)),
    fetched_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

CREATE TABLE IF NOT EXISTS report_metadata (
    property            TEXT PRIMARY KEY,
    name_sv             TEXT NOT NULL,
    name_en             TEXT NOT NULL,
    format              TEXT CHECK (format IN ('MCURR','CURR','MILL',NULL)),
    fetched_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

-- Per-branch KPI allow-list built from kpis/{reporttype}/summary discovery
CREATE TABLE IF NOT EXISTS branch_kpi_allowlist (
    branch_id           INTEGER NOT NULL REFERENCES branches(branch_id),
    kpi_id              INTEGER NOT NULL REFERENCES kpi_metadata(kpi_id),
    discovered_via      TEXT NOT NULL DEFAULT 'summary',
    discovered_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (branch_id, kpi_id)
) STRICT;
