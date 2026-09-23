"""Stable identity for deterministic MFN report-history rules."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from alphaforge.evidence.mfn_taxonomy import INVITATION_MARKERS, REPORT_TITLE_TERMS

# This version is the schema/interpretation version of the rule input record.
# The content fingerprint also changes when any listed rule input changes.
REPORT_RULES_VERSION = 1
_DEFAULT_HISTORY_WINDOW = {
    "interim_lookback_years": 2,
    "annual_lookback_years": 5,
    "max_offsets": 12,
    "max_detail_fetches": 60,
    "limit_per_offset": 48,
}


def _history_window_values(window: Any | None) -> dict[str, int]:
    values = dict(_DEFAULT_HISTORY_WINDOW)
    if window is not None:
        for key in values:
            value = getattr(window, key, values[key])
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"history window {key} must be an integer")
            values[key] = value
    return values


def report_rules_inputs(window: Any | None = None) -> dict[str, Any]:
    """Return the canonical inputs that decide report evidence coverage.

    Keep title semantics in :mod:`mfn_taxonomy`; this module only fingerprints
    the already-owned predicates and the scraper's guarded distribution rules.
    """
    from alphaforge.providers.mfn import scraper

    return {
        "version": REPORT_RULES_VERSION,
        "taxonomy": {
            "report_title_terms": list(REPORT_TITLE_TERMS),
            "invitation_markers": list(INVITATION_MARKERS),
            "attachment_report_terms": list(scraper._REPORT_ATTACHMENT_TERMS),
            "attachment_non_report_terms": list(scraper._NON_REPORT_ATTACHMENT_TERMS),
            "report_kind_classes": ["annual", "quarterly"],
        },
        "distribution": {
            "cis_release_path": scraper._CIS_RELEASE_PATH_RE.pattern,
            "attachment_hosts": list(scraper._ATTACHMENT_HOST_MARKERS),
            "attachment_tiers": list(scraper.ATTACHMENT_TIERS),
            "main_path_marker": "/main/",
        },
        "completeness": {
            "grouping": "bilingual_dedupe_without_feed_group_id",
            "class_rule": "report_kind_annual_or_quarterly",
        },
        "history_window": _history_window_values(window),
    }


def report_rules_fingerprint(window: Any | None = None) -> str:
    canonical = json.dumps(
        report_rules_inputs(window), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def report_rules_metadata(window: Any | None = None) -> dict[str, Any]:
    """Return the compact rule stamp stored in every frozen packet."""
    inputs = report_rules_inputs(window)
    return {
        "version": REPORT_RULES_VERSION,
        "fingerprint": report_rules_fingerprint(window),
        "history_window": inputs["history_window"],
    }


def current_report_rules_fingerprint() -> str:
    """Fingerprint for the default production history window."""
    return report_rules_fingerprint()
