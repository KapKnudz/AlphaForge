"""Repository helpers — ON CONFLICT DO UPDATE idempotence, jobs, mfn checks."""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def upsert_company(conn: sqlite3.Connection, borsdata_ins: dict[str, Any]) -> int:
    """Upsert companies row; return company id."""
    # Börsdata instrument fields vary; normalize
    borsdata_id = (
        borsdata_ins.get("insId") or borsdata_ins.get("id") or borsdata_ins.get("borsdata_id")
    )
    name = borsdata_ins.get("name") or borsdata_ins.get("companyName") or ""
    ticker = borsdata_ins.get("ticker")
    isin = borsdata_ins.get("isin")
    instrument = int(borsdata_ins.get("instrument") or 0)
    sector_id = borsdata_ins.get("sectorId") or borsdata_ins.get("sector_id")
    branch_id = borsdata_ins.get("branchId") or borsdata_ins.get("branch_id")
    market_id = borsdata_ins.get("marketId") or borsdata_ins.get("market_id")
    country_id = borsdata_ins.get("countryId") or borsdata_ins.get("country_id")
    listing_date = borsdata_ins.get("listingDate")
    stock_price_currency = borsdata_ins.get("stockPriceCurrency")
    report_currency = borsdata_ins.get("reportCurrency")
    raw_payload = json.dumps(borsdata_ins, ensure_ascii=False)
    conn.execute(
        """
        INSERT INTO companies
            (borsdata_id, name, ticker, isin, instrument, sector_id, branch_id, market_id, country_id, listing_date, stock_price_currency, report_currency, raw_payload)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(borsdata_id) DO UPDATE SET
            name=excluded.name,
            ticker=excluded.ticker,
            isin=excluded.isin,
            instrument=excluded.instrument,
            sector_id=excluded.sector_id,
            branch_id=excluded.branch_id,
            market_id=excluded.market_id,
            country_id=excluded.country_id,
            listing_date=excluded.listing_date,
            stock_price_currency=excluded.stock_price_currency,
            report_currency=excluded.report_currency,
            raw_payload=excluded.raw_payload,
            updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
        """,
        (
            borsdata_id,
            name,
            ticker,
            isin,
            instrument,
            sector_id,
            branch_id,
            market_id,
            country_id,
            listing_date,
            stock_price_currency,
            report_currency,
            raw_payload,
        ),
    )
    conn.commit()
    cur = conn.execute("SELECT id FROM companies WHERE borsdata_id=?", (borsdata_id,))
    row = cur.fetchone()
    return int(row[0]) if row else 0


