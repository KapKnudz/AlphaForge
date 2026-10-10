"""Closed, frozen supplied-schedule research contract; no issuer defaults or I/O."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime

SCHEMA = "forward-fcff-input-v1"
METHOD = "forward-fcff-schedule-v1"
CONVENTION = "nominal-annual-end-year-unlevered-shield-excluded-v1"
VALIDATION = "forward-fcff-controls-v1"
OUTPUTS = {"operating_cash_flows", "operating_value", "common_equity", "per_share"}
TOLERANCE = 1e-8


class ContractError(ValueError):
    def __init__(self, code: str, path: str = ""):
        self.code, self.path = code, path
        super().__init__(f"{code}: {path}")


def canonical(value) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def closed(value, required: str, optional: str = "", path: str = ""):
    if not isinstance(value, dict):
        raise ContractError("invalid_shape", path)
    req, opt = set(required.split()), set(optional.split())
    if set(value) - req - opt or req - set(value):
        raise ContractError("unknown_or_missing_fields", path)
    return value


def text(value, path=""):
    if not isinstance(value, str) or not value.strip():
        raise ContractError("nonempty_text_required", path)
    return value


def number(value, path=""):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ContractError("finite_number_required", path)
    return float(value)


def integer(value, path="", minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ContractError("invalid_integer", path)
    return value


def iso_date(value):
    text(value)
    try:
        d = date.fromisoformat(value)
        if d.isoformat() != value:
            raise ValueError(value)
        return d
    except ValueError:
        raise ContractError("invalid_date", str(value)) from None


def utc(value):
    text(value)
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if d.utcoffset() is None or d.utcoffset().total_seconds() != 0:
            raise ValueError(value)
        return d
    except ValueError:
        raise ContractError("invalid_utc_timestamp", str(value)) from None


def sequence(value, path=""):
    if not isinstance(value, list):
        raise ContractError("array_required", path)
    return value


def unique(values, path=""):
    if len(values) != len(set(values)):
        raise ContractError("duplicate_id", path)


def check_finite_tree(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise ContractError("finite_number_required")
    if isinstance(value, dict):
        for v in value.values():
            check_finite_tree(v)
    elif isinstance(value, list):
        for v in value:
            check_finite_tree(v)


def _pairs(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ContractError("duplicate_json_key", k)
        out[k] = v
    return out


@dataclass(frozen=True)
class FrozenRequest:
    """Canonical immutable bytes; decoding yields an evaluation-local mutable tree."""

    body: str

    def unpack(self):
        return json.loads(self.body)


def freeze(value: dict | str) -> FrozenRequest:
    if isinstance(value, str):
        value = json.loads(
            value,
            object_pairs_hook=_pairs,
            parse_constant=lambda v: (_ for _ in ()).throw(ContractError("nonfinite", v)),
        )
    check_finite_tree(value)
    closed(
        value,
        "schema_version method_id economic_convention company_id scenario_id valuation_date evidence_cutoff prepared_on knowledge_basis object denomination evidence_context operating_schedule limitations reviews",
        "discount terminal bridge ownership",
    )
    if (value["schema_version"], value["method_id"], value["economic_convention"]) != (
        SCHEMA,
        METHOD,
        CONVENTION,
    ):
        raise ContractError("unsupported_version")
    integer(value["company_id"], "company_id", 1)
    text(value["scenario_id"])
    valuation = iso_date(value["valuation_date"])
    cutoff = iso_date(value["evidence_cutoff"])
    prepared = iso_date(value["prepared_on"])
    if cutoff > prepared or (
        cutoff > valuation and value["knowledge_basis"] != "retrospective_research"
    ):
        raise ContractError("knowledge_date_mismatch")
    if value["knowledge_basis"] not in {"cutoff_filtered", "retrospective_research"}:
        raise ContractError("knowledge_basis_unsupported")
    # Normalize ID-keyed economic arrays; annual periods remain ordered.
    id_arrays = {
        "sources",
        "reviews",
        "limitations",
        "acquisitions",
        "investment_items",
        "disposals",
        "settlements",
        "asset_controls",
        "tax_ledgers",
        "tax_lots",
        "lots",
        "allocations",
        "items",
        "categories",
        "cohorts",
        "templates",
        "tax_kernels",
        "queues",
        "exhausted_states",
        "financing_diagnostics",
        "boundary_cash",
        "transfer_events",
        "noncash_items",
        "lot_movements",
        "rights",
        "payable_other",
        "receivable_other",
        "other",
    }

    stage_owned_id_arrays = {
        "items",
        "categories",
        "templates",
        "tax_kernels",
        "queues",
        "exhausted_states",
        "boundary_cash",
        "rights",
    }

    def order(tree, path=()):
        key = path[-1] if path else ""
        if key == "packet":
            return tree  # The existing packet contract owns its array ordering/hash.
        if isinstance(tree, dict):
            return {k: order(v, (*path, k)) for k, v in tree.items()}
        if isinstance(tree, list):
            result = [order(v, (*path, "[]")) for v in tree]
            if (
                key in id_arrays
                and result
                and all(isinstance(v, dict) and "id" in v for v in result)
            ):
                if key in stage_owned_id_arrays:
                    result.sort(
                        key=lambda v: (
                            (0, v["id"]) if isinstance(v["id"], str) else (1, canonical(v["id"])),
                            canonical(v),
                        )
                    )
                else:
                    identities = [text(v["id"]) for v in result]
                    unique(identities, key)
                    result.sort(key=lambda v: v["id"])
            elif key == "weights" and result and all(isinstance(v, dict) for v in result):
                field = "offset_years" if "offset_years" in result[0] else "period_index"
                if all(field in v for v in result):
                    result.sort(
                        key=lambda v: (
                            (0, v[field])
                            if isinstance(v[field], int) and not isinstance(v[field], bool)
                            else (1, canonical(v[field])),
                            canonical(v),
                        )
                    )
            elif path in {
                ("bridge", "completeness", "inventoried_categories"),
                ("ownership", "rights", "[]", "claim_event_ids"),
            }:
                result.sort(key=canonical)
            elif key == "supporting_operands" and result:
                result.sort(key=lambda v: text(v["provenance"]["id"]))
            return result
        return tree

    ordered = order(value)
    context = value.get("evidence_context")
    if isinstance(context, dict) and isinstance(context.get("catalog"), dict):
        if context.get("catalog_hash") == digest(context["catalog"]):
            ordered["evidence_context"]["catalog_hash"] = digest(
                ordered["evidence_context"]["catalog"]
            )
    return FrozenRequest(canonical(ordered))


class Operands:
    """One provenance/source/review resolver per frozen request."""

    def __init__(self, request):
        self.request = request
        self.ids = {}
        self.sources = {}
        self.reviews = {}
        self.used = set()
        self.events = {}
        self.review_diagnostics = []
        self.ineligible_review_links = set()
        self.cutoff = iso_date(request["evidence_cutoff"])
        self.prepared = iso_date(request["prepared_on"])
        for limitation in sequence(request["limitations"]):
            closed(limitation, "id text affected_outputs conditional_outputs material")
            text(limitation["id"])
            text(limitation["text"])
            targets = sequence(limitation["affected_outputs"])
            conditional = sequence(limitation["conditional_outputs"])
            unique(targets, limitation["id"])
            unique(conditional, limitation["id"])
            if any(
                not isinstance(target, str) or target not in OUTPUTS for target in targets
            ) or any(target not in targets for target in conditional):
                raise ContractError("invalid_limitation_output", limitation["id"])
            if not isinstance(limitation["material"], bool):
                raise ContractError("invalid_limitation")
            if limitation["material"] and not targets:
                raise ContractError("invalid_limitation_output", limitation["id"])
            if not limitation["material"] and conditional:
                raise ContractError("invalid_limitation_output", limitation["id"])
        unique([v["id"] for v in request["limitations"]])
        for r in sequence(request["reviews"]):
            closed(
                r,
                "id reviewer reviewer_kind reviewed_on scope_operand_ids basis_identity conclusion limitations",
            )
            if (
                r["reviewer_kind"] not in {"human", "automated_check"}
                or iso_date(r["reviewed_on"]) > self.prepared
            ):
                raise ContractError("invalid_review_date_or_kind")
            text(r["reviewer"])
            text(r["basis_identity"])
            text(r["conclusion"])
            sequence(r["scope_operand_ids"])
            sequence(r["limitations"])
            self.reviews[text(r["id"])] = r
        unique([r["id"] for r in request["reviews"]])
        context = closed(request["evidence_context"], "catalog catalog_hash", "packet packet_hash")
        if "packet" in context or "packet_hash" in context:
            from alphaforge.core.frozen_packet import validate_frozen_packet

            packet = context.get("packet")
            if not validate_frozen_packet(packet) or context.get("packet_hash") != packet.get(
                "packet_hash"
            ):
                raise ContractError("invalid_evidence_packet")
            if (
                packet.get("company_id") != request["company_id"]
                or packet.get("as_of") != request["evidence_cutoff"]
            ):
                raise ContractError("packet_identity_mismatch")
        catalog = closed(context["catalog"], "sources")
        if digest(catalog) != context["catalog_hash"]:
            raise ContractError("source_catalog_hash_mismatch")
        for s in sequence(catalog["sources"]):
            closed(
                s, "id url sha256 publication_date observation_date ingested_at anchors limitations"
            )
            text(s["url"])
            if len(text(s["sha256"])) != 64 or any(
                c not in "0123456789abcdef" for c in s["sha256"]
            ):
                raise ContractError("invalid_source_hash")
            if s["publication_date"] is not None:
                iso_date(s["publication_date"])
            elif "publication_unknown" not in s["limitations"]:
                raise ContractError("publication_unknown_limitation_missing")
            if s["observation_date"] is not None:
                iso_date(s["observation_date"])
            elif "observation_unknown" not in s["limitations"]:
                raise ContractError("observation_unknown_limitation_missing")
            if utc(s["ingested_at"]).date() > self.prepared:
                raise ContractError("ingestion_after_preparation")
            if not isinstance(s["anchors"], dict) or not s["anchors"]:
                raise ContractError("source_anchors_missing")
            for a, body in s["anchors"].items():
                text(a)
                text(body)
            self.sources[text(s["id"])] = s
        unique([s["id"] for s in catalog["sources"]])

        self.add(request["operating_schedule"])

    def add(self, tree):
        if isinstance(tree, dict):
            if set(tree) == {"value", "provenance"}:
                p = tree["provenance"]
                if isinstance(p, dict) and isinstance(p.get("id"), str):
                    identity = p["id"]
                    if identity in self.ids:
                        raise ContractError("duplicate_operand_id", identity)
                    self.ids[identity] = tree
            else:
                for value in tree.values():
                    self.add(value)
        elif isinstance(tree, list):
            for value in tree:
                self.add(value)

    def _source_lineage(self, provenance, seen=()):
        source_ids = []
        for ref in provenance["source_refs"]:
            if ref["source_id"] not in source_ids:
                source_ids.append(ref["source_id"])
        for parent in provenance["derived_from"]:
            if parent in seen:
                continue
            parent_provenance = self.ids[parent]["provenance"]
            for source_id in self._source_lineage(parent_provenance, (*seen, parent)):
                if source_id not in source_ids:
                    source_ids.append(source_id)
        return source_ids

    def _record_review_chronology(self, identity, provenance):
        for rid in provenance["review_ids"]:
            review = self.reviews[rid]
            for source_id in self._source_lineage(provenance, (identity,)):
                source = self.sources[source_id]
                publication = source["publication_date"]
                code = None
                if publication is None:
                    code = "review_source_publication_unknown"
                elif iso_date(review["reviewed_on"]) < iso_date(publication):
                    code = "review_precedes_source_publication"
                if code is None:
                    continue
                link = (identity, rid, source_id)
                if link not in self.ineligible_review_links:
                    self.ineligible_review_links.add(link)
                    self.review_diagnostics.append(
                        {
                            "code": code,
                            "operand_id": identity,
                            "review_id": rid,
                            "source_id": source_id,
                        }
                    )

    def operand(self, operand, path="", nonnegative=False, _ancestors=()):
        closed(operand, "value provenance", path=path)
        p = closed(
            operand["provenance"],
            "id class source_refs derived_from rationale accounting_date period_start period_end uncertainty review_ids limitations",
            "mechanism effective_periods adjustment",
        )
        identity = text(p["id"])
        if identity in _ancestors:
            raise ContractError("cyclic_operand_derivation", identity)
        self.used.add(identity)
        text(p["rationale"])
        if p["uncertainty"] not in {
            "source_observed",
            "analyst_estimate",
            "hypothetical_sensitivity",
            "unresolved",
        }:
            raise ContractError("invalid_uncertainty", identity)
        if p["uncertainty"] == "unresolved":
            raise ContractError("required_operand_unresolved", path or identity)
        sequence(p["limitations"])
        for rid in sequence(p["review_ids"]):
            if rid not in self.reviews or identity not in self.reviews[rid]["scope_operand_ids"]:
                raise ContractError("review_scope_mismatch", identity)
        for parent in sequence(p["derived_from"]):
            if parent not in self.ids or parent == identity:
                raise ContractError("derived_operand_missing", identity)
            self.operand(self.ids[parent], parent, _ancestors=(*_ancestors, identity))
        for field in ("accounting_date", "period_start", "period_end"):
            if p[field] is not None:
                iso_date(p[field])
        if (p["period_start"] is None) != (p["period_end"] is None) or (
            p["period_start"] and iso_date(p["period_start"]) > iso_date(p["period_end"])
        ):
            raise ContractError("invalid_accounting_period", identity)
        for ref in sequence(p["source_refs"]):
            closed(ref, "source_id anchor", "url sha256 publication_date observation_date")
            s = self.sources.get(ref["source_id"])
            if s is None or ref["anchor"] not in s["anchors"]:
                raise ContractError("source_reference_unresolved", identity)
            for k in ("url", "sha256", "publication_date", "observation_date"):
                if k in ref and ref[k] != s[k]:
                    raise ContractError("source_reference_mismatch", identity)
            if p["class"] == "reported_fact" and s["publication_date"] is not None:
                observed = p["accounting_date"] or p["period_end"]
                if observed is not None and iso_date(observed) > iso_date(s["publication_date"]):
                    raise ContractError("future_amount_not_observed_fact", identity)
        self._record_review_chronology(identity, p)
        if p["class"] == "reported_fact":
            for ref in p["source_refs"]:
                publication = self.sources[ref["source_id"]]["publication_date"]
                if publication is None or iso_date(publication) > self.cutoff:
                    raise ContractError("fact_not_cutoff_qualified", identity)
            if (
                not p["source_refs"]
                or not (p["accounting_date"] or p["period_start"])
                or p["uncertainty"] != "source_observed"
            ):
                raise ContractError("reported_fact_provenance_missing", identity)
        elif p["class"] == "forecast_assumption":
            text(p.get("mechanism"))
            sequence(p.get("effective_periods"))
            if not p["effective_periods"] or p["uncertainty"] == "source_observed":
                raise ContractError("forecast_provenance_missing", identity)
        elif p["class"] == "analyst_adjustment":
            if p["uncertainty"] == "source_observed":
                raise ContractError("adjustment_not_reported_fact", identity)
            if not p["derived_from"]:
                raise ContractError("adjustment_sources_missing", identity)
            a = closed(p.get("adjustment"), "source_operand delta operation result")
            if a["operation"] != "add" or a["source_operand"] not in p["derived_from"]:
                raise ContractError("adjustment_operation_unsupported", identity)
            base = self.ids[a["source_operand"]]["value"]
            if (
                not close(number(base) + number(a["delta"]), number(a["result"]))
                or a["result"] != operand["value"]
            ):
                raise ContractError("adjustment_reconciliation_mismatch", identity)
        else:
            raise ContractError("provenance_class_unsupported", identity)
        if operand["value"] is None:
            raise ContractError("required_operand_missing", path or identity)
        v = number(operand["value"], path)
        if nonnegative and v < 0:
            raise ContractError("negative_operand", path)
        return v

    def event(self, identity, category=None, direction=None, amount=None, period_index=None):
        text(identity)
        if identity in self.events:
            raise ContractError("double_counted_event", identity)
        self.events[identity] = {
            "category": category,
            "direction": direction,
            "amount": amount,
            "period_index": period_index,
        }


def close(lhs, rhs):
    return (
        math.isfinite(lhs)
        and math.isfinite(rhs)
        and abs(lhs - rhs) <= TOLERANCE * max(1, abs(lhs), abs(rhs))
    )


def reconcile(lhs, rhs, code, path=""):
    if not close(lhs, rhs):
        raise ContractError(code, path)


def output(value=None, status="conditional", reason="", missing="", conditions=(), limitations=()):
    return {
        "available": status == "conditional",
        "status": status,
        "reason_codes": [reason] if reason else [],
        "missing_inputs": [missing] if missing else [],
        "conditions": list(conditions),
        "limitations": list(limitations),
        "value": value,
    }
