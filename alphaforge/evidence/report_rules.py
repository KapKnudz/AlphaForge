"""Stable identity for deterministic MFN report-history rules."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from alphaforge.evidence.mfn_taxonomy import (
    ATTACHMENT_HOST_MARKERS,
    ATTACHMENT_TIERS,
    CIS_RELEASE_PATH_RE,
    INVITATION_MARKERS,
    NON_REPORT_ATTACHMENT_TERMS,
    RECOGNIZED_ATTACHMENT_TIERS,
    REPORT_ANNUAL_TAG,
    REPORT_ATTACHMENT_TERMS,
    REPORT_FEED_TAG,
    REPORT_INTERIM_TAG_PREFIX,
    REPORT_PDF_ATTACHMENT_TAG,
    REPORT_TITLE_TERMS,
)

# This version is the schema/interpretation version of the rule input record.
# The content fingerprint also changes when any listed rule input changes.
REPORT_RULES_VERSION = 15


@dataclass(frozen=True)
class ReportHistoryWindow:
    """Bounded historical retrieval window for annual/quarterly reports.

    Interim reports (Q1-Q3 + year-end BKS) and official annual reports
    drive different horizons: the Hedborg credibility ledger needs
    ~8-12 quarters, while the annual valuation history benefits from
    a deeper annual tail.  Both windows are applied as *cutoffs*
    relative to ``as_of`` so a deeper offset scan can stop early
    without fetching the entire MFN sales-noise tail.
    """

    interim_lookback_years: int = 2
    annual_lookback_years: int = 5
    max_offsets: int = 12
    max_detail_fetches: int = 60
    limit_per_offset: int = 48


DEFAULT_HISTORY_WINDOW = ReportHistoryWindow()


def _history_window_values() -> dict[str, int]:
    return asdict(DEFAULT_HISTORY_WINDOW)


def report_rules_inputs() -> dict[str, Any]:
    """Return the canonical inputs that decide report evidence coverage.

    Keep title semantics in :mod:`mfn_taxonomy`; this module only fingerprints
    the already-owned predicates and the guarded distribution rules.
    """
    return {
        "version": REPORT_RULES_VERSION,
        "taxonomy": {
            "report_title_terms": list(REPORT_TITLE_TERMS),
            "feed_report_tag": REPORT_FEED_TAG,
            "feed_report_pdf_attachment_tag": REPORT_PDF_ATTACHMENT_TAG,
            "feed_interim_tag_prefix": REPORT_INTERIM_TAG_PREFIX,
            "feed_annual_tag": REPORT_ANNUAL_TAG,
            "invitation_markers": list(INVITATION_MARKERS),
            "attachment_report_terms": list(REPORT_ATTACHMENT_TERMS),
            "attachment_non_report_terms": list(NON_REPORT_ATTACHMENT_TERMS),
            "report_kind_classes": ["annual", "quarterly"],
        },
        "distribution": {
            "cis_release_path": CIS_RELEASE_PATH_RE.pattern,
            "attachment_hosts": list(ATTACHMENT_HOST_MARKERS),
            "attachment_tiers": list(ATTACHMENT_TIERS),
            "recognized_attachment_tiers": list(RECOGNIZED_ATTACHMENT_TIERS),
            "main_path_marker": "/main/",
        },
        "completeness": {
            "grouping": "bilingual_dedupe_without_feed_group_id",
            "class_rule": "report_kind_annual_or_quarterly",
        },
        "fiscal_interpretation": {
            "annual_gate": "guarded-document-type-before-broad-report-kind",
            "compound_annual_heading": "annual-and-sustainability-report",
            "forecast_context_guard": ["forecast", "forecasts", "forecasting"],
            "compound_annual_publication_year_guard": {
                "publication_markers": ["published", "publication"],
                "explicit_covered_year_cue": "for fiscal year",
            },
        },
        "history_window": _history_window_values(),
    }


def report_rules_fingerprint() -> str:
    canonical = json.dumps(
        report_rules_inputs(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def report_rules_metadata() -> dict[str, Any]:
    """Return the compact rule stamp stored in every frozen packet."""
    inputs = report_rules_inputs()
    return {
        "version": REPORT_RULES_VERSION,
        "fingerprint": report_rules_fingerprint(),
        "history_window": inputs["history_window"],
    }


def current_report_rules_fingerprint() -> str:
    """Fingerprint for the production history window."""
    return report_rules_fingerprint()
