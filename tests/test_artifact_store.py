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


def seed_object(root: Path, content: bytes) -> Path:
    path = object_path(root, content)
    path.parent.mkdir(parents=True)
    (root / "sha256" / ".tmp").mkdir()
    path.write_bytes(content)
    return path


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


def test_put_durably_creates_each_directory_and_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "store" / "objects"
    store = LocalPdfArtifactStore(root)
    synced: list[Path] = []
    monkeypatch.setattr(store, "_fsync_directory", synced.append)

    artifact = store.put_pdf(io.BytesIO(PDF_A))

    sha256_directory = root / "sha256"
    prefix_directory = sha256_directory / artifact.sha256[:2]
    assert synced == [
        sha256_directory / ".tmp",
        sha256_directory,
        root,
        tmp_path / "store",
        tmp_path,
        prefix_directory,
        sha256_directory,
        root,
        tmp_path / "store",
        prefix_directory,
    ]


def test_put_syncs_directories_created_by_concurrent_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalPdfArtifactStore(tmp_path)
    sha256_directory = tmp_path / "sha256"
    staging_directory = sha256_directory / ".tmp"
    digest = hashlib.sha256(PDF_A).hexdigest()
    prefix_directory = sha256_directory / digest[:2]
    race_directories = {staging_directory, prefix_directory}
    raced: set[Path] = set()
    real_mkdir = Path.mkdir

    def mkdir_with_race(path: Path, *args: object, **kwargs: object) -> None:
        if path in race_directories and path not in raced:
            raced.add(path)
            real_mkdir(path, *args, **kwargs)
            raise FileExistsError
        real_mkdir(path, *args, **kwargs)

    synced: list[Path] = []
    monkeypatch.setattr(Path, "mkdir", mkdir_with_race)
    monkeypatch.setattr(store, "_fsync_directory", synced.append)

    artifact = store.put_pdf(io.BytesIO(PDF_A))

    assert artifact.sha256 == digest
    assert raced == race_directories
    assert synced == [
        staging_directory,
        sha256_directory,
        tmp_path,
        tmp_path.parent,
        prefix_directory,
        sha256_directory,
        tmp_path,
        tmp_path.parent,
        prefix_directory,
    ]


def test_put_syncs_directories_already_visible_during_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalPdfArtifactStore(tmp_path)
    sha256_directory = tmp_path / "sha256"
    staging_directory = sha256_directory / ".tmp"
    digest = hashlib.sha256(PDF_A).hexdigest()
    prefix_directory = sha256_directory / digest[:2]
    staging_directory.mkdir(parents=True)
    prefix_directory.mkdir()
    synced: list[Path] = []
    monkeypatch.setattr(store, "_fsync_directory", synced.append)

    artifact = store.put_pdf(io.BytesIO(PDF_A))

    assert artifact.sha256 == digest
    assert synced == [
        staging_directory,
        sha256_directory,
        tmp_path,
        tmp_path.parent,
        prefix_directory,
        sha256_directory,
        tmp_path,
        tmp_path.parent,
        prefix_directory,
    ]


def test_repeated_put_reuses_verified_object_without_overwrite(tmp_path: Path) -> None:
    store = LocalPdfArtifactStore(tmp_path)
    first = store.put_pdf(io.BytesIO(PDF_A))
    path = object_path(tmp_path, PDF_A)
    initial_stat = path.stat()

    second = store.put_pdf(io.BytesIO(PDF_A))

    assert second == first
    assert path.stat().st_ino == initial_stat.st_ino
    assert path.read_bytes() == PDF_A


def test_put_makes_verified_concurrent_object_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = seed_object(tmp_path, PDF_A)
    store = LocalPdfArtifactStore(tmp_path)
    synced_files: list[Path] = []
    synced_directories: list[Path] = []
    monkeypatch.setattr(store, "_fsync_file", synced_files.append)
    monkeypatch.setattr(store, "_fsync_directory", synced_directories.append)

    artifact = store.put_pdf(io.BytesIO(PDF_A))

    assert artifact.sha256 == hashlib.sha256(PDF_A).hexdigest()
    assert synced_files == [path]
    assert synced_directories[-1] == path.parent
    assert path.read_bytes() == PDF_A


def test_put_propagates_hierarchy_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = LocalPdfArtifactStore(tmp_path)

    def fail_fsync(directory: Path) -> None:
        if directory == tmp_path / "sha256":
            raise OSError("hierarchy fsync failed")

    monkeypatch.setattr(store, "_fsync_directory", fail_fsync)

    with pytest.raises(OSError, match="hierarchy fsync failed"):
        store.put_pdf(io.BytesIO(PDF_A))
    assert not list(tmp_path.rglob("*.pdf"))


def test_put_propagates_reused_file_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_object(tmp_path, PDF_A)
    store = LocalPdfArtifactStore(tmp_path)

    def fail_fsync(_path: Path) -> None:
        raise OSError("file fsync failed")

    monkeypatch.setattr(store, "_fsync_file", fail_fsync)

    with pytest.raises(OSError, match="file fsync failed"):
        store.put_pdf(io.BytesIO(PDF_A))


def test_put_propagates_reused_link_directory_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = seed_object(tmp_path, PDF_A)
    store = LocalPdfArtifactStore(tmp_path)
    destination_syncs = 0

    def fail_final_destination_sync(directory: Path) -> None:
        nonlocal destination_syncs
        if directory == path.parent:
            destination_syncs += 1
            if destination_syncs == 2:
                raise OSError("directory fsync failed")

    monkeypatch.setattr(store, "_fsync_directory", fail_final_destination_sync)

    with pytest.raises(OSError, match="directory fsync failed"):
        store.put_pdf(io.BytesIO(PDF_A))
    assert destination_syncs == 2


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
