"""Worktree-safe repository `.env` resolution for opt-in integration tests.

Tests usually run in the primary checkout, but a linked worktree has its own
root while the live key lives in the primary checkout's untracked `.env`.
`git rev-parse --git-common-dir` points at the primary checkout's `.git` in
both cases, so its parent is the directory holding the shared `.env`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

KEY = "BORSDATA_API_KEY"
FILENAME = ".env"


def git_common_dir(cwd: Path) -> Path | None:
    """Return the absolute git common dir for `cwd`, or None outside git."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    out = Path(proc.stdout.strip())
    return out if out.is_absolute() else cwd / out


def find_dotenv(cwd: Path | None = None) -> Path | None:
    """Return the repository-root `.env` path, or None when absent/unresolvable."""
    base = Path(cwd) if cwd is not None else Path.cwd()
    common = git_common_dir(base)
    if common is None:
        return None
    candidate = common.parent / FILENAME
    return candidate if candidate.is_file() else None
