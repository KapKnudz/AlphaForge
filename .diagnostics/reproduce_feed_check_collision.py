"""Run unchanged baseline behavior with a deterministic millisecond clock."""

from pathlib import Path
from unittest.mock import patch

import test_evidence_flow as baseline_test

import alphaforge.db.repositories as repository

print("repository:", repository.__file__)
print("test:", baseline_test.__file__)
original_connection = baseline_test._connection


def fixed_clock_connection():
    conn = original_connection()
    conn.create_function("strftime", -1, lambda *args: "2026-09-30T13:30:00.000Z")
    return conn


objects = Path(".collision-objects")
objects.mkdir(exist_ok=True)
with patch.object(baseline_test, "_connection", side_effect=fixed_clock_connection):
    baseline_test.test_v2_feed_revocation_supersedes_incomplete_observation_without_legacy_document(
        objects, fixed_clock=True
    )
