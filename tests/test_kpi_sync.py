from __future__ import annotations

import json
from collections import defaultdict

from alphaforge.cli.kpi_sync import sync_company_kpis
from alphaforge.config import Settings
from alphaforge.db.connection import get_connection
from alphaforge.db.migrations import migrate
from alphaforge.db.repositories import upsert_company


class RecordingKpiProvider:
    def __init__(self, summaries=None, histories=None):
        self.summaries = summaries or {}
        self.histories = histories or {}
        self.calls = []
        self.history_calls = defaultdict(int)

    def get_kpi_summary(self, borsdata_id, report_type):
        self.calls.append(("summary", report_type))
        result = self.summaries.get(report_type, {"kpis": []})
        if isinstance(result, BaseException):
            raise result
        return result

    def get_kpi_history(self, borsdata_id, kpi_id, report_type, price_type):
        key = (kpi_id, report_type)
        self.calls.append(("history", *key, price_type))
        self.history_calls[key] += 1
        result = self.histories.get(key, [])
        if isinstance(result, list) and result and isinstance(result[0], BaseException):
            result = result.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def _database(path):
    dsn = f"sqlite:///{path}" if path is not None else "sqlite:///:memory:"
    conn = get_connection(Settings.from_env(dsn=dsn))
    migrate(conn)
    conn.execute("INSERT INTO branches (branch_id, name_sv) VALUES (1, 'Test branch')")
    company_id = upsert_company(
        conn,
        {"insId": 909, "name": "KPI Test AB", "ticker": "KPI", "instrument": 1, "branchId": 1},
    )
    conn.executemany(
        "INSERT INTO kpi_metadata (kpi_id, name_sv, name_en, is_string) VALUES (?, ?, ?, 0)",
        [(kpi_id, f"KPI {kpi_id}", f"KPI {kpi_id}") for kpi_id in (37, 42, 99)],
    )
    conn.commit()
    return conn, company_id


def _jobs(conn):
    return {
        row["job_type"]: (
            row["status"],
            row["attempt"],
            json.loads(row["error"]) if row["error"] else None,
        )
        for row in conn.execute(
            "SELECT job_type, status, attempt, error FROM jobs WHERE job_type LIKE 'sync_kpis%'"
        ).fetchall()
    }


def test_empty_summaries_keep_summary_then_dedicated_history_order_and_attempts():
    conn, company_id = _database(None)
    provider = RecordingKpiProvider()
    try:
        outcome = sync_company_kpis(conn, provider, company_id, 909)
        assert outcome.had_failure is False
        assert provider.calls == [
            ("summary", "year"),
            ("summary", "r12"),
            ("summary", "quarter"),
            ("history", 37, "year", "mean"),
            ("history", 37, "r12", "mean"),
            ("history", 42, "year", "mean"),
            ("history", 42, "r12", "mean"),
        ]
        jobs = _jobs(conn)
        assert jobs["sync_kpis"] == ("success", 1, None)
        assert all(
            jobs[f"sync_kpis_{kpi}_{period}"][:2] == ("success", 1)
            for kpi in (37, 42)
            for period in ("year", "r12")
        )
        assert conn.execute("SELECT count(*) FROM branch_kpi_allowlist").fetchone()[0] == 0

        sync_company_kpis(conn, provider, company_id, 909)
        assert _jobs(conn)["sync_kpis"][:2] == ("success", 2)
        assert all(
            _jobs(conn)[f"sync_kpis_{kpi}_{period}"][:2] == ("success", 2)
            for kpi in (37, 42)
            for period in ("year", "r12")
        )
    finally:
        conn.close()


