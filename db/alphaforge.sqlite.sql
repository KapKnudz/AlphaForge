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
CREATE INDEX IF NOT EXISTS idx_financials_company_period_end ON financial_periods(company_id, period_type, period_end DESC);
CREATE INDEX IF NOT EXISTS idx_financials_pit ON financial_periods(company_id, period_type, report_date, period_end) WHERE is_placeholder = 0;

CREATE TABLE IF NOT EXISTS financial_period_rejections (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    reason              TEXT NOT NULL,
    period_type         TEXT,
    report_year         TEXT,
    report_period       TEXT,
    payload_hash        TEXT NOT NULL,
    raw_payload         TEXT NOT NULL CHECK (json_valid(raw_payload)),
    rejected_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (company_id, payload_hash, reason)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_financial_period_rejections_company
    ON financial_period_rejections(company_id, rejected_at DESC);

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
    dividend_type       INTEGER NOT NULL CHECK (dividend_type IN (0,1,2,4)),
    distribution_frequency TEXT,
    currency_verified   INTEGER NOT NULL DEFAULT 0 CHECK (currency_verified IN (0,1)),
    currency_conflicted INTEGER NOT NULL DEFAULT 0 CHECK (currency_conflicted IN (0,1)),
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

-- Exact (start,end] assurance; never inferred from min/max dividend dates.
CREATE TABLE IF NOT EXISTS dividend_window_coverage (
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('unknown','partial','complete')),
    source TEXT NOT NULL,
    assurance TEXT,
    verified_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (company_id, window_start, window_end),
    CHECK (window_end > window_start),
    CHECK (status <> 'complete' OR
        (length(trim(assurance)) > 0 AND assurance IS NOT NULL
         AND length(trim(verified_at)) > 0 AND verified_at IS NOT NULL))
) WITHOUT ROWID, STRICT;

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
    report_rules_fingerprint TEXT,
    UNIQUE (company_id, source_url)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_research_documents_checksum ON research_documents(checksum);

-- Deterministic MFN issuer identity. Candidate discovery is auditable, but
-- only a reviewed row with status=\"mapped\" may drive report ingestion.
CREATE TABLE IF NOT EXISTS mfn_issuer_mappings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL UNIQUE REFERENCES companies(id) ON DELETE CASCADE,
    mfn_slug            TEXT,
    source_url          TEXT,
    status              TEXT NOT NULL CHECK (status IN ('mapped','ambiguous','unmapped')),
    discovery_source    TEXT NOT NULL,
    verified_at         TEXT,
    identity_evidence   TEXT CHECK (identity_evidence IS NULL OR json_valid(identity_evidence)),
    last_checked_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (status <> 'mapped' OR (mfn_slug IS NOT NULL AND source_url IS NOT NULL AND verified_at IS NOT NULL))
) STRICT;

CREATE TABLE IF NOT EXISTS mfn_issuer_candidates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    mfn_slug            TEXT NOT NULL,
    source_url          TEXT NOT NULL,
    discovery_source    TEXT NOT NULL,
    match_basis         TEXT,
    identity_evidence   TEXT CHECK (identity_evidence IS NULL OR json_valid(identity_evidence)),
    status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected')),
    discovered_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (company_id, mfn_slug, source_url)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_mfn_issuer_candidates_review
    ON mfn_issuer_candidates(company_id, status, discovered_at);

-- Attachment, extraction and page-level provenance remain separate from the
-- logical release row so raw bytes never need to be re-downloaded to rebuild a
-- packet and sibling translations remain auditable.
CREATE TABLE IF NOT EXISTS research_attachments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id         INTEGER NOT NULL REFERENCES research_documents(id) ON DELETE CASCADE,
    source_url          TEXT NOT NULL,
    content_type        TEXT,
    byte_size           INTEGER NOT NULL CHECK (byte_size >= 0),
    sha256              TEXT NOT NULL,
    magic_valid         INTEGER NOT NULL CHECK (magic_valid IN (0,1)),
    http_status         INTEGER,
    fetched_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    raw_metadata        TEXT CHECK (raw_metadata IS NULL OR json_valid(raw_metadata)),
    UNIQUE (document_id, source_url)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_research_attachments_checksum ON research_attachments(sha256);

