"""Immutable models for the desktop release and installed archive contract."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PackageKind(StrEnum):
    USER_ARCHIVE = "user-archive"
    RPM = "rpm"


class ArtifactOwnership(StrEnum):
    PER_USER = "per-user"
    SYSTEM = "system"


class TargetOperatingSystem(StrEnum):
    LINUX = "linux"
    MACOS = "macos"
    WINDOWS = "windows"


class TargetArchitecture(StrEnum):
    X86_64 = "x86_64"
    AARCH64 = "aarch64"


class ArchiveEntryType(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    HARDLINK = "hardlink"
    CHARACTER_DEVICE = "character-device"
    BLOCK_DEVICE = "block-device"
    FIFO = "fifo"
    OTHER = "other"


class ArtifactContractErrorCode(StrEnum):
    INVALID_JSON = "invalid_json"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    INVALID_FIELD = "invalid_field"
    DUPLICATE_VALUE = "duplicate_value"
    LIMIT_EXCEEDED = "limit_exceeded"
    INCOMPATIBLE = "incompatible"
    ARTIFACT_MISMATCH = "artifact_mismatch"
    INVALID_ARCHIVE = "invalid_archive"
    INVALID_LAYOUT = "invalid_layout"


class ArtifactContractError(ValueError):
    """Stable safe validation failure shared by producers and consumers."""

    def __init__(self, code: ArtifactContractErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class DesktopPlatform:
    operating_system: TargetOperatingSystem
    architecture: TargetArchitecture
    distribution: str
    distribution_version: str
    abi: str


@dataclass(frozen=True, slots=True)
class DesktopCompatibility:
    core_minimum: str
    core_maximum_exclusive: str
    rpc_api_major: int
    plugin_api_major: int


@dataclass(frozen=True, slots=True)
class ExtractionLimits:
    max_entries: int
    max_total_bytes: int
    max_file_bytes: int
    max_path_bytes: int


@dataclass(frozen=True, slots=True)
class DesktopArtifact:
    artifact_id: str
    name: str
    package_kind: PackageKind
    ownership: ArtifactOwnership
    platform: DesktopPlatform
    byte_count: int
    sha256: str
    extraction_limits: ExtractionLimits


@dataclass(frozen=True, slots=True)
class DesktopReleaseManifest:
    schema_version: int
    release_version: str
    source_commit: str
    compatibility: DesktopCompatibility
    artifacts: tuple[DesktopArtifact, ...]


@dataclass(frozen=True, slots=True)
class InstallFile:
    path: str
    byte_count: int
    sha256: str
    executable: bool = False


@dataclass(frozen=True, slots=True)
class DesktopInstallManifest:
    schema_version: int
    release_version: str
    package_kind: PackageKind
    ownership: ArtifactOwnership
    platform: DesktopPlatform
    compatibility: DesktopCompatibility
    electron_version: str
    launcher_path: str
    extraction_limits: ExtractionLimits
    files: tuple[InstallFile, ...]


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    path: str
    entry_type: ArchiveEntryType
    mode: int
    byte_count: int
    sha256: str | None = None


@dataclass(frozen=True, slots=True)
class ArchiveInspection:
    entries: tuple[ArchiveEntry, ...]
    install_document: bytes


@dataclass(frozen=True, slots=True)
class ValidatedArchiveLayout:
    entries: tuple[ArchiveEntry, ...]
    file_count: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class ValidatedArtifactContract:
    release: DesktopReleaseManifest
    artifact: DesktopArtifact
    install: DesktopInstallManifest
    layout: ValidatedArchiveLayout
