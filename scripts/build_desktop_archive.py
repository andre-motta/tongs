"""Build one reproducible production Electron archive and its manifest inputs."""

from __future__ import annotations

import argparse
import binascii
import hashlib
import io
import json
import os
import platform
import re
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import zipfile
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from tongs.desktop.artifact_contract import (
    ArtifactContractError,
    parse_install_manifest,
    parse_release_manifest,
    validate_artifact_archive,
)

_CONTRACT_PATH: Final = Path("packaging/desktop/archive/contract.json")
_ASAR_HELPER_PATH: Final = Path("packaging/desktop/archive/pack_asar.mjs")
_APP_LICENSES: Final = ("react", "react-dom", "scheduler")
_SOURCE_INPUTS: Final = (
    Path("desktop/assets"),
    Path("desktop/scripts/build.mjs"),
    Path("desktop/src"),
    Path("desktop/package.json"),
    Path("desktop/package-lock.json"),
    Path("desktop/tsconfig.json"),
    Path("LICENSE"),
    Path("scripts/build_desktop_archive.py"),
    Path("src/tongs/desktop/artifact_contract/__init__.py"),
    Path("src/tongs/desktop/artifact_contract/_json.py"),
    Path("src/tongs/desktop/artifact_contract/_validation.py"),
    Path("src/tongs/desktop/artifact_contract/archive.py"),
    Path("src/tongs/desktop/artifact_contract/manifests.py"),
    Path("src/tongs/desktop/artifact_contract/models.py"),
    Path("src/tongs/desktop/artifact_contract/schema_resources.py"),
    Path("src/tongs/desktop/artifact_contract/schemas/__init__.py"),
    Path(
        "src/tongs/desktop/artifact_contract/schemas/"
        "desktop-install-manifest-v1.schema.json"
    ),
    Path(
        "src/tongs/desktop/artifact_contract/schemas/"
        "desktop-release-manifest-v1.schema.json"
    ),
    Path("src/tongs/__init__.py"),
    Path("packaging/desktop/archive"),
    Path("packaging/desktop/common"),
)
_SHA_RE: Final = re.compile(r"^[0-9a-f]{40}$")
_VERSION_RE: Final = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


class ArchiveBuildError(RuntimeError):
    """Reject an unsafe, incomplete, or inconsistent producer input."""


@dataclass(frozen=True, slots=True)
class BuildParameters:
    """All release identity values that must not be inferred from a tag."""

    release_version: str
    core_minimum: str
    core_maximum_exclusive: str
    source_commit: str
    source_date_epoch: int


@dataclass(frozen=True, slots=True)
class BuiltArchive:
    """Deterministic bytes and metadata produced from prepared payload files."""

    archive_name: str
    archive: bytes
    install_manifest: bytes
    release_manifest: bytes