def upsert_financial_periods(
    conn: sqlite3.Connection, company_id: int, periods: list[dict[str, Any]]
) -> int:
    count = 0
    for p in periods:
        # Use-core kpi_taxonomy to map? Keep raw mapping here minimal
        # Determine is_placeholder: revenue 0.0 + report_Date null → placeholder
        revenue = p.get("revenues")
        report_date = p.get("report_Date") or p.get("reportDate")
        is_placeholder = 1 if (revenue == 0.0 or revenue == 0) and report_date is None else 0
        # If is_placeholder and all core financials are null/0 → quarantine
        # Respect plan: is_placeholder=1 rows never enter ranking/valuation (WHERE is_placeholder=0)
        # Also handle fx
        currency = p.get("currency")
        currency_ratio = (
            p.get("currency_Ratio") if "currency_Ratio" in p else p.get("currency_ratio")
        )
        fx_rate_to_sek = currency_ratio
        fx_source = None
        if fx_rate_to_sek is not None:
            try:
                if float(fx_rate_to_sek) > 0:  # type: ignore[arg-type]
                    fx_source = "currency_ratio"
                else:
                    fx_rate_to_sek = None
            except (TypeError, ValueError):
                fx_rate_to_sek = None
        else:
            # If reportCurrency != stockPriceCurrency but ratio null → keep null, fallback manual later
            pass
        raw_payload = json.dumps(p, ensure_ascii=False)
        # Map fields via taxonomy where possible
        from alphaforge.core.kpi_taxonomy import REPORT_FIELD_MAP

        mapped: dict[str, Any] = {}
        for k, v in p.items():
            canon = REPORT_FIELD_MAP.get(k)
            if canon:
                mapped[canon] = v
        # period_type / period_end handling — caller supplies period_type if not in payload
        period_type = p.get("period_type") or mapped.get("period_type") or "year"
        period_end = (
            p.get("period_End") or p.get("period_end") or p.get("report_Date") or p.get("date")
        )
        # Fallback: use report_year/period to synthesize period_end if missing → skip
        if not period_end:
            # Try to derive from year/period for quarantine check
            if is_placeholder:
                # For placeholder rows, use a synthetic period_end to allow quarantine visibility
                # Use report year or current placeholder key
                ry = p.get("year") or p.get("report_year") or 0
                rp = p.get("period") or p.get("report_period") or 0
                period_end = f"{ry:04d}-{rp:02d}-01" if ry else None
            if not period_end:
                continue
        # Normalize to YYYY-MM-DD
        if isinstance(period_end, str) and len(period_end) > 10:
            period_end = period_end[:10]
        # revenue etc already extracted via mapped or raw
        revenue_val = mapped.get("revenue") if "revenue" in mapped else p.get("revenues")
        if revenue_val == 0.0 and is_placeholder:
            pass
        # Use mapped for other financials
        conn.execute(
            """
            INSERT INTO financial_periods
                (company_id, period_type, period_end, report_year, report_period, report_date, broken_fiscal_year, currency, currency_ratio, fx_rate_to_sek, fx_source, revenue, gross_income, operating_profit, ebit, ebitda, net_income, free_cash_flow, operating_cash_flow, investing_cash_flow, financing_cash_flow, equity, total_assets, total_debt, cash, eps, dividend_per_share, shares_outstanding, is_placeholder, raw_payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(company_id, period_type, period_end) DO UPDATE SET
                report_year=excluded.report_year,
                report_period=excluded.report_period,
                report_date=excluded.report_date,
                broken_fiscal_year=excluded.broken_fiscal_year,
                currency=excluded.currency,
                currency_ratio=excluded.currency_ratio,
                fx_rate_to_sek=excluded.fx_rate_to_sek,
                fx_source=excluded.fx_source,
                revenue=excluded.revenue,
                gross_income=excluded.gross_income,
                operating_profit=excluded.operating_profit,
                ebit=excluded.ebit,
                ebitda=excluded.ebitda,
                net_income=excluded.net_income,
                free_cash_flow=excluded.free_cash_flow,
                operating_cash_flow=excluded.operating_cash_flow,
                investing_cash_flow=excluded.investing_cash_flow,
                financing_cash_flow=excluded.financing_cash_flow,
                equity=excluded.equity,
                total_assets=excluded.total_assets,
                total_debt=excluded.total_debt,
                cash=excluded.cash,
                eps=excluded.eps,
                dividend_per_share=excluded.dividend_per_share,
                shares_outstanding=excluded.shares_outstanding,
                is_placeholder=excluded.is_placeholder,
                raw_payload=excluded.raw_payload
            """,
            (
                company_id,
                period_type,
                period_end,
                p.get("year") or p.get("report_year"),
                p.get("period") or p.get("report_period"),
                report_date,
                p.get("broken_Fiscal_Year") or p.get("broken_fiscal_year"),
                currency,
                currency_ratio,
                fx_rate_to_sek,
                fx_source,
                mapped.get("revenue", p.get("revenues")),
                mapped.get("gross_income", p.get("gross_Income")),
                mapped.get("operating_profit", p.get("operating_Income")),
                mapped.get("ebit", p.get("ebit")),
                mapped.get("ebitda", p.get("ebitda")),
                mapped.get("net_income", p.get("profit_To_Equity_Holders")),
                mapped.get("free_cash_flow", p.get("free_Cash_Flow")),
                mapped.get("operating_cash_flow", p.get("operating_Cash_Flow")),
                mapped.get("investing_cash_flow", p.get("investing_Cash_Flow")),
                mapped.get("financing_cash_flow", p.get("financing_Cash_Flow")),
                mapped.get("equity", p.get("book_Value")),
                p.get("total_Assets"),
                p.get("total_Debt"),
                p.get("cash_And_Equivalents"),
                mapped.get("eps", p.get("earnings_Per_Share")),
                mapped.get("dividend_per_share", p.get("dividend")),
                mapped.get("shares_outstanding", p.get("number_Of_Shares")),
                is_placeholder,
                raw_payload,
            ),
        )
        count += 1
    conn.commit()
    return count


