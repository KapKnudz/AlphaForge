"""Repository helpers — ON CONFLICT DO UPDATE idempotence, jobs, mfn checks."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def upsert_company(conn: Any, borsdata_ins: dict[str, Any]) -> int:
    """Upsert companies row; return company id."""
    # Börsdata instrument fields vary; normalize
    borsdata_id = (
        borsdata_ins.get("insId") or borsdata_ins.get("id") or borsdata_ins.get("borsdata_id")
    )
    name = borsdata_ins.get("name") or borsdata_ins.get("companyName") or ""
    ticker = borsdata_ins.get("ticker")
    isin = borsdata_ins.get("isin")
    raw_instrument = borsdata_ins.get("instrument")
    # The live API has returned instrument values outside the schema's 0/1
    # contract. Preserve the provider payload, but store a strict preference
    # flag (zero is false; every other provider value is true).
    if isinstance(raw_instrument, str):
        instrument = 0 if raw_instrument.strip().lower() in {"", "0", "false", "no"} else 1
    else:
        instrument = int(bool(raw_instrument))
    sector_id = borsdata_ins.get("sectorId") or borsdata_ins.get("sector_id")
    branch_id = borsdata_ins.get("branchId") or borsdata_ins.get("branch_id")
    market_id = borsdata_ins.get("marketId") or borsdata_ins.get("market_id")
    country_id = borsdata_ins.get("countryId") or borsdata_ins.get("country_id")
    listing_date = borsdata_ins.get("listingDate")
    stock_price_currency = borsdata_ins.get("stockPriceCurrency") or borsdata_ins.get(
        "stock_price_currency"
    )
    report_currency = borsdata_ins.get("reportCurrency") or borsdata_ins.get("report_currency")
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


def relink_watchlist(conn: Any) -> int:
    """Match imported rows after instruments have been seeded.

    The original source row remains untouched; only its foreign key and match
    provenance are filled in.  Matching is deliberately deterministic and
    uses the same precedence as watchlist import.
    """
    rows = conn.execute(
        "SELECT id, isin, ticker, borsdata_id FROM watchlist WHERE company_id IS NULL"
    ).fetchall()
    linked = 0
    for row in rows:
        company_id = None
        matched_via = None
        if row[1]:
            found = conn.execute("SELECT id FROM companies WHERE isin=?", (row[1],)).fetchone()
            if found:
                company_id, matched_via = int(found[0]), "isin"
        if company_id is None and row[2]:
            found = conn.execute(
                "SELECT id FROM companies WHERE ticker=? COLLATE NOCASE", (row[2],)
            ).fetchone()
            if found:
                company_id, matched_via = int(found[0]), "ticker"
        if company_id is None and row[3] is not None:
            found = conn.execute(
                "SELECT id FROM companies WHERE borsdata_id=?", (row[3],)
            ).fetchone()
            if found:
                company_id, matched_via = int(found[0]), "borsdata_id"
        if company_id is None:
            continue
        try:
            conn.execute(
                "UPDATE watchlist SET company_id=?, matched_via=? WHERE id=?",
                (company_id, matched_via, int(row[0])),
            )
            linked += 1
        except Exception as exc:
            # A company may already be represented by another source row. Keep
            # the original unmatched row rather than deleting source data.
            if exc.__class__.__name__ != "IntegrityError":
                raise
            continue
    conn.commit()
    return linked


def upsert_financial_periods(
    conn: Any, company_id: int, periods: list[dict[str, Any]]
) -> int:
    count = 0
    for p in periods:
        # Use-core kpi_taxonomy to map? Keep raw mapping here minimal
        # Determine is_placeholder: revenue 0.0 + report_Date null → placeholder
        revenue = p.get("revenues")
        report_date = p.get("report_Date") or p.get("reportDate") or p.get("ReportDate")
        is_placeholder = 1 if (revenue == 0.0 or revenue == 0) and report_date is None else 0
        # If is_placeholder and all core financials are null/0 → quarantine
        # Respect plan: is_placeholder=1 rows never enter ranking/valuation (WHERE is_placeholder=0)
        # Also handle fx
        currency = p.get("currency")
        currency_ratio = (
            p.get("currency_Ratio")
            if "currency_Ratio" in p
            else p.get("currency_ratio") or p.get("currencyRatio")
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
            p.get("period_End")
            or p.get("report_End_Date")
            or p.get("period_end")
            or p.get("periodEnd")
            or p.get("report_Date")
            or p.get("reportDate")
            or p.get("date")
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
    conn: Any,
    company_id: int,
    rows: list[dict[str, Any]],
    *,
    currency: str | None = None,
) -> int:
    count = 0
    for r in rows:
        price_date = r.get("price_Date") or r.get("price_date") or r.get("d") or r.get("date")
        close = next(
            (r.get(key) for key in ("close", "c", "price") if r.get(key) is not None),
            None,
        )
        volume = next(
            (r.get(key) for key in ("volume", "vol", "v") if r.get(key) is not None),
            None,
        )
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


def upsert_dividends(conn: Any, company_id: int, rows: list[dict[str, Any]]) -> int:
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
        currency = r.get("currency") or r.get("currencyShortName") or "SEK"
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
    conn: Any,
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
        year = r.get("year") if "year" in r else r.get("y")
        report_period = r.get("reportPeriod") or r.get("report_period") or r.get("p")
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
            year_int = int(year)
            report_period_int = int(report_period) if report_period is not None else None
            if report_period_int is None:
                existing = conn.execute(
                    """
                    SELECT id FROM kpi_observations
                    WHERE company_id=? AND kpi_id=? AND period_type=? AND price_type=?
                      AND year=? AND report_period IS NULL
                    LIMIT 1
                    """,
                    (company_id, kpi_id, period_type, price_type, year_int),
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE kpi_observations SET value=? WHERE id=?",
                        (val_f, int(existing[0])),
                    )
                    count += 1
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
                    year_int,
                    report_period_int,
                    val_f,
                ),
            )
        count += 1
    conn.commit()
    return count


def upsert_news_release(conn: Any, company_id: int, article: dict[str, Any]) -> None:
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
    conn: Any, company_id: int, mfn_slug: str, discovered: int, unseen: int
) -> None:
    conn.execute(
        "INSERT INTO mfn_feed_checks (company_id, mfn_slug, discovered_count, unseen_count) VALUES (?, ?, ?, ?)",
        (company_id, mfn_slug, discovered, unseen),
    )


def upsert_stock_splits(
    conn: Any,
    rows: list[dict[str, Any]],
    *,
    company_map: dict[int, int] | None = None,
) -> int:
    count = 0
    for r in rows:
        borsdata_id = r.get("insId") or r.get("instrumentId") or r.get("borsdata_id")
        split_type = str(r.get("splitType") or r.get("type") or "S").upper()
        split_type = {"SPLIT": "S", "FORWARD": "S", "REVERSE": "RS", "REVERSE_SPLIT": "RS"}.get(
            split_type, split_type
        )
        ratio = r.get("ratio") or r.get("splitRatio") or "1:1"
        split_date = r.get("splitDate") or r.get("date")
        if borsdata_id is None or split_date is None or split_type not in {"S", "RS"}:
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
    conn: Any,
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
    conn: Any,
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


def save_ranking_run(
    conn: Any,
    *,
    as_of: str,
    model_version: str,
    packet_hash: str | None,
    universe_hash: str | None,
    company_count: int,
    eligible_count: int,
    scores: list[dict[str, Any]],
    inputs_summary: dict[str, Any] | None = None,
) -> int:
    import json as _json

    scores_json = _json.dumps(scores, ensure_ascii=False)
    inputs_json = _json.dumps(inputs_summary, ensure_ascii=False) if inputs_summary else None
    conn.execute(
        """
        INSERT INTO ranking_runs
            (as_of, model_version, packet_hash, universe_hash, company_count, eligible_count, scores, inputs_summary)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            as_of,
            model_version,
            packet_hash,
            universe_hash,
            company_count,
            eligible_count,
            scores_json,
            inputs_json,
        ),
    )
    conn.commit()
    cur = conn.execute("SELECT last_insert_rowid()")
    row = cur.fetchone()
    return int(row[0]) if row else 0


