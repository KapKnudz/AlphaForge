"""Migration shim — PRAGMA user_version.

Choice: PRAGMA user_version (single integer in SQLite header) over a runner
(alembic/dbmate) because the MVP has a small linear schema history and no
concurrent migration writers. A full runner would add a dependency without
incremental benefit at this scale. If concurrent writers or a larger or
branched migration history appears (plan §3.4 promotion signal), switch to
alembic with autogenerate and keep this module as the SQLite→Postgres
translation entry point.

Current version: SCHEMA_VERSION = 3 (db/alphaforge.sqlite.sql).
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
    if current < SCHEMA_VERSION:
        _apply_initial_schema(conn)
        set_user_version(conn, SCHEMA_VERSION)
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
