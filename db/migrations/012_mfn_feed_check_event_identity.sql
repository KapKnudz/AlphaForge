-- Wall-clock milliseconds are observation metadata, not audit event identity.
-- Preserve every old value and use the old rowid as the persisted event id.
BEGIN;
CREATE TABLE mfn_feed_checks_v12 (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    mfn_slug            TEXT NOT NULL,
    checked_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    discovered_count    INTEGER NOT NULL,
    unseen_count        INTEGER NOT NULL
) STRICT;
INSERT INTO mfn_feed_checks_v12
    (id, company_id, mfn_slug, checked_at, discovered_count, unseen_count)
SELECT rowid, company_id, mfn_slug, checked_at, discovered_count, unseen_count
FROM mfn_feed_checks;
DROP TABLE mfn_feed_checks;
ALTER TABLE mfn_feed_checks_v12 RENAME TO mfn_feed_checks;
CREATE INDEX idx_mfn_feed_checks_chronological
    ON mfn_feed_checks(company_id, checked_at DESC, id DESC);
COMMIT;
