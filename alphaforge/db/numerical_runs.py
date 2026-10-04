"""Bounded executed-run retention. No historical-code download or live fallback."""

from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
import subprocess
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from alphaforge.cli.ranking_loader import (
    MAX_PRICE_AGE_DAYS,
    SELECTION_VERSION,
    _select_kpis,
    load_results_for_company,
    report_selection_reason,
    verified_price_date,
)
from alphaforge.core.gate.readiness import AgentReadinessGate
from alphaforge.core.ranking.engine import RankingEngine
from alphaforge.core.valuation.dcf_policy import DcfAssumptionPolicy
from alphaforge.core.valuation.dividend_yield import (
    DIVIDEND_YIELD_POLICY_VERSION,
    trailing_dividend_window,
)
from alphaforge.core.valuation.reinvestment import CALIBRATION_VERSION, ECONOMIC_CONVENTION
from alphaforge.core.valuation.required_return import RequiredReturnPolicy
from alphaforge.db.migrations import migrate
from alphaforge.evidence.manifest_store import load_evidence_view

ROOT = Path(__file__).resolve().parents[2]
TABLES = (
    "companies",
    "financial_periods",
    "financial_period_rejections",
    "prices",
    "kpi_observations",
    "reinvestment_calibrations",
    "market_input_rejections",
    "stock_splits",
    "dividends",
    "dividend_window_coverage",
)
ENCODING = "executed-numerical-v2"


