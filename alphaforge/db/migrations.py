"""Migration shim — PRAGMA user_version.

Choice: PRAGMA user_version (single integer in SQLite header) over a runner
(alembic/dbmate) because the MVP has a small linear schema history and no
concurrent migration writers. A full runner would add a dependency without
incremental benefit at this scale. If concurrent writers or a larger or
branched migration history appears (plan §3.4 promotion signal), switch to
alembic with autogenerate and keep this module as the SQLite→Postgres
translation entry point.

Current version: SCHEMA_VERSION = 4 (db/alphaforge.sqlite.sql).
Bumping the version means: add db/migrations/NNN.sql and extend
migrate() to apply it when user_version < NNN.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alphaforge.config import SCHEMA_VERSION


def get_user_version(conn: sqlite3.Connection) -> int:
    cur = conn.execute("PRAGMA user_version;")
    row = cur.fetchone()
    return int(row[0]) if row else 0


def set_user_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(f"PRAGMA user_version={int(version)};")


def migrate(conn: sqlite3.Connection) -> None:
    current = get_user_version(conn)
    if current >= SCHEMA_VERSION:
        _ensure_schema_extensions(conn)
        conn.commit()
        return
    if current == 0:
        _apply_initial_schema(conn)
        _ensure_schema_extensions(conn)
        set_user_version(conn, SCHEMA_VERSION)
        conn.commit()
        return
    if current < 2:
        candidates = [
            Path("db/migrations/002_allow_dividend_type_4.sql"),
            Path(__file__).resolve().parents[2]
            / "db"
            / "migrations"
            / "002_allow_dividend_type_4.sql",
        ]
        migration_path = next((path for path in candidates if path.exists()), None)
        if migration_path is None:
            raise FileNotFoundError(f"dividend type migration not found (tried {candidates})")
        conn.executescript(migration_path.read_text(encoding="utf-8"))
        set_user_version(conn, 2)
        conn.commit()
        current = 2
    if current < 3:
        candidates = [
            Path("db/migrations/003_allow_bilingual_pdf_checksums.sql"),
            Path(__file__).resolve().parents[2]
            / "db"
            / "migrations"
            / "003_allow_bilingual_pdf_checksums.sql",
        ]
        migration_path = next((path for path in candidates if path.exists()), None)
        if migration_path is None:
            raise FileNotFoundError(f"bilingual checksum migration not found (tried {candidates})")
        conn.executescript(migration_path.read_text(encoding="utf-8"))
        set_user_version(conn, 3)
        conn.commit()
        current = 3
    if current < 4:
        candidates = [
            Path("db/migrations/004_jev_shadow_audit.sql"),
            Path(__file__).resolve().parents[2] / "db" / "migrations" / "004_jev_shadow_audit.sql",
        ]
        migration_path = next((path for path in candidates if path.exists()), None)
        if migration_path is None:
            raise FileNotFoundError(f"Jev shadow audit migration not found (tried {candidates})")
        conn.executescript(migration_path.read_text(encoding="utf-8"))
        set_user_version(conn, 4)
        conn.commit()
        current = 4
    if current < 5:
        # Idempotent add: if the column already exists (e.g. DB created from
        # the updated sqlite.sql which already includes net_debt), skip the ALTER.
        try:
            cols = {
                row[1] for row in conn.execute("PRAGMA table_info(financial_periods);").fetchall()
            }
        except Exception:
            cols = set()
        if "net_debt" not in cols:
            candidates = [
                Path("db/migrations/005_add_net_debt_column.sql"),
                Path(__file__).resolve().parents[2]
                / "db"
                / "migrations"
                / "005_add_net_debt_column.sql",
            ]
            migration_path = next((path for path in candidates if path.exists()), None)
            if migration_path is None:
                raise FileNotFoundError(f"net_debt column migration not found (tried {candidates})")
            conn.executescript(migration_path.read_text(encoding="utf-8"))
        set_user_version(conn, 5)
        conn.commit()
        current = 5
    if current < SCHEMA_VERSION:
        _apply_initial_schema(conn)
        set_user_version(conn, SCHEMA_VERSION)
    _ensure_schema_extensions(conn)
    conn.commit()


def _ensure_schema_extensions(conn: sqlite3.Connection) -> None:
    """Apply additive objects to databases created by earlier v1 builds."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS translation_metadata (
            translation_key TEXT PRIMARY KEY,
            name_sv TEXT,
            name_en TEXT,
            fetched_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        ) STRICT;

        -- Deterministic MFN identity is explicit source-of-truth data.  A
        -- candidate row is deliberately separate from the verified mapping so
        -- an ambiguous discovery can never become an issuer link implicitly.
        CREATE TABLE IF NOT EXISTS mfn_issuer_mappings (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id          INTEGER NOT NULL UNIQUE REFERENCES companies(id) ON DELETE CASCADE,
            mfn_slug            TEXT,
            source_url          TEXT,
            status              TEXT NOT NULL CHECK (status IN ('mapped','ambiguous','unmapped')),
            discovery_source    TEXT NOT NULL,
            verified_at         TEXT,
            identity_evidence   TEXT CHECK (identity_evidence IS NULL OR json_valid(identity_evidence)),
            last_checked_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            CHECK (status <> 'mapped' OR (mfn_slug IS NOT NULL AND source_url IS NOT NULL AND verified_at IS NOT NULL))
        ) STRICT;

        CREATE TABLE IF NOT EXISTS mfn_issuer_candidates (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            mfn_slug            TEXT NOT NULL,
            source_url          TEXT NOT NULL,
            discovery_source    TEXT NOT NULL,
            match_basis         TEXT,
            identity_evidence   TEXT CHECK (identity_evidence IS NULL OR json_valid(identity_evidence)),
            status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','accepted','rejected')),
            discovered_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE (company_id, mfn_slug, source_url)
        ) STRICT;
        CREATE INDEX IF NOT EXISTS idx_mfn_issuer_candidates_review
            ON mfn_issuer_candidates(company_id, status, discovered_at);

        CREATE TABLE IF NOT EXISTS research_attachments (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id         INTEGER NOT NULL REFERENCES research_documents(id) ON DELETE CASCADE,
            source_url          TEXT NOT NULL,
            content_type        TEXT,
            byte_size           INTEGER NOT NULL CHECK (byte_size >= 0),
            sha256              TEXT NOT NULL,
            magic_valid         INTEGER NOT NULL CHECK (magic_valid IN (0,1)),
            http_status         INTEGER,
            fetched_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            raw_metadata        TEXT CHECK (raw_metadata IS NULL OR json_valid(raw_metadata)),
            UNIQUE (document_id, source_url)
        ) STRICT;
        CREATE INDEX IF NOT EXISTS idx_research_attachments_checksum ON research_attachments(sha256);

        CREATE TABLE IF NOT EXISTS document_extractions (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id         INTEGER NOT NULL UNIQUE REFERENCES research_documents(id) ON DELETE CASCADE,
            extractor           TEXT NOT NULL,
            text_checksum       TEXT,
            page_count          INTEGER NOT NULL CHECK (page_count >= 0),
            pages_included      TEXT,
            page_truncated      INTEGER NOT NULL CHECK (page_truncated IN (0,1)),
            scanned             INTEGER NOT NULL CHECK (scanned IN (0,1)),
            limitations         TEXT CHECK (limitations IS NULL OR json_valid(limitations)),
            extracted_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        ) STRICT;

        CREATE TABLE IF NOT EXISTS document_pages (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            extraction_id       INTEGER NOT NULL REFERENCES document_extractions(id) ON DELETE CASCADE,
            page_number         INTEGER NOT NULL CHECK (page_number > 0),
            anchor              TEXT NOT NULL,
            text                TEXT NOT NULL,
            text_checksum       TEXT NOT NULL,
            UNIQUE (extraction_id, page_number)
        ) STRICT;
        CREATE INDEX IF NOT EXISTS idx_document_pages_anchor ON document_pages(anchor);

        CREATE TABLE IF NOT EXISTS evidence_packets (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id          INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            as_of               TEXT NOT NULL,
            packet_hash         TEXT NOT NULL,
            packet_json         TEXT NOT NULL CHECK (json_valid(packet_json)),
            frozen_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            UNIQUE (company_id, as_of, packet_hash)
        ) STRICT;
        CREATE INDEX IF NOT EXISTS idx_evidence_packets_current ON evidence_packets(company_id, as_of, id DESC);
        """
    )


def _apply_initial_schema(conn: sqlite3.Connection) -> None:
    # Locate db/alphaforge.sqlite.sql relative to repo root
    candidates = [
        Path("db/alphaforge.sqlite.sql"),
        Path(__file__).resolve().parents[2] / "db" / "alphaforge.sqlite.sql",
    ]
    sql_path: Path | None = None
    for p in candidates:
        if p.exists():
            sql_path = p
            break
    if sql_path is None:
        raise FileNotFoundError(f"db/alphaforge.sqlite.sql not found (tried {candidates})")
    sql = sql_path.read_text(encoding="utf-8")
    # Strip the trailing PRAGMAS python list if present in DDL file
    if "PRAGMAS = [" in sql:
        sql = sql.split("PRAGMAS = [")[0]
    conn.executescript(sql)
