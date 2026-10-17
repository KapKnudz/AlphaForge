"""Frozen, source-attributed aggregate estimate packs; independent of legacy admission."""

from __future__ import annotations

import copy
import re
from datetime import date
from decimal import Decimal, InvalidOperation

from alphaforge.core.valuation.forward_fcff_contract import (
    ContractError,
    canonical,
    digest,
    iso_date,
    text,
    utc,
)

STANDARD = "estimated-valuation-v1"
CONTRACT = "estimated-forward-v1"
LABEL = "PROVISIONAL — ESTIMATED, NOT A VERIFIED FORECAST"
OUTPUTS = (
    "annual_fcff",
    "explicit_pv",
    "operating_value",
    "common_equity",
    "per_share_reporting",
    "per_share_quote",
)


class Unavailable(ContractError):
    def __init__(self, code, path="", status="insufficient_evidence"):
        self.status = status
        super().__init__(code, path)


def require(condition, code, path="", status="invalid_input"):
    if not condition:
        raise Unavailable(code, path, status)


def decimal(value, path=""):
    if value is None:
        raise Unavailable("missing_operand", path)
    try:
        require(not isinstance(value, bool), "invalid_number", path)
        result = Decimal(str(value))
        require(result.is_finite(), "nonfinite_operand", path)
        return result
    except (InvalidOperation, ValueError):
        raise Unavailable("invalid_number", path, "invalid_input") from None