def _has_structured_mfn_identity_evidence(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    provenance = value.get("provenance")
    reason = value.get("reason")
    return (
        isinstance(provenance, str)
        and bool(provenance.strip())
        and isinstance(reason, str)
        and bool(reason.strip())
    )


def upsert_mfn_issuer_mapping(
    conn: Any,
    company_id: int,
    *,
    status: str,
    mfn_slug: str | None = None,
    source_url: str | None = None,
    discovery_source: str,
    verified_at: str | None = None,
    identity_evidence: dict[str, Any] | list[Any] | None = None,
) -> int:
    """Persist an explicit MFN identity decision keyed by ``companies.id``.

    This function intentionally does not infer a slug. Callers must provide a
    mapping decision and its evidence; unresolved or ambiguous decisions can
    be stored without creating a usable report-ingestion link.
    """
    if status not in {"mapped", "ambiguous", "unmapped"}:
        raise ValueError(f"invalid MFN mapping status: {status}")
    if status == "mapped" and not (mfn_slug and source_url and verified_at):
        raise ValueError("a mapped MFN issuer requires slug, source_url, and verified_at")
    if status == "mapped" and not _has_structured_mfn_identity_evidence(identity_evidence):
        raise ValueError("a mapped MFN issuer requires structured provenance and reason")
    evidence_json = (
        json.dumps(identity_evidence, ensure_ascii=False, sort_keys=True)
        if identity_evidence is not None
        else None
    )
    conn.execute(
        """
        INSERT INTO mfn_issuer_mappings
            (company_id, mfn_slug, source_url, status, discovery_source, verified_at, identity_evidence)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(company_id) DO UPDATE SET
            mfn_slug=excluded.mfn_slug,
            source_url=excluded.source_url,
            status=excluded.status,
            discovery_source=excluded.discovery_source,
            verified_at=excluded.verified_at,
            identity_evidence=excluded.identity_evidence,
            last_checked_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
        """,
        (
            company_id,
            mfn_slug,
            source_url,
            status,
            discovery_source,
            verified_at,
            evidence_json,
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM mfn_issuer_mappings WHERE company_id=?", (company_id,)
    ).fetchone()
    return int(row[0]) if row else 0


def get_verified_mfn_mapping(conn: Any, company_id: int) -> dict[str, Any] | None:
    """Return only a complete, reviewed mapping suitable for ingestion."""
    row = conn.execute(
        """
        SELECT company_id, mfn_slug, source_url, status, discovery_source,
               verified_at, identity_evidence, last_checked_at
        FROM mfn_issuer_mappings
        WHERE company_id=? AND status='mapped'
          AND mfn_slug IS NOT NULL AND source_url IS NOT NULL AND verified_at IS NOT NULL
        """,
        (company_id,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    raw_evidence = result.get("identity_evidence")
    if isinstance(raw_evidence, str):
        try:
            result["identity_evidence"] = json.loads(raw_evidence)
        except ValueError:
            result["identity_evidence"] = None
    if not _has_structured_mfn_identity_evidence(result.get("identity_evidence")):
        return None
    return result


def get_mfn_mapping_review(conn: Any, company_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM mfn_issuer_mappings WHERE company_id=?", (company_id,)
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["candidates"] = [
        dict(candidate)
        for candidate in conn.execute(
            """
            SELECT id, mfn_slug, source_url, discovery_source, match_basis,
                   identity_evidence, status, discovered_at
            FROM mfn_issuer_candidates
            WHERE company_id=? ORDER BY mfn_slug, source_url, id
            """,
            (company_id,),
        ).fetchall()
    ]
    return result


def persist_mfn_issuer_candidates(
    conn: Any,
    company_id: int,
    candidates: list[dict[str, Any]],
    *,
    discovery_source: str,
) -> int:
    """Store bounded candidate discoveries for operator review."""
    count = 0
    for candidate in candidates:
        slug = str(candidate.get("mfn_slug") or candidate.get("slug") or "").strip()
        source_url = str(candidate.get("source_url") or candidate.get("url") or "").strip()
        if not slug or not source_url:
            continue
        evidence = candidate.get("identity_evidence")
        evidence_json = (
            json.dumps(evidence, ensure_ascii=False, sort_keys=True) if evidence is not None else None
        )
        conn.execute(
            """
            INSERT INTO mfn_issuer_candidates
                (company_id, mfn_slug, source_url, discovery_source, match_basis, identity_evidence)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(company_id, mfn_slug, source_url) DO UPDATE SET
                discovery_source=excluded.discovery_source,
                match_basis=excluded.match_basis,
                identity_evidence=excluded.identity_evidence,
                discovered_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
            """,
            (
                company_id,
                slug,
                source_url,
                discovery_source,
                candidate.get("match_basis"),
                evidence_json,
            ),
        )
        count += 1
    conn.commit()
    return count


def find_research_document(conn: Any, company_id: int, source_url: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM research_documents WHERE company_id=? AND source_url=?",
        (company_id, source_url),
    ).fetchone()
    return dict(row) if row is not None else None


def find_complete_evidence_document(
    conn: Any, company_id: int, source_url: str
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT d.* FROM research_documents d
        WHERE d.company_id=? AND d.source_url=?
          AND EXISTS (
              SELECT 1 FROM research_attachments a
              WHERE a.document_id=COALESCE(d.duplicate_of, d.id)
          )
          AND EXISTS (
              SELECT 1 FROM document_extractions e
              WHERE e.document_id=COALESCE(d.duplicate_of, d.id)
          )
        """,
        (company_id, source_url),
    ).fetchone()
    return dict(row) if row is not None else None


def find_complete_evidence_attachment(
    conn: Any, source_url: str, company_id: int | None = None
) -> dict[str, Any] | None:
    company_clause = " AND d.company_id=?" if company_id is not None else ""
    parameters: tuple[Any, ...] = (source_url, company_id) if company_id is not None else (source_url,)
    row = conn.execute(
        f"""
        SELECT a.* FROM research_attachments a
        JOIN research_documents d ON d.id=a.document_id
        JOIN document_extractions e ON e.document_id=d.id
        WHERE a.source_url=?{company_clause} ORDER BY a.id LIMIT 1
        """,
        parameters,
    ).fetchone()
    return dict(row) if row is not None else None


def find_attachment_by_url(
    conn: Any, source_url: str, company_id: int | None = None
) -> dict[str, Any] | None:
    if company_id is None:
        row = conn.execute(
            "SELECT * FROM research_attachments WHERE source_url=? ORDER BY id LIMIT 1",
            (source_url,),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT a.* FROM research_attachments a
            JOIN research_documents d ON d.id=a.document_id
            WHERE a.source_url=? AND d.company_id=? ORDER BY a.id LIMIT 1
            """,
            (source_url, company_id),
        ).fetchone()
    return dict(row) if row is not None else None


def persist_evidence_document(
    conn: Any,
    *,
    company_id: int,
    article: dict[str, Any],
    attachment: dict[str, Any],
    extraction: dict[str, Any],
    pages: list[dict[str, Any]],
    suppressed_variants: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Persist one selected report and its complete PDF provenance.

    The flow passes structured values to this repository function instead of
    issuing SQL itself. A sibling translation is persisted as an auditable
    document row, but only the selected edition receives attachment/extraction
    rows and enters the frozen packet.
    """
    source_url = str(article.get("source_url") or article.get("url") or "").strip()
    published_at = article.get("published_at")
    if not source_url or not published_at:
        raise ValueError("evidence documents require source_url and authoritative published_at")
    language = article.get("ingested_lang") or article.get("lang") or "en"
    checksum = str(attachment.get("sha256") or "")
    if not checksum:
        raise ValueError("evidence attachment requires sha256")
    metadata = dict(article.get("raw_metadata") or {})
    if not isinstance(metadata, dict):
        metadata = {}
    for key in (
        "mfn_slug",
        "report_kind",
        "report_period",
        "fiscal_period",
        "observation_date",
        "lang_confidence",
    ):
        if article.get(key) is not None:
            metadata[key] = article[key]
    metadata["authoritative_publication_timestamp"] = True
    metadata["attachment_sha256"] = checksum
    metadata["bilingual_selection_rule"] = article.get(
        "bilingual_selection_rule", "deterministic_en_fallback"
    )
    metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    conn.execute("SAVEPOINT evidence_document")
    try:
        conn.execute(
            """
            INSERT INTO research_documents
                (company_id, source_url, source_type, title, published_at, content_text,
                 page_count, pages_included, page_truncated, duplicate_of,
                 ingested_lang, checksum, raw_metadata)
            VALUES (?, ?, 'mfn', ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
            ON CONFLICT(company_id, source_url) DO UPDATE SET
                title=excluded.title,
                published_at=excluded.published_at,
                content_text=excluded.content_text,
                page_count=excluded.page_count,
                pages_included=excluded.pages_included,
                page_truncated=excluded.page_truncated,
                ingested_lang=excluded.ingested_lang,
                checksum=excluded.checksum,
                raw_metadata=excluded.raw_metadata
            """,
            (
                company_id,
                source_url,
                article.get("title"),
                published_at,
                article.get("content_text") or article.get("body"),
                extraction.get("page_count"),
                extraction.get("pages_included"),
                int(bool(extraction.get("page_truncated"))),
                language,
                checksum,
                metadata_json,
            ),
        )
        document = conn.execute(
            "SELECT id FROM research_documents WHERE company_id=? AND source_url=?",
            (company_id, source_url),
        ).fetchone()
        if document is None:
            raise RuntimeError("research document was not persisted")
        document_id = int(document[0])
        conn.execute(
            """
            INSERT INTO research_attachments
                (document_id, source_url, content_type, byte_size, sha256, magic_valid, http_status, raw_metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id, source_url) DO UPDATE SET
                content_type=excluded.content_type,
                byte_size=excluded.byte_size,
                sha256=excluded.sha256,
                magic_valid=excluded.magic_valid,
                http_status=excluded.http_status,
                raw_metadata=excluded.raw_metadata
            """,
            (
                document_id,
                attachment.get("source_url") or article.get("attachment_url") or source_url,
                attachment.get("content_type"),
                int(attachment.get("byte_size") or 0),
                checksum,
                int(bool(attachment.get("magic_valid"))),
                attachment.get("http_status"),
                json.dumps(attachment.get("raw_metadata"), ensure_ascii=False, sort_keys=True)
                if attachment.get("raw_metadata") is not None
                else None,
            ),
        )
        limitations = list(extraction.get("limitations") or [])
        extraction_json = json.dumps(limitations, ensure_ascii=False, sort_keys=True)
        conn.execute(
            """
            INSERT INTO document_extractions
                (document_id, extractor, text_checksum, page_count, pages_included,
                 page_truncated, scanned, limitations)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
                extractor=excluded.extractor,
                text_checksum=excluded.text_checksum,
                page_count=excluded.page_count,
                pages_included=excluded.pages_included,
                page_truncated=excluded.page_truncated,
                scanned=excluded.scanned,
                limitations=excluded.limitations
            """,
            (
                document_id,
                extraction.get("extractor", "pypdf"),
                extraction.get("text_checksum"),
                int(extraction.get("page_count") or 0),
                extraction.get("pages_included"),
                int(bool(extraction.get("page_truncated"))),
                int(bool(extraction.get("scanned"))),
                extraction_json,
            ),
        )
        extraction_row = conn.execute(
            "SELECT id FROM document_extractions WHERE document_id=?", (document_id,)
        ).fetchone()
        if extraction_row is None:
            raise RuntimeError("document extraction was not persisted")
        extraction_id = int(extraction_row[0])
        conn.execute("DELETE FROM document_pages WHERE extraction_id=?", (extraction_id,))
        for page in pages:
            text = str(page.get("text") or "")
            page_number = int(page["page_number"])
            conn.execute(
                """
                INSERT INTO document_pages
                    (extraction_id, page_number, anchor, text, text_checksum)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    extraction_id,
                    page_number,
                    (
                        page.get("anchor")
                        if str(page.get("anchor") or "").startswith("document:")
                        else f"document:{document_id}#page:{page_number}"
                    ),
                    text,
                    hashlib.sha256(text.encode("utf-8")).hexdigest(),
                ),
            )
        for sibling in suppressed_variants or []:
            sibling_url = str(sibling.get("source_url") or sibling.get("url") or "").strip()
            if not sibling_url or sibling_url == source_url:
                continue
            sibling_meta = dict(sibling.get("raw_metadata") or {})
            sibling_meta["duplicate_of_source_url"] = source_url
            sibling_meta["bilingual_selection_rule"] = metadata["bilingual_selection_rule"]
            sibling_checksum = sibling.get("pdf_checksum") or sibling.get("attachment_checksum")
            conn.execute(
                """
                INSERT INTO research_documents
                    (company_id, source_url, source_type, title, published_at, content_text,
                     duplicate_of, ingested_lang, checksum, raw_metadata)
                VALUES (?, ?, 'mfn', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(company_id, source_url) DO UPDATE SET
                    duplicate_of=excluded.duplicate_of,
                    ingested_lang=excluded.ingested_lang,
                    checksum=COALESCE(excluded.checksum, research_documents.checksum),
                    raw_metadata=excluded.raw_metadata
                """,
                (
                    company_id,
                    sibling_url,
                    sibling.get("title"),
                    sibling.get("published_at") or published_at,
                    sibling.get("content_text") or sibling.get("body"),
                    document_id,
                    sibling.get("lang") or sibling.get("ingested_lang") or "sv",
                    sibling_checksum,
                    json.dumps(sibling_meta, ensure_ascii=False, sort_keys=True),
                ),
            )
        conn.execute("RELEASE SAVEPOINT evidence_document")
        conn.commit()
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT evidence_document")
        conn.execute("RELEASE SAVEPOINT evidence_document")
        raise
    return {"document_id": document_id, "extraction_id": extraction_id, "checksum": checksum}


def persist_evidence_packet(
    conn: Any, *, company_id: int, as_of: str, packet: dict[str, Any]
) -> int:
    packet_json = json.dumps(packet, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    packet_hash = str(packet.get("packet_hash") or "")
    if not packet_hash:
        raise ValueError("frozen evidence packet requires packet_hash")
    conn.execute(
        """
        INSERT INTO evidence_packets (company_id, as_of, packet_hash, packet_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(company_id, as_of, packet_hash) DO UPDATE SET packet_json=excluded.packet_json
        """,
        (company_id, as_of, packet_hash, packet_json),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM evidence_packets WHERE company_id=? AND as_of=? AND packet_hash=?",
        (company_id, as_of, packet_hash),
    ).fetchone()
    return int(row[0]) if row else 0


def load_evidence_packet(
    conn: Any, company_id: int, as_of: str
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT packet_json FROM evidence_packets
        WHERE company_id=? AND as_of=? ORDER BY id DESC LIMIT 1
        """,
        (company_id, as_of),
    ).fetchone()
    if row is None:
        return None
    try:
        packet = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    return packet if isinstance(packet, dict) else None
