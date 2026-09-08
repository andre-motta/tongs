"""Contract tests for the reproducible production archive producer."""

from __future__ import annotations

import importlib.util
import json
import struct
import sys
from dataclasses import replace
from pathlib import Path
from typing import Protocol

import pytest

from tongs.desktop.artifact_contract import (
    ArtifactContractError,
    ArtifactContractErrorCode,
    DesktopReleaseManifest,
    TargetArchitecture,
    parse_release_manifest,
    select_release_artifact,
    validate_artifact_archive,
)

ROOT = Path(__file__).parents[4]
SCRIPT = ROOT / "scripts/build_desktop_archive.py"
SPEC = importlib.util.spec_from_file_location("build_desktop_archive", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
producer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = producer
SPEC.loader.exec_module(producer)

ArchiveBuildError = producer.ArchiveBuildError
BuildParameters = producer.BuildParameters
build_contract_documents = producer.build_contract_documents

PARAMETERS = BuildParameters(
    "0.5.0",
    "0.4.2-dev.183",
    "0.5.0",
    "0123456789abcdef0123456789abcdef01234567",
    1_788_846_594,
)
PAYLOAD = {
    "runtime/LICENSES.json": (b'{"schema_version":1}\n', 0o644),
    "runtime/resources/app.asar": (b"prepared-asar", 0o644),
    "runtime/tongs-desktop": (b"prepared-launcher", 0o755),
}


class _BuiltArchive(Protocol):
    install_manifest: bytes


def _contract() -> dict[str, object]:
    return json.loads((ROOT / "packaging/desktop/archive/contract.json").read_bytes())


def _rebind_release(archive: bytes, release_document: bytes) -> DesktopReleaseManifest:
    release = parse_release_manifest(release_document)
    artifact = replace(
        release.artifacts[0],
        byte_count=len(archive),
        sha256=producer._sha256(archive),
    )
    return replace(release, artifacts=(artifact,))


def _rewritten_archive(
    built: _BuiltArchive, payload: dict[str, tuple[bytes, int]]
) -> bytes:
    return producer._build_tar_gzip(
        {"desktop-install.json": (built.install_manifest, 0o644), **payload},
        PARAMETERS.source_date_epoch,
        9,
    )


def test_documents_are_reproducible_and_validate_against_s0() -> None:
    first = build_contract_documents(PAYLOAD, PARAMETERS, _contract())
    second = build_contract_documents(PAYLOAD, PARAMETERS, _contract())

    assert first == second
    assert first.archive[:4] == b"\x1f\x8b\x08\x00"
    assert struct.unpack("<I", first.archive[4:8])[0] == PARAMETERS.source_date_epoch
    assert first.archive[8:10] == b"\x02\xff"
    release = parse_release_manifest(first.release_manifest)
    validated = validate_artifact_archive(
        first.archive,
        first.archive_name,
        release,
        "fedora-44-x86_64-user-archive",
    )
    assert validated.layout.file_count == 4
    assert validated.install.compatibility.core_minimum == "0.4.2-dev.183"


def test_wrong_platform_has_no_candidate() -> None:
    built = build_contract_documents(PAYLOAD, PARAMETERS, _contract())
    release = parse_release_manifest(built.release_manifest)
    wrong_platform = replace(
        release.artifacts[0].platform,
        architecture=TargetArchitecture.AARCH64,
    )

    with pytest.raises(
        ArtifactContractError,
        check=lambda error: error.code is ArtifactContractErrorCode.INCOMPATIBLE,
    ):
        select_release_artifact(
            release,
            wrong_platform,
            release.artifacts[0].package_kind,
        )


def test_corrupt_archive_is_rejected_after_identity_is_rebound() -> None:
    built = build_contract_documents(PAYLOAD, PARAMETERS, _contract())
    corrupted = built.archive[:-1]
    release = _rebind_release(corrupted, built.release_manifest)

    with pytest.raises(
        ArtifactContractError,
        check=lambda error: error.code is ArtifactContractErrorCode.INVALID_ARCHIVE,
    ):
        validate_artifact_archive(
            corrupted,
            built.archive_name,
            release,
            "fedora-44-x86_64-user-archive",
        )


@pytest.mark.parametrize(
    "payload",
    [
        {
            key: value
            for key, value in PAYLOAD.items()
            if key != "runtime/LICENSES.json"
        },
        PAYLOAD | {"runtime/tongs-desktop": (b"prepared-launcher", 0o644)},
    ],
)
def test_consumer_rejects_missing_file_and_changed_mode(
    payload: dict[str, tuple[bytes, int]],
) -> None:
    built = build_contract_documents(PAYLOAD, PARAMETERS, _contract())
    archive = _rewritten_archive(built, payload)
    release = _rebind_release(archive, built.release_manifest)

    with pytest.raises(
        ArtifactContractError,
        check=lambda error: error.code is ArtifactContractErrorCode.INVALID_LAYOUT,
    ):
        validate_artifact_archive(
            archive,
            built.archive_name,
            release,
            "fedora-44-x86_64-user-archive",
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {
                key: value
                for key, value in PAYLOAD.items()
                if key != "runtime/LICENSES.json"
            },
            "missing a required file",
        ),
        (
            PAYLOAD | {"runtime/tongs-desktop": (b"prepared-launcher", 0o644)},
            "violates the S0 contract",
        ),
    ],
)
def test_missing_required_file_and_wrong_launcher_mode_are_rejected(
    payload: dict[str, tuple[bytes, int]], message: str
) -> None:
    with pytest.raises(ArchiveBuildError, match=message):
        build_contract_documents(payload, PARAMETERS, _contract())


def test_release_manifest_mismatch_is_rejected() -> None:
    built = build_contract_documents(PAYLOAD, PARAMETERS, _contract())
    raw_release = json.loads(built.release_manifest)
    raw_release["release_version"] = "0.5.1"
    release = parse_release_manifest(producer.canonical_json(raw_release))

    with pytest.raises(
        ArtifactContractError,
        check=lambda error: error.code is ArtifactContractErrorCode.ARTIFACT_MISMATCH,
    ):
        validate_artifact_archive(
            built.archive,
            built.archive_name,
            release,
            "fedora-44-x86_64-user-archive",
        )


@pytest.mark.parametrize(
    "path",
    ["/runtime/tongs-desktop", "runtime/../escape", "runtime\\escape"],
)
def test_unsafe_prepared_paths_are_rejected(path: str) -> None:
    payload = PAYLOAD | {path: (b"unsafe", 0o644)}

    with pytest.raises(ArchiveBuildError, match="path is invalid"):
        build_contract_documents(payload, PARAMETERS, _contract())
