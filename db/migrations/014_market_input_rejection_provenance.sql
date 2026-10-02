CREATE TABLE IF NOT EXISTS market_input_rejections (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    input_type          TEXT NOT NULL CHECK (input_type IN ('price','kpi')),
    reason              TEXT NOT NULL,
    kpi_id              INTEGER,
    period_type         TEXT,
    price_type          TEXT,
    payload_hash        TEXT NOT NULL,
    raw_payload         TEXT NOT NULL CHECK (json_valid(raw_payload)),
    rejected_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (company_id, payload_hash, reason)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_market_input_rejections_company
    ON market_input_rejections(company_id, input_type, rejected_at DESC);
