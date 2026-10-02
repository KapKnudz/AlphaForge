"""Audit event identity is independent of SQLite's millisecond clock."""

from pathlib import Path

from alphaforge.config import SCHEMA_VERSION, Settings
from alphaforge.db.connection import get_connection
from alphaforge.db.migrations import migrate, set_user_version
from alphaforge.db.repositories import record_mfn_feed_check, upsert_company


def test_v11_migration_preserves_every_audit_value_and_row_identity():
    conn = get_connection(Settings.from_env(dsn="sqlite:///:memory:"))
    schema = Path("db/alphaforge.sqlite.sql").read_text()
    start = schema.index("CREATE TABLE IF NOT EXISTS mfn_feed_checks (")
    end = schema.index("CREATE TABLE IF NOT EXISTS news_releases (", start)
    old_table = """
    CREATE TABLE mfn_feed_checks (
        company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
        mfn_slug TEXT NOT NULL,
        checked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
        discovered_count INTEGER NOT NULL,
        unseen_count INTEGER NOT NULL,
        PRIMARY KEY (company_id, checked_at)
    ) STRICT;
    """
    conn.executescript(schema[:start] + old_table + schema[end:])
    first = upsert_company(conn, {"insId": 1, "name": "First AB"})
    second = upsert_company(conn, {"insId": 2, "name": "Second AB"})
    old_rows = [
        (7, first, "all/a/first", "2026-09-30T13:30:00.000Z", 3, 2),
        (12, second, "all/a/second", "2026-09-30T13:30:00.000Z", 0, 0),
        (20, first, "all/a/first", "2026-09-29T13:30:00.000Z", 5, 1),
    ]
    conn.executemany(
        "INSERT INTO mfn_feed_checks (rowid, company_id, mfn_slug, checked_at, discovered_count, unseen_count) VALUES (?, ?, ?, ?, ?, ?)",
        old_rows,
    )
    set_user_version(conn, 11)
    conn.commit()

    migrate(conn)
    assert [
        tuple(row) for row in conn.execute("SELECT * FROM mfn_feed_checks ORDER BY id")
    ] == old_rows
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.create_function("strftime", -1, lambda *args: "2026-09-30T13:30:00.000Z")
    record_mfn_feed_check(conn, first, "all/a/first", 4, 0)
    record_mfn_feed_check(conn, first, "all/a/first", 9, 1)
    conn.commit()
    rows = conn.execute(
        "SELECT * FROM mfn_feed_checks WHERE company_id=? ORDER BY checked_at DESC, id DESC",
        (first,),
    ).fetchall()
    assert [row["discovered_count"] for row in rows] == [9, 4, 3, 5]
    assert [row["checked_at"] for row in rows[:3]] == [old_rows[0][3]] * 3
    assert len({row["id"] for row in rows}) == 4
    assert rows[0]["id"] > rows[1]["id"] > 20
    index_columns = [
        row[2] for row in conn.execute("PRAGMA index_info(idx_mfn_feed_checks_chronological)")
    ]
    assert index_columns == ["company_id", "checked_at", "id"]
    before_replay = [
        tuple(row) for row in conn.execute("SELECT * FROM mfn_feed_checks ORDER BY id")
    ]
    migrate(conn)
    assert [
        tuple(row) for row in conn.execute("SELECT * FROM mfn_feed_checks ORDER BY id")
    ] == before_replay
    conn.execute("DELETE FROM companies WHERE id=?", (second,))
    assert (
        conn.execute(
            "SELECT count(*) FROM mfn_feed_checks WHERE company_id=?", (second,)
        ).fetchone()[0]
        == 0
    )
    conn.close()
