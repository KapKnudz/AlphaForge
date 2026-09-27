"""Immutable local content-addressed storage for retained PDF bytes."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

DEFAULT_OBJECT_ROOT = Path("data/evidence/objects")
DEFAULT_MAX_PDF_BYTES = 25 * 1024 * 1024
DEFAULT_CHUNK_SIZE = 64 * 1024
_PDF_MAGIC = b"%PDF-"


class ArtifactStoreError(Exception):
    """Base class for explicit artifact-store failures."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class ArtifactValidationError(ArtifactStoreError, ValueError):
    """The proposed artifact identity or PDF bytes are invalid."""


class ArtifactUnavailableError(ArtifactStoreError, FileNotFoundError):
    """The requested retained artifact is absent."""


class ArtifactChecksumMismatchError(ArtifactStoreError):
    """A retained object does not match its expected immutable identity."""


@dataclass(frozen=True)
class StoredPdfArtifact:
    """Metadata returned after durable local installation or verified reuse."""

    sha256: str
    byte_size: int
    object_uri: str


class LocalPdfArtifactStore:
    """Store and verify immutable PDFs under a local SHA-256 object root."""

    def __init__(
        self,
        root: Path | str = DEFAULT_OBJECT_ROOT,
        *,
        max_pdf_bytes: int = DEFAULT_MAX_PDF_BYTES,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        if max_pdf_bytes < len(_PDF_MAGIC):
            raise ValueError("max_pdf_bytes must accommodate PDF magic bytes")
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self.root = Path(root)
        self.max_pdf_bytes = max_pdf_bytes
        self.chunk_size = chunk_size

    def put_pdf(self, source: BinaryIO) -> StoredPdfArtifact:
        """Stream, validate, and durably install one PDF without replacement."""
        staging_directory = self.root / "sha256" / ".tmp"
        staging_directory.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix="pdf-", dir=staging_directory)
        temporary_path = Path(temporary_name)
        digest = hashlib.sha256()
        byte_size = 0
        prefix = bytearray()

        try:
            with os.fdopen(fd, "wb") as temporary_file:
                while chunk := source.read(self.chunk_size):
                    if not isinstance(chunk, bytes):
                        raise TypeError("PDF source must be a binary stream")
                    byte_size += len(chunk)
                    if byte_size > self.max_pdf_bytes:
                        raise ArtifactValidationError(
                            "resource_limit", "PDF exceeds the configured byte limit"
                        )
                    if len(prefix) < len(_PDF_MAGIC):
                        prefix.extend(chunk[: len(_PDF_MAGIC) - len(prefix)])
                    digest.update(chunk)
                    temporary_file.write(chunk)
                if bytes(prefix) != _PDF_MAGIC:
                    raise ArtifactValidationError(
                        "invalid_pdf_magic", "artifact does not start with PDF magic bytes"
                    )
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            sha256 = digest.hexdigest()
            destination = self._object_path(sha256)
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.link(temporary_path, destination)
            except FileExistsError:
                self._verify_file(destination, sha256, byte_size, return_bytes=False)
            else:
                self._fsync_directory(destination.parent)

            return StoredPdfArtifact(
                sha256=sha256,
                byte_size=byte_size,
                object_uri=self._object_uri(sha256),
            )
        finally:
            temporary_path.unlink(missing_ok=True)

    def read_pdf(self, sha256: str, *, expected_size: int) -> bytes:
        """Return bytes only after streaming hash, size, limit, and PDF checks pass."""
        self._validate_sha256(sha256)
        if (
            not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or expected_size < 0
        ):
            raise ArtifactValidationError(
                "invalid_size", "expected_size must be a non-negative integer"
            )
        if expected_size > self.max_pdf_bytes:
            raise ArtifactValidationError(
                "resource_limit", "expected PDF size exceeds the configured byte limit"
            )
        path = self._object_path(sha256)
        try:
            result = self._verify_file(path, sha256, expected_size, return_bytes=True)
        except FileNotFoundError as exc:
            raise ArtifactUnavailableError(
                "artifact_unavailable", f"retained object is missing: {self._object_uri(sha256)}"
            ) from exc
        assert result is not None
        return result

    def _verify_file(
        self,
        path: Path,
        expected_sha256: str,
        expected_size: int,
        *,
        return_bytes: bool,
    ) -> bytes | None:
        digest = hashlib.sha256()
        byte_size = 0
        prefix = bytearray()
        content = bytearray() if return_bytes else None

        with path.open("rb") as artifact_file:
            while chunk := artifact_file.read(self.chunk_size):
                byte_size += len(chunk)
                if byte_size > expected_size:
                    raise ArtifactChecksumMismatchError(
                        "artifact_checksum_mismatch",
                        "retained object exceeds its expected byte size",
                    )
                if len(prefix) < len(_PDF_MAGIC):
                    prefix.extend(chunk[: len(_PDF_MAGIC) - len(prefix)])
                digest.update(chunk)
                if content is not None:
                    content.extend(chunk)

        actual_sha256 = digest.hexdigest()
        if byte_size != expected_size or actual_sha256 != expected_sha256:
            raise ArtifactChecksumMismatchError(
                "artifact_checksum_mismatch",
                "retained object does not match its expected SHA-256 and byte size",
            )
        if bytes(prefix) != _PDF_MAGIC:
            raise ArtifactValidationError(
                "invalid_pdf_magic", "retained object does not start with PDF magic bytes"
            )
        return bytes(content) if content is not None else None

    def _object_path(self, sha256: str) -> Path:
        self._validate_sha256(sha256)
        return self.root / "sha256" / sha256[:2] / f"{sha256}.pdf"

    @staticmethod
    def _object_uri(sha256: str) -> str:
        return f"file:sha256/{sha256[:2]}/{sha256}.pdf"

    @staticmethod
    def _validate_sha256(sha256: str) -> None:
        if len(sha256) != 64 or any(character not in "0123456789abcdef" for character in sha256):
            raise ArtifactValidationError(
                "invalid_sha256", "SHA-256 must be 64 lowercase hexadecimal characters"
            )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        directory_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