class ReplayRefusal(ValueError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _json_date(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical(value) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_date,
    )


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def run_artifact_id(
    run_id: int,
    numerical_identity: str,
    textual_context_hash: str,
    outputs_hash: str,
    source_rows_hash: str | None = None,
) -> str:
    identity = {
        "run_id": run_id,
        "numerical_identity": numerical_identity,
        "textual_context_hash": textual_context_hash,
        "outputs_hash": outputs_hash,
    }
    if source_rows_hash is not None:
        identity["source_rows_hash"] = source_rows_hash
    return digest(identity)


def rules_bundle() -> dict:
    # The digest pins actual executing sources, including uncommitted fixture development.
    # Revision is separately retained and must still exist locally for audit replay.
    paths = (
        sorted(
            p
            for p in (ROOT / "alphaforge/core").rglob("*.py")
            if p.parent.name not in {"coverage", "returns"}
        )
        + [
            ROOT / "alphaforge/cli/ranking_loader.py",
            ROOT / "alphaforge/cli/main.py",
            Path(__file__),
            ROOT / "alphaforge/db/migrations.py",
            ROOT / "alphaforge/db/reinvestment.py",
            ROOT / "alphaforge/config.py",
            ROOT / "alphaforge/evidence/report_rules.py",
            ROOT / "alphaforge/evidence/mfn_taxonomy.py",
            ROOT / "db/alphaforge.sqlite.sql",
        ]
        + sorted((ROOT / "db/migrations").glob("*.sql"))
    )
    sources = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    return {
        "encoding": ENCODING,
        "code_revision": revision,
        "source_files": sources,
        "runtime": platform.python_implementation() + "-" + platform.python_version(),
        "sqlite_runtime": sqlite3.sqlite_version,
        "dcf_defaults": {
            name: getattr(DcfAssumptionPolicy, name)
            for name in (
                "PROJECTION_YEARS",
                "TAX_RATE",
                "TERMINAL_GROWTH",
                "GROWTH_RANGE",
                "NET_REINVESTMENT_RANGE",
                "FCF_MARGIN_VOLATILITY",
                "FCF_MARGIN_RANGE",
                "WINDOW_DISAGREEMENT",
                "MATERIAL_INVESTING_MARGIN",
                "EXTREME_INVESTING_YEAR",
            )
        },
        "required_return_buckets": [
            {
                "upper_sek_exclusive": upper if upper != float("inf") else None,
                "name": name,
                "return": rate,
            }
            for upper, name, rate in RequiredReturnPolicy.SIZE_BUCKETS
        ],
        "ranking": RankingEngine.RANKING_MODEL_VERSION,
        "selection": SELECTION_VERSION,
        "max_price_age_calendar_days": MAX_PRICE_AGE_DAYS,
        "dcf": DcfAssumptionPolicy.VERSION,
        "reinvestment_calibration": CALIBRATION_VERSION,
        "economic_convention": ECONOMIC_CONVENTION,
        "solve_bounds": DcfAssumptionPolicy.SOLVE_BOUNDS,
        "required_return": RequiredReturnPolicy.VERSION,
        "dividend_yield": DIVIDEND_YIELD_POLICY_VERSION,
        "report_amounts_and_shares_scale": 1000000,
        "historical_shares_basis": "latest-admitted-report; provider prices split-adjusted",
        "serialization": "sorted UTF-8 JSON; finite IEEE-754 Python floats; no rounding tolerance",
    }


def capture_inputs(
    conn, companies, as_of: str, *, source_rows: dict | None = None
) -> tuple[dict, dict]:
    """Freeze the loader's candidate/selection domain before running any calculator.

    Examined rejection/history rows are retained because they affect refusal and
    chronology, not as a general observation-vintage service. Text stays separate.
    """
    tables = {table: [] for table in TABLES}
    original_ids = {}
    text = {}
    cutoff = date.fromisoformat(as_of)
    start, end = trailing_dividend_window(cutoff)
    conn.execute("SAVEPOINT numerical_capture")
    try:
        for company in companies:
            cid = company.id
            for table in TABLES:
                predicate = "id=?" if table == "companies" else "company_id=?"
                params = [cid]
                if table == "dividends":
                    predicate += " AND substr(ex_date,1,10)>? AND substr(ex_date,1,10)<=?"
                    params.extend([start.isoformat(), end.isoformat()])
                if table == "dividend_window_coverage":
                    predicate += " AND window_start=? AND window_end=?"
                    params.extend([start.isoformat(), end.isoformat()])
                rows = conn.execute(f"SELECT * FROM {table} WHERE {predicate}", params).fetchall()
                if table == "kpi_observations":
                    selected_ids = set()
                    _select_kpis(conn, cid, cutoff, selected_input_ids=selected_ids)
                    rows = [row for row in rows if row["id"] in selected_ids]
                for row in rows:
                    item = dict(row)
                    for key in ("created_at", "updated_at", "fetched_at"):
                        item.pop(key, None)
                    # Rejection dates are diagnostic output, so retain them. Provider raw
                    # facts survive alongside stored transformations and denomination.
                    if table != "companies":
                        item.pop("id", None)
                    if item.get("raw_payload"):
                        item["raw_payload"] = canonical(json.loads(item["raw_payload"]))
                    tables[table].append(item)
                    if table not in {"companies", "prices", "dividend_window_coverage"}:
                        original_ids[id(item)] = row["id"]
            # Only the current candidate and actually paired historical closes
            # participate; intervening unselected daily prices are not retained.
            reports = sorted(
                [
                    r
                    for r in tables["financial_periods"]
                    if r["company_id"] == cid and report_selection_reason(r, cutoff)[0] is None
                ],
                key=lambda r: (
                    r["period_end"],
                    r["report_date"],
                    {"quarter": 0, "year": 1, "r12": 2}[r["period_type"]],
                ),
            )
            admitted_prices = sorted(
                [
                    r
                    for r in tables["prices"]
                    if r["company_id"] == cid
                    and verified_price_date(r) is not None
                    and verified_price_date(r) <= cutoff
                ],
                key=lambda r: r["price_date"],
            )
            used_dates = {admitted_prices[-1]["price_date"]} if admitted_prices else set()
            for report in reports[:-1]:
                eligible = [p for p in admitted_prices if p["price_date"] <= report["period_end"]]
                if eligible:
                    used_dates.add(eligible[-1]["price_date"])
            tables["prices"] = [
                r
                for r in tables["prices"]
                if r["company_id"] != cid
                or r["price_date"] in used_dates
                or verified_price_date(r) is None
                or verified_price_date(r) > cutoff
            ]
            packet, manifest = load_evidence_view(conn, company_id=cid, as_of=as_of)
            text[str(cid)] = {
                "documents": [
                    {
                        "id": r.get("document_id"),
                        "source_url": r.get("source_url"),
                        "title": r.get("title"),
                        "published_at": r.get("published_at"),
                    }
                    for r in manifest.audit_history
                    if r.get("published_at") and str(r["published_at"])[:10] <= as_of
                ],
                "evidence_packet": packet,
                "evidence_manifest": manifest.to_dict(),
                "evidence_lane": bool(packet),
            }
        source_mapping = []
        for table, rows in tables.items():
            rows.sort(key=canonical)
            # Normalize internal surrogate IDs: no insertion-order numerical identity.
            if table not in {"companies", "prices", "dividend_window_coverage"}:
                for index, row in enumerate(rows, 1):
                    row["id"] = index
                    source_mapping.append(
                        {
                            "table": table,
                            "snapshot_row_id": index,
                            "source_row_id": original_ids[id(row)],
                            "company_id": row["company_id"],
                        }
                    )
        if source_rows is not None:
            source_rows.update(
                {
                    "encoding": "numerical-source-rows-v1",
                    "snapshot_namespace": "retained_numerical_body",
                    "source_namespace": "originating_sqlite_database",
                    "rows": source_mapping,
                }
            )
        body = {
            "encoding": ENCODING,
            "as_of": as_of,
            "universe": [asdict(c) for c in companies],
            "tables": tables,
        }
        return json.loads(canonical(body)), json.loads(canonical(text))
    finally:
        conn.execute("RELEASE numerical_capture")


def retain_inputs(conn, body: dict, rules: dict) -> tuple[str, str]:
    financial_hash = digest(body)
    identity = digest({"financial_inputs_hash": financial_hash, "rules": rules})
    expected = (financial_hash, canonical(body), canonical(rules))
    existing = conn.execute(
        "SELECT financial_inputs_hash,body,rules FROM numerical_input_bodies WHERE numerical_identity=?",
        (identity,),
    ).fetchone()
    if existing is not None and tuple(existing) != expected:
        raise ReplayRefusal("conflicting_immutable_insertion")
    if existing is None:
        try:
            conn.execute(
                "INSERT INTO numerical_input_bodies VALUES (?,?,?,?)", (identity, *expected)
            )
        except sqlite3.IntegrityError:
            # A concurrent identical insertion can win after our absence read.
            # Do not swallow unrelated constraint/retention faults or contradictions.
            winner = conn.execute(
                "SELECT financial_inputs_hash,body,rules FROM numerical_input_bodies WHERE numerical_identity=?",
                (identity,),
            ).fetchone()
            if winner is None:
                raise
            if tuple(winner) != expected:
                raise ReplayRefusal("conflicting_immutable_insertion") from None
    conn.commit()  # Required before calculation, let retention failures abort consumption.
    return identity, financial_hash


def evaluate(body: dict, text: dict):
    """Rebuild only a disposable calculation DB; never read mutable live rows."""
    memory = sqlite3.connect(":memory:")
    memory.row_factory = sqlite3.Row
    try:
        migrate(memory)
        for table in TABLES:
            for row in body["tables"][table]:
                columns = list(row)
                memory.execute(
                    f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    [row[key] for key in columns],
                )
        companies = [SimpleNamespace(**c) for c in body["universe"]]
        results = {
            c.id: load_results_for_company(
                memory, c.id, body["as_of"], retained_research_evidence=text[str(c.id)]
            )
            for c in companies
        }
        engine = RankingEngine()
        ranking = engine.rank(companies, results)
        for score in ranking.scores:
            candidate = results[score.company_id]["candidate"]
            candidate.ticker = score.ticker
            candidate.ranking_model = score.ranking_model
            assessment = AgentReadinessGate().assess(candidate)
            score.readiness_status = assessment.status
            score.readiness_blockers = [f"{r.code}: {r.message}" for r in assessment.blockers]
            score.readiness_limitations = [f"{r.code}: {r.message}" for r in assessment.limitations]
        outputs = {
            "scores": [asdict(s) for s in ranking.scores],
            "dcf": {str(cid): r["reverse_dcf"] for cid, r in results.items()},
            "metrics": {
                str(cid): {
                    "financial": asdict(r["financial"]) if r["financial"] is not None else None,
                    "valuation": asdict(r["valuation"]) if r["valuation"] is not None else None,
                    "dividend_yield": r.get("dividend_yield"),
                }
                for cid, r in results.items()
            },
        }
        return ranking, results, json.loads(canonical(outputs))
    finally:
        memory.close()


def retain_outputs(
    conn, run_id: int, identity: str, text: dict, outputs: dict, *, source_rows: dict | None = None
) -> str:
    text_hash = digest(text)
    outputs_hash = digest(outputs)
    expected = (identity, canonical(text), text_hash, canonical(outputs), outputs_hash)
    existing = conn.execute(
        "SELECT numerical_identity,textual_context,textual_context_hash,outputs,outputs_hash FROM executed_numerical_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if existing is not None and tuple(existing) != expected:
        raise ReplayRefusal("conflicting_immutable_insertion")
    if existing is None:
        conn.execute(
            "INSERT INTO executed_numerical_runs VALUES (?,?,?,?,?,?)", (run_id, *expected)
        )
    mapping = conn.execute(
        "SELECT source_rows,source_rows_hash FROM numerical_run_source_rows WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if source_rows is not None:
        expected_mapping = (canonical(source_rows), digest(source_rows))
        if mapping is not None and tuple(mapping) != expected_mapping:
            raise ReplayRefusal("conflicting_immutable_insertion")
        if mapping is None:
            if existing is not None:
                # Never manufacture original row IDs for already retained runs.
                raise ReplayRefusal("conflicting_immutable_insertion")
            conn.execute(
                "INSERT INTO numerical_run_source_rows VALUES (?,?,?)", (run_id, *expected_mapping)
            )
            mapping = expected_mapping
    conn.commit()
    return run_artifact_id(
        run_id, identity, text_hash, outputs_hash, mapping[1] if mapping else None
    )


def replay_run(conn, run_id: int) -> dict:
    row = conn.execute("SELECT * FROM executed_numerical_runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        exists = conn.execute(
            "SELECT inputs_summary FROM ranking_runs WHERE id=?", (run_id,)
        ).fetchone()
        if exists is None:
            raise ReplayRefusal("missing_run")
        try:
            summary = json.loads(exists[0] or "{}")
        except (TypeError, ValueError) as exc:
            raise ReplayRefusal("corrupt_retained_body") from exc
        if not isinstance(summary, dict):
            raise ReplayRefusal("corrupt_retained_body")
        raise ReplayRefusal(
            "missing_snapshot" if summary.get("numerical_identity") else "legacy_not_replayable"
        )
    snapshot = conn.execute(
        "SELECT * FROM numerical_input_bodies WHERE numerical_identity=?",
        (row["numerical_identity"],),
    ).fetchone()
    if snapshot is None:
        raise ReplayRefusal("missing_snapshot")
    try:
        body, rules = json.loads(snapshot["body"]), json.loads(snapshot["rules"])
        text, original = json.loads(row["textual_context"]), json.loads(row["outputs"])
        if not all(isinstance(value, dict) for value in (body, rules, text, original)):
            raise ReplayRefusal("corrupt_retained_body")
        if (
            digest(body) != snapshot["financial_inputs_hash"]
            or digest({"financial_inputs_hash": digest(body), "rules": rules})
            != row["numerical_identity"]
            or digest(original) != row["outputs_hash"]
        ):
            raise ReplayRefusal("corrupt_retained_body")
        if digest(text) != row["textual_context_hash"]:
            raise ReplayRefusal("invalid_textual_context")
        if body.get("encoding") != ENCODING:
            raise ReplayRefusal("unsupported_rules_or_code")
        revision = rules["code_revision"]
        try:
            available = (
                subprocess.run(
                    ["git", "cat-file", "-e", revision + "^{commit}"], cwd=ROOT, capture_output=True
                ).returncode
                == 0
            )
        except OSError as exc:
            raise ReplayRefusal("unavailable_code_revision") from exc
        if not available:
            raise ReplayRefusal("unavailable_code_revision")
        try:
            supported = rules_bundle()
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ReplayRefusal("unsupported_rules_or_code") from exc
        supported["code_revision"] = revision
        if canonical(rules) != canonical(supported):
            raise ReplayRefusal("unsupported_rules_or_code")
        # Existing textual tamper checks remain authoritative; historical replay
        # never reactivates usability or bypasses current evidence selection.
        from alphaforge.core.frozen_packet import validate_frozen_packet

        for context in text.values():
            packet = context.get("evidence_packet")
            if packet is not None and not validate_frozen_packet(packet):
                raise ReplayRefusal("invalid_textual_context")
        source_mapping = conn.execute(
            "SELECT source_rows,source_rows_hash FROM numerical_run_source_rows WHERE run_id=?",
            (run_id,),
        ).fetchone()
        source_rows = json.loads(source_mapping[0]) if source_mapping else None
        if source_mapping is not None and digest(source_rows) != source_mapping[1]:
            raise ReplayRefusal("invalid_source_row_map")
        _, _, outputs = evaluate(body, text)
        if canonical(outputs) != canonical(original):
            raise ReplayRefusal("output_mismatch")
        return {
            "run_id": run_id,
            "artifact_id": run_artifact_id(
                run_id,
                row["numerical_identity"],
                row["textual_context_hash"],
                row["outputs_hash"],
                source_mapping[1] if source_mapping else None,
            ),
            "source_rows": source_rows,
            "source_rows_hash": source_mapping[1] if source_mapping else None,
            "source_rows_status": "retained" if source_mapping else "not_retained",
            "audit_only": True,
            "numerical_identity": row["numerical_identity"],
            "outputs": outputs,
        }
    except ReplayRefusal:
        raise
    except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
        raise ReplayRefusal("corrupt_retained_body") from exc
