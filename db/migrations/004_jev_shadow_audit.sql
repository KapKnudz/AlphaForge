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