def upsert_prices(
    conn: sqlite3.Connection,
    company_id: int,
    rows: list[dict[str, Any]],
    *,
    currency: str | None = None,
) -> int:
    count = 0
    for r in rows:
        price_date = r.get("price_Date") or r.get("price_date") or r.get("d") or r.get("date")
        close = r.get("close") or r.get("c") or r.get("price")
        volume = r.get("volume") or r.get("vol") or r.get("v")
        if price_date is None or close is None:
            continue
        if isinstance(price_date, str) and len(price_date) > 10:
            price_date = price_date[:10]
        cur_currency = r.get("currency") or currency
        conn.execute(
            """
            INSERT INTO prices (company_id, price_date, close, volume, currency)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(company_id, price_date) DO UPDATE SET
                close=excluded.close, volume=excluded.volume, currency=excluded.currency
            """,
            (
                company_id,
                price_date,
                float(close),
                int(volume) if volume is not None else None,
                cur_currency,
            ),
        )
        count += 1
    conn.commit()
    return count


def upsert_dividends(conn: sqlite3.Connection, company_id: int, rows: list[dict[str, Any]]) -> int:
    count = 0
    for r in rows:
        ex_date = r.get("exDate") or r.get("ex_date") or r.get("date")
        amount = r.get("amountPaid") if "amountPaid" in r else r.get("amount")
        if ex_date is None or amount is None:
            continue
        try:
            if float(amount) == 0.0:
                continue
        except (TypeError, ValueError):
            pass
        if isinstance(ex_date, str) and len(ex_date) > 10:
            ex_date = ex_date[:10]
        currency = r.get("currency") or "SEK"
        dividend_type = int(
            r.get("dividendType") if "dividendType" in r else r.get("dividend_type", 0)
        )
        distribution_frequency = r.get("distributionFrequency")
        conn.execute(
            """
            INSERT INTO dividends (company_id, ex_date, amount, currency, dividend_type, distribution_frequency)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(company_id, ex_date, dividend_type, amount) DO NOTHING
            """,
            (company_id, ex_date, float(amount), currency, dividend_type, distribution_frequency),
        )
        count += 1
    conn.commit()
    return count


def upsert_kpi_observations(
    conn: sqlite3.Connection,
    company_id: int,
    kpi_id: int,
    period_type: str,
    price_type: str,
    rows: list[dict[str, Any]],
) -> int:
    count = 0
    for r in rows:
        val = r.get("v") if "v" in r else r.get("value")
        if val is None:
            continue
        try:
            val_f = float(val)
        except (TypeError, ValueError):
            continue
        # v null-filtered client-side
        if r.get("v") is None and "v" in r:
            # keep null-filtered?
            pass
        year = r.get("year")
        report_period = r.get("reportPeriod") or r.get("report_period")
        observation_date = r.get("observationDate") or r.get("observation_date") or r.get("date")
        if period_type == "last":
            if not observation_date:
                observation_date = r.get("date") or r.get("observation_date")
            if not observation_date:
                continue
            if isinstance(observation_date, str) and len(observation_date) > 10:
                observation_date = observation_date[:10]
            conn.execute(
                """
                INSERT INTO kpi_observations (company_id, kpi_id, period_type, price_type, observation_date, value)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(company_id, kpi_id, period_type, price_type, observation_date)
                WHERE period_type='last' DO UPDATE SET value=excluded.value
                """,
                (company_id, kpi_id, period_type, price_type, observation_date, val_f),
            )
        else:
            if year is None:
                continue
            conn.execute(
                """
                INSERT INTO kpi_observations (company_id, kpi_id, period_type, price_type, year, report_period, value)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(company_id, kpi_id, period_type, price_type, year, report_period)
                WHERE period_type IN ('year','r12') DO UPDATE SET value=excluded.value
                """,
                (
                    company_id,
                    kpi_id,
                    period_type,
                    price_type,
                    int(year),
                    int(report_period) if report_period is not None else None,
                    val_f,
                ),
            )
        count += 1
    conn.commit()
    return count


