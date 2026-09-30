-- Additive assurance independent of distribution rows. Legacy min/max stays unknown.
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
