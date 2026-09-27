"""MFN integration for append-only evidence revisions.

The storage and repository components deliberately do not choose report slots.
This adapter turns one validated flow candidate into their immutable records;
the selection manifest remains the only winner-selection boundary.
"""

from __future__ import annotations

import hashlib
import io
import json
from importlib.metadata import PackageNotFoundError, version
from typing import Any


def _extractor_version() -> str:
    try:
        return version("pypdf")
    except PackageNotFoundError:
        return "unknown"


def _page_checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RevisionRecorder:
    """Append independently owned MFN candidate observations for one run."""

    def __init__(
        self,
        conn: Any,
        artifact_store: Any,
        *,
        company_id: int,
        as_of: str,
        source_input_fingerprint: str,
        report_rules_fingerprint: str,
        effective_at: str,
        max_pages: int,
    ) -> None:
        from alphaforge.db.evidence_repository import (
            ObservationBatchInput,
            append_observation_batch,
        )

        self.conn = conn
        self.artifact_store = artifact_store
        self.company_id = company_id
        self.report_rules_fingerprint = report_rules_fingerprint
        self.effective_at = effective_at
        self.config_fingerprint = hashlib.sha256(
            json.dumps(
                {"extractor": "pypdf", "max_pages": max_pages},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        self.batch = append_observation_batch(
            conn,
            ObservationBatchInput(
                company_id=company_id,
                as_of=as_of[:10],
                source_input_fingerprint=source_input_fingerprint,
                report_rules_fingerprint=report_rules_fingerprint,
                effective_at=effective_at,
                first_recorded_at=effective_at,
            ),
        )

    @property
    def batch_id(self) -> str:
        return str(self.batch["batch_id"])

    def record(
        self,
        article: dict[str, Any],
        *,
        eligibility: str,
        eligibility_reason: str,
        downloaded: Any | None = None,
        extracted: Any | None = None,
    ) -> dict[str, Any]:
        """Append one candidate state; bytes/extraction are optional for failures."""
        from alphaforge.db.evidence_repository import (
            ArtifactInput,
            ArtifactObjectInput,
            AttachmentObservationInput,
            CandidateInput,
            CandidateObservationInput,
            ExtractionInput,
            ExtractionPage,
            append_artifact,
            append_artifact_object,
            append_attachment_observation,
            append_candidate,
            append_candidate_observation,
            append_extraction,
        )

        source_url = str(article.get("source_url") or article.get("url") or "")
        if not source_url:
            raise ValueError("revision candidate requires source_url")
        candidate = append_candidate(
            self.conn,
            CandidateInput(
                company_id=self.company_id,
                release_source_url=source_url,
                first_observed_at=self.effective_at,
            ),
        )
        attachment_row = None
        extraction_row = None
        artifact_row = None
        if downloaded is not None:
            stored = self.artifact_store.put_pdf(io.BytesIO(downloaded.content))
            artifact_row = append_artifact(
                self.conn,
                ArtifactInput(
                    sha256=stored.sha256,
                    byte_size=stored.byte_size,
                    content_type=downloaded.content_type,
                    first_observed_at=self.effective_at,
                ),
            )
            append_artifact_object(
                self.conn,
                ArtifactObjectInput(
                    artifact_id=artifact_row["artifact_id"],
                    object_uri=stored.object_uri,
                    storage_kind="local_cas",
                    verified_sha256=stored.sha256,
                    verified_size=stored.byte_size,
                    stored_at=self.effective_at,
                ),
            )
            attachment_row = append_attachment_observation(
                self.conn,
                AttachmentObservationInput(
                    candidate_key=candidate["candidate_key"],
                    artifact_id=artifact_row["artifact_id"],
                    batch_id=self.batch_id,
                    attachment_source_url=downloaded.source_url,
                    content_type=downloaded.content_type,
                    http_status=downloaded.http_status,
                    feed_attachment_attested=(
                        downloaded.source_url
                        == str(article.get("feed_report_attachment_url") or "")
                    ),
                    raw_metadata={"attachment_tier": article.get("attachment_tier")},
                ),
            )
        if extracted is not None and artifact_row is not None:
            pages = tuple(
                ExtractionPage(
                    page_number=int(page["page_number"]),
                    anchor=str(
                        page.get("anchor")
                        or f"artifact:{artifact_row['artifact_id']}#page:{int(page['page_number'])}"
                    ),
                    text=str(page.get("text") or ""),
                    text_checksum=_page_checksum(str(page.get("text") or "")),
                )
                for page in extracted.pages
            )
            extraction_row = append_extraction(
                self.conn,
                ExtractionInput(
                    artifact_id=artifact_row["artifact_id"],
                    extractor="pypdf",
                    extractor_version=_extractor_version(),
                    config_fingerprint=self.config_fingerprint,
                    text_checksum=hashlib.sha256(
                        str(extracted.text).encode("utf-8")
                    ).hexdigest(),
                    page_count=int(extracted.page_count),
                    pages_included=extracted.pages_included,
                    page_truncated=bool(extracted.page_truncated),
                    scanned=bool(extracted.scanned),
                    limitations=tuple(extracted.limitations),
                    extracted_at=self.effective_at,
                    pages=pages,
                ),
            )
        metadata = {
            key: article[key]
            for key in (
                "attachment_tier",
                "language_evidence",
                "observation_date",
                "provider_event_id",
                "mfn_event_id",
                "mfn_slug",
            )
            if article.get(key) is not None
        }
        observation = append_candidate_observation(
            self.conn,
            CandidateObservationInput(
                candidate_key=candidate["candidate_key"],
                batch_id=self.batch_id,
                authoritative_feed_title=str(article.get("title") or ""),
                detail_title=str(article.get("detail_title") or article.get("title") or ""),
                published_at=article.get("published_at"),
                language=article.get("pdf_language")
                or article.get("ingested_lang")
                or article.get("lang"),
                report_kind=article.get("report_kind"),
                document_type=article.get("document_type"),
                fiscal_period=article.get("fiscal_period") or article.get("report_period"),
                period_start=article.get("period_start"),
                period_end=article.get("period_end") or article.get("report_period_end"),
                feed_report_identity=article.get("feed_report_identity"),
                invitation_veto=bool(article.get("invitation_veto")),
                eligibility=eligibility,
                eligibility_reason=eligibility_reason,
                attachment_observation_id=(
                    attachment_row["attachment_observation_id"] if attachment_row else None
                ),
                extraction_id=extraction_row["extraction_id"] if extraction_row else None,
                report_rules_fingerprint=self.report_rules_fingerprint,
                raw_metadata=metadata,
            ),
        )
        return {
            **observation,
            "candidate_key": candidate["candidate_key"],
            "artifact_id": artifact_row["artifact_id"] if artifact_row else None,
            "attachment_observation_id": (
                attachment_row["attachment_observation_id"] if attachment_row else None
            ),
            "extraction_id": extraction_row["extraction_id"] if extraction_row else None,
        }

    def record_relation(
        self,
        left: dict[str, Any],
        right: dict[str, Any],
        *,
        relation_type: str,
        disposition: str,
        corroboration: dict[str, Any],
    ) -> dict[str, Any]:
        from alphaforge.db.evidence_repository import (
            RelationObservationInput,
            append_relation_observation,
        )

        return append_relation_observation(
            self.conn,
            RelationObservationInput(
                batch_id=self.batch_id,
                left_candidate_observation_id=left["candidate_observation_id"],
                right_candidate_observation_id=right["candidate_observation_id"],
                relation_type=relation_type,
                disposition=disposition,
                corroboration=corroboration,
                rules_fingerprint=self.report_rules_fingerprint,
            ),
        )
