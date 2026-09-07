"""Strict parsers for external and archive-internal desktop manifests."""

from __future__ import annotations

import re
from collections.abc import Callable

from tongs.desktop.artifact_contract._json import (
    JsonValue,
    decode_json_document,
    require_array,
    require_bool,
    require_enum,
    require_exact_keys,
    require_integer,
    require_object,
    require_string,
)
from tongs.desktop.artifact_contract._validation import (
    HARD_MAX_ENTRIES,
    HARD_MAX_FILE_BYTES,
    HARD_MAX_PATH_BYTES,
    HARD_MAX_TOTAL_BYTES,
    normalized_name,
    validate_archive_path,
    validate_artifact_name,
    validate_digest,
    validate_identifier,
    validate_limits,
    validate_semantic_version,
    validate_source_commit,
)
from tongs.desktop.artifact_contract.models import (
    ArtifactContractError,
    ArtifactContractErrorCode,
    ArtifactOwnership,
    DesktopArtifact,
    DesktopCompatibility,
    DesktopInstallManifest,
    DesktopPlatform,
    DesktopReleaseManifest,
    ExtractionLimits,
    InstallFile,
    PackageKind,
    TargetArchitecture,
    TargetOperatingSystem,
)

RELEASE_SCHEMA_VERSION = 1
INSTALL_SCHEMA_VERSION = 1
FIXED_LAUNCHER_PATH = "runtime/tongs-desktop"
FIXED_APP_ASAR_PATH = "runtime/resources/app.asar"
FIXED_LICENSE_INVENTORY_PATH = "runtime/LICENSES.json"
MAX_ARTIFACTS = 256

_PLATFORM_KEYS = frozenset(
    {
        "operating_system",
        "architecture",
        "distribution",
        "distribution_version",
        "abi",
    }
)
_COMPATIBILITY_KEYS = frozenset(
    {
        "core_minimum",
        "core_maximum_exclusive",
        "rpc_api_major",
        "plugin_api_major",
    }
)
_LIMIT_KEYS = frozenset(
    {"max_entries", "max_total_bytes", "max_file_bytes", "max_path_bytes"}
)
_PORTABLE_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")


def parse_release_manifest(document: bytes) -> DesktopReleaseManifest:
    """Parse the signed external ``desktop-manifest-v1.json`` contract."""
    root = decode_json_document(document)
    require_exact_keys(
        root,
        frozenset(
            {
                "schema_version",
                "release_version",
                "source_commit",
                "compatibility",
                "artifacts",
            }
        ),
        "release manifest fields",
    )
    schema_version = require_integer(
        root["schema_version"], "schema_version", minimum=1, maximum=2**31 - 1
    )
    if schema_version != RELEASE_SCHEMA_VERSION:
        _unsupported_schema()
    release_version = _semantic_version(root["release_version"], "release_version")
    source_commit = validate_source_commit(
        require_string(root["source_commit"], "source_commit", max_bytes=64)
    )
    compatibility = _parse_compatibility(root["compatibility"])
    raw_artifacts = require_array(root["artifacts"], "artifacts")
    if not raw_artifacts or len(raw_artifacts) > MAX_ARTIFACTS:
        _limit("Release artifact count is outside supported bounds")
    artifacts = tuple(_parse_artifact(item) for item in raw_artifacts)
    _validate_artifact_set(artifacts)
    return DesktopReleaseManifest(
        schema_version,
        release_version,
        source_commit,
        compatibility,
        artifacts,
    )


