"""ResearchDocumentIngestionService — 50+tail, both URLs, bilingual dedupe."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any


def _detect_lang(text: str) -> tuple[str, float]:
    # Light heuristic: Swedish chars and words
    sv_markers = [" och ", " att ", " för ", " är ", "ä", "ö", "å", "bokslut", "delår"]
    lower = text.lower()
    score = sum(1 for m in sv_markers if m in lower)
    if score >= 2:
        return ("sv", 0.8)
    return ("en", 0.6)


def bilingual_dedupe(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group by normalized key and calendar date, keep one per logical release.

    Returns deduplicated docs with duplicate_of pointer for suppressed variants.
    """
    from collections import defaultdict

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in docs:
        url = d.get("source_url") or d.get("url") or ""
        # Bilingual key: canonical URL without lang suffix + storage_id (and mfn_slug if present)
        # Title slug is NOT included — Swedish/English titles differ for same release.
        mfn_slug = d.get("mfn_slug") or d.get("slug") or ""
        canon = re.sub(r"/(sv|en)(/|$)", "/", url.lower()) if url else ""
        storage_id = ""
        m = re.search(r"storage\.mfn\.se/([^/?#]+)", url) if url else None
        if m:
            storage_id = m.group(1)
        # Also consider checksum dedupe: if bodies are translations, canonical URL grouping is primary
        # Fallback to title slug only when URL grouping would be empty (no url)
        if not url:
            title = d.get("title") or ""
            base = (
                re.sub(r"\b(inbjudan|invitation to)\b", "", title, flags=re.IGNORECASE)
                .strip()
                .lower()
            )
            base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
            key = base
        else:
            key = f"{mfn_slug}|{canon}|{storage_id}"
        # Include calendar date if available (published_at date part)
        pub = (d.get("published_at") or "")[:10]
        group_key = f"{key}|{pub}" if pub else key
        groups[group_key].append(d)

    out: list[dict[str, Any]] = []
    for _gk, variants in groups.items():
        if len(variants) == 1:
            out.append(variants[0])
            continue

        # When size 2, prefer variant matching packet majority lang, otherwise en
        # For dedupe at ingest, prefer 'en' deterministically if langs differ.
        # Mark suppressed with duplicate_of pointer.
        # Sort: en preferred, then longer body
        def sort_key(v: dict[str, Any]) -> tuple[int, int]:
            lang = (v.get("ingested_lang") or v.get("lang") or "en").lower()
            pref = 0 if lang == "en" else 1
            body_len = len(v.get("content_text") or v.get("body") or "")
            return (pref, -body_len)

        variants_sorted = sorted(variants, key=sort_key)
        preferred = variants_sorted[0]
        out.append(preferred)
        # suppressed variants are not appended to out, but we record duplicate_of
        # via a synthetic field that persist will use
        for suppressed in variants_sorted[1:]:
            suppressed["duplicate_of"] = preferred.get("source_url") or preferred.get("url")
            suppressed["ingest_status"] = "superseded_by_translation"
            # We keep one suppressed row in DB with duplicate_of pointer for audit,
            # but not in the returned packet.
            # Persist caller should insert suppressed with duplicate_of.
            # We still return suppressed in a side list for DB insert; here we
            # stash on preferred for caller to find.
            preferred.setdefault("_suppressed_variants", []).append(suppressed)
    return out


@dataclass
class IngestResult:
    inserted: int
    suppressed: int


