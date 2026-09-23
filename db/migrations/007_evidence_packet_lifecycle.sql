-- Preserve frozen packet history while making stale/incomplete runs unusable.
ALTER TABLE evidence_packets ADD COLUMN report_rules_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE evidence_packets ADD COLUMN report_rules_fingerprint TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE evidence_packets ADD COLUMN usable INTEGER NOT NULL DEFAULT 1 CHECK (usable IN (0,1));
ALTER TABLE evidence_packets ADD COLUMN usable_reason TEXT;

UPDATE evidence_packets
SET report_rules_version = COALESCE(json_extract(packet_json, '$.report_rules.version'), 0),
    report_rules_fingerprint = COALESCE(json_extract(packet_json, '$.report_rules.fingerprint'), 'legacy')
WHERE report_rules_fingerprint = 'legacy';

CREATE INDEX IF NOT EXISTS idx_evidence_packets_usable
    ON evidence_packets(company_id, as_of, usable, report_rules_fingerprint, id DESC);