def parse_install_manifest(document: bytes) -> DesktopInstallManifest:
    """Parse the archive-root ``desktop-install.json`` contract."""
    root = decode_json_document(document)
    require_exact_keys(
        root,
        frozenset(
            {
                "schema_version",
                "release_version",
                "package_kind",
                "ownership",
                "platform",
                "compatibility",
                "electron_version",
                "launcher_path",
                "extraction_limits",
                "files",
            }
        ),
        "install manifest fields",
    )
    schema_version = require_integer(
        root["schema_version"], "schema_version", minimum=1, maximum=2**31 - 1
    )
    if schema_version != INSTALL_SCHEMA_VERSION:
        _unsupported_schema()
    release_version = _semantic_version(root["release_version"], "release_version")
    package_kind = require_enum(root["package_kind"], PackageKind, "package_kind")
    ownership = require_enum(root["ownership"], ArtifactOwnership, "ownership")
    _validate_kind_ownership(package_kind, ownership)
    platform = _parse_platform(root["platform"])
    compatibility = _parse_compatibility(root["compatibility"])
    electron_version = _semantic_version(root["electron_version"], "electron_version")
    launcher_path = require_string(root["launcher_path"], "launcher_path")
    if launcher_path != FIXED_LAUNCHER_PATH:
        _invalid("launcher_path")
    extraction_limits = _parse_limits(root["extraction_limits"])
    raw_files = require_array(root["files"], "files")
    if not raw_files or len(raw_files) > extraction_limits.max_entries:
        _limit("Install file count exceeds extraction limits")
    files = tuple(
        _parse_install_file(item, extraction_limits.max_path_bytes)
        for item in raw_files
    )
    _validate_install_files(files)
    return DesktopInstallManifest(
        schema_version,
        release_version,
        package_kind,
        ownership,
        platform,
        compatibility,
        electron_version,
        launcher_path,
        extraction_limits,
        files,
    )


def _parse_artifact(value: JsonValue) -> DesktopArtifact:
    item = require_object(value, "artifact")
    require_exact_keys(
        item,
        frozenset(
            {
                "artifact_id",
                "name",
                "package_kind",
                "ownership",
                "platform",
                "byte_count",
                "sha256",
                "extraction_limits",
            }
        ),
        "artifact fields",
    )
    artifact_id = validate_identifier(
        require_string(item["artifact_id"], "artifact_id", max_bytes=128),
        "artifact_id",
    )
    name = validate_artifact_name(
        require_string(item["name"], "artifact name", max_bytes=255)
    )
    package_kind = require_enum(item["package_kind"], PackageKind, "package_kind")
    ownership = require_enum(item["ownership"], ArtifactOwnership, "ownership")
    _validate_kind_ownership(package_kind, ownership)
    if package_kind is PackageKind.USER_ARCHIVE and not name.endswith(".tar.gz"):
        _invalid("artifact name")
    if package_kind is PackageKind.RPM and not name.endswith(".rpm"):
        _invalid("artifact name")
    return DesktopArtifact(
        artifact_id,
        name,
        package_kind,
        ownership,
        _parse_platform(item["platform"]),
        require_integer(
            item["byte_count"],
            "artifact byte_count",
            minimum=1,
            maximum=HARD_MAX_TOTAL_BYTES,
        ),
        validate_digest(require_string(item["sha256"], "artifact sha256")),
        _parse_limits(item["extraction_limits"]),
    )


def _parse_install_file(value: JsonValue, max_path_bytes: int) -> InstallFile:
    item = require_object(value, "install file")
    require_exact_keys(
        item,
        frozenset({"path", "byte_count", "sha256", "executable"}),
        "install file fields",
    )
    path = validate_archive_path(
        require_string(item["path"], "install file path", max_bytes=max_path_bytes),
        max_path_bytes,
    )
    if not path.startswith("runtime/"):
        _invalid("install file path")
    return InstallFile(
        path,
        require_integer(
            item["byte_count"],
            "install file byte_count",
            maximum=HARD_MAX_FILE_BYTES,
        ),
        validate_digest(require_string(item["sha256"], "install file sha256")),
        require_bool(item["executable"], "install file executable"),
    )


def _parse_platform(value: JsonValue) -> DesktopPlatform:
    item = require_object(value, "platform")
    require_exact_keys(item, _PLATFORM_KEYS, "platform fields")
    distribution = _portable_value(item["distribution"], "distribution")
    distribution_version = _portable_value(
        item["distribution_version"], "distribution_version"
    )
    abi = _portable_value(item["abi"], "abi")
    return DesktopPlatform(
        require_enum(
            item["operating_system"],
            TargetOperatingSystem,
            "operating_system",
        ),
        require_enum(item["architecture"], TargetArchitecture, "architecture"),
        distribution,
        distribution_version,
        abi,
    )