class ResearchDocumentIngestionService:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def persist_articles(
        self,
        company_id: int | None,
        articles: list[dict[str, Any]],
        *,
        source_type: str = "mfn",
    ) -> IngestResult:
        """Persist articles with ON CONFLICT(source_url) and bilingual dedupe."""
        # Deduplicate before persist
        deduped = bilingual_dedupe(articles)
        inserted = 0
        suppressed = 0
        for doc in deduped:
            source_url = doc.get("source_url") or doc.get("url") or ""
            if not source_url:
                continue
            title = doc.get("title")
            body = doc.get("content_text") or doc.get("body")
            published_at = doc.get("published_at")
            lang, _ = _detect_lang(f"{title or ''} {body or ''}")
            checksum = hashlib.sha256((body or "").encode()).hexdigest() if body else None
            # Keep source_release_url distinct if present
            raw_metadata: str | None = None
            import json

            if doc.get("source_release_url"):
                raw_metadata = json.dumps({"source_release_url": doc["source_release_url"]})
            # Tranche: check bilingual key dedupe via canonical grouping already done
            # Insert deduped doc
            try:
                cur = self.conn.execute(
                    """
                    INSERT INTO research_documents
                        (company_id, source_url, source_type, title, published_at, content_text, ingested_lang, checksum, raw_metadata)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(company_id, source_url) DO NOTHING
                    """,
                    (
                        company_id,
                        source_url,
                        source_type,
                        title,
                        published_at,
                        body,
                        lang,
                        checksum,
                        raw_metadata,
                    ),
                )
                if cur.rowcount and cur.rowcount > 0:
                    inserted += 1
            except Exception:
                pass
            # Insert suppressed variants for audit with duplicate_of
            for sup in doc.get("_suppressed_variants", []):  # type: ignore[union-attr]
                sup_url = sup.get("source_url") or sup.get("url") or ""
                sup_title = sup.get("title")
                sup_body = sup.get("content_text") or sup.get("body")
                sup_lang, _ = _detect_lang(f"{sup_title or ''} {sup_body or ''}")
                sup_checksum = (
                    hashlib.sha256((sup_body or "").encode()).hexdigest() if sup_body else None
                )
                try:
                    cur = self.conn.execute(
                        """
                        INSERT INTO research_documents
                            (company_id, source_url, source_type, title, published_at, content_text, ingested_lang, checksum, duplicate_of, raw_metadata)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, (SELECT id FROM research_documents WHERE source_url=?), ?)
                        ON CONFLICT(company_id, source_url) DO NOTHING
                        """,
                        (
                            company_id,
                            sup_url,
                            source_type,
                            sup_title,
                            published_at,
                            sup_body,
                            sup_lang,
                            sup_checksum,
                            source_url,
                            None,
                        ),
                    )
                    if cur.rowcount and cur.rowcount > 0:
                        suppressed += 1
                except Exception:
                    pass
        self.conn.commit()
        return IngestResult(inserted=inserted, suppressed=suppressed)

    def extract_pdf_text(self, pdf_bytes: bytes) -> tuple[str, int, str, int]:
        """Extract text with 50+tail logic.

        Returns (text, page_count, pages_included, page_truncated_flag).
        Tail: pages[80:90] when len>50 and holder table suspected.
        """
        try:
            import io

            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(pdf_bytes))
            n = len(reader.pages)
            pages_included = ""
            truncated = 0
            texts: list[str] = []
            if n <= 50:
                for i in range(n):
                    try:
                        texts.append(reader.pages[i].extract_text() or "")
                    except Exception:
                        texts.append("")
                pages_included = f"0-{n - 1}" if n else ""
            else:
                # pages[:50]
                for i in range(50):
                    try:
                        texts.append(reader.pages[i].extract_text() or "")
                    except Exception:
                        texts.append("")
                pages_included = "0-49"
                truncated = 1
                # Check if holder table suspected in tail
                tail_needed = n > 80
                if tail_needed:
                    # Heuristic: check if first 50 contains no holder header but pdf is annual
                    combined = "\n".join(texts).lower()
                    if "aktieägare" not in combined and "shareholder" not in combined:
                        # still include tail slice 80:90
                        tail_texts: list[str] = []
                        for i in range(80, min(90, n)):
                            try:
                                tail_texts.append(reader.pages[i].extract_text() or "")
                            except Exception:
                                tail_texts.append("")
                        texts.extend(tail_texts)
                        pages_included = "0-49,80-89"
                    else:
                        # holder suspected but we still truncate flag
                        pass
            text = "\n".join(texts)
            text = re.sub(r"\n{3,}", "\n\n", text)
            # Scanned-image detection: very short text for many pages
            if n > 5 and len(text.strip()) < 200:
                # Return empty with missing_information signal via empty text
                pass
            return (text, n, pages_included, truncated)
        except ImportError:
            # pypdf not available — return empty
            return ("", 0, "", 0)
        except Exception:
            return ("", 0, "", 0)
