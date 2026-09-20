PRAGMA foreign_keys=OFF;

CREATE TABLE research_documents_v3 (
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
    duplicate_of        INTEGER REFERENCES research_documents_v3(id),
    ingested_lang       TEXT,
    checksum            TEXT,
    raw_metadata        TEXT CHECK (raw_metadata IS NULL OR json_valid(raw_metadata)),
    UNIQUE (company_id, source_url)
) STRICT;

INSERT INTO research_documents_v3 (
    id, company_id, source_url, source_type, title, published_at, fetched_at,
    content_text, page_count, pages_included, page_truncated, duplicate_of,
    ingested_lang, checksum, raw_metadata
)
SELECT
    id, company_id, source_url, source_type, title, published_at, fetched_at,
    content_text, page_count, pages_included, page_truncated, duplicate_of,
    ingested_lang, checksum, raw_metadata
FROM research_documents;

DROP TABLE research_documents;
ALTER TABLE research_documents_v3 RENAME TO research_documents;
CREATE INDEX idx_research_documents_checksum ON research_documents(checksum);

PRAGMA foreign_keys=ON;
