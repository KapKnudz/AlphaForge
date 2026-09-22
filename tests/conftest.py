"""Session env setup for opt-in integration tests (automatic, not a manual step).

When integration-marked tests are actually selected, resolve the
repository-root `.env` (worktree-safe via `env_resolver`) and load it so a
locally configured key is picked up with no copy step. The default suite
selects no integration tests, so this is a no-op there and CI stays
fixture-only.

The hard-fail lives at collection time on purpose: `skipif` marks are
evaluated before any fixture runs, so a session fixture alone could never
fail fast — the test would skip first. The autouse fixture below repeats the
same resolution as belt and braces for runners that bypass collection hooks.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dotenv import load_dotenv
from env_resolver import KEY, find_dotenv


def _ensure_key() -> Path | None:
    """Load the key from the resolved `.env` when missing; return its path."""
    if os.environ.get(KEY):
        return None
    dotenv_path = find_dotenv(Path(__file__).resolve().parent)
    if dotenv_path is not None:
        load_dotenv(dotenv_path)
    return dotenv_path


def _fail_when_key_missing(dotenv_path: Path | None) -> None:
    if not os.environ.get(KEY):
        pytest.exit(
            f"{KEY} is still unset after automatic .env resolution "
            f"(see tests/env_resolver.py:find_dotenv, resolved {dotenv_path}); "
            f"set {KEY} in the environment or in the repository-root .env",
            returncode=2,
        )


def _integration_selected(items) -> bool:
    return any(item.get_closest_marker("integration") for item in items)


def pytest_collection_finish(session):
    # Gate on the items that survive `-m` deselection. A conftest
    # `pytest_collection_modifyitems` hook still sees to-be-deselected
    # integration items, so gating there loads the root `.env` into every
    # default-suite run; this hook runs after deselection instead.
    if _integration_selected(session.items):
        _fail_when_key_missing(_ensure_key())


@pytest.fixture(scope="session", autouse=True)
def _integration_env(request: pytest.FixtureRequest):
    if not _integration_selected(getattr(request.session, "items", [])):
        return
    _fail_when_key_missing(_ensure_key())
