from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest

from alphaforge.evidence.artifact_store import (
    ArtifactChecksumMismatchError,
    ArtifactUnavailableError,
    ArtifactValidationError,
    LocalPdfArtifactStore,
)

PDF_A = b"%PDF-1.7\nfirst retained report\n%%EOF\n"
PDF_B = b"%PDF-1.7\nrevised retained report\n%%EOF\n"


def object_path(root: Path, content: bytes) -> Path:
    sha256 = hashlib.sha256(content).hexdigest()
    return root / "sha256" / sha256[:2] / f"{sha256}.pdf"


def test_put_streams_pdf_to_content_addressed_path_and_reads_verified_bytes(
    tmp_path: Path,
) -> None:
    store = LocalPdfArtifactStore(tmp_path, chunk_size=7)

    artifact = store.put_pdf(io.BytesIO(PDF_A))

    assert artifact.sha256 == hashlib.sha256(PDF_A).hexdigest()
    assert artifact.byte_size == len(PDF_A)
    assert artifact.object_uri == f"file:sha256/{artifact.sha256[:2]}/{artifact.sha256}.pdf"
    assert object_path(tmp_path, PDF_A).read_bytes() == PDF_A
    assert store.read_pdf(artifact.sha256, expected_size=artifact.byte_size) == PDF_A
    assert not list((tmp_path / "sha256" / ".tmp").iterdir())


def test_repeated_put_reuses_verified_object_without_overwrite(tmp_path: Path) -> None:
    store = LocalPdfArtifactStore(tmp_path)
    first = store.put_pdf(io.BytesIO(PDF_A))
    path = object_path(tmp_path, PDF_A)
    initial_stat = path.stat()

    second = store.put_pdf(io.BytesIO(PDF_A))

    assert second == first
    assert path.stat().st_ino == initial_stat.st_ino
    assert path.read_bytes() == PDF_A


def test_changed_bytes_create_new_object_and_preserve_original(tmp_path: Path) -> None:
    store = LocalPdfArtifactStore(tmp_path)

    first = store.put_pdf(io.BytesIO(PDF_A))
    second = store.put_pdf(io.BytesIO(PDF_B))

    assert first.sha256 != second.sha256
    assert object_path(tmp_path, PDF_A).read_bytes() == PDF_A
    assert object_path(tmp_path, PDF_B).read_bytes() == PDF_B


def test_existing_corrupt_destination_fails_without_overwrite(tmp_path: Path) -> None:
    store = LocalPdfArtifactStore(tmp_path)
    path = object_path(tmp_path, PDF_A)
    path.parent.mkdir(parents=True)
    path.write_bytes(PDF_B)

    with pytest.raises(ArtifactChecksumMismatchError) as error:
        store.put_pdf(io.BytesIO(PDF_A))

    assert error.value.code == "artifact_checksum_mismatch"
    assert path.read_bytes() == PDF_B


def test_read_reports_missing_and_corrupt_objects_explicitly(tmp_path: Path) -> None:
    store = LocalPdfArtifactStore(tmp_path)
    artifact = store.put_pdf(io.BytesIO(PDF_A))
    path = object_path(tmp_path, PDF_A)
    path.unlink()

    with pytest.raises(ArtifactUnavailableError) as missing:
        store.read_pdf(artifact.sha256, expected_size=artifact.byte_size)
    assert missing.value.code == "artifact_unavailable"

    path.write_bytes(PDF_B)
    with pytest.raises(ArtifactChecksumMismatchError) as corrupt:
        store.read_pdf(artifact.sha256, expected_size=artifact.byte_size)
    assert corrupt.value.code == "artifact_checksum_mismatch"


def test_read_rejects_recorded_size_mismatch(tmp_path: Path) -> None:
    store = LocalPdfArtifactStore(tmp_path)
    artifact = store.put_pdf(io.BytesIO(PDF_A))

    with pytest.raises(ArtifactChecksumMismatchError) as error:
        store.read_pdf(artifact.sha256, expected_size=artifact.byte_size - 1)

    assert error.value.code == "artifact_checksum_mismatch"


@pytest.mark.parametrize(
    ("content", "max_pdf_bytes", "code"),
    [
        (b"not a pdf", 100, "invalid_pdf_magic"),
        (PDF_A, len(PDF_A) - 1, "resource_limit"),
    ],
)
def test_put_rejects_invalid_or_oversized_pdf_and_removes_temporary_file(
    tmp_path: Path,
    content: bytes,
    max_pdf_bytes: int,
    code: str,
) -> None:
    store = LocalPdfArtifactStore(tmp_path, max_pdf_bytes=max_pdf_bytes, chunk_size=6)

    with pytest.raises(ArtifactValidationError) as error:
        store.put_pdf(io.BytesIO(content))

    assert error.value.code == code
    assert not list((tmp_path / "sha256" / ".tmp").iterdir())
    assert not list(tmp_path.rglob("*.pdf"))


def test_read_rejects_invalid_hash_without_resolving_outside_root(tmp_path: Path) -> None:
    store = LocalPdfArtifactStore(tmp_path)

    with pytest.raises(ArtifactValidationError) as error:
        store.read_pdf("../not-an-object", expected_size=1)

    assert error.value.code == "invalid_sha256"