def canonical_json(value: object) -> bytes:
    """Serialize deterministic manifest bytes."""
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def build_contract_documents(
    runtime_files: Mapping[str, tuple[bytes, int]],
    parameters: BuildParameters,
    contract: Mapping[str, object],
) -> BuiltArchive:
    """Build S0 documents from already validated runtime payload bytes."""
    _validate_parameters(parameters)
    platform_value = _mapping(contract, "platform")
    limits = _mapping(contract, "extraction_limits")
    electron = _mapping(contract, "electron")
    electron_version = _string(electron, "version")
    files: list[dict[str, object]] = []
    normalized: dict[str, tuple[bytes, int]] = {}
    for path, value in sorted(runtime_files.items()):
        _validate_payload_path(path)
        if path in normalized:
            raise ArchiveBuildError("runtime payload contains a duplicate path")
        content, mode = value
        if not isinstance(content, bytes) or mode not in (0o644, 0o755):
            raise ArchiveBuildError("runtime payload content or mode is invalid")
        normalized[path] = value
        files.append(
            {
                "path": path,
                "byte_count": len(content),
                "sha256": _sha256(content),
                "executable": mode == 0o755,
            }
        )
    required = {
        "runtime/tongs-desktop",
        "runtime/resources/app.asar",
        "runtime/LICENSES.json",
    }
    if not required <= normalized.keys():
        raise ArchiveBuildError("runtime payload is missing a required file")
    compatibility = {
        "core_minimum": parameters.core_minimum,
        "core_maximum_exclusive": parameters.core_maximum_exclusive,
        "rpc_api_major": 1,
        "plugin_api_major": 1,
    }
    install = {
        "schema_version": 1,
        "release_version": parameters.release_version,
        "package_kind": "user-archive",
        "ownership": "per-user",
        "platform": dict(platform_value),
        "compatibility": compatibility,
        "electron_version": electron_version,
        "launcher_path": "runtime/tongs-desktop",
        "extraction_limits": dict(limits),
        "files": files,
    }
    install_document = canonical_json(install)
    archive_name = f"tongs-desktop-{parameters.release_version}-fedora44-x86_64.tar.gz"
    archive = _build_tar_gzip(
        {"desktop-install.json": (install_document, 0o644), **normalized},
        parameters.source_date_epoch,
        _integer(_mapping(contract, "compression"), "gzip_level"),
    )
    release = {
        "schema_version": 1,
        "release_version": parameters.release_version,
        "source_commit": parameters.source_commit,
        "compatibility": compatibility,
        "artifacts": [
            {
                "artifact_id": "fedora-44-x86_64-user-archive",
                "name": archive_name,
                "package_kind": "user-archive",
                "ownership": "per-user",
                "platform": dict(platform_value),
                "byte_count": len(archive),
                "sha256": _sha256(archive),
                "extraction_limits": dict(limits),
            }
        ],
    }
    release_document = canonical_json(release)
    try:
        parsed_release = parse_release_manifest(release_document)
        parse_install_manifest(install_document)
        validate_artifact_archive(
            archive,
            archive_name,
            parsed_release,
            "fedora-44-x86_64-user-archive",
        )
    except ArtifactContractError as error:
        raise ArchiveBuildError(
            f"generated archive violates the S0 contract: {error.message}"
        ) from error
    return BuiltArchive(
        archive_name,
        archive,
        install_document,
        release_document,
    )


