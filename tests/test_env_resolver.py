"""Fixture tests for the worktree-safe `.env` resolver (no network, no key)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from env_resolver import find_dotenv


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init")
    return tmp_path


def test_find_dotenv_in_plain_repo(repo: Path) -> None:
    (repo / ".env").write_text("SOME_VAR=value\n")
    assert find_dotenv(repo) == repo / ".env"


def test_find_dotenv_missing_file_returns_none(repo: Path) -> None:
    assert find_dotenv(repo) is None


def test_find_dotenv_outside_git_returns_none(tmp_path: Path) -> None:
    assert find_dotenv(tmp_path) is None


def test_find_dotenv_from_linked_worktree_resolves_primary_env(tmp_path: Path) -> None:
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init")
    _git(main, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-m", "init")
    (main / ".env").write_text("SOME_VAR=value\n")
    work = tmp_path / "work"
    _git(main, "worktree", "add", str(work))
    try:
        assert find_dotenv(work) == main / ".env"
    finally:
        _git(main, "worktree", "remove", "--force", str(work))
