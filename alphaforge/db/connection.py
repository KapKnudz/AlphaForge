"""SQLite connection with WAL pragmas — the only place sqlite3 is imported."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alphaforge.config import Settings


def get_connection(
    settings: Settings | None = None, *, path: Path | str | None = None
) -> sqlite3.Connection:
    if path is not None:
        db_path = Path(path)
    elif settings is not None:
        db_path = settings.sqlite_path
        if db_path == Path(":memory:"):
            conn = sqlite3.connect(":memory:", check_same_thread=False)
            _apply_pragmas(conn)
            return conn
    else:
        from alphaforge.config import DEFAULT_DSN

        raw = DEFAULT_DSN.removeprefix("sqlite://")
        db_path = Path(raw)

    if db_path != Path(":memory:"):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
    else:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
    _apply_pragmas(conn)
    return conn


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")
    try:
        conn.execute("PRAGMA cache_size=-20000;")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("PRAGMA temp_store=MEMORY;")
    except sqlite3.OperationalError:
        pass
    conn.row_factory = sqlite3.Row


def init_db(conn: sqlite3.Connection) -> None:
    from alphaforge.db.migrations import migrate

    migrate(conn)
