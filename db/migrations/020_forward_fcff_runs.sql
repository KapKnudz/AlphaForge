-- Additive supplied-schedule research lane; no ranking FK or issuer admission.
CREATE TABLE IF NOT EXISTS forward_fcff_runs (
    id INTEGER PRIMARY KEY,
    numerical_identity TEXT NOT NULL REFERENCES numerical_input_bodies(numerical_identity),
    textual_context TEXT NOT NULL CHECK(json_valid(textual_context)),
    textual_context_hash TEXT NOT NULL,
    outputs TEXT NOT NULL CHECK(json_valid(outputs)),
    outputs_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
) STRICT;
CREATE TRIGGER IF NOT EXISTS forward_fcff_no_replace BEFORE INSERT ON forward_fcff_runs
WHEN EXISTS (SELECT 1 FROM forward_fcff_runs WHERE id=NEW.id)
BEGIN SELECT RAISE(ABORT, 'immutable forward FCFF run'); END;
CREATE TRIGGER IF NOT EXISTS forward_fcff_no_update BEFORE UPDATE ON forward_fcff_runs
BEGIN SELECT RAISE(ABORT, 'immutable forward FCFF run'); END;
CREATE TRIGGER IF NOT EXISTS forward_fcff_no_delete BEFORE DELETE ON forward_fcff_runs
BEGIN SELECT RAISE(ABORT, 'immutable forward FCFF run'); END;
