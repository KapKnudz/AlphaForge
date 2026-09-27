"""Immutable local content-addressed storage for retained PDF bytes."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
import uuid
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
        self._mkdir_durable(staging_directory)
        staging_fd = self._open_store_directory(".tmp")
        temporary_name = f"pdf-{uuid.uuid4().hex}"
        destination_fd: int | None = None
        temporary_created = False
        digest = hashlib.sha256()
        byte_size = 0
        prefix = bytearray()

        try:
            fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=staging_fd,
            )
            temporary_created = True
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
            self._mkdir_durable(destination.parent)
            destination_fd = self._open_store_directory(sha256[:2])
            try:
                os.link(
                    temporary_name,
                    destination.name,
                    src_dir_fd=staging_fd,
                    dst_dir_fd=destination_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                self._verify_file(
                    destination,
                    sha256,
                    byte_size,
                    return_bytes=False,
                    fsync=True,
                    directory_fd=destination_fd,
                )
            os.fsync(destination_fd)

            return StoredPdfArtifact(
                sha256=sha256,
                byte_size=byte_size,
                object_uri=self._object_uri(sha256),
            )
        finally:
            if destination_fd is not None:
                os.close(destination_fd)
            if temporary_created:
                try:
                    os.unlink(temporary_name, dir_fd=staging_fd)
                except FileNotFoundError:
                    pass
            os.close(staging_fd)

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
        fsync: bool = False,
        directory_fd: int | None = None,
    ) -> bytes | None:
        digest = hashlib.sha256()
        byte_size = 0
        prefix = bytearray()
        content = bytearray() if return_bytes else None

        with self._open_regular_file(path, directory_fd=directory_fd) as artifact_file:
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
            if fsync:
                os.fsync(artifact_file.fileno())

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

    def _open_regular_file(self, path: Path, *, directory_fd: int | None = None) -> BinaryIO:
        owns_directory_fd = directory_fd is None
        if directory_fd is None:
            directory_fd = self._open_store_directory(path.parent.name)
        try:
            path_mode = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False).st_mode
            if not stat.S_ISREG(path_mode):
                raise ArtifactChecksumMismatchError(
                    "artifact_checksum_mismatch",
                    "retained object path is not a regular file",
                )
            file_fd = os.open(
                path.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=directory_fd,
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise ArtifactChecksumMismatchError(
                    "artifact_checksum_mismatch",
                    "retained object path is not a regular file",
                ) from exc
            raise
        finally:
            if owns_directory_fd:
                os.close(directory_fd)

        try:
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise ArtifactChecksumMismatchError(
                    "artifact_checksum_mismatch",
                    "retained object path is not a regular file",
                )
            return os.fdopen(file_fd, "rb")
        except BaseException:
            os.close(file_fd)
            raise

    def _open_store_directory(self, child: str) -> int:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        root_fd = self._open_directory(self.root, flags=flags)
        try:
            sha256_fd = self._open_directory("sha256", flags=flags, dir_fd=root_fd)
        finally:
            os.close(root_fd)
        try:
            return self._open_directory(child, flags=flags, dir_fd=sha256_fd)
        finally:
            os.close(sha256_fd)

    @staticmethod
    def _open_directory(path: Path | str, *, flags: int, dir_fd: int | None = None) -> int:
        try:
            return os.open(path, flags, dir_fd=dir_fd)
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise ArtifactChecksumMismatchError(
                    "artifact_checksum_mismatch",
                    "artifact store path contains a non-directory component",
                ) from exc
            raise

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

    def _mkdir_durable(self, path: Path) -> None:
        self._validate_store_directories(path)
        missing = []
        current = path
        while not current.exists():
            missing.append(current)
            current = current.parent

        for directory in reversed(missing):
            try:
                directory.mkdir()
            except FileExistsError:
                if not directory.is_dir():
                    raise

        self._validate_store_directories(path)
        durability_root = missing[-1] if self.root in missing else self.root
        current = path
        while True:
            self._fsync_directory(current)
            if current == durability_root:
                self._fsync_directory(current.parent)
                break
            current = current.parent

    def _validate_store_directories(self, path: Path) -> None:
        current = self.root
        directories = [current]
        for part in path.relative_to(self.root).parts:
            current /= part
            directories.append(current)

        for directory in directories:
            try:
                mode = directory.lstat().st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISDIR(mode):
                raise ArtifactChecksumMismatchError(
                    "artifact_checksum_mismatch",
                    "artifact store path contains a non-directory component",
                )

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        directory_fd = LocalPdfArtifactStore._open_directory(
            path,
            flags=os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