CREATE TABLE IF NOT EXISTS document_extractions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id         INTEGER NOT NULL UNIQUE REFERENCES research_documents(id) ON DELETE CASCADE,
    extractor           TEXT NOT NULL,
    text_checksum       TEXT,
    page_count          INTEGER NOT NULL CHECK (page_count >= 0),
    pages_included      TEXT,
    page_truncated      INTEGER NOT NULL CHECK (page_truncated IN (0,1)),
    scanned             INTEGER NOT NULL CHECK (scanned IN (0,1)),
    limitations         TEXT CHECK (limitations IS NULL OR json_valid(limitations)),
    extracted_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;

CREATE TABLE IF NOT EXISTS document_pages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    extraction_id       INTEGER NOT NULL REFERENCES document_extractions(id) ON DELETE CASCADE,
    page_number         INTEGER NOT NULL CHECK (page_number > 0),
    anchor              TEXT NOT NULL,
    text                TEXT NOT NULL,
    text_checksum       TEXT NOT NULL,
    UNIQUE (extraction_id, page_number)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_document_pages_anchor ON document_pages(anchor);

CREATE TABLE IF NOT EXISTS evidence_packets (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    as_of               TEXT NOT NULL,
    packet_hash         TEXT NOT NULL,
    packet_json         TEXT NOT NULL CHECK (json_valid(packet_json)),
    report_rules_version INTEGER NOT NULL DEFAULT 0,
    report_rules_fingerprint TEXT NOT NULL DEFAULT 'legacy',
    usable              INTEGER NOT NULL DEFAULT 1 CHECK (usable IN (0,1)),
    usable_reason       TEXT,
    frozen_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (company_id, as_of, packet_hash)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_evidence_packets_current ON evidence_packets(company_id, as_of, id DESC);
CREATE INDEX IF NOT EXISTS idx_evidence_packets_usable
    ON evidence_packets(company_id, as_of, usable, report_rules_fingerprint, id DESC);

CREATE TABLE IF NOT EXISTS evidence_run_diagnostics (
    company_id              INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    as_of                   TEXT NOT NULL,
    status                  TEXT NOT NULL,
    diagnostic              TEXT NOT NULL CHECK (json_valid(diagnostic)),
    packet_hash             TEXT,
    report_rules_fingerprint TEXT NOT NULL,
    recorded_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (company_id, as_of)
) STRICT;

CREATE TABLE IF NOT EXISTS evidence_selection_manifests (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    as_of               TEXT NOT NULL,
    manifest_id         TEXT NOT NULL,
    manifest_json       TEXT NOT NULL CHECK (json_valid(manifest_json)),
    report_rules_fingerprint TEXT NOT NULL,
    packet_hash         TEXT,
    recorded_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (company_id, as_of, manifest_id)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_evidence_selection_manifests_current
    ON evidence_selection_manifests(company_id, as_of, id DESC);

CREATE TABLE IF NOT EXISTS jev_shadow_audit (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at           TEXT NOT NULL,
    feature               TEXT NOT NULL CHECK (feature IN ('citation_relation', 'missing_information')),
    packet_hash           TEXT,
    source_id             TEXT,
    missing_item_id       TEXT,
    anchor                TEXT,
    span_checksum         TEXT,
    coverage_checksum     TEXT,
    claim_hash            TEXT,
    question_hash         TEXT,
    question_version      TEXT NOT NULL,
    criteria_version      TEXT NOT NULL,
    pinned_model_version  TEXT NOT NULL,
    returned_model        TEXT,
    selected_class        TEXT,
    probabilities         TEXT CHECK (probabilities IS NULL OR json_valid(probabilities)),
    confidence            REAL CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    input_tokens          INTEGER CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens         INTEGER CHECK (output_tokens IS NULL OR output_tokens >= 0),
    usage                 TEXT CHECK (usage IS NULL OR json_valid(usage)),
    latency_ms            INTEGER NOT NULL CHECK (latency_ms >= 0),
    retry_outcome         TEXT NOT NULL,
    error_code            TEXT,
    input_hash            TEXT,
    action                TEXT NOT NULL DEFAULT 'shadow_only'
) STRICT;
CREATE INDEX IF NOT EXISTS idx_jev_shadow_audit_packet ON jev_shadow_audit(packet_hash, observed_at);
CREATE INDEX IF NOT EXISTS idx_jev_shadow_audit_feature ON jev_shadow_audit(feature, observed_at);

CREATE TRIGGER IF NOT EXISTS jev_shadow_audit_no_update
BEFORE UPDATE ON jev_shadow_audit
BEGIN
    SELECT RAISE(ABORT, 'jev_shadow_audit is append-only');
END;

CREATE TRIGGER IF NOT EXISTS jev_shadow_audit_no_delete
BEFORE DELETE ON jev_shadow_audit
BEGIN
    SELECT RAISE(ABORT, 'jev_shadow_audit is append-only');
END;

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

CREATE TABLE IF NOT EXISTS mfn_feed_checks (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    mfn_slug            TEXT NOT NULL,
    checked_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    discovered_count    INTEGER NOT NULL,
    unseen_count        INTEGER NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS idx_mfn_feed_checks_chronological
    ON mfn_feed_checks(company_id, checked_at DESC, id DESC);

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

-- Schema version 10: immutable evidence history (kept in sync with migration 010).
-- Additive immutable evidence history beneath manifest-owned selection.
-- Exactly nine tables are introduced; legacy evidence and packet tables are untouched.

CREATE TABLE IF NOT EXISTS evidence_observation_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL UNIQUE,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    provider TEXT NOT NULL CHECK (provider = 'mfn'),
    as_of TEXT NOT NULL,
    source_input_fingerprint TEXT NOT NULL,
    report_rules_fingerprint TEXT NOT NULL,
    effective_at TEXT NOT NULL CHECK (
        length(effective_at) = 27 AND substr(effective_at, 11, 1) = 'T'
        AND substr(effective_at, 20, 1) = '.' AND substr(effective_at, -1) = 'Z'
        AND datetime(effective_at) IS NOT NULL
    ),
    first_recorded_at TEXT NOT NULL CHECK (
        length(first_recorded_at) = 27 AND substr(first_recorded_at, 11, 1) = 'T'
        AND substr(first_recorded_at, 20, 1) = '.' AND substr(first_recorded_at, -1) = 'Z'
        AND datetime(first_recorded_at) IS NOT NULL
    ),
    CHECK (length(as_of) = 10 AND date(as_of) IS NOT NULL AND date(as_of) = as_of)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_evidence_batches_precedence
    ON evidence_observation_batches(company_id, provider, effective_at, batch_id);

CREATE TABLE IF NOT EXISTS evidence_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_key TEXT NOT NULL UNIQUE,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    provider TEXT NOT NULL CHECK (provider = 'mfn'),
    release_source_url TEXT NOT NULL,
    first_observed_at TEXT NOT NULL,
    UNIQUE(company_id, provider, release_source_url)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_evidence_candidates_company
    ON evidence_candidates(company_id, provider, candidate_key);

CREATE TABLE IF NOT EXISTS evidence_artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    artifact_id TEXT NOT NULL UNIQUE,
    sha256 TEXT NOT NULL UNIQUE,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    content_type TEXT NOT NULL,
    first_observed_at TEXT NOT NULL,
    CHECK (artifact_id = 'sha256:' || sha256),
    CHECK (length(sha256) = 64)
) STRICT;

CREATE TABLE IF NOT EXISTS evidence_artifact_objects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    object_record_id TEXT NOT NULL UNIQUE,
    artifact_id INTEGER NOT NULL REFERENCES evidence_artifacts(id),
    object_uri TEXT NOT NULL UNIQUE,
    storage_kind TEXT NOT NULL CHECK (storage_kind IN ('local_cas','external_cas')),
    verified_sha256 TEXT NOT NULL,
    verified_size INTEGER NOT NULL CHECK (verified_size >= 0),
    stored_at TEXT NOT NULL,
    acquisition_max_pdf_bytes INTEGER,
    CHECK (verified_sha256 <> ''),
    CHECK (
        acquisition_max_pdf_bytes IS NULL
        OR acquisition_max_pdf_bytes >= verified_size
    )
) STRICT;
CREATE INDEX IF NOT EXISTS idx_evidence_artifact_objects_artifact
    ON evidence_artifact_objects(artifact_id, object_record_id);

CREATE TABLE IF NOT EXISTS evidence_attachment_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attachment_observation_id TEXT NOT NULL UNIQUE,
    candidate_id INTEGER NOT NULL REFERENCES evidence_candidates(id),
    artifact_id INTEGER NOT NULL REFERENCES evidence_artifacts(id),
    batch_id INTEGER NOT NULL REFERENCES evidence_observation_batches(id),
    attachment_source_url TEXT NOT NULL,
    content_type TEXT NOT NULL,
    http_status INTEGER,
    feed_attachment_attested INTEGER NOT NULL CHECK (feed_attachment_attested IN (0,1)),
    raw_metadata TEXT CHECK (raw_metadata IS NULL OR json_valid(raw_metadata))
) STRICT;
CREATE INDEX IF NOT EXISTS idx_evidence_attachments_candidate_batch
    ON evidence_attachment_observations(candidate_id, batch_id, attachment_observation_id);