def build_desktop_archive(
    source_root: Path,
    electron_archive: Path,
    output_dir: Path,
    parameters: BuildParameters,
    *,
    node_executable: str,
    npm_executable: str,
) -> BuiltArchive:
    """Compile and package the exact prepared source and Electron runtime."""
    source = source_root.resolve(strict=True)
    desktop = source / "desktop"
    contract_path = source / _CONTRACT_PATH
    contract = _load_object(contract_path)
    _validate_build_contract(source, desktop, contract)
    source_inventory = _source_inventory(source)
    runtime_inventory_path = contract_path.parent / _string(
        contract, "electron_runtime_inventory"
    )
    runtime_inventory = _load_object(runtime_inventory_path)
    if _string(runtime_inventory, "electron_version") != _string(
        _mapping(contract, "electron"), "version"
    ):
        raise ArchiveBuildError("Electron version inputs disagree")
    toolchain = _toolchain_inventory(
        desktop, node_executable=node_executable, npm_executable=npm_executable
    )
    if toolchain != dict(_mapping(contract, "toolchain")):
        raise ArchiveBuildError("build toolchain is not the approved pinned input")
    _run_build(desktop, npm_executable, parameters.source_date_epoch)
    output = output_dir.resolve()
    output.mkdir(mode=0o755, parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ArchiveBuildError("output directory must be empty")

    with tempfile.TemporaryDirectory(
        prefix=".tongs-archive-", dir=output.parent
    ) as raw:
        temporary = Path(raw)
        electron_dist = temporary / "electron-runtime"
        electron_archive_identity = _prepare_electron_archive(
            electron_archive.resolve(strict=True),
            electron_dist,
            runtime_inventory,
            parameters.source_date_epoch,
        )
        _validate_electron_runtime(electron_dist, runtime_inventory)
        asar_input = temporary / "asar-input"
        asar_path = temporary / "app.asar"
        asar_paths = _prepare_asar_input(source, desktop, asar_input, parameters)
        listed = _pack_asar(
            source,
            desktop,
            asar_input,
            asar_path,
            node_executable,
        )
        expected_listed = _expected_asar_entries(asar_paths)
        if listed != expected_listed:
            raise ArchiveBuildError("ASAR inventory disagrees with prepared inputs")
        asar_bytes = asar_path.read_bytes()
        asar_input_inventory = _content_inventory(
            {path: (asar_input / path).read_bytes() for path in asar_paths}
        )
        runtime_files, license_inventory = _prepare_runtime_files(
            source,
            desktop,
            electron_dist,
            runtime_inventory,
            asar_bytes,
        )

    built = build_contract_documents(runtime_files, parameters, contract)
    (output / built.archive_name).write_bytes(built.archive)
    (output / "desktop-install.json").write_bytes(built.install_manifest)
    (output / "desktop-manifest-v1.json").write_bytes(built.release_manifest)
    (output / "app-asar-inventory.json").write_bytes(
        canonical_json(
            {
                "schema_version": 1,
                "asar_sha256": _sha256(asar_bytes),
                "asar_byte_count": len(asar_bytes),
                "files": asar_input_inventory,
            }
        )
    )
    (output / "runtime-inventory.json").write_bytes(
        canonical_json(
            {
                "schema_version": 1,
                "files": _mode_inventory(runtime_files),
            }
        )
    )
    (output / "prepared-source-inventory.json").write_bytes(
        canonical_json(
            {
                "schema_version": 1,
                "source_commit": parameters.source_commit,
                "files": source_inventory,
                "npm_packages": _npm_inventory(desktop / "package-lock.json"),
            }
        )
    )
    (output / "license-inventory.json").write_bytes(canonical_json(license_inventory))
    provenance = {
        "schema_version": 1,
        "candidate": "UNPUBLISHED",
        "release_version": parameters.release_version,
        "source_commit": parameters.source_commit,
        "source_date_epoch": parameters.source_date_epoch,
        "compatibility": {
            "core_minimum": parameters.core_minimum,
            "core_maximum_exclusive": parameters.core_maximum_exclusive,
            "rpc_api_major": 1,
            "plugin_api_major": 1,
        },
        "toolchain": toolchain,
        "electron_input": {
            "archive": electron_archive_identity,
            "inventory_sha256": _sha256(runtime_inventory_path.read_bytes()),
            "upstream_archive": runtime_inventory["upstream_archive"],
        },
        "outputs": {
            "archive": {"name": built.archive_name, **_file_identity(built.archive)},
            "install_manifest": _file_identity(built.install_manifest),
            "release_manifest": _file_identity(built.release_manifest),
        },
        "compression": contract["compression"],
    }
    (output / "build-provenance.json").write_bytes(canonical_json(provenance))
    _write_checksums(output)
    return built


def _prepare_electron_archive(
    archive_path: Path,
    destination: Path,
    inventory: Mapping[str, object],
    source_date_epoch: int,
) -> dict[str, object]:
    _require_regular_file(archive_path, "Electron archive")
    archive = archive_path.read_bytes()
    upstream = _mapping(inventory, "upstream_archive")
    if archive_path.name != _string(upstream, "name") or _sha256(archive) != _string(
        upstream, "sha256"
    ):
        raise ArchiveBuildError("Electron archive identity changed")
    raw_files = inventory.get("files")
    if not isinstance(raw_files, list):
        raise ArchiveBuildError("Electron inventory files are invalid")
    expected = {
        _string(_object(raw, "Electron file"), "path"): _object(raw, "Electron file")
        for raw in raw_files
    }
    destination.mkdir(mode=0o755, parents=True)
    observed: set[str] = set()
    try:
        with zipfile.ZipFile(io.BytesIO(archive), mode="r") as source:
            for member in source.infolist():
                path = member.filename
                _validate_relative_path(path)
                if path in observed or path not in expected or member.is_dir():
                    raise ArchiveBuildError("Electron archive inventory changed")
                item = expected[path]
                mode = stat.S_IMODE(member.external_attr >> 16)
                file_type = stat.S_IFMT(member.external_attr >> 16)
                if (
                    member.flag_bits & 0x1
                    or file_type != stat.S_IFREG
                    or member.file_size != _integer(item, "byte_count")
                    or mode != _integer(item, "mode")
                ):
                    raise ArchiveBuildError(f"Electron archive member changed: {path}")
                content = source.read(member)
                if _sha256(content) != _string(item, "sha256"):
                    raise ArchiveBuildError(f"Electron archive member changed: {path}")
                target = destination / path
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                target.write_bytes(content)
                target.chmod(mode)
                os.utime(target, (source_date_epoch, source_date_epoch))
                observed.add(path)
    except ArchiveBuildError:
        raise
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        raise ArchiveBuildError("Electron archive is invalid") from error
    if observed != set(expected):
        raise ArchiveBuildError("Electron archive inventory changed")
    for directory in sorted(
        (path for path in destination.rglob("*") if path.is_dir()), reverse=True
    ):
        directory.chmod(0o755)
        os.utime(directory, (source_date_epoch, source_date_epoch))
    return {"name": archive_path.name, **_file_identity(archive)}


def _prepare_asar_input(
    source: Path,
    desktop: Path,
    destination: Path,
    parameters: BuildParameters,
) -> tuple[str, ...]:
    paths_file = source / "packaging/desktop/archive/app-asar-paths.txt"
    paths = tuple(
        line.strip() for line in paths_file.read_text("utf-8").splitlines() if line
    )
    if not paths or paths != tuple(sorted(set(paths))):
        raise ArchiveBuildError("ASAR path allowlist must be sorted and unique")
    destination.mkdir(mode=0o755, parents=True)
    package_document = canonical_json(
        {
            "description": "Production Tongs desktop shell",
            "license": "MIT",
            "main": "dist/src/main/index.js",
            "name": "tongs-desktop",
            "private": True,
            "productName": "Tongs",
            "type": "module",
            "version": parameters.release_version,
        }
    )
    for relative in paths:
        _validate_relative_path(relative)
        target = destination / relative
        target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        if relative == "package.json":
            content = package_document
        else:
            source_path = desktop / relative
            _require_regular_file(source_path, "ASAR source")
            content = source_path.read_bytes()
        target.write_bytes(content)
        target.chmod(0o644)
        os.utime(target, (parameters.source_date_epoch, parameters.source_date_epoch))
    for directory in sorted(
        (path for path in destination.rglob("*") if path.is_dir()), reverse=True
    ):
        directory.chmod(0o755)
        os.utime(
            directory, (parameters.source_date_epoch, parameters.source_date_epoch)
        )
    return paths


def _pack_asar(
    source: Path,
    desktop: Path,
    asar_input: Path,
    asar_path: Path,
    node_executable: str,
) -> list[str]:
    completed = _run(
        [
            node_executable,
            os.fspath(source / _ASAR_HELPER_PATH),
            os.fspath(desktop),
            os.fspath(asar_input),
            os.fspath(asar_path),
        ],
        cwd=source,
    )
    try:
        listed = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ArchiveBuildError("ASAR helper returned invalid output") from error
    if not isinstance(listed, list) or any(
        not isinstance(item, str) for item in listed
    ):
        raise ArchiveBuildError("ASAR helper returned an invalid inventory")
    _require_regular_file(asar_path, "ASAR output")
    return sorted(listed)


def _expected_asar_entries(paths: Sequence[str]) -> list[str]:
    entries: set[str] = set()
    for path in paths:
        current = PurePosixPath(path)
        entries.add(f"/{current}")
        for parent in current.parents:
            if str(parent) != ".":
                entries.add(f"/{parent}")
    return sorted(entries)


def _prepare_runtime_files(
    source: Path,
    desktop: Path,
    electron_dist: Path,
    runtime_inventory: Mapping[str, object],
    asar_bytes: bytes,
) -> tuple[dict[str, tuple[bytes, int]], dict[str, object]]:
    files: dict[str, tuple[bytes, int]] = {}
    raw_files = runtime_inventory.get("files")
    if not isinstance(raw_files, list):
        raise ArchiveBuildError("Electron inventory files are invalid")
    for raw in raw_files:
        item = _object(raw, "Electron file")
        path = _string(item, "path")
        if path == "resources/default_app.asar":
            continue
        target = "runtime/tongs-desktop" if path == "electron" else f"runtime/{path}"
        content = (electron_dist / path).read_bytes()
        files[target] = (content, _integer(item, "mode"))
    files["runtime/resources/app.asar"] = (asar_bytes, 0o644)
    desktop_entry = source / "packaging/desktop/common/tongs.desktop"
    icon = source / "packaging/desktop/common/tongs.png"
    _require_regular_file(desktop_entry, "prepared desktop entry")
    _require_regular_file(icon, "prepared icon")
    if icon.read_bytes() != (desktop / "assets/icon.png").read_bytes():
        raise ArchiveBuildError("prepared desktop icons disagree")
    files["runtime/share/applications/tongs.desktop"] = (
        desktop_entry.read_bytes(),
        0o644,
    )
    files["runtime/share/icons/hicolor/512x512/apps/tongs.png"] = (
        icon.read_bytes(),
        0o644,
    )

    license_components: list[dict[str, object]] = [
        {
            "name": "Tongs",
            "version": "source",
            "license": "MIT",
            "license_paths": ["runtime/licenses/tongs/LICENSE"],
        },
        {
            "name": "Electron",
            "version": _string(runtime_inventory, "electron_version"),
            "license": "MIT",
            "license_paths": ["runtime/LICENSE"],
        },
        {
            "name": "Chromium and Electron third-party components",
            "version": f"bundled-with-electron-{_string(runtime_inventory, 'electron_version')}",
            "license": "multiple",
            "license_paths": ["runtime/LICENSES.chromium.html"],
        },
    ]
    tongs_license = source / "LICENSE"
    _require_regular_file(tongs_license, "Tongs license")
    files["runtime/licenses/tongs/LICENSE"] = (tongs_license.read_bytes(), 0o644)
    for package_name in _APP_LICENSES:
        package_root = desktop / "node_modules" / package_name
        metadata = _load_object(package_root / "package.json")
        license_path = package_root / "LICENSE"
        _require_regular_file(license_path, f"{package_name} license")
        target = f"runtime/licenses/{package_name}/LICENSE"
        files[target] = (license_path.read_bytes(), 0o644)
        license_components.append(
            {
                "name": _string(metadata, "name"),
                "version": _string(metadata, "version"),
                "license": _string(metadata, "license"),
                "license_paths": [target],
            }
        )
    license_inventory: dict[str, object] = {
        "schema_version": 1,
        "components": license_components,
    }
    files["runtime/LICENSES.json"] = (canonical_json(license_inventory), 0o644)
    return files, license_inventory


def _validate_build_contract(
    source: Path, desktop: Path, contract: Mapping[str, object]
) -> None:
    if contract.get("schema_version") != 1:
        raise ArchiveBuildError("build contract schema is unsupported")
    lock = desktop / "package-lock.json"
    if _sha256(lock.read_bytes()) != _string(contract, "expected_package_lock_sha256"):
        raise ArchiveBuildError("desktop package lock is not the approved input")
    lock_document = _load_object(lock)
    package = _load_object(desktop / "package.json")
    dev = _mapping(package, "devDependencies")
    asar = _mapping(contract, "asar")
    asar_package = _string(asar, "package")
    if asar_package != "@electron/asar":
        raise ArchiveBuildError("ASAR package identity is invalid")
    if dev.get(asar_package) != _string(asar, "version"):
        raise ArchiveBuildError("ASAR package version is not pinned")
    locked_asar = _mapping(
        _mapping(lock_document, "packages"), "node_modules/@electron/asar"
    )
    if locked_asar.get("version") != asar.get("version") or locked_asar.get(
        "integrity"
    ) != asar.get("integrity"):
        raise ArchiveBuildError("ASAR package lock identity changed")
    installed_asar = _load_object(desktop / "node_modules/@electron/asar/package.json")
    if installed_asar.get("version") != asar.get("version"):
        raise ArchiveBuildError("installed ASAR package disagrees with the contract")
    compression = _mapping(contract, "compression")
    if compression != {
        "gzip_filename": "",
        "gzip_level": 9,
        "gzip_os": 255,
        "tar_format": "ustar",
    }:
        raise ArchiveBuildError("archive encoding contract changed")
    if _string(contract, "app_asar_paths") != "app-asar-paths.txt":
        raise ArchiveBuildError("ASAR input path contract changed")
    for relative in _SOURCE_INPUTS:
        if not (source / relative).exists():
            raise ArchiveBuildError(f"required source input is missing: {relative}")


def _validate_electron_runtime(runtime: Path, inventory: Mapping[str, object]) -> None:
    if inventory.get("schema_version") != 1:
        raise ArchiveBuildError("Electron inventory schema is unsupported")
    expected: dict[str, Mapping[str, object]] = {}
    raw_files = inventory.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ArchiveBuildError("Electron inventory is empty")
    for raw in raw_files:
        item = _object(raw, "Electron file")
        path = _string(item, "path")
        _validate_relative_path(path)
        if path in expected:
            raise ArchiveBuildError("Electron inventory contains a duplicate path")
        expected[path] = item
    actual = _filesystem_files(runtime)
    if set(actual) != set(expected):
        raise ArchiveBuildError("Electron runtime file inventory changed")
    for path, source_path in actual.items():
        item = expected[path]
        details = source_path.stat()
        if (
            details.st_size != _integer(item, "byte_count")
            or stat.S_IMODE(details.st_mode) != _integer(item, "mode")
            or _sha256(source_path.read_bytes()) != _string(item, "sha256")
        ):
            raise ArchiveBuildError(f"Electron runtime file changed: {path}")
    version = (runtime / "version").read_text("utf-8").strip()
    if version != _string(inventory, "electron_version"):
        raise ArchiveBuildError("Electron runtime version changed")


def _run_build(desktop: Path, npm_executable: str, source_date_epoch: int) -> None:
    environment = dict(os.environ)
    environment.update(
        {
            "LC_ALL": "C.UTF-8",
            "SOURCE_DATE_EPOCH": str(source_date_epoch),
            "TZ": "UTC",
        }
    )
    _run([npm_executable, "run", "build"], cwd=desktop, env=environment)


def _toolchain_inventory(
    desktop: Path, *, node_executable: str, npm_executable: str
) -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "zlib_compile": zlib.ZLIB_VERSION,
        "zlib_runtime": zlib.ZLIB_RUNTIME_VERSION,
        "node": _run([node_executable, "--version"], cwd=desktop).stdout.strip(),
        "npm": _run([npm_executable, "--version"], cwd=desktop).stdout.strip(),
        "typescript": _package_version(desktop, "typescript"),
        "esbuild": _package_version(desktop, "esbuild"),
        "asar": _package_version(desktop, "@electron/asar"),
        "electron": _package_version(desktop, "electron"),
    }


