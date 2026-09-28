ALTER TABLE evidence_artifact_objects
ADD COLUMN acquisition_max_pdf_bytes INTEGER
CHECK (
    acquisition_max_pdf_bytes IS NULL
    OR acquisition_max_pdf_bytes >= verified_size
);