CREATE INDEX IF NOT EXISTS idx_evidence_attachments_artifact
    ON evidence_attachment_observations(artifact_id);

CREATE TABLE IF NOT EXISTS evidence_artifact_extractions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    extraction_id TEXT NOT NULL UNIQUE,
    artifact_id INTEGER NOT NULL REFERENCES evidence_artifacts(id),
    extractor TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    text_checksum TEXT NOT NULL,
    page_count INTEGER NOT NULL CHECK (page_count >= 0),
    pages_included TEXT,
    page_truncated INTEGER NOT NULL CHECK (page_truncated IN (0,1)),
    scanned INTEGER NOT NULL CHECK (scanned IN (0,1)),
    limitations TEXT NOT NULL CHECK (json_valid(limitations)),
    extracted_at TEXT NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS idx_evidence_extractions_artifact
    ON evidence_artifact_extractions(artifact_id, extraction_id);

CREATE TABLE IF NOT EXISTS evidence_artifact_pages (
    extraction_id INTEGER NOT NULL REFERENCES evidence_artifact_extractions(id),
    page_number INTEGER NOT NULL CHECK (page_number > 0),
    anchor TEXT NOT NULL,
    text TEXT NOT NULL,
    text_checksum TEXT NOT NULL,
    PRIMARY KEY(extraction_id, page_number)
) WITHOUT ROWID, STRICT;

