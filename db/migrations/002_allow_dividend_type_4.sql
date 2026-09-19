PRAGMA foreign_keys=OFF;

CREATE TABLE dividends_new (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    ex_date             TEXT NOT NULL,
    amount              REAL NOT NULL CHECK (amount >= 0),
    currency            TEXT NOT NULL,
    dividend_type       INTEGER NOT NULL CHECK (dividend_type IN (0,1,2,4)),
    distribution_frequency TEXT,
    fetched_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (company_id, ex_date, dividend_type, amount)
) STRICT;

INSERT INTO dividends_new (
    id, company_id, ex_date, amount, currency, dividend_type,
    distribution_frequency, fetched_at
)
SELECT
    id, company_id, ex_date, amount, currency, dividend_type,
    distribution_frequency, fetched_at
FROM dividends;

DROP TABLE dividends;
ALTER TABLE dividends_new RENAME TO dividends;

PRAGMA foreign_keys=ON;
