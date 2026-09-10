"""Private extraction and atomic acceptance for verified desktop releases."""

from __future__ import annotations

import hashlib
import io
import os
import re
import secrets
import shutil
import stat
import tarfile
from pathlib import Path, PurePosixPath

import httpx
from packaging.version import InvalidVersion, Version

from tongs.desktop.artifact_contract import (
    ArchiveEntry,
    ArchiveEntryType,
    ArtifactContractError,
    ArtifactContractErrorCode,
    validate_artifact_archive,
)
from tongs.desktop.installer.download import download_asset_bytes
from tongs.desktop.installer.metadata import discover_release, verify_release_metadata
from tongs.desktop.installer.models import (
    AcceptedReleaseState,
    Clock,
    DSSEVerifier,
    InstallerError,
    InstallerErrorCode,
    InstallerLimits,
    InstallRequest,
    PlatformProbe,
    ReleaseStateStore,
    VerifiedReleaseMetadata,
    VerifiedStagedArtifact,
)

_STAGING_ATTEMPTS = 16
_STATE_ATTEMPTS = 8
_STAGE_NAME_RE = re.compile(r"^\.tongs-stage-[0-9a-f]{32}$")
_DEFAULT_REQUEST = InstallRequest()
_DEFAULT_LIMITS = InstallerLimits()


async def stage_desktop_release(
    client: httpx.AsyncClient,
    verifier: DSSEVerifier,
    state_store: ReleaseStateStore,
    platform_probe: PlatformProbe,
    staging_root: Path,
    *,
    core_version: str,
    rpc_api_major: int,
    plugin_api_major: int,
    request: InstallRequest = _DEFAULT_REQUEST,
    limits: InstallerLimits = _DEFAULT_LIMITS,
    clock: Clock,
) -> VerifiedStagedArtifact:
    """Verify, privately stage, then atomically record one desktop release.

    The accepted-version state is a local rollback watermark. It does not provide
    global release freshness or protect against compromise of the authorized
    repository and its production signing workflow.
    """
    platform = platform_probe()
    release = await discover_release(
        client, request, limits=limits, clock=clock, core_version=core_version
    )
    metadata = await verify_release_metadata(
        client,
        verifier,
        release,
        platform,
        core_version=core_version,
        rpc_api_major=rpc_api_major,
        plugin_api_major=plugin_api_major,
        limits=limits,
    )
    archive = await download_asset_bytes(
        client,
        metadata.artifact_asset,
        limits=limits,
        maximum_bytes=min(limits.max_archive_bytes, metadata.artifact.byte_count),
        expected_sha256=metadata.artifact.sha256,
    )
    staged = extract_verified_archive(archive, metadata, staging_root)
    accepted = False
    try:
        await _accept_release(state_store, staged, request)
        accepted = True
        return staged
    finally:
        if not accepted:
            discard_staged_artifact(staged)


def extract_verified_archive(
    archive: bytes,
    metadata: VerifiedReleaseMetadata,
    staging_root: Path,
) -> VerifiedStagedArtifact:
    """Validate and extract the same immutable archive bytes into a private path."""
    try:
        contract = validate_artifact_archive(
            archive,
            metadata.artifact.name,
            metadata.manifest,
            metadata.artifact.artifact_id,
        )
    except ArtifactContractError as error:
        code = (
            InstallerErrorCode.INTEGRITY_FAILED
            if error.code is ArtifactContractErrorCode.ARTIFACT_MISMATCH
            else InstallerErrorCode.EXTRACTION_FAILED
        )
        raise InstallerError(
            code,
            "The desktop archive failed validation before extraction.",
        ) from error

    root_fd = _open_private_staging_root(staging_root)
    stage_fd: int | None = None
    stage_name: str | None = None
    completed = False
    try:
        stage_name, stage_fd = _create_staging_directory(root_fd)
        os.fsync(root_fd)
        _extract_archive(archive, contract.layout.entries, stage_fd)
        os.fsync(stage_fd)
        completed = True
    except InstallerError:
        raise
    except (EOFError, OSError, tarfile.TarError) as error:
        raise InstallerError(
            InstallerErrorCode.EXTRACTION_FAILED,
            "The desktop archive could not be extracted safely.",
        ) from error
    finally:
        if stage_fd is not None:
            os.close(stage_fd)
        try:
            if stage_name is not None and not completed:
                _discard_incomplete(root_fd, stage_name)
        finally:
            os.close(root_fd)

    if stage_name is None:
        raise AssertionError("staging directory was not created")
    staging_path = staging_root / stage_name
    return VerifiedStagedArtifact(
        contract=contract,
        release=metadata.release,
        build_identity=metadata.build_identity,
        manifest_sha256=metadata.manifest_sha256,
        manifest_byte_count=metadata.manifest_byte_count,
        archive_sha256=hashlib.sha256(archive).hexdigest(),
        archive_byte_count=len(archive),
        source_commit=metadata.source_commit,
        staging_path=staging_path,
        launcher_path=staging_path / contract.install.launcher_path,
    )


