-- Postgres variant — delta from db/alphaforge.sqlite.sql
-- Apply after running the SQLite DDL through a Postgres-compatible translation.
-- Differences: types, defaults, identity, JSONB, pragmas removed.
--
-- Migration path (one-line DSN switch):
--   ALPHAFORGE_DSN=postgresql://user:pass@host/db  python -m alphaforge.db.migrate
--
-- This file contains ONLY the Postgres-specific DDL statements.
-- The base schema is in db/alphaforge.sqlite.sql; this file replays the
-- Postgres adjustments on top of it (idempotent via IF NOT EXISTS where
-- applicable). For a fresh Postgres database, run the SQLite DDL through
-- the translation below and then this delta.

-- Type mappings for Postgres (applied by migrate.py translation):
--   INTEGER PRIMARY KEY AUTOINCREMENT -> BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY
--   TEXT (json_valid checks)          -> JSONB where json_valid() was used
--   STRICT, WITHOUT ROWID             -> removed
--   strftime('%Y-%m-%dT%H:%M:%fZ','now') -> now() AT TIME ZONE 'UTC'
--   PRAGMA journal_mode / synchronous / foreign_keys / busy_timeout -> removed (Postgres MVCC)
--
-- The statements below are the Postgres-only objects that have no SQLite equivalent
-- or that require Postgres-specific syntax. They are safe to run even if the base
-- translation was applied.

-- Ensure pgcrypto for gen_random_uuid if needed by future extensions
-- CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Convert TEXT json columns to JSONB where applicable (idempotent)
-- These ALTERs assume the base tables already exist via translated DDL.
-- If running fresh, the translator already creates JSONB columns; these are no-ops guarded.

DO $$
BEGIN
    -- companies.raw_payload: TEXT -> JSONB if still TEXT
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='companies' AND column_name='raw_payload' AND data_type='text'
    ) THEN
        ALTER TABLE companies ALTER COLUMN raw_payload TYPE JSONB USING raw_payload::jsonb;
    END IF;
    -- financial_periods.raw_payload
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='financial_periods' AND column_name='raw_payload' AND data_type='text'
    ) THEN
        ALTER TABLE financial_periods ALTER COLUMN raw_payload TYPE JSONB USING raw_payload::jsonb;
    END IF;
    -- research_documents.raw_metadata
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='research_documents' AND column_name='raw_metadata' AND data_type='text'
    ) THEN
        ALTER TABLE research_documents ALTER COLUMN raw_metadata TYPE JSONB USING raw_metadata::jsonb;
    END IF;
    -- ranking_runs.scores / inputs_summary
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='ranking_runs' AND column_name='scores' AND data_type='text'
    ) THEN
        ALTER TABLE ranking_runs ALTER COLUMN scores TYPE JSONB USING scores::jsonb;
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='ranking_runs' AND column_name='inputs_summary' AND data_type='text'
    ) THEN
        ALTER TABLE ranking_runs ALTER COLUMN inputs_summary TYPE JSONB USING inputs_summary::jsonb;
    END IF;
    -- theses.thesis_json / change_log
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='theses' AND column_name='thesis_json' AND data_type='text'
    ) THEN
        ALTER TABLE theses ALTER COLUMN thesis_json TYPE JSONB USING thesis_json::jsonb;
    END IF;
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='theses' AND column_name='change_log' AND data_type='text'
    ) THEN
        ALTER TABLE theses ALTER COLUMN change_log TYPE JSONB USING change_log::jsonb;
    END IF;
    -- jobs.error
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='jobs' AND column_name='error' AND data_type='text'
    ) THEN
        ALTER TABLE jobs ALTER COLUMN error TYPE JSONB USING error::jsonb;
    END IF;
    -- ownership_events_staging.source_ids
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name='ownership_events_staging' AND column_name='source_ids' AND data_type='text'
    ) THEN
        ALTER TABLE ownership_events_staging ALTER COLUMN source_ids TYPE JSONB USING source_ids::jsonb;
    END IF;
END $$;

-- Postgres-specific indexes for JSONB (GIN) — optional, improves thesis_json queries
CREATE INDEX IF NOT EXISTS idx_theses_thesis_json_gin ON theses USING GIN (thesis_json);
CREATE INDEX IF NOT EXISTS idx_ranking_runs_scores_gin ON ranking_runs USING GIN (scores);

-- Postgres advisory: no WAL pragmas; MVCC handles concurrency.
-- Keep foreign_keys via standard REFERENCES (already enforced).