CREATE TABLE IF NOT EXISTS evidence_candidate_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_observation_id TEXT NOT NULL UNIQUE,
    candidate_id INTEGER NOT NULL REFERENCES evidence_candidates(id),
    batch_id INTEGER NOT NULL REFERENCES evidence_observation_batches(id),
    authoritative_feed_title TEXT,
    detail_title TEXT,
    published_at TEXT,
    language TEXT,
    report_kind TEXT,
    document_type TEXT,
    fiscal_period TEXT,
    period_start TEXT,
    period_end TEXT,
    feed_report_identity TEXT,
    invitation_veto INTEGER NOT NULL CHECK (invitation_veto IN (0,1)),
    eligibility TEXT NOT NULL CHECK (eligibility IN ('eligible','revoked','rejected','incomplete')),
    eligibility_reason TEXT NOT NULL,
    attachment_observation_id INTEGER REFERENCES evidence_attachment_observations(id),
    extraction_id INTEGER REFERENCES evidence_artifact_extractions(id),
    report_rules_fingerprint TEXT NOT NULL,
    raw_metadata TEXT NOT NULL CHECK (json_valid(raw_metadata)),
    UNIQUE(candidate_id, batch_id)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_evidence_candidate_observations_current
    ON evidence_candidate_observations(candidate_id, batch_id, candidate_observation_id);

CREATE TABLE IF NOT EXISTS evidence_candidate_relation_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    relation_observation_id TEXT NOT NULL UNIQUE,
    relation_key TEXT NOT NULL,
    batch_id INTEGER NOT NULL REFERENCES evidence_observation_batches(id),
    left_candidate_observation_id INTEGER NOT NULL REFERENCES evidence_candidate_observations(id),
    right_candidate_observation_id INTEGER NOT NULL REFERENCES evidence_candidate_observations(id),
    relation_type TEXT NOT NULL CHECK (relation_type IN ('TRANSLATION','REVISION')),
    disposition TEXT NOT NULL CHECK (disposition IN ('asserted','withdrawn')),
    corroboration TEXT NOT NULL CHECK (json_valid(corroboration)),
    rules_fingerprint TEXT NOT NULL,
    CHECK (left_candidate_observation_id <> right_candidate_observation_id),
    UNIQUE(relation_key, batch_id)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_evidence_relations_current
    ON evidence_candidate_relation_observations(relation_key, batch_id, relation_observation_id);