def discard_staged_artifact(staged: VerifiedStagedArtifact) -> None:
    """Remove only the private staging directory owned by this result."""
    stage_path = staged.staging_path
    if _STAGE_NAME_RE.fullmatch(stage_path.name) is None:
        raise InstallerError(
            InstallerErrorCode.EXTRACTION_FAILED,
            "The desktop staging directory identity is invalid.",
        )
    try:
        root_fd = _open_existing_private_root(stage_path.parent)
    except FileNotFoundError:
        return
    try:
        stage_fd = os.open(
            stage_path.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=root_fd,
        )
        try:
            details = os.fstat(stage_fd)
            if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != 0o700:
                raise InstallerError(
                    InstallerErrorCode.EXTRACTION_FAILED,
                    "The desktop staging directory is unsafe.",
                )
        finally:
            os.close(stage_fd)
        shutil.rmtree(stage_path.name, dir_fd=root_fd)
    except FileNotFoundError:
        return
    except OSError as error:
        raise InstallerError(
            InstallerErrorCode.EXTRACTION_FAILED,
            "The incomplete desktop staging directory could not be removed.",
        ) from error
    finally:
        os.close(root_fd)


async def _accept_release(
    store: ReleaseStateStore,
    staged: VerifiedStagedArtifact,
    request: InstallRequest,
) -> None:
    replacement = AcceptedReleaseState(
        version=staged.release.version,
        manifest_sha256=staged.manifest_sha256,
        source_commit=staged.source_commit,
    )
    try:
        current = await store.read()
        for _attempt in range(_STATE_ATTEMPTS):
            relationship = _compare_release_state(current, replacement)
            if relationship < 0:
                if request.version is not None and request.allow_downgrade:
                    return
                raise InstallerError(
                    InstallerErrorCode.ROLLBACK_BLOCKED,
                    "The verified desktop release is older than the accepted version.",
                )
            if relationship == 0:
                if current == replacement:
                    return
                raise InstallerError(
                    InstallerErrorCode.STATE_CONFLICT,
                    "The accepted desktop release identity conflicts with this build.",
                )
            if await store.compare_and_swap(current, replacement):
                return
            current = await store.read()
    except InstallerError:
        raise
    except Exception as error:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The accepted desktop release state could not be updated.",
            retryable=True,
        ) from error
    raise InstallerError(
        InstallerErrorCode.STATE_CONFLICT,
        "The accepted desktop release state changed too frequently.",
        retryable=True,
    )


def _compare_release_state(
    current: AcceptedReleaseState | None,
    replacement: AcceptedReleaseState,
) -> int:
    if current is None:
        return 1
    try:
        old_version = Version(current.version)
        new_version = Version(replacement.version)
    except InvalidVersion as error:
        raise InstallerError(
            InstallerErrorCode.STATE_CONFLICT,
            "The accepted desktop release state is invalid.",
        ) from error
    if new_version < old_version:
        return -1
    if new_version == old_version:
        return 0
    return 1


def _open_private_staging_root(path: Path) -> int:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise InstallerError(
            InstallerErrorCode.EXTRACTION_FAILED,
            "The private desktop staging root is unavailable.",
        ) from error
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise InstallerError(
            InstallerErrorCode.EXTRACTION_FAILED,
            "The private desktop staging root is unsafe.",
        ) from error
    details = os.fstat(descriptor)
    if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077:
        os.close(descriptor)
        raise InstallerError(
            InstallerErrorCode.EXTRACTION_FAILED,
            "The private desktop staging root has unsafe ownership or permissions.",
        )
    return descriptor


