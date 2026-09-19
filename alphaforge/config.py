"""Typed Settings — ALPHAFORGE_DSN, watchlist path, as_of.

Migration shim: PRAGMA user_version is used for SQLite schema versioning.
Postgres path uses the same user_version table (schema_migrations) when DSN is postgres.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Keep the default relative to the repository/worktree.  ``sqlite:///data`` is
# interpreted as an absolute ``/data`` path by sqlite URL parsers.
DEFAULT_DSN = "sqlite:///data/alphaforge.db"
SCHEMA_VERSION = 3


def _default_dsn() -> str:
    return os.environ.get("ALPHAFORGE_DSN", DEFAULT_DSN)


@dataclass(frozen=True)
class Settings:
    dsn: str
    watchlist_path: Path | None
    as_of: str | None

    @classmethod
    def from_env(
        cls,
        *,
        dsn: str | None = None,
        watchlist_path: str | Path | None = None,
        as_of: str | None = None,
    ) -> Settings:
        resolved_dsn = dsn or _default_dsn()
        wp = Path(watchlist_path) if watchlist_path is not None else None
        return cls(dsn=resolved_dsn, watchlist_path=wp, as_of=as_of)

    @property
    def is_sqlite(self) -> bool:
        return self.dsn.startswith("sqlite:")

    @property
    def sqlite_path(self) -> Path:
        if not self.is_sqlite:
            raise ValueError(f"DSN is not sqlite: {self.dsn}")
        raw = self.dsn.removeprefix("sqlite://")
        # Keep the deliberately simple DSN contract explicit:
        #   sqlite:///data/x.db   -> repository-relative data/x.db
        #   sqlite://./data/x.db  -> repository-relative data/x.db
        #   sqlite:////tmp/x.db   -> absolute /tmp/x.db
        # A URL parser that treats every leading slash as absolute turns the
        # documented default into /data, which is unwritable on many hosts.
        if raw in {"/:memory:", ":memory:"}:
            return Path(":memory:")
        if raw.startswith("//"):
            return Path(raw[1:])
        return Path(raw.removeprefix("./").removeprefix("/"))

    def sqlite_path_for_test(self) -> Path:
        p = self.sqlite_path
        if p == Path(":memory:"):
            return p
        # tests use data/alphaforge.test.db
        return p.parent / "alphaforge.test.db"
