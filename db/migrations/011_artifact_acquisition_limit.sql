DROP TRIGGER evidence_artifact_objects_no_update;
CREATE TRIGGER evidence_artifact_objects_no_update
BEFORE UPDATE ON evidence_artifact_objects
WHEN NOT (
    OLD.acquisition_max_pdf_bytes IS NULL
    AND NEW.acquisition_max_pdf_bytes IS NOT NULL
    AND NEW.acquisition_max_pdf_bytes >= NEW.verified_size
    AND NEW.id IS OLD.id
    AND NEW.object_record_id IS OLD.object_record_id
    AND NEW.artifact_id IS OLD.artifact_id
    AND NEW.object_uri IS OLD.object_uri
    AND NEW.storage_kind IS OLD.storage_kind
    AND NEW.verified_sha256 IS OLD.verified_sha256
    AND NEW.verified_size IS OLD.verified_size
    AND NEW.stored_at IS OLD.stored_at
)
BEGIN
    SELECT RAISE(ABORT, 'evidence_artifact_objects is append-only');
END;
