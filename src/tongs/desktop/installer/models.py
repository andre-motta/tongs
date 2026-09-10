"""Immutable models for verified desktop release staging."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from sigstore.models import Bundle
from sigstore.verify.policy import VerificationPolicy

from tongs.desktop.artifact_contract import (
    DesktopArtifact,
    DesktopPlatform,
    DesktopReleaseManifest,
    ValidatedArtifactContract,
)


class InstallerErrorCode(StrEnum):
    """Stable failure categories for installer callers."""

    INVALID_METADATA = "invalid_metadata"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    INCOMPATIBLE = "incompatible"
    PROVENANCE_FAILED = "provenance_failed"
    DOWNLOAD_FAILED = "download_failed"
    INTEGRITY_FAILED = "integrity_failed"
    EXTRACTION_FAILED = "extraction_failed"
    LIMIT_EXCEEDED = "limit_exceeded"
    DESTINATION_COLLISION = "destination_collision"
    ROLLBACK_BLOCKED = "rollback_blocked"
    STATE_CONFLICT = "state_conflict"


class InstallerError(RuntimeError):
    """Safe installer failure without remote response or credential details."""

    def __init__(
        self, code: InstallerErrorCode, message: str, *, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


PERSISTENT_INSTALL_GUIDANCE = (
    "Install Tongs with 'pipx install tongs' or "
    "'python -m pip install --user tongs', then retry."
)
"""Single wording for the persistent installations the desktop commands accept."""


@dataclass(frozen=True, slots=True)
class InstallerLimits:
    """Implementation-owned bounds independent of untrusted manifests."""

    max_release_pages: int = 10
    releases_per_page: int = 100
    max_metadata_bytes: int = 1024 * 1024
    max_bundle_bytes: int = 2 * 1024 * 1024
    max_archive_bytes: int = 256 * 1024 * 1024
    stream_chunk_bytes: int = 64 * 1024
    max_redirects: int = 5
    max_tag_indirections: int = 4

    def __post_init__(self) -> None:
        values = (
            self.max_release_pages,
            self.releases_per_page,
            self.max_metadata_bytes,
            self.max_bundle_bytes,
            self.max_archive_bytes,
            self.stream_chunk_bytes,
            self.max_redirects,
            self.max_tag_indirections,
        )
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("installer limits must be positive integers")
        if self.releases_per_page > 100:
            raise ValueError("GitHub release page size cannot exceed 100")
        if self.stream_chunk_bytes > self.max_archive_bytes:
            raise ValueError("stream chunk cannot exceed archive limit")


@dataclass(frozen=True, slots=True)
class InstallRequest:
    """Requested desktop release selection and explicit downgrade control."""

    version: str | None = None
    allow_downgrade: bool = False

    def __post_init__(self) -> None:
        if self.version is not None and not isinstance(self.version, str):
            raise TypeError("version must be a string or None")
        if type(self.allow_downgrade) is not bool:
            raise TypeError("allow_downgrade must be a boolean")
        if self.version is None and self.allow_downgrade:
            raise ValueError("implicit release selection cannot allow downgrade")


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    """Validated immutable GitHub release asset identity."""

    asset_id: int
    name: str
    byte_count: int
    sha256: str
    api_url: str


@dataclass(frozen=True, slots=True)
class ReleaseRecord:
    """Validated immutable stable desktop release metadata."""

    release_id: int
    tag: str
    version: str
    published_at: datetime
    assets: tuple[ReleaseAsset, ...]


@dataclass(frozen=True, slots=True)
class BuildIdentity:
    """Exact GitHub Actions identity verified by certificate and predicate."""

    issuer: str
    repository: str
    workflow_path: str
    ref: str
    source_commit: str
    event: str
    builder_id: str


@dataclass(frozen=True, slots=True)
class AcceptedReleaseState:
    """Highest fully staged release accepted by this installation."""

    version: str
    manifest_sha256: str
    source_commit: str


class ReleaseStateStore(Protocol):
    """Atomic accepted-version persistence required by activation work."""

    async def read(self) -> AcceptedReleaseState | None: ...

    async def compare_and_swap(
        self,
        expected: AcceptedReleaseState | None,
        replacement: AcceptedReleaseState,
    ) -> bool: ...


class DSSEVerifier(Protocol):
    """Public Sigstore verifier surface used by production and test harnesses."""

    def verify_dsse(
        self, bundle: Bundle, policy: VerificationPolicy
    ) -> tuple[str, bytes]: ...


type PlatformProbe = Callable[[], DesktopPlatform]
type Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class VerifiedReleaseMetadata:
    """Manifest selection bound to verified release and build provenance."""

    release: ReleaseRecord
    manifest: DesktopReleaseManifest
    artifact: DesktopArtifact
    artifact_asset: ReleaseAsset
    manifest_sha256: str
    manifest_byte_count: int
    source_commit: str
    build_identity: BuildIdentity


@dataclass(frozen=True, slots=True)
class VerifiedStagedArtifact:
    """Complete verified archive extracted into a private inactive directory."""

    contract: ValidatedArtifactContract
    release: ReleaseRecord
    build_identity: BuildIdentity
    manifest_sha256: str
    manifest_byte_count: int
    archive_sha256: str
    archive_byte_count: int
    source_commit: str
    staging_path: Path
    launcher_path: Path


__all__ = [
    "PERSISTENT_INSTALL_GUIDANCE",
    "AcceptedReleaseState",
    "BuildIdentity",
    "Clock",
    "DSSEVerifier",
    "InstallRequest",
    "InstallerError",
    "InstallerErrorCode",
    "InstallerLimits",
    "PlatformProbe",
    "ReleaseAsset",
    "ReleaseRecord",
    "ReleaseStateStore",
    "VerifiedReleaseMetadata",
    "VerifiedStagedArtifact",
]