def encode(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: encode(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [encode(v) for v in value]
    return value


def _nonempty_anchor(value):
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    try:
        return len(value) > 0
    except TypeError:
        return True


def content_identity(pack):
    return digest(pack)


def validate_envelope(pack):
    require(isinstance(pack, dict), "invalid_pack")
    require(pack.get("standard") == STANDARD, "unsupported_standard", status="unsupported")
    require(pack.get("contract") == CONTRACT, "unsupported_contract", status="unsupported")
    for key in ("pack_version", "entity", "perimeter", "prepared_by"):
        text(pack.get(key), key)
    v = iso_date(pack["valuation_date"])
    k = utc(pack["knowledge_cutoff"])
    prepared = iso_date(pack["prepared_on"])
    require(k.date() <= prepared, "preparation_before_cutoff")
    require(
        pack["knowledge_basis"] in {"cutoff_filtered", "retrospective_research"},
        "unsupported_knowledge_basis",
    )
    require(
        k.date() <= v or pack["knowledge_basis"] == "retrospective_research",
        "retrospective_label_required",
    )
    require(pack["currency"] in {"EUR", "SEK"}, "unsupported_currency", status="unsupported")
    require(
        pack["quote_currency"] in {"EUR", "SEK"},
        "unsupported_quote_currency",
        status="unsupported",
    )
    require(
        pack["money_units"] == "millions" and pack["share_units"] == "absolute",
        "unsupported_units",
        status="unsupported",
    )
    require(
        pack["lease_basis"] == "capitalized_debt_like",
        "unsupported_lease_basis",
        status="unsupported",
    )
    require(
        pack["object"] == "controlled_nonfinancial_operations",
        "unsupported_object",
        status="unsupported",
    )
    require(
        isinstance(pack["conditions"], list) and bool(pack["conditions"]), "conditions_required"
    )
    require(isinstance(pack["limitations"], list), "invalid_limitations")
    for limitation in pack["limitations"]:
        require(isinstance(limitation, dict), "invalid_limitation")
        text(limitation["reason"])
        require(bool(limitation["affected_outputs"]), "limitation_dependency_required")
        require(
            set(limitation["affected_outputs"]) <= set(OUTPUTS) | {"reverse", "conclusion"},
            "unknown_output_dependency",
        )
        require(
            limitation["status"]
            in {"conditional", "insufficient_evidence", "unsupported", "invalid_input"},
            "invalid_limitation_status",
        )
    sources = pack["sources"]
    require(isinstance(sources, list), "invalid_sources")
    require(all(isinstance(source, dict) for source in sources), "invalid_source")
    require(len({s["id"] for s in sources}) == len(sources), "duplicate_source")
    for source in sources:
        text(source["id"])
        text(source["content_sha256"])
        require(
            re.fullmatch(r"[0-9a-f]{64}", source["content_sha256"]) is not None,
            "invalid_source_identity",
        )
        utc(source["ingested_at"])
        if source["observed_on"] is not None:
            iso_date(source["observed_on"])
        require(
            isinstance(source["anchors"], list) and bool(source["anchors"]),
            "source_anchors_required",
        )
    require(isinstance(pack["records"], dict), "invalid_records")
    require(isinstance(pack["scenarios"], dict), "invalid_scenarios")
    require(bool(pack["scenarios"]) and "base" in pack["scenarios"], "base_scenario_required")
    scenario_names = list(pack["scenarios"])
    for name in scenario_names:
        require(
            isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]+", name) is not None,
            "unsafe_scenario_id",
        )
    require(
        len({name.casefold() for name in scenario_names}) == len(scenario_names),
        "duplicate_scenario_export_id",
    )
    require(isinstance(pack["allocations"], list), "invalid_allocations")
    require(all(isinstance(item, dict) for item in pack["allocations"]), "invalid_allocation")
    return v, k, prepared


class Evidence:
    """Resolve only consumed records; missing optional premises withhold their own stage."""

    def __init__(self, pack):
        self.pack = pack
        self.sources = {s["id"]: s for s in pack["sources"]}

    def block(self, block, path):
        if block is None:
            raise Unavailable("not_supplied", path, "not_requested")
        require(isinstance(block, dict), "invalid_block", path)
        ids = block.get("record_ids")
        if not ids:
            raise Unavailable("estimate_record_required", path)
        for rid in ids:
            record = self.pack["records"].get(rid)
            if record is None:
                raise Unavailable("record_not_catalogued", rid)
            require(
                record["kind"] in {"fact", "adjustment", "estimate", "condition"},
                "invalid_record_kind",
                rid,
            )
            require(
                record.get("status", "conditional")
                in {"conditional", "insufficient_evidence", "invalid_input", "unsupported"},
                "invalid_record_status",
                rid,
            )
            if record.get("status", "conditional") != "conditional":
                raise Unavailable("unqualified_record", rid, record["status"])
            for field in (
                "rationale",
                "mechanism",
                "counterevidence",
                "response",
                "alternatives",
                "effective_period",
                "prepared_by",
            ):
                text(record.get(field), f"records.{rid}.{field}")
            require(
                iso_date(record["prepared_on"]) <= iso_date(self.pack["prepared_on"]),
                "record_preparation_in_future",
                rid,
            )
            if not record.get("anchors"):
                raise Unavailable("material_anchor_required", rid)
            for ref in record["anchors"]:
                if not isinstance(ref, dict):
                    raise Unavailable("anchor_not_owned", rid)
                source = self.sources.get(ref.get("source_id"))
                anchor = ref.get("anchor")
                if (
                    source is None
                    or not _nonempty_anchor(anchor)
                    or not any(
                        _nonempty_anchor(owned) and owned == anchor for owned in source["anchors"]
                    )
                ):
                    raise Unavailable("anchor_not_owned", rid)
                # A retained publication upper bound proves cutoff eligibility,
                # never an exact first release date or historical-known-then claim.
                public_by = source["published_on"] or source.get("public_by")
                if public_by is None:
                    raise Unavailable("unknown_source_publication", source["id"])
                cutoff = utc(self.pack["knowledge_cutoff"])
                require(iso_date(public_by) <= cutoff.date(), "source_after_cutoff", source["id"])
                moments = [
                    utc(source[key])
                    for key in ("published_at", "public_by_at")
                    if source.get(key) is not None
                ]
                for moment in moments:
                    require(moment <= cutoff, "source_after_precise_cutoff", source["id"])
                if iso_date(public_by) == cutoff.date() and not moments:
                    raise Unavailable("same_day_publication_time_unknown", source["id"])
                if not source["published_on"]:
                    text(source.get("publication_limitation"), source["id"])
        return block

    def qualify(self, target):
        for limitation in self.pack["limitations"]:
            if target in limitation["affected_outputs"] and limitation["status"] != "conditional":
                raise Unavailable(
                    "unresolved_limitation", limitation["reason"], limitation["status"]
                )


def revise(pack, *, version, author, prepared_on, reason, changes):
    """Replace supplied top-level fields in a preserved child; never inherit reviews."""
    text(reason)
    text(author)
    require(version != pack["pack_version"], "new_version_required")
    require(iso_date(prepared_on) >= iso_date(pack["prepared_on"]), "override_chronology")
    require(
        bool(changes)
        and not set(changes)
        & {
            "parent",
            "pack_version",
            "revision",
            "reviews",
            "prepared_on",
            "prepared_by",
            "standard",
            "contract",
        },
        "invalid_override_fields",
    )
    child = copy.deepcopy(pack)
    child.update(copy.deepcopy(changes))
    changed = []
    missing = object()

    def diff(a, b, path):
        if a is not missing and b is not missing and isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b)):
                diff(
                    a[key] if key in a else missing,
                    b[key] if key in b else missing,
                    f"{path}.{key}" if path else key,
                )
        elif a is missing or b is missing or a != b:
            changed.append(
                {
                    "path": path,
                    "before": None if a is missing else copy.deepcopy(a),
                    "after": None if b is missing else copy.deepcopy(b),
                    "before_present": a is not missing,
                    "after_present": b is not missing,
                }
            )

    diff(pack, child, "")
    require(bool(changed), "override_has_no_change")
    affected = set()
    for entry in changed:
        path = entry["path"]
        if ".reverse." in path or path.endswith(".reverse"):
            affected.add("reverse")
        elif ".fx." in path:
            affected.update(OUTPUTS[1:])
        elif ".ownership." in path:
            affected.update(OUTPUTS[1:])
        elif ".bridge." in path or ".rights." in path:
            affected.update(("common_equity", "per_share_reporting", "per_share_quote"))
        elif ".terminal." in path:
            affected.update(OUTPUTS[2:])
        elif ".discount." in path:
            affected.update(OUTPUTS[1:])
        else:
            affected.update(OUTPUTS)
    if affected & set(OUTPUTS) and any(
        isinstance(scenario, dict) and scenario.get("reverse") is not None
        for version_pack in (pack, child)
        for scenario in version_pack["scenarios"].values()
    ):
        affected.add("reverse")
    child.update(
        {
            "pack_version": version,
            "parent": {
                "pack_version": pack["pack_version"],
                "content_identity": content_identity(pack),
            },
            "prepared_by": author,
            "prepared_on": prepared_on,
            "reviews": [],
            "revision": {
                "reason": reason,
                "changed_inputs": changed,
                "affected_outputs": sorted(affected) + ["conclusion"],
                "invalidated_reviews": copy.deepcopy(pack["reviews"]),
            },
        }
    )
    # Recompute both versions through the same forward path; record actual output
    # differences without promoting any original review to the new premise set.
    from alphaforge.core.valuation.estimated_fcff import evaluate

    before, after = evaluate(pack), evaluate(child)
    scenario_differences = {}
    for name in sorted(set(before["scenarios"]) | set(after["scenarios"])):
        before_result = before["scenarios"].get(name, {})
        after_result = after["scenarios"].get(name, {})
        output_differences = {}
        for key in sorted(set(before_result) | set(after_result)):
            before_present = key in before_result
            after_present = key in after_result
            before_value = before_result.get(key)
            after_value = after_result.get(key)
            if before_present != after_present or before_value != after_value:
                output_differences[key] = {
                    "before": before_value,
                    "after": after_value,
                    "before_present": before_present,
                    "after_present": after_present,
                }
        if output_differences:
            scenario_differences[name] = output_differences
    result_differences = {"scenarios": scenario_differences}
    if before["firm_investment_conclusion"] != after["firm_investment_conclusion"]:
        result_differences["firm_investment_conclusion"] = {
            "before": before["firm_investment_conclusion"],
            "after": after["firm_investment_conclusion"],
        }
    child["revision"]["result_differences"] = result_differences
    return child


