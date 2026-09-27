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
    CHECK (verified_sha256 <> '')
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
CREATE TRIGGER IF NOT EXISTS evidence_artifact_objects_no_update BEFORE UPDATE ON evidence_artifact_objects BEGIN SELECT RAISE(ABORT, 'evidence_artifact_objects is append-only'); END;
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
