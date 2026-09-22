"""alphaforge CLI — import-watchlist + sync."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path


def _get_settings(dsn: str | None = None) -> object:
    from alphaforge.config import Settings

    return Settings.from_env(dsn=dsn)


def cmd_import_watchlist(args: argparse.Namespace) -> int:
    from alphaforge.config import Settings
    from alphaforge.db.connection import get_connection
    from alphaforge.db.migrations import migrate

    settings = Settings.from_env(dsn=args.dsn) if args.dsn else Settings.from_env()
    conn = get_connection(settings)
    migrate(conn)

    file_path = Path(args.file)
    if not file_path.exists():
        print(f"watchlist file not found: {file_path}", file=sys.stderr)
        return 1
    source_file = args.source_file or file_path.name

    # Read CSV — handles both semicolon and comma, ISIN;Name;Ticker;ISIN variations
    content = file_path.read_text(encoding="utf-8-sig")
    # Detect delimiter
    delimiter = ";" if content.count(";") > content.count(",") else ","
    reader = csv.DictReader(content.splitlines(), delimiter=delimiter)
    if reader.fieldnames is None:
        print("empty watchlist file", file=sys.stderr)
        return 1
    # ISIN → ticker via instrument.py:26; normalized match on isin/ticker/borsdata_id
    # Build isin/ticker index from companies table for matched_via
    # First, need to load companies? But import-watchlist may run before sync; allow unmatched.
    inserted = 0
    for row in reader:
        # Normalize keys
        norm: dict[str, str] = {}
        for k, v in row.items():
            if k is None:
                continue
            norm[k.strip().lower()] = (v or "").strip()
        isin = norm.get("isin") or norm.get("isin_code") or ""
        ticker = norm.get("ticker") or norm.get("instrument") or ""
        name_hint = norm.get("name") or norm.get("company") or norm.get("bolagsnamn") or ""
        borsdata_id_raw = norm.get("id") or norm.get("borsdata_id") or norm.get("borsdata id") or ""
        borsdata_id = None
        if borsdata_id_raw:
            try:
                borsdata_id = int(borsdata_id_raw)
            except ValueError:
                borsdata_id = None
        # row_hash for UNIQUE(source_file,row_hash)
        row_hash = hashlib.sha256(json.dumps(norm, sort_keys=True).encode()).hexdigest()[:16]
        # matched_via — ISIN preferred, then ticker, then borsdata_id, else unmatched
        # Try to match against companies table
        matched_via = "unmatched"
        company_id = None
        if isin:
            cur = conn.execute("SELECT id FROM companies WHERE isin=?", (isin,))
            r = cur.fetchone()
            if r:
                company_id = int(r[0])
                matched_via = "isin"
        if company_id is None and ticker:
            cur = conn.execute("SELECT id FROM companies WHERE ticker=? COLLATE NOCASE", (ticker,))
            r = cur.fetchone()
            if r:
                company_id = int(r[0])
                matched_via = "ticker"
        if company_id is None and borsdata_id is not None:
            cur = conn.execute("SELECT id FROM companies WHERE borsdata_id=?", (borsdata_id,))
            r = cur.fetchone()
            if r:
                company_id = int(r[0])
                matched_via = "borsdata_id"
        try:
            conn.execute(
                """
                INSERT INTO watchlist
                    (company_id, ticker, isin, name_hint, source_file,
                     source_row_hash, matched_via, borsdata_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_file, source_row_hash) DO NOTHING
                """,
                (
                    company_id,
                    ticker or name_hint or isin or "UNKNOWN",
                    isin or None,
                    name_hint or None,
                    source_file,
                    row_hash,
                    matched_via,
                    borsdata_id,
                ),
            )
            # Also handle UNIQUE(company_id) — if already exists, skip
            # The above will fail on second unique; catch
            inserted += 1
        except Exception as e:
            # UNIQUE(company_id) conflict → skip
            if "UNIQUE" in str(e) or "unique" in str(e).lower():
                continue
            print(f"watchlist insert failed for {norm}: {e}", file=sys.stderr)
            continue
    conn.commit()
    # Report
    cur = conn.execute("SELECT count(*) FROM watchlist WHERE source_file=?", (source_file,))
    total = cur.fetchone()[0]
    print(f"import-watchlist: {total} rows for source_file={source_file} ({inserted} attempted)")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    from alphaforge.config import Settings
    from alphaforge.db.connection import get_connection
    from alphaforge.db.migrations import migrate
    from alphaforge.db.repositories import (
        record_job,
        relink_watchlist,
        upsert_company,
        upsert_dividends,
        upsert_financial_periods,
        upsert_kpi_observations,
        upsert_prices,
        upsert_report_calendar,
        upsert_stock_splits,
    )
    from alphaforge.providers.borsdata.adapter import BorsdataAdapter

    settings = Settings.from_env(dsn=args.dsn) if args.dsn else Settings.from_env()
    conn = get_connection(settings)
    migrate(conn)
    sync_failed = False

    # Resolve company scope
    company_rows: list[tuple[int, int, str | None]] = []  # (company_id, borsdata_id, ticker)
    if args.all:
        cur = conn.execute("SELECT id, borsdata_id, ticker FROM companies")
        company_rows = [(int(r[0]), int(r[1]), r[2]) for r in cur.fetchall()]
        if not company_rows and not args.allow_empty_companies:
            # If no companies yet, fetch instruments first
            pass
    # Requested ticker scope is resolved after instrument seeding below. On a
    # fresh database this is the difference between a real company sync and a
    # successful no-op.

    adapter = BorsdataAdapter()

    # Always sync instruments first (seed companies) — idempotent ON CONFLICT
    # This also logs currency exposure per plan 1.3
    try:
        instruments = adapter.get_instruments()
        currency_exposure: list[tuple[int, str | None, str | None, float | None]] = []
        # Persist instruments as companies
        for ins in instruments:
            try:
                cid = upsert_company(conn, ins)
                # Log currency surface for Swedish watchlist (country_id=1)
                # Live: 203 mismatched Nordics; we log for all instruments
                if ins.get("countryId") == 1 or ins.get("country_id") == 1 or True:
                    stock_ccy = ins.get("stockPriceCurrency") or ins.get("stock_price_currency")
                    report_ccy = ins.get("reportCurrency") or ins.get("report_currency")
                    # ratio not on instrument; logged per report later
                    if stock_ccy != report_ccy:
                        currency_exposure.append(
                            (int(ins.get("insId") or 0), stock_ccy, report_ccy, None)
                        )
            except Exception as e:
                # per-company failure isolation
                sync_failed = True
                record_job(
                    conn,
                    "sync_instruments",
                    company_id=None,
                    borsdata_id=ins.get("insId"),
                    status="failed",
                    error={"code": "company_upsert_failed", "message": str(e), "retryable": True},
                )
                continue
        if instruments:
            record_job(
                conn,
                "sync_instruments",
                company_id=None,
                borsdata_id=None,
                status="failed" if sync_failed else "success",
                error=(
                    {
                        "code": "instrument_upsert_failed",
                        "message": "one or more instruments failed",
                    }
                    if sync_failed
                    else None
                ),
            )
            # Currency exposure log (plan 1.3) — print to stderr for visibility
            mismatched = [c for c in currency_exposure if c[1] != c[2]]
            if mismatched:
                print(
                    f"currency_exposure: {len(mismatched)} mismatched stockPriceCurrency vs reportCurrency (e.g. {mismatched[:3]})",
                    file=sys.stderr,
                )
    except Exception as e:
        sync_failed = True
        print(f"sync instruments failed: {e}", file=sys.stderr)
        record_job(
            conn,
            "sync_instruments",
            company_id=None,
            borsdata_id=None,
            status="failed",
            error={"code": "instruments_fetch_failed", "message": str(e), "retryable": True},
        )

    # Instruments now exist, so relink rows imported before the sync and only
    # then resolve a requested ticker.  ``--all`` means the imported watchlist
    # when one exists; a database with no watchlist retains the useful bootstrap
    # behavior of syncing the complete seeded instrument universe.
    relink_watchlist(conn)
    if args.all:
        matched = conn.execute(
            """
            SELECT c.id, c.borsdata_id, c.ticker
            FROM companies c JOIN watchlist w ON w.company_id=c.id
            ORDER BY c.ticker COLLATE NOCASE, c.id
            """
        ).fetchall()
        if matched:
            company_rows = [(int(r[0]), int(r[1]), r[2]) for r in matched]
        elif not company_rows:
            all_rows = conn.execute(
                "SELECT id, borsdata_id, ticker FROM companies ORDER BY ticker COLLATE NOCASE, id"
            ).fetchall()
            company_rows = [(int(r[0]), int(r[1]), r[2]) for r in all_rows]
    if args.company or args.ticker:
        requested = args.company or args.ticker
        cur = conn.execute(
            "SELECT id, borsdata_id, ticker FROM companies WHERE ticker=? COLLATE NOCASE",
            (requested,),
        )
        r = cur.fetchone()
        if r:
            company_rows = [(int(r[0]), int(r[1]), r[2])]
        else:
            sync_failed = True
            message = f"requested company ticker not found after instrument seeding: {requested}"
            print(f"sync failed: {message}", file=sys.stderr)
            record_job(
                conn,
                "sync_instruments",
                company_id=None,
                borsdata_id=None,
                status="failed",
                error={"code": "company_not_found", "message": message, "retryable": False},
            )
            return 1

    # Resolve watchlist-scoped companies if --all and still empty → use watchlist
    if args.all and not company_rows:
        cur = conn.execute(
            "SELECT company_id, borsdata_id FROM watchlist WHERE company_id IS NOT NULL"
        )
        for r in cur.fetchall():
            if r[0] is not None:
                # lookup ticker
                cur2 = conn.execute("SELECT ticker FROM companies WHERE id=?", (int(r[0]),))
                t = cur2.fetchone()
                company_rows.append(
                    (int(r[0]), int(r[1]) if r[1] is not None else 0, t[0] if t else None)
                )

    # Reference dictionaries (should-adds) — seed once
    try:
        for name, getter in [
            ("sectors", adapter.get_sectors),
            ("branches", adapter.get_branches),
            ("countries", adapter.get_countries),
        ]:
            rows = getter()
            for r in rows:
                try:
                    if name == "branches":
                        conn.execute(
                            "INSERT INTO branches (branch_id, name_sv, name_en, sector_id) VALUES (?, ?, ?, ?) ON CONFLICT(branch_id) DO UPDATE SET name_sv=excluded.name_sv, name_en=excluded.name_en",
                            (
                                r.get("id") or r.get("branchId"),
                                r.get("name") or r.get("nameSv") or "",
                                r.get("nameEn"),
                                r.get("sectorId"),
                            ),
                        )
                    elif name == "sectors":
                        conn.execute(
                            "INSERT INTO sectors (sector_id, name_sv, name_en) VALUES (?, ?, ?) ON CONFLICT(sector_id) DO UPDATE SET name_sv=excluded.name_sv, name_en=excluded.name_en",
                            (
                                r.get("id") or r.get("sectorId"),
                                r.get("name") or r.get("nameSv") or "",
                                r.get("nameEn"),
                            ),
                        )
                    elif name == "countries":
                        conn.execute(
                            "INSERT INTO countries (country_id, name_sv, name_en) VALUES (?, ?, ?) ON CONFLICT(country_id) DO UPDATE SET name_sv=excluded.name_sv, name_en=excluded.name_en",
                            (
                                r.get("id") or r.get("countryId"),
                                r.get("name") or r.get("nameSv") or "",
                                r.get("nameEn"),
                            ),
                        )
                except Exception:
                    continue
        conn.commit()
        # translationmetadata
        try:
            trows = adapter.get_translation_metadata()
            for translation in trows:
                key = translation.get("translationKey") or translation.get("key")
                if key:
                    conn.execute(
                        "INSERT INTO translation_metadata (translation_key, name_sv, name_en) VALUES (?, ?, ?) ON CONFLICT(translation_key) DO UPDATE SET name_sv=excluded.name_sv, name_en=excluded.name_en",
                        (key, translation.get("nameSv"), translation.get("nameEn")),
                    )
            conn.commit()
        except Exception as exc:
            sync_failed = True
            print(f"translation metadata sync failed: {exc}", file=sys.stderr)
        # kpi/report metadata caches
        try:
            kpis_meta = adapter.get_kpi_metadata()
            for km in kpis_meta:
                conn.execute(
                    "INSERT INTO kpi_metadata (kpi_id, name_sv, name_en, format, is_string) VALUES (?, ?, ?, ?, ?) ON CONFLICT(kpi_id) DO UPDATE SET name_sv=excluded.name_sv, name_en=excluded.name_en, format=excluded.format",
                    (
                        km.get("kpiId") or km.get("id"),
                        km.get("nameSv") or km.get("name") or "",
                        km.get("nameEn") or "",
                        km.get("format"),
                        int(bool(km.get("isString"))),
                    ),
                )
            rep_meta = adapter.get_report_metadata()
            for rm in rep_meta:
                conn.execute(
                    "INSERT INTO report_metadata (property, name_sv, name_en, format) VALUES (?, ?, ?, ?) ON CONFLICT(property) DO UPDATE SET name_sv=excluded.name_sv, name_en=excluded.name_en",
                    (
                        rm.get("property") or rm.get("reportPropery") or rm.get("name") or "",
                        rm.get("nameSv") or "",
                        rm.get("nameEn") or "",
                        rm.get("format"),
                    ),
                )
            conn.commit()
        except Exception as exc:
            sync_failed = True
            print(f"metadata cache sync failed: {exc}", file=sys.stderr)
    except Exception as e:
        sync_failed = True
        print(f"reference dictionaries sync failed: {e}", file=sys.stderr)

    # Per-company sync with failure isolation — each company wrapped individually
    # Reports (batch 50 inside adapter)
    if company_rows:
        ins_ids = [borsdata_id for _, borsdata_id, _ in company_rows if borsdata_id]
        # Batch reports fetch (adapter handles 50 + sleep)
        try:
            if ins_ids:
                reports = adapter.get_reports(ins_ids, original=0)
                # Group by insId
                from collections import defaultdict

                grouped: dict[int, list[dict]] = defaultdict(list)
                for rep in reports:
                    iid = rep.get("insId") or rep.get("instrumentId") or rep.get("instrument")
                    if iid is not None:
                        grouped[int(iid)].append(rep)
                # Map borsdata_id → company_id
                b2c = {bid: cid for cid, bid, _ in company_rows}
                for cid, bid, _ticker in company_rows:
                    reps = grouped.get(int(bid), [])
                    if not reps:
                        # A requested company with no company-level response is
                        # not a successful sync.  For a fleet run, retain the
                        # row-level diagnostic without making an empty provider
                        # response abort every other watchlist company.
                        missing_error = {
                            "code": "company_reports_missing",
                            "message": f"no report rows returned for instrument {bid}",
                            "retryable": True,
                        }
                        record_job(
                            conn,
                            "sync_reports",
                            company_id=cid,
                            borsdata_id=bid,
                            status="failed" if args.company or args.ticker else "partial",
                            error=missing_error,
                        )
                        if args.company or args.ticker:
                            sync_failed = True
                        continue
                    try:
                        upsert_financial_periods(conn, cid, reps)
                        record_job(
                            conn, "sync_reports", company_id=cid, borsdata_id=bid, status="success"
                        )
                    except Exception as e:
                        sync_failed = True
                        record_job(
                            conn,
                            "sync_reports",
                            company_id=cid,
                            borsdata_id=bid,
                            status="failed",
                            error={
                                "code": "reports_upsert_failed",
                                "message": str(e),
                                "retryable": True,
                            },
                        )
        except Exception as e:
            sync_failed = True
            print(f"reports sync failed: {e}", file=sys.stderr)
            for cid, bid, _ticker in company_rows:
                record_job(
                    conn,
                    "sync_reports",
                    company_id=cid,
                    borsdata_id=bid,
                    status="failed",
                    error={"code": "reports_contract_failed", "message": str(e), "retryable": True},
                )

        # Per-company prices / dividends / kpi branches etc — isolated
        for cid, bid, _ticker in company_rows:
            # prices
            try:
                price_rows = adapter.get_stock_prices(bid)
                if price_rows:
                    # Need currency for prices — lookup from companies
                    cur = conn.execute(
                        "SELECT stock_price_currency FROM companies WHERE id=?", (cid,)
                    )
                    r = cur.fetchone()
                    cur_ccy = r[0] if r and r[0] else None
                    upsert_prices(conn, cid, price_rows, currency=cur_ccy)
                record_job(conn, "sync_prices", company_id=cid, borsdata_id=bid, status="success")
            except Exception as e:
                sync_failed = True
                record_job(
                    conn,
                    "sync_prices",
                    company_id=cid,
                    borsdata_id=bid,
                    status="failed",
                    error={"code": "prices_fetch_failed", "message": str(e), "retryable": True},
                )
            # kpis — per-instrument branch allowlist discovery via summary
            try:
                # summary discovery to build branch_kpi_allowlist (should-add)
                cur = conn.execute("SELECT branch_id FROM companies WHERE id=?", (cid,))
                r = cur.fetchone()
                branch_id = r[0] if r else None
                if branch_id is not None:
                    for rt in ("year", "r12", "quarter"):
                        summary = adapter.get_kpi_summary(bid, rt)
                        if summary and isinstance(summary, dict):
                            # summary contains kpis with values array
                            kpis = summary.get("kpis") or summary.get("values") or []
                            if isinstance(kpis, dict):
                                kpis = [kpis]
                            for kp in kpis if isinstance(kpis, list) else []:
                                kpi_id = kp.get("kpiId") or kp.get("id")
                                values = kp.get("values") or kp.get("value")
                                # If values non-empty → allow-list
                                has_values = False
                                if isinstance(values, list) and len(values) > 0:
                                    has_values = any(v is not None for v in values)
                                elif values is not None:
                                    has_values = True
                                if kpi_id is not None and has_values:
                                    kpi_id_int = int(kpi_id)
                                    if (
                                        rt in ("year", "r12")
                                        and isinstance(values, list)
                                        and all(isinstance(item, dict) for item in values)
                                    ):
                                        upsert_kpi_observations(
                                            conn, cid, kpi_id_int, rt, "mean", values
                                        )
                                    try:
                                        conn.execute(
                                            "INSERT INTO branch_kpi_allowlist (branch_id, kpi_id) VALUES (?, ?) ON CONFLICT(branch_id, kpi_id) DO NOTHING",
                                            (int(branch_id), kpi_id_int),
                                        )
                                    except Exception:
                                        pass
                                    if rt in ("year", "r12"):
                                        history_rows = adapter.get_kpi_history(
                                            bid, kpi_id_int, rt, "mean"
                                        )
                                        if history_rows:
                                            upsert_kpi_observations(
                                                conn,
                                                cid,
                                                kpi_id_int,
                                                rt,
                                                "mean",
                                                history_rows,
                                            )
                    # Dedicated fetch for ROIC (37) and net-debt/EBITDA (42) even when
                    # Börsdata summary omits them (Clas Ohlson live: summary has 42 ids
                    # but not 37/42, while history endpoints return 10 rows each).
                    # Safe for genuinely unavailable KPIs: 400/empty is treated as missing.
                    from alphaforge.core.kpi_taxonomy import KpiIds as _KpiIds

                    for _kpi_id, _rt in (
                        (_KpiIds.ROIC, "year"),
                        (_KpiIds.ROIC, "r12"),
                        (_KpiIds.NET_DEBT_EBITDA, "year"),
                        (_KpiIds.NET_DEBT_EBITDA, "r12"),
                    ):
                        try:
                            _rows = adapter.get_kpi_history(bid, int(_kpi_id), _rt, "mean")
                        except Exception as exc:
                            sync_failed = True
                            record_job(
                                conn,
                                "sync_kpis",
                                company_id=cid,
                                borsdata_id=bid,
                                status="failed",
                                error={
                                    "code": "kpi_history_fetch_failed",
                                    "message": f"kpi {_kpi_id}/{_rt}: {exc}",
                                    "retryable": True,
                                },
                            )
                            continue
                        if _rows:
                            try:
                                upsert_kpi_observations(
                                    conn, cid, int(_kpi_id), _rt, "mean", _rows
                                )
                            except Exception as exc:
                                sync_failed = True
                                record_job(
                                    conn,
                                    "sync_kpis",
                                    company_id=cid,
                                    borsdata_id=bid,
                                    status="failed",
                                    error={
                                        "code": "kpi_history_upsert_failed",
                                        "message": f"kpi {_kpi_id}/{_rt}: {exc}",
                                        "retryable": True,
                                    },
                                )
                                continue
                            try:
                                conn.execute(
                                    "INSERT INTO branch_kpi_allowlist (branch_id, kpi_id) VALUES (?, ?) ON CONFLICT(branch_id, kpi_id) DO NOTHING",
                                    (int(branch_id), int(_kpi_id)),
                                )
                            except Exception:
                                pass
                    conn.commit()
            except Exception as exc:
                sync_failed = True
                record_job(
                    conn,
                    "sync_kpis",
                    company_id=cid,
                    borsdata_id=bid,
                    status="failed",
                    error={"code": "kpi_contract_failed", "message": str(exc), "retryable": True},
                )

        # Dividends (global calendar, not per-company) — filter by company if possible
        try:
            div_rows = adapter.get_dividends(ins_ids)
            # dividends payload may contain insId; group similarly
            from collections import defaultdict

            div_grouped: dict[int, list[dict]] = defaultdict(list)
            ungrouoped: list[dict] = []
            for d in div_rows:
                iid = d.get("insId") or d.get("instrumentId")
                if iid is not None:
                    div_grouped[int(iid)].append(d)
                else:
                    ungrouoped.append(d)
            b2c = {bid: cid for cid, bid, _ in company_rows}
            for bid, drows in div_grouped.items():
                cid = b2c.get(int(bid))
                if cid is None:
                    continue
                try:
                    upsert_dividends(conn, cid, drows)
                    record_job(
                        conn, "sync_dividends", company_id=cid, borsdata_id=bid, status="success"
                    )
                except Exception as e:
                    sync_failed = True
                    record_job(
                        conn,
                        "sync_dividends",
                        company_id=cid,
                        borsdata_id=bid,
                        status="failed",
                        error={
                            "code": "dividends_upsert_failed",
                            "message": str(e),
                            "retryable": True,
                        },
                    )
            # dividend_coverage — update throughput for each company touched
            for cid, _, _ in company_rows:
                try:
                    cur = conn.execute(
                        "SELECT min(ex_date), max(ex_date) FROM dividends WHERE company_id=?",
                        (cid,),
                    )
                    r = cur.fetchone()
                    if r and r[0] and r[1]:
                        conn.execute(
                            "INSERT INTO dividend_coverage (company_id, covered_from, covered_through) VALUES (?, ?, ?) ON CONFLICT(company_id) DO UPDATE SET covered_from=excluded.covered_from, covered_through=excluded.covered_through, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                            (cid, r[0], r[1]),
                        )
                except Exception:
                    pass
            conn.commit()
        except Exception as e:
            sync_failed = True
            print(f"dividends sync failed: {e}", file=sys.stderr)

        # Stock splits (rolling 1-year window, MAX 1 year per API) — global fetch
        try:
            splits = adapter.get_stock_splits()
            if splits:
                b2c = {bid: cid for cid, bid, _ in company_rows}
                upsert_stock_splits(conn, splits, company_map=b2c)
        except Exception as e:
            sync_failed = True
            print(f"stock_splits sync failed: {e}", file=sys.stderr)

        # Report calendar (weekly, but sync opportunistically)
        try:
            cal = adapter.get_report_calendar(ins_ids)
            if cal:
                b2c = {bid: cid for cid, bid, _ in company_rows}
                upsert_report_calendar(conn, cal, company_map=b2c)
        except Exception as e:
            sync_failed = True
            print(f"report_calendar sync failed: {e}", file=sys.stderr)

        # Holdings snapshots (global) — insider, buyback, shorts (shorts is global snapshot)
        # These are deferred per-company detail but snapshot tables are updated
        # For MVP, we persist shorts per company when mapping exists.
        try:
            shorts = adapter.get_shorts()
            if shorts:
                for s in shorts:
                    iid = s.get("insId") or s.get("instrumentId")
                    if iid is None:
                        continue
                    cid = next((c for c, b, _ in company_rows if b == int(iid)), None)
                    if cid is None:
                        continue
                    try:
                        observation_date = (
                            s.get("observationDate")
                            or s.get("date")
                            or s.get("observation_date")
                            or ""
                        )
                        observation_date = str(observation_date)[:10] or date.today().isoformat()
                        conn.execute(
                            "INSERT INTO company_short_snapshots (company_id, observation_date, shorts_proc, shorts_holders, shorts_milj, source) VALUES (?, ?, ?, ?, ?, 'borsdata') ON CONFLICT(company_id, observation_date) DO UPDATE SET shorts_proc=excluded.shorts_proc, shorts_holders=excluded.shorts_holders, shorts_milj=excluded.shorts_milj",
                            (
                                cid,
                                observation_date,
                                s.get("shortsProc")
                                if s.get("shortsProc") is not None
                                else s.get("shorts_proc"),
                                s.get("holders")
                                if s.get("holders") is not None
                                else s.get("shortsHolders"),
                                s.get("milj") if s.get("milj") is not None else s.get("shortsMilj"),
                            ),
                        )
                    except Exception:
                        continue
                conn.commit()
        except Exception as e:
            sync_failed = True
            print(f"shorts sync failed: {e}", file=sys.stderr)

    conn.commit()
    if sync_failed:
        print("sync: failed (see stderr diagnostics)", file=sys.stderr)
        return 1
    print("sync: complete", file=sys.stderr)
    return 0


def export_ranking_files(
    ranking,
    as_of: str,
    model_version: str,
    exports_dir: Path,
    *,
    evidence_packet_hash: str | None = None,
    evidence_packet_hashes: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    from dataclasses import asdict

    exports_dir.mkdir(parents=True, exist_ok=True)

    if evidence_packet_hashes is None:
        evidence_packet_hashes = {
            str(score.company_id): str(score.evidence_packet_hash)
            for score in ranking.scores
            if score.evidence_packet_hash
        }
    if (
        evidence_packet_hash is None
        and len(ranking.scores) == 1
        and len(evidence_packet_hashes) == 1
    ):
        evidence_packet_hash = next(iter(evidence_packet_hashes.values()))

    ranking_data = {
        "as_of": as_of,
        "model_version": model_version,
        "company_count": len(ranking.scores),
        "eligible_count": sum(1 for s in ranking.scores if s.rank_eligible),
        "unranked_count": sum(1 for s in ranking.scores if not s.rank_eligible),
        "evidence_packet_hash": evidence_packet_hash,
        "evidence_packet_hashes": evidence_packet_hashes or {},
        "scores": [asdict(s) for s in ranking.scores],
    }

    ranking_json_path = exports_dir / "ranking.json"
    ranking_json_path.write_text(json.dumps(ranking_data, indent=2, ensure_ascii=False))

    ranking_csv_path = exports_dir / "ranking.csv"
    with open(ranking_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "rank",
                "ticker",
                "name",
                "total_score",
                "quality_score",
                "growth_score",
                "valuation_score",
                "balance_sheet_score",
                "ranking_model",
                "rank_eligible",
                "ranking_section",
                "eligibility_reasons",
                "readiness_status",
                "readiness_blockers",
                "readiness_limitations",
                "evidence_packet_hash",
                "data_quality",
            ]
        )
        for i, score in enumerate(ranking.scores, 1):
            writer.writerow(
                [
                    i if score.rank_eligible else "",
                    score.ticker,
                    score.name,
                    score.total_score,
                    score.quality_score,
                    score.growth_score,
                    score.valuation_score,
                    score.balance_sheet_score,
                    score.ranking_model,
                    score.rank_eligible,
                    score.ranking_section,
                    ";".join(score.eligibility_reasons),
                    score.readiness_status,
                    ";".join(score.readiness_blockers),
                    ";".join(score.readiness_limitations),
                    score.evidence_packet_hash or "",
                    score.data_quality,
                ]
            )

    return ranking_json_path, ranking_csv_path


def cmd_rank(args: argparse.Namespace) -> int:
    import hashlib
    import json
    from dataclasses import asdict
    from pathlib import Path

    from alphaforge.config import Settings
    from alphaforge.core.ranking.engine import RankingEngine
    from alphaforge.db.connection import get_connection
    from alphaforge.db.migrations import migrate
    from alphaforge.db.repositories import save_ranking_run

    settings = Settings.from_env(dsn=args.dsn) if args.dsn else Settings.from_env()
    conn = get_connection(settings)
    migrate(conn)

    as_of = args.as_of
    watchlist_path = args.watchlist

    # Load companies from watchlist or DB
    companies = []
    if watchlist_path:
        # Load from CSV
        import csv

        file_path = Path(watchlist_path)
        if not file_path.exists():
            print(f"watchlist file not found: {file_path}", file=sys.stderr)
            return 1
        content = file_path.read_text(encoding="utf-8-sig")
        delimiter = ";" if content.count(";") > content.count(",") else ","
        reader = csv.DictReader(content.splitlines(), delimiter=delimiter)
        for row in reader:
            norm = {}
            for k, v in row.items():
                if k is None:
                    continue
                norm[k.strip().lower()] = (v or "").strip()
            ticker = norm.get("ticker") or norm.get("instrument") or ""
            if ticker:
                cur = conn.execute(
                    "SELECT id, name, ticker, branch_id FROM companies WHERE ticker=? COLLATE NOCASE",
                    (ticker,),
                )
                r = cur.fetchone()
                if r:
                    from dataclasses import dataclass

                    @dataclass
                    class Company:
                        id: int
                        name: str
                        ticker: str
                        branch_id: int | None

                    companies.append(
                        Company(
                            id=int(r[0]),
                            name=r[1] or "",
                            ticker=r[2] or ticker,
                            branch_id=r[3],
                        )
                    )
    else:
        # Load from DB watchlist
        cur = conn.execute(
            """
            SELECT c.id, c.name, c.ticker, c.branch_id
            FROM companies c
            JOIN watchlist w ON c.id = w.company_id
            """
        )
        from dataclasses import dataclass

        @dataclass
        class Company:
            id: int
            name: str
            ticker: str
            branch_id: int | None

        for r in cur.fetchall():
            companies.append(
                Company(
                    id=int(r[0]),
                    name=r[1] or "",
                    ticker=r[2] or "",
                    branch_id=r[3],
                )
            )

    if not companies:
        print("no companies to rank", file=sys.stderr)
        return 1

    # Reconstruct deterministic inputs from stored observations. The rank
    # command must not fetch live data or substitute an empty result map.
    from alphaforge.cli.ranking_loader import load_results_for_company

    results_by_company: dict[int, dict] = {}
    for company in companies:
        results_by_company[company.id] = load_results_for_company(conn, company.id, as_of)

    # Run ranking
    engine = RankingEngine()
    ranking = engine.rank(companies, results_by_company)

    # The readiness gate is deterministic and runs before any future model
    # call. Persist its verdict alongside each score for auditability.
    from alphaforge.core.gate.readiness import AgentReadinessGate

    gate = AgentReadinessGate()
    for score in ranking.scores:
        loaded = results_by_company.get(score.company_id, {})
        candidate = loaded.get("candidate")
        if candidate is None:
            score.readiness_status = "evidence_blocked"
            score.readiness_blockers = ["ranking inputs unavailable"]
            continue
        candidate.ticker = score.ticker
        assessment = gate.assess(candidate)
        score.readiness_status = assessment.status
        score.readiness_blockers = [f"{item.code}: {item.message}" for item in assessment.blockers]
        score.readiness_limitations = [
            f"{item.code}: {item.message}" for item in assessment.limitations
        ]

    # Ranking provenance is the frozen evidence packet input, not a hash of
    # the resulting score JSON.  A multi-company run records the stable map in
    # ``evidence_packet_hashes`` and uses its canonical hash in the legacy
    # single-value column; a one-company run carries the packet hash verbatim.
    evidence_packet_hashes = {
        str(company_id): str(score.evidence_packet_hash)
        for company_id, score in ((score.company_id, score) for score in ranking.scores)
        if score.evidence_packet_hash
    }
    evidence_packet_hash = (
        next(iter(evidence_packet_hashes.values()))
        if len(ranking.scores) == 1 and len(evidence_packet_hashes) == 1
        else None
    )

    exports_dir = Path("exports") / as_of
    ranking_json_path, ranking_csv_path = export_ranking_files(
        ranking,
        as_of,
        engine.RANKING_MODEL_VERSION,
        exports_dir,
        evidence_packet_hash=evidence_packet_hash,
        evidence_packet_hashes=evidence_packet_hashes,
    )
    # Export auditable DCF artefacts alongside the heuristic ranking —
    # keeps valuation_score and DCF fair-value clearly separate.
    try:
        dcf_export: dict[str, dict] = {}
        for company in companies:
            loaded = results_by_company.get(company.id, {})
            rd = loaded.get("reverse_dcf") or {}
            if rd:
                # Keep only serializable, auditable fields
                dcf_export[company.ticker or str(company.id)] = rd
        dcf_path = exports_dir / "dcf.json"
        dcf_path.write_text(json.dumps(dcf_export, indent=2, ensure_ascii=False, default=str))
    except Exception as exc:
        print(f"rank: DCF export failed, {dcf_path} not written: {exc}", file=sys.stderr)

    # Save ranking run to DB.
    universe_bytes = json.dumps(sorted([c.ticker for c in companies]), sort_keys=True).encode()
    universe_hash = hashlib.sha256(universe_bytes).hexdigest()

    eligible_count = sum(1 for s in ranking.scores if s.rank_eligible)
    run_id = save_ranking_run(
        conn,
        as_of=as_of,
        model_version=engine.RANKING_MODEL_VERSION,
        packet_hash=evidence_packet_hash,
        universe_hash=universe_hash,
        company_count=len(ranking.scores),
        eligible_count=eligible_count,
        scores=[asdict(s) for s in ranking.scores],
        inputs_summary={
            "ranking_type": "deterministic_watchlist",
            "total_companies": len(companies),
            "eligible_count": eligible_count,
            "ranking_models_used": list({s.ranking_model for s in ranking.scores}),
            "evidence_packet_hashes": evidence_packet_hashes,
        },
    )

    print(
        f"rank: {len(ranking.scores)} companies ranked, "
        f"{eligible_count} eligible, "
        f"exports written to {exports_dir}/",
        file=sys.stderr,
    )
    print(f"ranking_run_id={run_id}")
    print(f"evidence_packet_hashes={json.dumps(evidence_packet_hashes, sort_keys=True)}")
    return 0


def _company_id_from_args(conn, args: argparse.Namespace) -> int | None:
    if args.company_id is not None:
        row = conn.execute(
            "SELECT id FROM companies WHERE id=?", (int(args.company_id),)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT id FROM companies WHERE ticker=? COLLATE NOCASE", (args.ticker,)
        ).fetchone()
    return int(row[0]) if row else None


def cmd_mfn_map_seed(args: argparse.Namespace) -> int:
    from alphaforge.config import Settings
    from alphaforge.db.connection import get_connection
    from alphaforge.db.migrations import migrate
    from alphaforge.providers.mfn.issuer import apply_reviewed_mapping_seed

    settings = Settings.from_env(dsn=args.dsn) if args.dsn else Settings.from_env()
    conn = get_connection(settings)
    migrate(conn)
    try:
        applied = apply_reviewed_mapping_seed(conn, args.file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"mfn-map-seed failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "complete", "applied": applied}, sort_keys=True))
    return 0


def cmd_mfn_map(args: argparse.Namespace) -> int:
    from alphaforge.config import Settings
    from alphaforge.db.connection import get_connection
    from alphaforge.db.migrations import migrate
    from alphaforge.db.repositories import upsert_mfn_issuer_mapping

    settings = Settings.from_env(dsn=args.dsn) if args.dsn else Settings.from_env()
    conn = get_connection(settings)
    migrate(conn)
    company_id = _company_id_from_args(conn, args)
    if company_id is None:
        print("mfn-map failed: company not found", file=sys.stderr)
        return 1
    try:
        evidence = json.loads(args.identity_evidence) if args.identity_evidence else None
        upsert_mfn_issuer_mapping(
            conn,
            company_id,
            status=args.status,
            mfn_slug=args.slug,
            source_url=args.source_url,
            discovery_source=args.discovery_source,
            verified_at=(
                args.verified_at
                or datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
                if args.status == "mapped"
                else None
            ),
            identity_evidence=evidence,
            reviewed=args.status == "ambiguous",
        )
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"mfn-map failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"status": "complete", "company_id": company_id}, sort_keys=True))
    return 0


def cmd_evidence(args: argparse.Namespace) -> int:
    from alphaforge.config import Settings
    from alphaforge.db.connection import get_connection
    from alphaforge.db.migrations import migrate
    from alphaforge.evidence.flow import EvidenceResourceLimits, OneCompanyEvidenceFlow

    settings = Settings.from_env(dsn=args.dsn) if args.dsn else Settings.from_env()
    conn = get_connection(settings)
    migrate(conn)
    company_id = _company_id_from_args(conn, args)
    if company_id is None:
        print(json.dumps({"status": "company_missing"}, sort_keys=True))
        return 1
    limits = EvidenceResourceLimits(
        max_pdf_bytes=args.max_pdf_bytes,
        max_pages=args.max_pages,
        max_retries=args.max_retries,
    )
    flow = OneCompanyEvidenceFlow(conn, limits=limits)
    shadow_citation = None
    if args.shadow_source_id and args.shadow_anchor and args.shadow_excerpt:
        shadow_citation = {
            "source_id": args.shadow_source_id,
            "anchor": args.shadow_anchor,
            "excerpt": args.shadow_excerpt,
        }
    result = flow.run(
        company_id,
        as_of=args.as_of,
        dry_run=args.dry_run,
        shadow_citation=shadow_citation,
        shadow_claim=args.shadow_claim,
        shadow_missing_item=args.shadow_missing_item,
        shadow_specialist_requirement=args.shadow_specialist_requirement,
    )
    output = result.diagnostic()
    if args.diagnostic and result.packet is not None:
        output["sources"] = len(result.packet.get("sources", []))
        output["limitations"] = result.packet.get("limitations", [])
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    if result.status in {"complete", "dry_run", "no_evidence"}:
        return 0
    if result.status.startswith("mapping_"):
        return 2
    return 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="alphaforge")
    p.add_argument("--dsn", dest="dsn", default=None, help="ALPHAFORGE_DSN override")
    sub = p.add_subparsers(dest="command", required=True)

    imp = sub.add_parser("import-watchlist", help="Import watchlist CSV")
    imp.add_argument("--file", required=True, help="CSV path")
    imp.add_argument("--source-file", default=None, help="source_file key for UNIQUE")
    imp.set_defaults(func=cmd_import_watchlist)

    sync = sub.add_parser("sync", help="Sync Börsdata data")
    g = sync.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="Sync all companies/watchlist")
    g.add_argument("--company", default=None, help="Company ticker")
    g.add_argument("--ticker", default=None, help="Ticker alias for --company")
    sync.add_argument(
        "--as-of",
        dest="as_of",
        default=None,
        help="as_of YYYY-MM-DD for PIT filtering (stored but sync fetches full)",
    )
    sync.add_argument("--allow-empty-companies", action="store_true", help=argparse.SUPPRESS)
    sync.set_defaults(func=cmd_sync)

    rank = sub.add_parser(
        "rank",
        help="Rank watchlist companies (exports full universe with eligibility flags)",
    )
    rank.add_argument(
        "--as-of",
        dest="as_of",
        required=True,
        help="as_of YYYY-MM-DD for ranking",
    )
    rank.add_argument(
        "--watchlist",
        default=None,
        help="Watchlist CSV path (optional, uses DB watchlist if omitted)",
    )
    rank.set_defaults(func=cmd_rank)

    mfn_seed = sub.add_parser(
        "mfn-map-seed",
        help="Apply an explicit reviewed company_id to MFN issuer mapping JSON seed",
    )
    mfn_seed.add_argument("--file", required=True, help="Reviewed JSON mapping seed")
    mfn_seed.set_defaults(func=cmd_mfn_map_seed)

    mfn_map = sub.add_parser("mfn-map", help="Persist one explicit MFN issuer mapping decision")
    map_scope = mfn_map.add_mutually_exclusive_group(required=True)
    map_scope.add_argument("--company-id", type=int)
    map_scope.add_argument("--ticker")
    mfn_map.add_argument("--status", choices=("mapped", "ambiguous", "unmapped"), default="mapped")
    mfn_map.add_argument("--slug", default=None)
    mfn_map.add_argument("--source-url", default=None)
    mfn_map.add_argument("--verified-at", default=None)
    mfn_map.add_argument("--discovery-source", default="operator_mapping")
    mfn_map.add_argument("--identity-evidence", default=None, help="JSON identity evidence")
    mfn_map.set_defaults(func=cmd_mfn_map)

    evidence = sub.add_parser(
        "evidence",
        help="Run one-company deterministic MFN PDF evidence flow",
    )
    evidence_scope = evidence.add_mutually_exclusive_group(required=True)
    evidence_scope.add_argument("--company-id", type=int)
    evidence_scope.add_argument("--ticker")
    evidence.add_argument("--as-of", required=True, help="Point-in-time cutoff YYYY-MM-DD")
    evidence.add_argument(
        "--dry-run", action="store_true", help="Discover and diagnose without writes"
    )
    evidence.add_argument("--diagnostic", action="store_true", help="Include packet diagnostics")
    evidence.add_argument("--max-pdf-bytes", type=int, default=25 * 1024 * 1024)
    evidence.add_argument("--max-pages", type=int, default=50)
    evidence.add_argument("--max-retries", type=int, default=3)
    evidence.add_argument("--shadow-source-id", default=None)
    evidence.add_argument("--shadow-anchor", default=None)
    evidence.add_argument("--shadow-excerpt", default=None)
    evidence.add_argument("--shadow-claim", default=None)
    evidence.add_argument("--shadow-missing-item", default=None)
    evidence.add_argument("--shadow-specialist-requirement", default=None)
    evidence.set_defaults(func=cmd_evidence)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