def _parse_compatibility(value: JsonValue) -> DesktopCompatibility:
    item = require_object(value, "compatibility")
    require_exact_keys(item, _COMPATIBILITY_KEYS, "compatibility fields")
    minimum_text = _semantic_version(item["core_minimum"], "core_minimum")
    maximum_text = _semantic_version(
        item["core_maximum_exclusive"], "core_maximum_exclusive"
    )
    if validate_semantic_version(
        minimum_text, "core_minimum"
    ) >= validate_semantic_version(maximum_text, "core_maximum_exclusive"):
        raise ArtifactContractError(
            ArtifactContractErrorCode.INCOMPATIBLE,
            "Core compatibility range is empty",
        )
    return DesktopCompatibility(
        minimum_text,
        maximum_text,
        require_integer(item["rpc_api_major"], "rpc_api_major", minimum=1, maximum=255),
        require_integer(
            item["plugin_api_major"], "plugin_api_major", minimum=1, maximum=255
        ),
    )


def _parse_limits(value: JsonValue) -> ExtractionLimits:
    item = require_object(value, "extraction_limits")
    require_exact_keys(item, _LIMIT_KEYS, "extraction_limits fields")
    limits = ExtractionLimits(
        require_integer(
            item["max_entries"], "max_entries", minimum=1, maximum=HARD_MAX_ENTRIES
        ),
        require_integer(
            item["max_total_bytes"],
            "max_total_bytes",
            minimum=1,
            maximum=HARD_MAX_TOTAL_BYTES,
        ),
        require_integer(
            item["max_file_bytes"],
            "max_file_bytes",
            minimum=1,
            maximum=HARD_MAX_FILE_BYTES,
        ),
        require_integer(
            item["max_path_bytes"],
            "max_path_bytes",
            minimum=1,
            maximum=HARD_MAX_PATH_BYTES,
        ),
    )
    validate_limits(
        limits.max_entries,
        limits.max_total_bytes,
        limits.max_file_bytes,
        limits.max_path_bytes,
    )
    return limits


def _validate_artifact_set(artifacts: tuple[DesktopArtifact, ...]) -> None:
    _unique(artifacts, lambda item: item.artifact_id, "artifact IDs")
    _unique(artifacts, lambda item: normalized_name(item.name), "artifact names")
    _unique(
        artifacts,
        lambda item: (item.platform, item.package_kind),
        "artifact targets",
    )


def _validate_install_files(files: tuple[InstallFile, ...]) -> None:
    _unique(files, lambda item: item.path, "install file paths")
    _unique(files, lambda item: normalized_name(item.path), "install file paths")
    by_path = {item.path: item for item in files}
    required = {
        FIXED_LAUNCHER_PATH,
        FIXED_APP_ASAR_PATH,
        FIXED_LICENSE_INVENTORY_PATH,
    }
    if not required <= by_path.keys():
        _invalid("required install files")
    if not by_path[FIXED_LAUNCHER_PATH].executable:
        _invalid("launcher executable declaration")
    if by_path[FIXED_APP_ASAR_PATH].executable:
        _invalid("app.asar executable declaration")


def _unique[ValueT, KeyT](
    values: tuple[ValueT, ...],
    key: Callable[[ValueT], KeyT],
    label: str,
) -> None:
    seen: set[KeyT] = set()
    for value in values:
        identity = key(value)
        if identity in seen:
            raise ArtifactContractError(
                ArtifactContractErrorCode.DUPLICATE_VALUE,
                f"Manifest contains duplicate or colliding {label}",
            )
        seen.add(identity)


def _semantic_version(value: JsonValue, label: str) -> str:
    text = require_string(value, label, max_bytes=128)
    validate_semantic_version(text, label)
    return text


def _portable_value(value: JsonValue, label: str) -> str:
    text = require_string(value, label, max_bytes=128)
    if _PORTABLE_VALUE_RE.fullmatch(text) is None:
        _invalid(label)
    return text


def _validate_kind_ownership(
    package_kind: PackageKind, ownership: ArtifactOwnership
) -> None:
    expected = (
        ArtifactOwnership.PER_USER
        if package_kind is PackageKind.USER_ARCHIVE
        else ArtifactOwnership.SYSTEM
    )
    if ownership is not expected:
        _invalid("package ownership")


def _unsupported_schema() -> None:
    raise ArtifactContractError(
        ArtifactContractErrorCode.UNSUPPORTED_SCHEMA,
        "Manifest schema version is unsupported",
    )


def _invalid(label: str) -> None:
    raise ArtifactContractError(
        ArtifactContractErrorCode.INVALID_FIELD,
        f"Manifest {label} is invalid",
    )


def _limit(message: str) -> None:
    raise ArtifactContractError(ArtifactContractErrorCode.LIMIT_EXCEEDED, message)
