"""Archive inspection, cross-document, and deterministic fixture tests."""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from dataclasses import replace

import pytest

from tongs.desktop.artifact_contract import (
    ArchiveEntry,
    ArchiveEntryType,
    ArtifactContractError,
    ArtifactContractErrorCode,
    inspect_archive,
    parse_install_manifest,
    parse_release_manifest,
    validate_archive_layout,
    validate_artifact_archive,
    validate_manifest_pair,
)

from .reference_builder import (
    ARCHIVE_NAME,
    FIXTURE_ROOT,
    build_reference_artifact,
)


def _fixture() -> tuple[bytes, bytes, bytes]:
    return (
        (FIXTURE_ROOT / ARCHIVE_NAME).read_bytes(),
        (FIXTURE_ROOT / "desktop-install.synthetic.json").read_bytes(),
        (FIXTURE_ROOT / "desktop-manifest-v1.synthetic.json").read_bytes(),
    )


def _raised(code: ArtifactContractErrorCode):
    return pytest.raises(
        ArtifactContractError, match=".", check=lambda e: e.code is code
    )


def test_committed_fixture_is_reproducible_and_clearly_synthetic() -> None:
    archive, install_document, release_document = _fixture()
    built = build_reference_artifact()
    rebuilt = build_reference_artifact()

    assert built == rebuilt
    assert (built.archive, built.install_manifest, built.release_manifest) == (
        archive,
        install_document,
        release_document,
    )
    sums = (FIXTURE_ROOT / "SHA256SUMS").read_text().splitlines()
    expected = {
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
        for path in (
            FIXTURE_ROOT / ARCHIVE_NAME,
            FIXTURE_ROOT / "desktop-install.synthetic.json",
            FIXTURE_ROOT / "desktop-manifest-v1.synthetic.json",
        )
    }
    assert set(sums) == expected
    assert b"synthetic" in release_document
    assert hashlib.sha256(archive).hexdigest().encode() not in install_document


def test_reference_archive_has_canonical_root_layout_and_metadata() -> None:
    archive, _, release_document = _fixture()
    release = parse_release_manifest(release_document)
    validated = validate_artifact_archive(
        archive, ARCHIVE_NAME, release, "fedora-44-x86_64-user-archive"
    )
    assert validated.layout.file_count == 7
    assert archive[:10] == b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff"
    assert archive[10] == 1  # One final stored DEFLATE block for this tiny fixture.

    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as opened:
        members = opened.getmembers()
    assert [item.name for item in members] == sorted(item.name for item in members)
    assert all(item.uid == item.gid == item.mtime == 0 for item in members)
    assert all(item.uname == item.gname == "root" for item in members)
    assert all(not item.pax_headers for item in members)
    assert all(
        "/" in item.name
        or item.name == "runtime"
        or item.name == "desktop-install.json"
        for item in members
    )


def test_archive_validation_is_read_only(tmp_path) -> None:
    archive, _, release_document = _fixture()
    before = tuple(tmp_path.iterdir())
    validate_artifact_archive(
        archive,
        ARCHIVE_NAME,
        parse_release_manifest(release_document),
        "fedora-44-x86_64-user-archive",
    )
    assert tuple(tmp_path.iterdir()) == before


@pytest.mark.parametrize("document", [b"", b"plain text", b"\x1f\x8bnot-gzip"])
def test_malformed_archives_have_stable_errors(document: bytes) -> None:
    limits = parse_install_manifest(_fixture()[1]).extraction_limits
    with _raised(ArtifactContractErrorCode.INVALID_ARCHIVE):
        inspect_archive(document, limits)


def test_external_archive_name_length_digest_and_identity_are_authoritative() -> None:
    archive, _, release_document = _fixture()
    release = parse_release_manifest(release_document)
    for document, name, artifact_id in (
        (archive, "renamed.tar.gz", "fedora-44-x86_64-user-archive"),
        (archive + b"x", ARCHIVE_NAME, "fedora-44-x86_64-user-archive"),
        (
            archive[:-1] + bytes([archive[-1] ^ 1]),
            ARCHIVE_NAME,
            "fedora-44-x86_64-user-archive",
        ),
        (archive, ARCHIVE_NAME, "missing"),
    ):
        with _raised(ArtifactContractErrorCode.ARTIFACT_MISMATCH):
            validate_artifact_archive(document, name, release, artifact_id)


@pytest.mark.parametrize(
    "entry_type",
    [
        ArchiveEntryType.SYMLINK,
        ArchiveEntryType.HARDLINK,
        ArchiveEntryType.FIFO,
        ArchiveEntryType.CHARACTER_DEVICE,
        ArchiveEntryType.BLOCK_DEVICE,
        ArchiveEntryType.OTHER,
    ],
)
def test_layout_rejects_every_non_regular_entry_type(
    entry_type: ArchiveEntryType,
) -> None:
    archive, install_document, _ = _fixture()
    install = parse_install_manifest(install_document)
    entries = list(inspect_archive(archive, install.extraction_limits).entries)
    entries[1] = replace(entries[1], entry_type=entry_type)
    with _raised(ArtifactContractErrorCode.INVALID_LAYOUT):
        validate_archive_layout(entries, install)