def _open_existing_private_root(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise InstallerError(
            InstallerErrorCode.EXTRACTION_FAILED,
            "The private desktop staging root is unsafe.",
        ) from error
    details = os.fstat(descriptor)
    if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077:
        os.close(descriptor)
        raise InstallerError(
            InstallerErrorCode.EXTRACTION_FAILED,
            "The private desktop staging root has unsafe ownership or permissions.",
        )
    return descriptor


def _create_staging_directory(root_fd: int) -> tuple[str, int]:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    for _attempt in range(_STAGING_ATTEMPTS):
        name = f".tongs-stage-{secrets.token_hex(16)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            continue
        descriptor = os.open(name, flags, dir_fd=root_fd)
        os.fchmod(descriptor, 0o700)
        return name, descriptor
    raise InstallerError(
        InstallerErrorCode.DESTINATION_COLLISION,
        "A private desktop staging directory could not be allocated.",
        retryable=True,
    )


def _extract_archive(
    archive: bytes, expected_entries: tuple[ArchiveEntry, ...], stage_fd: int
) -> None:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as opened:
        members = opened.getmembers()
        if len(members) != len(expected_entries):
            raise InstallerError(
                InstallerErrorCode.EXTRACTION_FAILED,
                "The desktop archive changed after validation.",
            )
        matched = tuple(zip(members, expected_entries, strict=True))
        for member, expected in matched:
            member_path = member.name.rstrip("/") if member.isdir() else member.name
            if (
                member_path != expected.path
                or member.size != expected.byte_count
                or member.mode != expected.mode
            ):
                raise InstallerError(
                    InstallerErrorCode.EXTRACTION_FAILED,
                    "The desktop archive changed after validation.",
                )
        directories = sorted(
            (
                expected
                for expected in expected_entries
                if expected.entry_type is ArchiveEntryType.DIRECTORY
            ),
            key=lambda entry: (len(PurePosixPath(entry.path).parts), entry.path),
        )
        for expected in directories:
            parts = PurePosixPath(expected.path).parts
            parent_fd = _open_parent(stage_fd, parts[:-1])
            try:
                os.mkdir(parts[-1], mode=0o700, dir_fd=parent_fd)
                directory_fd = os.open(
                    parts[-1],
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=parent_fd,
                )
                try:
                    os.fchmod(directory_fd, expected.mode)
                finally:
                    os.close(directory_fd)
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)

        for member, expected in matched:
            if expected.entry_type is ArchiveEntryType.DIRECTORY:
                continue
            parts = PurePosixPath(expected.path).parts
            parent_fd = _open_parent(stage_fd, parts[:-1])
            try:
                _extract_file(opened, member, expected, parent_fd, parts[-1])
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)


def _open_parent(stage_fd: int, parts: tuple[str, ...]) -> int:
    descriptor = os.dup(stage_fd)
    try:
        for part in parts:
            child = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _extract_file(
    opened: tarfile.TarFile,
    member: tarfile.TarInfo,
    expected: ArchiveEntry,
    parent_fd: int,
    name: str,
) -> None:
    source = opened.extractfile(member)
    if source is None:
        raise InstallerError(
            InstallerErrorCode.EXTRACTION_FAILED,
            "The desktop archive file content is unavailable.",
        )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
    observed = 0
    digest = hashlib.sha256()
    try:
        while chunk := source.read(64 * 1024):
            observed += len(chunk)
            if observed > expected.byte_count:
                raise InstallerError(
                    InstallerErrorCode.EXTRACTION_FAILED,
                    "The desktop archive file exceeded its validated length.",
                )
            digest.update(chunk)
            _write_all(descriptor, chunk)
        if observed != expected.byte_count or digest.hexdigest() != expected.sha256:
            raise InstallerError(
                InstallerErrorCode.EXTRACTION_FAILED,
                "The desktop archive file disagrees with its validated content.",
            )
        os.fchmod(descriptor, expected.mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short desktop archive write")
        view = view[written:]


def _discard_incomplete(root_fd: int, name: str) -> None:
    try:
        shutil.rmtree(name, dir_fd=root_fd)
    except FileNotFoundError:
        return
    except OSError as error:
        raise InstallerError(
            InstallerErrorCode.EXTRACTION_FAILED,
            "The incomplete desktop staging directory could not be removed.",
        ) from error


__all__ = [
    "discard_staged_artifact",
    "extract_verified_archive",
    "stage_desktop_release",
]