def _source_inventory(source: Path) -> list[dict[str, object]]:
    paths: set[Path] = set()
    for relative in _SOURCE_INPUTS:
        candidate = source / relative
        details = candidate.lstat()
        if stat.S_ISDIR(details.st_mode):
            paths.update(_filesystem_files(candidate).values())
        else:
            paths.add(candidate)
    entries = []
    for path in sorted(paths):
        _require_regular_file(path, "source input")
        relative = path.relative_to(source).as_posix()
        entries.append({"path": relative, **_file_identity(path.read_bytes())})
    return entries


def _npm_inventory(lock_path: Path) -> list[dict[str, object]]:
    root = _load_object(lock_path)
    packages = root.get("packages")
    if not isinstance(packages, dict):
        raise ArchiveBuildError("npm lock package inventory is invalid")
    values: list[dict[str, object]] = []
    for path, raw in sorted(packages.items()):
        if not path:
            continue
        item = _object(raw, "npm package")
        values.append(
            {
                key: item[key]
                for key in ("version", "resolved", "integrity", "license", "dev")
                if key in item
            }
            | {"path": path}
        )
    return values


def _build_tar_gzip(
    files: Mapping[str, tuple[bytes, int]], source_date_epoch: int, level: int
) -> bytes:
    if not 0 <= source_date_epoch <= 0xFFFFFFFF:
        raise ArchiveBuildError("SOURCE_DATE_EPOCH is outside gzip bounds")
    directories = {"runtime"}
    for path in files:
        _validate_relative_path(path)
        parent = PurePosixPath(path).parent
        while str(parent) not in {"", "."}:
            directories.add(str(parent))
            parent = parent.parent
    members: dict[str, tuple[bytes | None, int]] = {
        path: (None, 0o755) for path in directories
    }
    members.update(files)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for path in sorted(members):
            content, mode = members[path]
            info = tarfile.TarInfo(path)
            info.mode = mode
            info.mtime = source_date_epoch
            info.uid = 0
            info.gid = 0
            info.uname = "root"
            info.gname = "root"
            if content is None:
                info.type = tarfile.DIRTYPE
                info.size = 0
                archive.addfile(info)
            else:
                info.type = tarfile.REGTYPE
                info.size = len(content)
                archive.addfile(info, io.BytesIO(content))
    return _gzip(raw.getvalue(), source_date_epoch, level)


