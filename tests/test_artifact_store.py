from __future__ import annotations

import hashlib
import io
import os
import stat
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
    target_stat = path.stat()
    target_directory_stat = path.parent.stat()

    def record_fsync(file_descriptor: int) -> None:
        descriptor_stat = os.fstat(file_descriptor)
        identity = (descriptor_stat.st_dev, descriptor_stat.st_ino)
        if stat.S_ISREG(descriptor_stat.st_mode) and identity == (
            target_stat.st_dev,
            target_stat.st_ino,
        ):
            synced_files.append(path)
        if stat.S_ISDIR(descriptor_stat.st_mode) and identity == (
            target_directory_stat.st_dev,
            target_directory_stat.st_ino,
        ):
            synced_directories.append(path.parent)

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(store, "_fsync_directory", synced_directories.append)

    artifact = store.put_pdf(io.BytesIO(PDF_A))

    assert artifact.sha256 == hashlib.sha256(PDF_A).hexdigest()
    assert synced_files == [path]
    assert path.parent in synced_directories
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
    path = seed_object(tmp_path, PDF_A)
    store = LocalPdfArtifactStore(tmp_path)
    target_stat = path.stat()

    def fail_fsync(file_descriptor: int) -> None:
        descriptor_stat = os.fstat(file_descriptor)
        if (descriptor_stat.st_dev, descriptor_stat.st_ino) == (
            target_stat.st_dev,
            target_stat.st_ino,
        ):
            raise OSError("file fsync failed")

    monkeypatch.setattr(os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="file fsync failed"):
        store.put_pdf(io.BytesIO(PDF_A))


def test_put_propagates_reused_link_directory_fsync_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = seed_object(tmp_path, PDF_A)
    store = LocalPdfArtifactStore(tmp_path)
    target_stat = path.parent.stat()
    destination_syncs = 0

    def fail_final_destination_sync(file_descriptor: int) -> None:
        nonlocal destination_syncs
        descriptor_stat = os.fstat(file_descriptor)
        if (descriptor_stat.st_dev, descriptor_stat.st_ino) == (
            target_stat.st_dev,
            target_stat.st_ino,
        ):
            destination_syncs += 1
            if destination_syncs == 2:
                raise OSError("directory fsync failed")

    monkeypatch.setattr(os, "fsync", fail_final_destination_sync)

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


@pytest.mark.parametrize("object_kind", ["symlink", "directory"])
def test_read_and_put_reject_non_regular_object_paths(tmp_path: Path, object_kind: str) -> None:
    root = tmp_path / "objects"
    store = LocalPdfArtifactStore(root)
    digest = hashlib.sha256(PDF_A).hexdigest()
    path = object_path(root, PDF_A)
    path.parent.mkdir(parents=True)

    if object_kind == "symlink":
        external = tmp_path / "external.pdf"
        external.write_bytes(PDF_A)
        path.symlink_to(external)
    else:
        path.mkdir()

    with pytest.raises(ArtifactChecksumMismatchError) as read_error:
        store.read_pdf(digest, expected_size=len(PDF_A))
    assert read_error.value.code == "artifact_checksum_mismatch"

    with pytest.raises(ArtifactChecksumMismatchError) as put_error:
        store.put_pdf(io.BytesIO(PDF_A))
    assert put_error.value.code == "artifact_checksum_mismatch"

    if object_kind == "symlink":
        assert path.is_symlink()
        assert external.read_bytes() == PDF_A
    else:
        assert path.is_dir()


def test_put_and_read_reject_symlinked_object_prefix(tmp_path: Path) -> None:
    root = tmp_path / "objects"
    external = tmp_path / "external"
    external.mkdir()
    sha256_directory = root / "sha256"
    sha256_directory.mkdir(parents=True)
    digest = hashlib.sha256(PDF_A).hexdigest()
    prefix = sha256_directory / digest[:2]
    prefix.symlink_to(external, target_is_directory=True)
    store = LocalPdfArtifactStore(root)

    with pytest.raises(ArtifactChecksumMismatchError) as new_put_error:
        store.put_pdf(io.BytesIO(PDF_A))
    assert new_put_error.value.code == "artifact_checksum_mismatch"
    assert not list(external.iterdir())

    external_object = external / f"{digest}.pdf"
    external_object.write_bytes(PDF_A)
    with pytest.raises(ArtifactChecksumMismatchError) as read_error:
        store.read_pdf(digest, expected_size=len(PDF_A))
    assert read_error.value.code == "artifact_checksum_mismatch"

    with pytest.raises(ArtifactChecksumMismatchError) as reuse_error:
        store.put_pdf(io.BytesIO(PDF_A))
    assert reuse_error.value.code == "artifact_checksum_mismatch"
    assert external_object.read_bytes() == PDF_A


def test_put_rejects_symlinked_staging_directory(tmp_path: Path) -> None:
    root = tmp_path / "objects"
    external = tmp_path / "external"
    external.mkdir()
    sha256_directory = root / "sha256"
    sha256_directory.mkdir(parents=True)
    (sha256_directory / ".tmp").symlink_to(external, target_is_directory=True)
    store = LocalPdfArtifactStore(root)

    with pytest.raises(ArtifactChecksumMismatchError) as error:
        store.put_pdf(io.BytesIO(PDF_A))

    assert error.value.code == "artifact_checksum_mismatch"
    assert not list(external.iterdir())


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
