-- Per-run audit mapping, separate from canonical numerical input/output identity.
-- Existing executed runs are not backfilled with guessed source IDs.
CREATE TABLE IF NOT EXISTS numerical_run_source_rows (
    run_id INTEGER PRIMARY KEY REFERENCES executed_numerical_runs(run_id),
    source_rows TEXT NOT NULL CHECK (json_valid(source_rows)),
    source_rows_hash TEXT NOT NULL
) STRICT;
CREATE TRIGGER IF NOT EXISTS numerical_source_rows_no_replace BEFORE INSERT ON numerical_run_source_rows
WHEN EXISTS (SELECT 1 FROM numerical_run_source_rows WHERE run_id=NEW.run_id)
BEGIN SELECT RAISE(ABORT, 'immutable numerical source rows'); END;
CREATE TRIGGER IF NOT EXISTS numerical_source_rows_no_update BEFORE UPDATE ON numerical_run_source_rows
BEGIN SELECT RAISE(ABORT, 'immutable numerical source rows'); END;
CREATE TRIGGER IF NOT EXISTS numerical_source_rows_no_delete BEFORE DELETE ON numerical_run_source_rows
BEGIN SELECT RAISE(ABORT, 'immutable numerical source rows'); END;