def load(body):
    # Reuse duplicate-key/nonfinite JSON protection without entering the legacy schema.
    import json

    from alphaforge.core.valuation.forward_fcff_contract import _pairs

    return json.loads(
        body,
        object_pairs_hook=_pairs,
        parse_constant=lambda v: (_ for _ in ()).throw(ContractError("nonfinite", v)),
    )


def identity_envelope(pack):
    from alphaforge.core.valuation.estimated_reverse import NUMERICAL_POLICY, SEARCH_POLICY

    return {
        "reverse_search_policy": SEARCH_POLICY,
        "reverse_numerical_policy": NUMERICAL_POLICY,
        "standard": STANDARD,
        "contract": CONTRACT,
        "label": LABEL,
        "pack_content_identity": content_identity(pack),
        "source_catalog_identity": digest(pack["sources"]),
        **{
            k: copy.deepcopy(pack[k])
            for k in (
                "pack_version",
                "parent",
                "entity",
                "perimeter",
                "valuation_date",
                "knowledge_cutoff",
                "knowledge_basis",
                "prepared_on",
                "prepared_by",
                "currency",
                "quote_currency",
                "money_units",
                "share_units",
                "lease_basis",
                "conditions",
                "limitations",
                "reviews",
                "records",
                "sources",
            )
        },
        "revision": pack.get("revision"),
        "financial_status": "not_assessed_by_calculator",
        "human_status": "not_implementation_accepted",
        "actual_review_records": copy.deepcopy(pack["reviews"]),
        "numerical_policy": "Decimal64; annual A=2e-8,R=2e-12; value A=1e-7,R=2e-11; per-share A=1e-8,R=2e-11",
        "preparation_date_is_not_valuation_date": date.fromisoformat(pack["prepared_on"])
        != date.fromisoformat(pack["valuation_date"]),
    }


__all__ = ["canonical", "digest"]