def _gzip(document: bytes, mtime: int, level: int) -> bytes:
    if level != 9:
        raise ArchiveBuildError("only the pinned gzip level is supported")
    compressor = zlib.compressobj(level=level, wbits=-zlib.MAX_WBITS)
    compressed = compressor.compress(document) + compressor.flush()
    header = b"\x1f\x8b\x08\x00" + struct.pack("<I", mtime) + b"\x02\xff"
    trailer = struct.pack(
        "<II", binascii.crc32(document) & 0xFFFFFFFF, len(document) & 0xFFFFFFFF
    )
    return header + compressed + trailer


def _content_inventory(files: Mapping[str, bytes]) -> list[dict[str, object]]:
    return [
        {"path": path, **_file_identity(content)}
        for path, content in sorted(files.items())
    ]


def _mode_inventory(
    files: Mapping[str, tuple[bytes, int]],
) -> list[dict[str, object]]:
    return [
        {"path": path, "mode": f"{mode:04o}", **_file_identity(content)}
        for path, (content, mode) in sorted(files.items())
    ]


def _filesystem_files(root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            path = current_path / name
            if path.is_symlink():
                raise ArchiveBuildError("prepared input contains a directory link")
        for name in filenames:
            path = current_path / name
            _require_regular_file(path, "prepared input")
            result[path.relative_to(root).as_posix()] = path
    return result


def _write_checksums(output: Path) -> None:
    lines = []
    for path in sorted(item for item in output.iterdir() if item.name != "SHA256SUMS"):
        _require_regular_file(path, "build output")
        lines.append(f"{_sha256(path.read_bytes())}  {path.name}\n")
    (output / "SHA256SUMS").write_text("".join(lines), encoding="ascii")


def _validate_parameters(parameters: BuildParameters) -> None:
    for value in (
        parameters.release_version,
        parameters.core_minimum,
        parameters.core_maximum_exclusive,
    ):
        if _VERSION_RE.fullmatch(value) is None:
            raise ArchiveBuildError("release or compatibility version is invalid")
    if _SHA_RE.fullmatch(parameters.source_commit) is None:
        raise ArchiveBuildError("source commit must be an exact full Git SHA")
    if type(parameters.source_date_epoch) is not int:
        raise ArchiveBuildError("SOURCE_DATE_EPOCH must be an integer")


def _validate_payload_path(path: str) -> None:
    _validate_relative_path(path)
    if not path.startswith("runtime/"):
        raise ArchiveBuildError("payload files must remain under runtime")


def _validate_relative_path(path: str) -> None:
    if not path or "\\" in path or path.startswith("/"):
        raise ArchiveBuildError("prepared path is invalid")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ArchiveBuildError("prepared path is invalid")


def _require_regular_file(path: Path, label: str) -> None:
    try:
        details = path.lstat()
    except OSError as error:
        raise ArchiveBuildError(f"{label} is missing") from error
    if not stat.S_ISREG(details.st_mode):
        raise ArchiveBuildError(f"{label} is not a regular file")


def _run(
    arguments: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(arguments),
        cwd=cwd,
        env=env,
        check=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout)[-4_096:].strip()
        raise ArchiveBuildError(f"build command failed: {detail}")
    return completed


def _package_version(desktop: Path, package_name: str) -> str:
    return _string(
        _load_object(desktop / "node_modules" / package_name / "package.json"),
        "version",
    )


def _load_object(path: Path) -> dict[str, object]:
    _require_regular_file(path, "JSON input")
    try:
        value = json.loads(path.read_bytes())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ArchiveBuildError("JSON input is invalid") from error
    return dict(_object(value, "JSON input"))


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ArchiveBuildError(f"{label} is not an object")
    return value


def _mapping(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    return _object(value.get(key), key)


def _string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ArchiveBuildError(f"{key} is not a string")
    return item


def _integer(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if type(item) is not int:
        raise ArchiveBuildError(f"{key} is not an integer")
    return item


def _file_identity(content: bytes) -> dict[str, object]:
    return {"byte_count": len(content), "sha256": _sha256(content)}


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--electron-archive", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--release-version", required=True)
    parser.add_argument("--core-minimum", required=True)
    parser.add_argument("--core-maximum-exclusive", required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-date-epoch", required=True, type=int)
    parser.add_argument("--node-executable", default="node")
    parser.add_argument("--npm-executable", default="npm")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Build the requested unpublished or production archive inputs."""
    args = _parser().parse_args(argv)
    parameters = BuildParameters(
        args.release_version,
        args.core_minimum,
        args.core_maximum_exclusive,
        args.source_commit,
        args.source_date_epoch,
    )
    try:
        built = build_desktop_archive(
            args.source_root,
            args.electron_archive,
            args.output_dir,
            parameters,
            node_executable=args.node_executable,
            npm_executable=args.npm_executable,
        )
    except ArchiveBuildError as error:
        print(f"desktop archive build failed: {error}", file=sys.stderr)
        return 1
    print(args.output_dir / built.archive_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
