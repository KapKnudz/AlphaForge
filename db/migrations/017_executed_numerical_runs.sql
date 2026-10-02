-- Executed-run retention is additive: legacy ranking_runs have no link.
CREATE TABLE IF NOT EXISTS numerical_input_bodies (
    numerical_identity TEXT PRIMARY KEY,
    financial_inputs_hash TEXT NOT NULL,
    body TEXT NOT NULL CHECK (json_valid(body)),
    rules TEXT NOT NULL CHECK (json_valid(rules))
) STRICT;
CREATE TABLE IF NOT EXISTS executed_numerical_runs (
    run_id INTEGER PRIMARY KEY REFERENCES ranking_runs(id),
    numerical_identity TEXT NOT NULL REFERENCES numerical_input_bodies(numerical_identity),
    textual_context TEXT NOT NULL CHECK (json_valid(textual_context)),
    textual_context_hash TEXT NOT NULL,
    outputs TEXT NOT NULL CHECK (json_valid(outputs)),
    outputs_hash TEXT NOT NULL
) STRICT;
CREATE TRIGGER IF NOT EXISTS numerical_body_no_replace BEFORE INSERT ON numerical_input_bodies
WHEN EXISTS (SELECT 1 FROM numerical_input_bodies WHERE numerical_identity=NEW.numerical_identity)
BEGIN SELECT RAISE(ABORT, 'immutable numerical body'); END;
CREATE TRIGGER IF NOT EXISTS numerical_run_no_replace BEFORE INSERT ON executed_numerical_runs
WHEN EXISTS (SELECT 1 FROM executed_numerical_runs WHERE run_id=NEW.run_id)
BEGIN SELECT RAISE(ABORT, 'immutable numerical run'); END;
CREATE TRIGGER IF NOT EXISTS numerical_body_no_update BEFORE UPDATE ON numerical_input_bodies
BEGIN SELECT RAISE(ABORT, 'immutable numerical body'); END;
CREATE TRIGGER IF NOT EXISTS numerical_body_no_delete BEFORE DELETE ON numerical_input_bodies
BEGIN SELECT RAISE(ABORT, 'immutable numerical body'); END;
CREATE TRIGGER IF NOT EXISTS numerical_run_no_update BEFORE UPDATE ON executed_numerical_runs
BEGIN SELECT RAISE(ABORT, 'immutable numerical run'); END;
CREATE TRIGGER IF NOT EXISTS numerical_run_no_delete BEFORE DELETE ON executed_numerical_runs
BEGIN SELECT RAISE(ABORT, 'immutable numerical run'); END;
