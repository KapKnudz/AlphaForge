-- Analyst-reviewed, append-only numerical disclosures; no fabricated backfill.
CREATE TABLE IF NOT EXISTS reinvestment_calibrations (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    identity TEXT NOT NULL CHECK(length(identity)=64),
    record_json TEXT NOT NULL CHECK(json_valid(record_json)),
    UNIQUE(company_id, identity)
) STRICT;
CREATE TRIGGER IF NOT EXISTS reinvestment_calibrations_no_replace
BEFORE INSERT ON reinvestment_calibrations
WHEN EXISTS (SELECT 1 FROM reinvestment_calibrations WHERE id=NEW.id OR (company_id=NEW.company_id AND identity=NEW.identity))
BEGIN SELECT RAISE(ABORT, 'reinvestment calibration is immutable'); END;
CREATE TRIGGER IF NOT EXISTS reinvestment_calibrations_no_update
BEFORE UPDATE ON reinvestment_calibrations BEGIN
    SELECT RAISE(ABORT, 'reinvestment calibration is immutable');
END;
CREATE TRIGGER IF NOT EXISTS reinvestment_calibrations_no_delete
BEFORE DELETE ON reinvestment_calibrations BEGIN
    SELECT RAISE(ABORT, 'reinvestment calibration is immutable');
END;
