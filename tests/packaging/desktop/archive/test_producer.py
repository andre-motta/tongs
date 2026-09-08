"""Contract tests for the reproducible production archive producer."""

from __future__ import annotations

import importlib.util
import io
import json
import stat
import struct
import sys
import zipfile
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


def _electron_zip(entries: dict[str, tuple[bytes, int]]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, (content, mode) in entries.items():
            member = zipfile.ZipInfo(path)
            member.create_system = 3
            member.external_attr = (stat.S_IFREG | mode) << 16
            archive.writestr(member, content)
    return output.getvalue()


def _electron_inventory(
    archive: bytes, entries: dict[str, tuple[bytes, int]]
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "electron_version": "44.2.0",
        "upstream_archive": {
            "name": "electron.zip",
            "sha256": producer._sha256(archive),
        },
        "files": [
            {
                "path": path,
                "byte_count": len(content),
                "sha256": producer._sha256(content),
                "mode": mode,
            }
            for path, (content, mode) in entries.items()
        ],
    }


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


def test_shared_desktop_icon_metadata_matches_original_pixmap() -> None:
    contract = _contract()
    metadata = contract["desktop_metadata"]
    assert isinstance(metadata, dict)
    icon = (ROOT / "packaging/desktop/common/tongs.png").read_bytes()
    entry = (ROOT / "packaging/desktop/common/tongs.desktop").read_bytes()

    assert icon == (ROOT / "desktop/assets/icon.png").read_bytes()
    assert producer._png_dimensions(icon) == (
        metadata["icon_width"],
        metadata["icon_height"],
    )
    assert metadata["icon_path"] == "runtime/share/pixmaps/tongs.png"
    assert f"Icon={metadata['icon_name']}\n".encode() in entry


def test_verified_electron_zip_is_extracted_with_normalized_modes(
    tmp_path: Path,
) -> None:
    entries = {
        "electron": (b"binary", 0o755),
        "resources/default_app.asar": (b"default", 0o644),
        "version": (b"44.2.0\n", 0o644),
    }
    archive = _electron_zip(entries)
    archive_path = tmp_path / "electron.zip"
    archive_path.write_bytes(archive)
    destination = tmp_path / "runtime"

    identity = producer._prepare_electron_archive(
        archive_path,
        destination,
        _electron_inventory(archive, entries),
        PARAMETERS.source_date_epoch,
    )

    assert identity == {
        "name": "electron.zip",
        "byte_count": len(archive),
        "sha256": producer._sha256(archive),
    }
    for path, (content, mode) in entries.items():
        extracted = destination / path
        assert extracted.read_bytes() == content
        assert stat.S_IMODE(extracted.stat().st_mode) == mode
        assert int(extracted.stat().st_mtime) == PARAMETERS.source_date_epoch


def test_electron_zip_rejects_wrong_digest_and_escaping_member(
    tmp_path: Path,
) -> None:
    entries = {"../escape": (b"bad", 0o644)}
    archive = _electron_zip(entries)
    archive_path = tmp_path / "electron.zip"
    archive_path.write_bytes(archive)
    inventory = _electron_inventory(archive, entries)

    with pytest.raises(ArchiveBuildError, match="path is invalid"):
        producer._prepare_electron_archive(
            archive_path,
            tmp_path / "runtime",
            inventory,
            PARAMETERS.source_date_epoch,
        )

    upstream = inventory["upstream_archive"]
    assert isinstance(upstream, dict)
    upstream["sha256"] = "0" * 64
    with pytest.raises(ArchiveBuildError, match="identity changed"):
        producer._prepare_electron_archive(
            archive_path,
            tmp_path / "runtime-wrong-digest",
            inventory,
            PARAMETERS.source_date_epoch,
        )


def test_electron_zip_rejects_member_mutation_duplicate_and_omission(
    tmp_path: Path,
) -> None:
    expected = {
        "electron": (b"expected", 0o755),
        "version": (b"44.2.0\n", 0o644),
    }
    mutated = _electron_zip(expected | {"electron": (b"mutated", 0o755)})
    mutated_path = tmp_path / "electron.zip"
    mutated_path.write_bytes(mutated)
    mutated_inventory = _electron_inventory(mutated, expected)
    with pytest.raises(ArchiveBuildError, match="member changed"):
        producer._prepare_electron_archive(
            mutated_path,
            tmp_path / "mutated",
            mutated_inventory,
            PARAMETERS.source_date_epoch,
        )

    omitted_entries = {"electron": expected["electron"]}
    omitted = _electron_zip(omitted_entries)
    omitted_path = tmp_path / "omitted-input/electron.zip"
    omitted_path.parent.mkdir()
    omitted_path.write_bytes(omitted)
    omitted_inventory = _electron_inventory(omitted, expected)
    with pytest.raises(ArchiveBuildError, match="inventory changed"):
        producer._prepare_electron_archive(
            omitted_path,
            tmp_path / "omitted",
            omitted_inventory,
            PARAMETERS.source_date_epoch,
        )

    duplicate_stream = io.BytesIO()
    with zipfile.ZipFile(duplicate_stream, mode="w") as duplicate_zip:
        member = zipfile.ZipInfo("electron")
        member.create_system = 3
        member.external_attr = (stat.S_IFREG | 0o755) << 16
        duplicate_zip.writestr(member, b"expected")
        with pytest.warns(UserWarning, match="Duplicate name"):
            duplicate_zip.writestr(member, b"expected")
    duplicate = duplicate_stream.getvalue()
    duplicate_path = tmp_path / "duplicate-input/electron.zip"
    duplicate_path.parent.mkdir()
    duplicate_path.write_bytes(duplicate)
    duplicate_inventory = _electron_inventory(
        duplicate, {"electron": (b"expected", 0o755)}
    )
    with pytest.raises(ArchiveBuildError, match="inventory changed"):
        producer._prepare_electron_archive(
            duplicate_path,
            tmp_path / "duplicate",
            duplicate_inventory,
            PARAMETERS.source_date_epoch,
        )


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


def _write_npm_package(
    desktop: Path,
    lock_path: str,
    *,
    name: str,
    version: str,
    license_expression: object = "MIT",
    license_name: str | None = "license",
    notice: bytes | None = None,
) -> None:
    root = desktop / lock_path
    root.mkdir(parents=True)
    (root / "package.json").write_text(
        json.dumps({"name": name, "version": version, "license": license_expression}),
        encoding="utf-8",
    )
    if license_name is not None:
        (root / license_name).write_bytes(f"license for {name}\n".encode())
    if notice is not None:
        (root / "NOTICE.txt").write_bytes(notice)


def _write_npm_fixture(desktop: Path) -> None:
    dependencies = {
        "@scope/gamma": "3.0.0",
        "alpha": "1.0.0",
        "react": "19.2.8",
    }
    (desktop / "package.json").write_text(
        json.dumps({"dependencies": dependencies}), encoding="utf-8"
    )
    packages = {
        "": {"dependencies": dependencies},
        "node_modules/@scope/gamma": {"version": "3.0.0", "license": "ISC"},
        "node_modules/alpha": {
            "version": "1.0.0",
            "license": "MIT",
            "dependencies": {"beta": "2.0.0"},
            "optionalDependencies": {"missing-optional": "1.0.0"},
        },
        "node_modules/alpha/node_modules/beta": {
            "version": "2.0.0",
            "license": "Apache-2.0",
        },
        "node_modules/react": {"version": "19.2.8", "license": "MIT"},
        "node_modules/dev-only": {
            "version": "9.0.0",
            "license": "MIT",
            "dev": True,
        },
    }
    (desktop / "package-lock.json").write_text(
        json.dumps({"lockfileVersion": 3, "packages": packages}), encoding="utf-8"
    )
    _write_npm_package(
        desktop,
        "node_modules/@scope/gamma",
        name="@scope/gamma",
        version="3.0.0",
        license_expression="ISC",
        license_name="LICENSE.md",
    )
    _write_npm_package(
        desktop,
        "node_modules/alpha",
        name="alpha",
        version="1.0.0",
        license_name="LICENSE",
        notice=b"required notice\n",
    )
    _write_npm_package(
        desktop,
        "node_modules/alpha/node_modules/beta",
        name="beta",
        version="2.0.0",
        license_expression="Apache-2.0",
        license_name="license.txt",
    )
    _write_npm_package(
        desktop,
        "node_modules/react",
        name="react",
        version="19.2.8",
        license_name="LICENSE",
    )


def test_production_npm_licenses_follow_exact_lock_graph_and_include_notices(
    tmp_path: Path,
) -> None:
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    _write_npm_fixture(desktop)

    files, components = producer._production_npm_licenses(desktop)

    assert set(files) == {
        "runtime/licenses/npm/THIRD_PARTY_NOTICES.txt",
        "runtime/licenses/react/LICENSE",
    }
    notice = files["runtime/licenses/npm/THIRD_PARTY_NOTICES.txt"][0]
    assert [component["name"] for component in components] == [
        "@scope/gamma",
        "alpha",
        "beta",
        "react",
    ]
    assert components[-1]["license_paths"] == ["runtime/licenses/react/LICENSE"]
    assert files["runtime/licenses/react/LICENSE"][0] == b"license for react\n"
    assert all(
        component["license_paths"] == ["runtime/licenses/npm/THIRD_PARTY_NOTICES.txt"]
        for component in components[:-1]
    )
    assert b"Package: alpha\nVersion: 1.0.0\nSPDX license: MIT" in notice
    assert b"--- BEGIN LICENSE ---\nlicense for alpha\n--- END LICENSE ---" in notice
    assert (
        b"--- BEGIN NOTICE.txt ---\nrequired notice\n--- END NOTICE.txt ---" in notice
    )
    assert b"dev-only" not in notice
    assert (
        notice
        == producer._production_npm_licenses(desktop)[0][
            "runtime/licenses/npm/THIRD_PARTY_NOTICES.txt"
        ][0]
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("version", "version disagrees"),
        ("missing_license", "no applicable license text"),
        ("license_metadata", "license is not a string"),
        ("dev", "development-only"),
    ],
)
def test_production_npm_licenses_reject_inconsistent_installed_inputs(
    tmp_path: Path, mutation: str, message: str
) -> None:
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    _write_npm_fixture(desktop)
    alpha = desktop / "node_modules/alpha"
    if mutation == "version":
        metadata = json.loads((alpha / "package.json").read_text())
        metadata["version"] = "1.0.1"
        (alpha / "package.json").write_text(json.dumps(metadata), encoding="utf-8")
    elif mutation == "missing_license":
        (alpha / "LICENSE").unlink()
    elif mutation == "license_metadata":
        metadata = json.loads((alpha / "package.json").read_text())
        metadata["license"] = {"type": "MIT"}
        (alpha / "package.json").write_text(json.dumps(metadata), encoding="utf-8")
    else:
        lock = json.loads((desktop / "package-lock.json").read_text())
        lock["packages"]["node_modules/alpha"]["dev"] = True
        (desktop / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")

    with pytest.raises(ArchiveBuildError, match=message):
        producer._production_npm_licenses(desktop)


def test_production_npm_licenses_reject_unsafe_dependency_names(
    tmp_path: Path,
) -> None:
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    dependencies = {"../escape": "1.0.0"}
    (desktop / "package.json").write_text(
        json.dumps({"dependencies": dependencies}), encoding="utf-8"
    )
    (desktop / "package-lock.json").write_text(
        json.dumps({"packages": {"": {"dependencies": dependencies}}}),
        encoding="utf-8",
    )

    with pytest.raises(ArchiveBuildError, match="package name is invalid"):
        producer._production_npm_licenses(desktop)


def test_production_npm_licenses_reject_linked_package_paths(tmp_path: Path) -> None:
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    _write_npm_fixture(desktop)
    alpha = desktop / "node_modules/alpha"
    moved = tmp_path / "moved-alpha"
    alpha.rename(moved)
    alpha.symlink_to(moved, target_is_directory=True)

    with pytest.raises(ArchiveBuildError, match="path is not a directory"):
        producer._production_npm_licenses(desktop)
