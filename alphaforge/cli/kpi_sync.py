"""Synchronous per-company Börsdata KPI acquisition workflow."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from alphaforge.db.repositories import record_job, upsert_kpi_observations
from alphaforge.providers.http import sanitize_provider_error


class KpiProvider(Protocol):
    def get_kpi_summary(self, borsdata_id: int, report_type: str) -> Any: ...

    def get_kpi_history(
        self, borsdata_id: int, kpi_id: int, report_type: str, price_type: str
    ) -> Any: ...


@dataclass(frozen=True)
class KpiSyncOutcome:
    had_failure: bool


def sync_company_kpis(
    conn: Any, provider: KpiProvider, company_id: int, borsdata_id: int
) -> KpiSyncOutcome:
    """Acquire one company's KPIs without owning the connection or transport."""
    had_failure = False
    # kpis — per-instrument branch allowlist discovery via summary
    try:
        # summary discovery to build branch_kpi_allowlist (should-add)
        cur = conn.execute("SELECT branch_id FROM companies WHERE id=?", (company_id,))
        r = cur.fetchone()
        branch_id = r[0] if r else None
        _kpi_summary_failed = False
        _allowlist_ok: set[int] = set()
        _allowlist_failed: set[int] = set()
        _kpi_history_ok: set[tuple[int, str]] = set()
        for rt in ("year", "r12", "quarter"):
            try:
                summary = provider.get_kpi_summary(borsdata_id, rt)
            except Exception as exc:
                had_failure = True
                _kpi_summary_failed = True
                record_job(
                    conn,
                    "sync_kpis",
                    company_id=company_id,
                    borsdata_id=borsdata_id,
                    status="failed",
                    error={
                        "code": "kpi_summary_fetch_failed",
                        "message": f"kpi summary/{rt}: {sanitize_provider_error(exc)}",
                        "retryable": True,
                    },
                )
                continue
            if summary and isinstance(summary, dict):
                # summary contains kpis with values array
                kpis = summary.get("kpis") or summary.get("values") or []
                if isinstance(kpis, dict):
                    kpis = [kpis]
                for kp in kpis if isinstance(kpis, list) else []:
                    try:
                        kpi_id = kp.get("kpiId") or kp.get("id")
                        values = kp.get("values") or kp.get("value")
                    except AttributeError:
                        continue
                    has_values = False
                    if isinstance(values, list) and len(values) > 0:
                        has_values = any(v is not None for v in values)
                    elif values is not None:
                        has_values = True
                    if kpi_id is not None and has_values:
                        try:
                            kpi_id_int = int(kpi_id)
                        except (TypeError, ValueError):
                            continue
                        if (
                            rt in ("year", "r12")
                            and isinstance(values, list)
                            and all(isinstance(item, dict) for item in values)
                        ):
                            try:
                                _upserted = upsert_kpi_observations(
                                    conn, company_id, kpi_id_int, rt, "mean", values
                                )
                            except Exception as exc:
                                had_failure = True
                                _kpi_summary_failed = True
                                record_job(
                                    conn,
                                    "sync_kpis",
                                    company_id=company_id,
                                    borsdata_id=borsdata_id,
                                    status="failed",
                                    error={
                                        "code": "kpi_history_upsert_failed",
                                        "message": f"kpi {kpi_id_int}/{rt} summary values: {sanitize_provider_error(exc)}",
                                        "retryable": True,
                                    },
                                )
                                continue
                            if _upserted < sum(
                                1
                                for item in values
                                if isinstance(item, dict)
                                and (item.get("v") is not None or item.get("value") is not None)
                            ):
                                had_failure = True
                                _kpi_summary_failed = True
                                record_job(
                                    conn,
                                    "sync_kpis",
                                    company_id=company_id,
                                    borsdata_id=borsdata_id,
                                    status="failed",
                                    error={
                                        "code": "kpi_history_upsert_failed",
                                        "message": f"kpi {kpi_id_int}/{rt} summary values: unpersistable history values",
                                        "retryable": True,
                                    },
                                )
                        if branch_id is not None:
                            try:
                                conn.execute(
                                    "INSERT INTO branch_kpi_allowlist (branch_id, kpi_id) VALUES (?, ?) ON CONFLICT(branch_id, kpi_id) DO NOTHING",
                                    (int(branch_id), kpi_id_int),
                                )
                                if kpi_id_int in (37, 42):
                                    _allowlist_ok.add(kpi_id_int)
                                    _allowlist_failed.discard(kpi_id_int)
                            except Exception as exc:
                                had_failure = True
                                if kpi_id_int in (37, 42):
                                    _allowlist_failed.add(kpi_id_int)
                                else:
                                    _kpi_summary_failed = True
                                record_job(
                                    conn,
                                    f"sync_kpis_allowlist_{kpi_id_int}"
                                    if kpi_id_int in (37, 42)
                                    else "sync_kpis",
                                    company_id=company_id,
                                    borsdata_id=borsdata_id,
                                    status="failed",
                                    error={
                                        "code": "kpi_allowlist_failed",
                                        "message": f"kpi {kpi_id_int}/{rt}: {sanitize_provider_error(exc)}",
                                        "retryable": True,
                                    },
                                )
                        if rt in ("year", "r12"):
                            try:
                                history_rows = provider.get_kpi_history(
                                    borsdata_id, kpi_id_int, rt, "mean"
                                )
                            except Exception as exc:
                                had_failure = True
                                _kpi_summary_failed = True
                                record_job(
                                    conn,
                                    "sync_kpis",
                                    company_id=company_id,
                                    borsdata_id=borsdata_id,
                                    status="failed",
                                    error={
                                        "code": "kpi_history_fetch_failed",
                                        "message": f"kpi {kpi_id_int}/{rt}: {sanitize_provider_error(exc)}",
                                        "retryable": True,
                                    },
                                )
                                continue
                            if not history_rows:
                                _kpi_history_ok.add((kpi_id_int, rt))
                            else:
                                try:
                                    _upserted = upsert_kpi_observations(
                                        conn,
                                        company_id,
                                        kpi_id_int,
                                        rt,
                                        "mean",
                                        history_rows,
                                    )
                                except Exception as exc:
                                    had_failure = True
                                    _kpi_summary_failed = True
                                    record_job(
                                        conn,
                                        "sync_kpis",
                                        company_id=company_id,
                                        borsdata_id=borsdata_id,
                                        status="failed",
                                        error={
                                            "code": "kpi_history_upsert_failed",
                                            "message": f"kpi {kpi_id_int}/{rt}: {sanitize_provider_error(exc)}",
                                            "retryable": True,
                                        },
                                    )
                                    continue
                                if _upserted < sum(
                                    1
                                    for item in history_rows
                                    if isinstance(item, dict)
                                    and (item.get("v") is not None or item.get("value") is not None)
                                ):
                                    had_failure = True
                                    _kpi_summary_failed = True
                                    record_job(
                                        conn,
                                        "sync_kpis",
                                        company_id=company_id,
                                        borsdata_id=borsdata_id,
                                        status="failed",
                                        error={
                                            "code": "kpi_history_upsert_failed",
                                            "message": f"kpi {kpi_id_int}/{rt}: unpersistable history values",
                                            "retryable": True,
                                        },
                                    )
                                    continue
                                _kpi_history_ok.add((kpi_id_int, rt))
        # Dedicated fetch for ROIC (37) and net-debt/EBITDA (42) even when
        # Börsdata summary omits them (Clas Ohlson live: summary has 42 ids
        # but not 37/42, while history endpoints return 10 rows each).
        # Safe for genuinely unavailable KPIs: 400/empty is treated as missing.
        from alphaforge.core.kpi_taxonomy import KpiIds

        for _kpi_id, _rt in (
            (KpiIds.ROIC, "year"),
            (KpiIds.ROIC, "r12"),
            (KpiIds.NET_DEBT_EBITDA, "year"),
            (KpiIds.NET_DEBT_EBITDA, "r12"),
        ):
            _job = f"sync_kpis_{int(_kpi_id)}_{_rt}"
            if (int(_kpi_id), _rt) in _kpi_history_ok:
                record_job(
                    conn, _job, company_id=company_id, borsdata_id=borsdata_id, status="success"
                )
            else:
                try:
                    _rows = provider.get_kpi_history(borsdata_id, int(_kpi_id), _rt, "mean")
                except Exception as exc:
                    had_failure = True
                    record_job(
                        conn,
                        _job,
                        company_id=company_id,
                        borsdata_id=borsdata_id,
                        status="failed",
                        error={
                            "code": "kpi_history_fetch_failed",
                            "message": f"kpi {_kpi_id}/{_rt}: {sanitize_provider_error(exc)}",
                            "retryable": True,
                        },
                    )
                    continue
                if _rows:
                    try:
                        _upserted = upsert_kpi_observations(
                            conn, company_id, int(_kpi_id), _rt, "mean", _rows
                        )
                    except Exception as exc:
                        had_failure = True
                        record_job(
                            conn,
                            _job,
                            company_id=company_id,
                            borsdata_id=borsdata_id,
                            status="failed",
                            error={
                                "code": "kpi_history_upsert_failed",
                                "message": f"kpi {_kpi_id}/{_rt}: {sanitize_provider_error(exc)}",
                                "retryable": True,
                            },
                        )
                        continue
                    if _upserted < sum(
                        1
                        for item in _rows
                        if isinstance(item, dict)
                        and (item.get("v") is not None or item.get("value") is not None)
                    ):
                        had_failure = True
                        record_job(
                            conn,
                            _job,
                            company_id=company_id,
                            borsdata_id=borsdata_id,
                            status="failed",
                            error={
                                "code": "kpi_history_upsert_failed",
                                "message": f"kpi {_kpi_id}/{_rt}: unpersistable history values",
                                "retryable": True,
                            },
                        )
                        continue
                    record_job(
                        conn, _job, company_id=company_id, borsdata_id=borsdata_id, status="success"
                    )
                else:
                    record_job(
                        conn, _job, company_id=company_id, borsdata_id=borsdata_id, status="success"
                    )
                    continue
            if branch_id is not None:
                try:
                    conn.execute(
                        "INSERT INTO branch_kpi_allowlist (branch_id, kpi_id) VALUES (?, ?) ON CONFLICT(branch_id, kpi_id) DO NOTHING",
                        (int(branch_id), int(_kpi_id)),
                    )
                    _allowlist_ok.add(int(_kpi_id))
                    _allowlist_failed.discard(int(_kpi_id))
                except Exception as exc:
                    _allowlist_failed.add(int(_kpi_id))
                    had_failure = True
                    record_job(
                        conn,
                        f"sync_kpis_allowlist_{int(_kpi_id)}",
                        company_id=company_id,
                        borsdata_id=borsdata_id,
                        status="failed",
                        error={
                            "code": "kpi_allowlist_failed",
                            "message": f"kpi {_kpi_id}/{_rt}: {sanitize_provider_error(exc)}",
                            "retryable": True,
                        },
                    )
        if not _kpi_summary_failed:
            record_job(
                conn, "sync_kpis", company_id=company_id, borsdata_id=borsdata_id, status="success"
            )
        for _allowlist_kpi in sorted(_allowlist_ok - _allowlist_failed):
            record_job(
                conn,
                f"sync_kpis_allowlist_{_allowlist_kpi}",
                company_id=company_id,
                borsdata_id=borsdata_id,
                status="success",
            )
        conn.commit()
    except Exception as exc:
        had_failure = True
        record_job(
            conn,
            "sync_kpis",
            company_id=company_id,
            borsdata_id=borsdata_id,
            status="failed",
            error={
                "code": "kpi_contract_failed",
                "message": f"kpi contract: {sanitize_provider_error(exc)}",
                "retryable": True,
            },
        )
    return KpiSyncOutcome(had_failure=had_failure)