@pytest.mark.parametrize(
    "replacement",
    [
        ArchiveEntry("../escape", ArchiveEntryType.FILE, 0o644, 0, "0" * 64),
        ArchiveEntry("/absolute", ArchiveEntryType.FILE, 0o644, 0, "0" * 64),
        ArchiveEntry("runtime\\escape", ArchiveEntryType.FILE, 0o644, 0, "0" * 64),
        ArchiveEntry("runtime/extra", ArchiveEntryType.FILE, 0o755, 0, "0" * 64),
        ArchiveEntry("runtime/extra", ArchiveEntryType.FILE, 0o4644, 0, "0" * 64),
    ],
)
def test_layout_rejects_traversal_extra_executable_and_special_bits(
    replacement: ArchiveEntry,
) -> None:
    archive, install_document, _ = _fixture()
    install = parse_install_manifest(install_document)
    entries = list(inspect_archive(archive, install.extraction_limits).entries)
    entries.append(replacement)
    with _raised(ArtifactContractErrorCode.INVALID_LAYOUT):
        validate_archive_layout(entries, install)


def test_layout_rejects_missing_duplicate_collision_content_and_modes() -> None:
    archive, install_document, _ = _fixture()
    install = parse_install_manifest(install_document)
    original = list(inspect_archive(archive, install.extraction_limits).entries)
    app_index = next(
        i
        for i, item in enumerate(original)
        if item.path == "runtime/resources/app.asar"
    )
    variants = [
        original[:app_index] + original[app_index + 1 :],
        original + [original[app_index]],
        original + [replace(original[app_index], path="RUNTIME/resources/app.asar")],
        [
            replace(item, sha256="0" * 64) if i == app_index else item
            for i, item in enumerate(original)
        ],
        [
            replace(item, mode=0o755) if i == app_index else item
            for i, item in enumerate(original)
        ],
    ]
    for entries in variants:
        with _raised(ArtifactContractErrorCode.INVALID_LAYOUT):
            validate_archive_layout(entries, install)


def test_declared_limits_are_enforced_against_layout() -> None:
    archive, install_document, _ = _fixture()
    install = parse_install_manifest(install_document)
    entries = inspect_archive(archive, install.extraction_limits).entries
    for limited in (
        replace(
            install, extraction_limits=replace(install.extraction_limits, max_entries=1)
        ),
        replace(
            install,
            extraction_limits=replace(install.extraction_limits, max_file_bytes=1),
        ),
        replace(
            install,
            extraction_limits=replace(install.extraction_limits, max_total_bytes=1),
        ),
    ):
        with _raised(ArtifactContractErrorCode.LIMIT_EXCEEDED):
            validate_archive_layout(entries, limited)

    over_hard_limit = replace(
        install,
        extraction_limits=replace(install.extraction_limits, max_entries=100_000),
    )
    with _raised(ArtifactContractErrorCode.LIMIT_EXCEEDED):
        validate_archive_layout(entries, over_hard_limit)


def test_inspector_enforces_entry_limit_while_reading_headers() -> None:
    install = parse_install_manifest(_fixture()[1])
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as opened:
        for name in ("one", "two"):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            opened.addfile(info)
    compressed = gzip.compress(raw.getvalue(), mtime=0)
    limits = replace(install.extraction_limits, max_entries=1)
    with _raised(ArtifactContractErrorCode.LIMIT_EXCEEDED):
        inspect_archive(compressed, limits)


def test_inspector_bounds_pax_metadata_before_tarfile_interprets_it() -> None:
    install_document = _fixture()[1]
    install = parse_install_manifest(install_document)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as opened:
        info = tarfile.TarInfo("desktop-install.json")
        info.size = len(install_document)
        info.pax_headers = {"comment": "x" * 2_048}
        opened.addfile(info, io.BytesIO(install_document))
    compressed = gzip.compress(raw.getvalue(), mtime=0)

    with _raised(ArtifactContractErrorCode.LIMIT_EXCEEDED):
        inspect_archive(compressed, install.extraction_limits)


def test_layout_rejects_tarfile_surrogateescaped_names_safely() -> None:
    archive, install_document, _ = _fixture()
    install = parse_install_manifest(install_document)
    entries = list(inspect_archive(archive, install.extraction_limits).entries)
    entries.append(
        ArchiveEntry(
            "runtime/\udcff",
            ArchiveEntryType.FILE,
            0o644,
            0,
            "0" * 64,
        )
    )
    with _raised(ArtifactContractErrorCode.INVALID_LAYOUT):
        validate_archive_layout(entries, install)


def test_cross_document_disagreements_are_rejected() -> None:
    _, install_document, release_document = _fixture()
    install = parse_install_manifest(install_document)
    release = parse_release_manifest(release_document)
    artifact = release.artifacts[0]
    variants = (
        replace(install, release_version="1.2.4"),
        replace(install, compatibility=replace(install.compatibility, rpc_api_major=2)),
        replace(install, platform=replace(install.platform, distribution_version="45")),
        replace(
            install,
            extraction_limits=replace(install.extraction_limits, max_entries=63),
        ),
    )
    for candidate in variants:
        with _raised(ArtifactContractErrorCode.ARTIFACT_MISMATCH):
            validate_manifest_pair(release, artifact, candidate)


def test_inspector_classifies_real_tar_link_without_following_it() -> None:
    install = parse_install_manifest(_fixture()[1])
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as opened:
        info = tarfile.TarInfo("desktop-install.json")
        info.size = len(_fixture()[1])
        opened.addfile(info, io.BytesIO(_fixture()[1]))
        link = tarfile.TarInfo("runtime/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/outside"
        opened.addfile(link)
    compressed = gzip.compress(raw.getvalue(), mtime=0)
    inspection = inspect_archive(compressed, install.extraction_limits)
    assert inspection.entries[-1].entry_type is ArchiveEntryType.SYMLINK
    with _raised(ArtifactContractErrorCode.INVALID_LAYOUT):
        validate_archive_layout(inspection.entries, install)
