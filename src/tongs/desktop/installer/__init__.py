"""Verified desktop release discovery, download, and private staging."""

from __future__ import annotations

from tongs.desktop.installer.extract import (
    discard_staged_artifact,
    extract_verified_archive,
    stage_desktop_release,
)
from tongs.desktop.installer.metadata import (
    discover_release,
    normalize_platform_identity,
    production_verification_policy,
    resolve_tag_commit,
    verify_release_metadata,
)
from tongs.desktop.installer.models import (
    AcceptedReleaseState,
    BuildIdentity,
    DSSEVerifier,
    InstallerError,
    InstallerErrorCode,
    InstallerLimits,
    InstallRequest,
    ReleaseAsset,
    ReleaseRecord,
    ReleaseStateStore,
    VerifiedReleaseMetadata,
    VerifiedStagedArtifact,
)

__all__ = [
    "AcceptedReleaseState",
    "BuildIdentity",
    "DSSEVerifier",
    "InstallRequest",
    "InstallerError",
    "InstallerErrorCode",
    "InstallerLimits",
    "ReleaseAsset",
    "ReleaseRecord",
    "ReleaseStateStore",
    "VerifiedReleaseMetadata",
    "VerifiedStagedArtifact",
    "discard_staged_artifact",
    "discover_release",
    "extract_verified_archive",
    "normalize_platform_identity",
    "production_verification_policy",
    "resolve_tag_commit",
    "stage_desktop_release",
    "verify_release_metadata",
]
