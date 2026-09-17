"""Migration shim — PRAGMA user_version.

Choice: PRAGMA user_version (single integer in SQLite header) over a runner
(alembic/dbmate) because the MVP has one schema version, zero history,
and no concurrent migration writers. The runner would add a dependency
and a migrations directory for no incremental benefit. If a second
concurrent writer or a multi-version history appears (plan §3.4 promotion
signal), switch to alembic with autogenerate and keep this module as
the SQLite→Postgres translation entry point.

Current version: SCHEMA_VERSION = 1 (db/alphaforge.sqlite.sql).
Bumping the version means: add db/migrations/NNN.sql and extend
_run_migration() to apply it when user_version < NNN.
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
        return
    if current == 0:
        _apply_initial_schema(conn)
        set_user_version(conn, SCHEMA_VERSION)
        conn.commit()
        return
    # Future migrations: if current < N: apply N and bump.
    # Example:
    # if current < 2:
    #     conn.executescript(Path("db/migrations/002_add_foo.sql").read_text())
    #     set_user_version(conn, 2)
    #     conn.commit()
    # For now, only version 1 exists — any 0 < current < 1 is impossible,
    # but we handle it by re-applying idempotent DDL and bumping.
    _apply_initial_schema(conn)
    set_user_version(conn, SCHEMA_VERSION)
    conn.commit()


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