-- DB-level protection catches accidental mutation regardless of caller.
CREATE TRIGGER IF NOT EXISTS evidence_observation_batches_no_update BEFORE UPDATE ON evidence_observation_batches BEGIN SELECT RAISE(ABORT, 'evidence_observation_batches is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_observation_batches_no_delete BEFORE DELETE ON evidence_observation_batches BEGIN SELECT RAISE(ABORT, 'evidence_observation_batches is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_candidates_no_update BEFORE UPDATE ON evidence_candidates BEGIN SELECT RAISE(ABORT, 'evidence_candidates is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_candidates_no_delete BEFORE DELETE ON evidence_candidates BEGIN SELECT RAISE(ABORT, 'evidence_candidates is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_artifacts_no_update BEFORE UPDATE ON evidence_artifacts BEGIN SELECT RAISE(ABORT, 'evidence_artifacts is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_artifacts_no_delete BEFORE DELETE ON evidence_artifacts BEGIN SELECT RAISE(ABORT, 'evidence_artifacts is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_artifact_objects_no_update
BEFORE UPDATE ON evidence_artifact_objects
WHEN NOT (
    OLD.acquisition_max_pdf_bytes IS NULL
    AND NEW.acquisition_max_pdf_bytes IS NOT NULL
    AND NEW.acquisition_max_pdf_bytes >= NEW.verified_size
    AND NEW.id IS OLD.id
    AND NEW.object_record_id IS OLD.object_record_id
    AND NEW.artifact_id IS OLD.artifact_id
    AND NEW.object_uri IS OLD.object_uri
    AND NEW.storage_kind IS OLD.storage_kind
    AND NEW.verified_sha256 IS OLD.verified_sha256
    AND NEW.verified_size IS OLD.verified_size
    AND NEW.stored_at IS OLD.stored_at
)
BEGIN
    SELECT RAISE(ABORT, 'evidence_artifact_objects is append-only');
END;
CREATE TRIGGER IF NOT EXISTS evidence_artifact_objects_no_delete BEFORE DELETE ON evidence_artifact_objects BEGIN SELECT RAISE(ABORT, 'evidence_artifact_objects is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_attachment_observations_no_update BEFORE UPDATE ON evidence_attachment_observations BEGIN SELECT RAISE(ABORT, 'evidence_attachment_observations is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_attachment_observations_no_delete BEFORE DELETE ON evidence_attachment_observations BEGIN SELECT RAISE(ABORT, 'evidence_attachment_observations is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_artifact_extractions_no_update BEFORE UPDATE ON evidence_artifact_extractions BEGIN SELECT RAISE(ABORT, 'evidence_artifact_extractions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_artifact_extractions_no_delete BEFORE DELETE ON evidence_artifact_extractions BEGIN SELECT RAISE(ABORT, 'evidence_artifact_extractions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_artifact_pages_no_update BEFORE UPDATE ON evidence_artifact_pages BEGIN SELECT RAISE(ABORT, 'evidence_artifact_pages is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_artifact_pages_no_delete BEFORE DELETE ON evidence_artifact_pages BEGIN SELECT RAISE(ABORT, 'evidence_artifact_pages is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_candidate_observations_no_update BEFORE UPDATE ON evidence_candidate_observations BEGIN SELECT RAISE(ABORT, 'evidence_candidate_observations is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_candidate_observations_no_delete BEFORE DELETE ON evidence_candidate_observations BEGIN SELECT RAISE(ABORT, 'evidence_candidate_observations is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_candidate_relation_observations_no_update BEFORE UPDATE ON evidence_candidate_relation_observations BEGIN SELECT RAISE(ABORT, 'evidence_candidate_relation_observations is append-only'); END;
CREATE TRIGGER IF NOT EXISTS evidence_candidate_relation_observations_no_delete BEFORE DELETE ON evidence_candidate_relation_observations BEGIN SELECT RAISE(ABORT, 'evidence_candidate_relation_observations is append-only'); END;