def upsert_news_release(conn: sqlite3.Connection, company_id: int, article: dict[str, Any]) -> None:
    url = article.get("url") or article.get("source_url") or ""
    if not url:
        return
    title = article.get("title") or ""
    body = article.get("body") or article.get("content_text")
    published_at = article.get("published_at")
    lang = article.get("lang")
    release_category = article.get("release_category")
    conn.execute(
        """
        INSERT INTO news_releases (company_id, url, title, body, published_at, lang, release_category)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(url) DO NOTHING
        """,
        (company_id, url, title, body, published_at, lang, release_category),
    )
    # commit left to caller


def record_mfn_feed_check(
    conn: sqlite3.Connection, company_id: int, mfn_slug: str, discovered: int, unseen: int
) -> None:
    conn.execute(
        "INSERT INTO mfn_feed_checks (company_id, mfn_slug, discovered_count, unseen_count) VALUES (?, ?, ?, ?)",
        (company_id, mfn_slug, discovered, unseen),
    )


def upsert_stock_splits(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    company_map: dict[int, int] | None = None,
) -> int:
    count = 0
    for r in rows:
        borsdata_id = r.get("insId") or r.get("instrumentId") or r.get("borsdata_id")
        split_type = r.get("splitType") or r.get("type") or "S"
        ratio = r.get("ratio") or r.get("splitRatio") or "1:1"
        split_date = r.get("splitDate") or r.get("date")
        if borsdata_id is None or split_date is None:
            continue
        if isinstance(split_date, str) and len(split_date) > 10:
            split_date = split_date[:10]
        try:
            borsdata_id_int = int(borsdata_id)
        except (TypeError, ValueError):
            continue
        company_id = None
        if company_map is not None:
            company_id = company_map.get(borsdata_id_int)
        else:
            cur = conn.execute("SELECT id FROM companies WHERE borsdata_id=?", (borsdata_id_int,))
            row = cur.fetchone()
            company_id = int(row[0]) if row else None
        conn.execute(
            """
            INSERT INTO stock_splits (company_id, borsdata_id, split_type, ratio, split_date)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(borsdata_id, split_date) DO UPDATE SET
                split_type=excluded.split_type, ratio=excluded.ratio, company_id=excluded.company_id
            """,
            (company_id, borsdata_id_int, split_type, str(ratio), split_date),
        )
        count += 1
    conn.commit()
    return count


def upsert_report_calendar(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    company_map: dict[int, int] | None = None,
) -> int:
    count = 0
    for r in rows:
        borsdata_id = r.get("insId") or r.get("instrumentId") or r.get("borsdata_id")
        release_date = r.get("releaseDate") or r.get("date")
        report_type = r.get("reportType") or r.get("type") or "Q1"
        if borsdata_id is None or release_date is None:
            continue
        if isinstance(release_date, str) and len(release_date) > 10:
            release_date = release_date[:10]
        if report_type not in ("Q1", "Q2", "Q3", "Q4"):
            # Normalize
            report_type = str(report_type).upper()
            if report_type not in ("Q1", "Q2", "Q3", "Q4"):
                continue
        try:
            borsdata_id_int = int(borsdata_id)
        except (TypeError, ValueError):
            continue
        company_id = None
        if company_map is not None:
            company_id = company_map.get(borsdata_id_int)
        else:
            cur = conn.execute("SELECT id FROM companies WHERE borsdata_id=?", (borsdata_id_int,))
            row = cur.fetchone()
            company_id = int(row[0]) if row else None
        conn.execute(
            """
            INSERT INTO report_calendar (company_id, borsdata_id, release_date, report_type)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(borsdata_id, release_date) DO UPDATE SET
                report_type=excluded.report_type, company_id=excluded.company_id
            """,
            (company_id, borsdata_id_int, release_date, report_type),
        )
        count += 1
    conn.commit()
    return count


def record_job(
    conn: sqlite3.Connection,
    job_type: str,
    *,
    company_id: int | None,
    borsdata_id: int | None,
    status: str,
    error: dict[str, Any] | None = None,
) -> None:
    import json

    err_json = json.dumps(error) if error else None
    conn.execute(
        """
        INSERT INTO jobs (job_type, company_id, borsdata_id, status, attempt, error)
        VALUES (?, ?, ?, ?, 1, ?)
        ON CONFLICT(job_type, company_id) DO UPDATE SET
            status=excluded.status,
            borsdata_id=excluded.borsdata_id,
            attempt=jobs.attempt + 1,
            error=excluded.error,
            finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
        """,
        (job_type, company_id, borsdata_id, status, err_json),
    )
    conn.commit()