def test_discovered_empty_history_is_not_fetched_again_by_the_dedicated_lane():
    conn, company_id = _database(None)
    provider = RecordingKpiProvider(
        summaries={
            "year": {
                "kpis": [
                    {
                        "kpiId": 37,
                        "values": [{"y": 2025, "p": 1, "v": 30, "observationDate": "2025-03-31"}],
                    }
                ]
            }
        }
    )
    try:
        outcome = sync_company_kpis(conn, provider, company_id, 909)
        assert outcome.had_failure is False
        assert provider.history_calls[(37, "year")] == 1
        assert provider.calls == [
            ("summary", "year"),
            ("history", 37, "year", "mean"),
            ("summary", "r12"),
            ("summary", "quarter"),
            ("history", 37, "r12", "mean"),
            ("history", 42, "year", "mean"),
            ("history", 42, "r12", "mean"),
        ]
        assert _jobs(conn)["sync_kpis_37_year"][:2] == ("success", 1)
        assert (
            conn.execute(
                "SELECT count(*) FROM branch_kpi_allowlist WHERE branch_id=1 AND kpi_id=37"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_discovered_history_failure_stays_sticky_while_dedicated_fallback_retries():
    conn, company_id = _database(None)
    provider = RecordingKpiProvider(
        summaries={
            "year": {
                "kpis": [
                    {
                        "kpiId": 37,
                        "values": [{"y": 2025, "p": 1, "v": 30, "observationDate": "2025-03-31"}],
                    }
                ]
            }
        },
        histories={(37, "year"): [RuntimeError("first history attempt failed")]},
    )
    try:
        outcome = sync_company_kpis(conn, provider, company_id, 909)
        assert outcome.had_failure is True
        assert provider.history_calls[(37, "year")] == 2
        assert provider.calls == [
            ("summary", "year"),
            ("history", 37, "year", "mean"),
            ("summary", "r12"),
            ("summary", "quarter"),
            ("history", 37, "year", "mean"),
            ("history", 37, "r12", "mean"),
            ("history", 42, "year", "mean"),
            ("history", 42, "r12", "mean"),
        ]
        jobs = _jobs(conn)
        assert jobs["sync_kpis"][0] == "failed"
        assert jobs["sync_kpis"][2]["code"] == "kpi_history_fetch_failed"
        assert jobs["sync_kpis_37_year"][:2] == ("success", 1)
        assert (
            conn.execute(
                "SELECT count(*) FROM branch_kpi_allowlist WHERE branch_id=1 AND kpi_id=37"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT value FROM kpi_observations WHERE kpi_id=37 AND year=2025"
            ).fetchone()[0]
            == 30
        )
    finally:
        conn.close()


def test_repository_failure_commits_prior_kpi_rows_visible_to_another_connection(tmp_path):
    db_path = tmp_path / "partial-commit.sqlite"
    conn, company_id = _database(db_path)
    reader = get_connection(Settings.from_env(dsn=f"sqlite:///{db_path}"))
    conn.execute(
        """CREATE TRIGGER reject_second_kpi BEFORE INSERT ON kpi_observations
           WHEN NEW.value = 20 BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END"""
    )
    conn.commit()
    provider = RecordingKpiProvider(
        summaries={
            "year": {
                "kpis": [
                    {
                        "kpiId": 99,
                        "values": [
                            {"y": 2025, "p": 1, "v": 10, "observationDate": "2025-03-31"},
                            {"y": 2025, "p": 2, "v": 20, "observationDate": "2025-06-30"},
                        ],
                    }
                ]
            }
        }
    )
    try:
        outcome = sync_company_kpis(conn, provider, company_id, 909)
        assert outcome.had_failure is True
        rows = reader.execute(
            "SELECT value FROM kpi_observations WHERE company_id=? AND kpi_id=99 ORDER BY value",
            (company_id,),
        ).fetchall()
        assert [row[0] for row in rows] == [10]
        jobs = _jobs(reader)
        assert jobs["sync_kpis"][0] == "failed"
        assert jobs["sync_kpis"][2]["code"] == "kpi_history_upsert_failed"
    finally:
        reader.close()
        conn.close()
