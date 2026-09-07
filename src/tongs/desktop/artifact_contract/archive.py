"""Read-only validation for desktop archive bytes and logical layouts."""

from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from collections.abc import Sequence
from pathlib import PurePosixPath

from tongs.desktop.artifact_contract._validation import (
    normalized_name,
    validate_archive_path,
    validate_digest,
    validate_limits,
)
from tongs.desktop.artifact_contract.manifests import (
    FIXED_APP_ASAR_PATH,
    FIXED_LAUNCHER_PATH,
    FIXED_LICENSE_INVENTORY_PATH,
    parse_install_manifest,
)
from tongs.desktop.artifact_contract.models import (
    ArchiveEntry,
    ArchiveEntryType,
    ArchiveInspection,
    ArtifactContractError,
    ArtifactContractErrorCode,
    DesktopArtifact,
    DesktopInstallManifest,
    DesktopPlatform,
    DesktopReleaseManifest,
    ExtractionLimits,
    PackageKind,
    ValidatedArchiveLayout,
    ValidatedArtifactContract,
)

INSTALL_MANIFEST_PATH = "desktop-install.json"


def select_release_artifact(
    release: DesktopReleaseManifest,
    platform: DesktopPlatform,
    package_kind: PackageKind,
) -> DesktopArtifact:
    """Select exactly one previously validated target without network access."""
    matches = tuple(
        artifact
        for artifact in release.artifacts
        if artifact.platform == platform and artifact.package_kind is package_kind
    )
    if len(matches) != 1:
        raise ArtifactContractError(
            ArtifactContractErrorCode.INCOMPATIBLE,
            "Release does not contain exactly one matching desktop target",
        )
    return matches[0]


def inspect_archive(document: bytes, limits: ExtractionLimits) -> ArchiveInspection:
    """Inspect a gzip tar archive in memory without extracting any path."""
    _validate_extraction_limits(limits)
    if not isinstance(document, bytes) or not document.startswith(b"\x1f\x8b"):
        _invalid_archive("Desktop artifact is not a gzip archive")
    entries: list[ArchiveEntry] = []
    install_document: bytes | None = None
    total_bytes = 0
    try:
        _preflight_archive_headers(document, limits)
        with tarfile.open(fileobj=io.BytesIO(document), mode="r:gz") as archive:
            for member in archive:
                if len(entries) >= limits.max_entries:
                    _limit("Archive entry count exceeds the declared limit")
                path = member.name.rstrip("/") if member.isdir() else member.name
                entry_type = _entry_type(member)
                byte_count = member.size
                digest: str | None = None
                if member.isfile():
                    if byte_count < 0 or byte_count > limits.max_file_bytes:
                        _limit("Archive file size exceeds the declared limit")
                    total_bytes += byte_count
                    if total_bytes > limits.max_total_bytes:
                        _limit("Archive content exceeds the declared total limit")
                    stream = archive.extractfile(member)
                    if stream is None:
                        _invalid_archive("Archive file content is unavailable")
                    hasher = hashlib.sha256()
                    chunks: list[bytes] | None = (
                        [] if path == INSTALL_MANIFEST_PATH else None
                    )
                    observed = 0
                    while chunk := stream.read(64 * 1024):
                        observed += len(chunk)
                        if observed > byte_count or observed > limits.max_file_bytes:
                            _limit("Archive file content exceeds its declared size")
                        hasher.update(chunk)
                        if chunks is not None:
                            chunks.append(chunk)
                    if observed != byte_count:
                        _invalid_archive("Archive file content length is inconsistent")
                    digest = hasher.hexdigest()
                    if chunks is not None:
                        if install_document is not None:
                            _layout_error(
                                "Archive contains duplicate install manifests"
                            )
                        install_document = b"".join(chunks)
                entries.append(
                    ArchiveEntry(
                        path=path,
                        entry_type=entry_type,
                        mode=member.mode,
                        byte_count=byte_count,
                        sha256=digest,
                    )
                )
    except ArtifactContractError:
        raise
    except (EOFError, OSError, tarfile.TarError) as error:
        raise ArtifactContractError(
            ArtifactContractErrorCode.INVALID_ARCHIVE,
            "Desktop artifact is not a valid gzip tar archive",
        ) from error
    if not entries:
        _limit("Archive entry count exceeds the declared limit")
    if install_document is None:
        _layout_error("Archive is missing its root install manifest")
    return ArchiveInspection(tuple(entries), install_document)


def _preflight_archive_headers(document: bytes, limits: ExtractionLimits) -> None:
    """Bound tar metadata before ``tarfile`` interprets extension headers."""
    maximum_archive_bytes = (
        limits.max_total_bytes
        + limits.max_entries * (limits.max_path_bytes + 2_048)
        + 10_240
    )
    if len(document) > maximum_archive_bytes:
        _limit("Compressed archive size exceeds declared limits")

    physical_headers = 0
    logical_entries = 0
    extension_bytes = 0
    maximum_extension_bytes = limits.max_entries * (limits.max_path_bytes + 1_024)
    with gzip.GzipFile(fileobj=io.BytesIO(document), mode="rb") as stream:
        while True:
            header = _read_exact(stream, 512)
            if header == b"":
                _invalid_archive("Archive is missing its end marker")
            if header == bytes(512):
                return
            physical_headers += 1
            if physical_headers > limits.max_entries * 3 + 2:
                _limit("Archive header count exceeds declared limits")
            size = _tar_number(header[124:136])
            if size < 0:
                _invalid_archive("Archive header contains a negative size")
            entry_type = header[156:157]
            if entry_type in {b"x", b"g", b"L", b"K"}:
                extension_bytes += size
                if (
                    size > limits.max_path_bytes + 1_024
                    or extension_bytes > maximum_extension_bytes
                ):
                    _limit("Archive extension metadata exceeds declared limits")
            else:
                logical_entries += 1
                if logical_entries > limits.max_entries:
                    _limit("Archive entry count exceeds the declared limit")
                if entry_type == b"S":
                    _invalid_archive("GNU sparse archive metadata is unsupported")
                if size > limits.max_file_bytes:
                    _limit("Archive member size exceeds the declared limit")
            _discard_exact(stream, size + (-size % 512))


def _read_exact(stream: gzip.GzipFile, byte_count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = byte_count
    while remaining:
        chunk = stream.read(min(remaining, 64 * 1024))
        if not chunk:
            if remaining == byte_count:
                return b""
            _invalid_archive("Archive ends inside a header or member")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _discard_exact(stream: gzip.GzipFile, byte_count: int) -> None:
    remaining = byte_count
    while remaining:
        chunk = stream.read(min(remaining, 64 * 1024))
        if not chunk:
            _invalid_archive("Archive ends inside a header or member")
        remaining -= len(chunk)


def _tar_number(field: bytes) -> int:
    if not field:
        _invalid_archive("Archive contains an invalid numeric header")
    if field[0] in (0o200, 0o377):
        value = 0
        for byte in field[1:]:
            value = (value << 8) + byte
        if field[0] == 0o377:
            value = -(256 ** (len(field) - 1) - value)
        return value
    try:
        text = field.split(b"\0", 1)[0].decode("ascii", errors="strict").strip()
        return int(text or "0", 8)
    except (UnicodeDecodeError, ValueError) as error:
        raise ArtifactContractError(
            ArtifactContractErrorCode.INVALID_ARCHIVE,
            "Archive contains an invalid numeric header",
        ) from error


def validate_archive_layout(
    entries: Sequence[ArchiveEntry],
    install: DesktopInstallManifest,
) -> ValidatedArchiveLayout:
    """Validate one logical archive layout without filesystem writes."""
    limits = install.extraction_limits
    _validate_extraction_limits(limits)
    if not entries or len(entries) > limits.max_entries:
        _limit("Archive entry count exceeds the declared limit")

    by_path: dict[str, ArchiveEntry] = {}
    normalized_paths: set[str] = set()
    total_bytes = 0
    file_count = 0
    for entry in entries:
        path = validate_archive_path(entry.path, limits.max_path_bytes)
        collision_key = normalized_name(path)
        if path in by_path or collision_key in normalized_paths:
            _layout_error("Archive contains duplicate or colliding paths")
        by_path[path] = entry
        normalized_paths.add(collision_key)
        if entry.entry_type not in (ArchiveEntryType.FILE, ArchiveEntryType.DIRECTORY):
            _layout_error("Archive contains a forbidden entry type")
        if entry.mode & ~0o777:
            _layout_error("Archive entry uses special permission bits")
        if entry.entry_type is ArchiveEntryType.DIRECTORY:
            if entry.mode != 0o755 or entry.byte_count != 0 or entry.sha256 is not None:
                _layout_error("Archive directory metadata is invalid")
            continue
        file_count += 1
        if entry.mode not in (0o644, 0o755):
            _layout_error("Archive file mode is invalid")
        if not 0 <= entry.byte_count <= limits.max_file_bytes:
            _limit("Archive file size exceeds the declared limit")
        total_bytes += entry.byte_count
        if total_bytes > limits.max_total_bytes:
            _limit("Archive content exceeds the declared total limit")
        if entry.sha256 is None:
            _layout_error("Archive file digest is missing")
        validate_digest(entry.sha256, "archive entry sha256")

    declarations = {item.path: item for item in install.files}
    allowed_directories = {"runtime"}
    for path in declarations:
        parent = PurePosixPath(path).parent
        while str(parent) not in {".", ""}:
            allowed_directories.add(str(parent))
            parent = parent.parent

    required_paths = {
        INSTALL_MANIFEST_PATH,
        "runtime",
        "runtime/resources",
        FIXED_LAUNCHER_PATH,
        FIXED_APP_ASAR_PATH,
        FIXED_LICENSE_INVENTORY_PATH,
    }
    if not required_paths <= by_path.keys():
        _layout_error("Archive is missing a required root or runtime entry")
    for path, entry in by_path.items():
        if entry.entry_type is ArchiveEntryType.DIRECTORY:
            if path not in allowed_directories:
                _layout_error("Archive contains an undeclared directory")
            continue
        if path == INSTALL_MANIFEST_PATH:
            if entry.mode != 0o644:
                _layout_error("Install manifest mode is invalid")
            continue
        declaration = declarations.get(path)
        if declaration is None:
            _layout_error("Archive contains an undeclared file")
        expected_mode = 0o755 if declaration.executable else 0o644
        if entry.mode != expected_mode:
            _layout_error("Archive file mode disagrees with its declaration")
        if (
            entry.byte_count != declaration.byte_count
            or entry.sha256 != declaration.sha256
        ):
            _layout_error("Archive file content disagrees with its declaration")

    for path in declarations:
        entry = by_path.get(path)
        if entry is None or entry.entry_type is not ArchiveEntryType.FILE:
            _layout_error("Archive is missing a declared file")
        parent = PurePosixPath(path).parent
        while str(parent) not in {".", ""}:
            parent_entry = by_path.get(str(parent))
            if (
                parent_entry is None
                or parent_entry.entry_type is not ArchiveEntryType.DIRECTORY
            ):
                _layout_error("Archive is missing a declared parent directory")
            parent = parent.parent

    launcher = declarations[install.launcher_path]
    if not launcher.executable:
        _layout_error("Archive launcher is not declared executable")
    return ValidatedArchiveLayout(tuple(entries), file_count, total_bytes)


def validate_manifest_pair(
    release: DesktopReleaseManifest,
    artifact: DesktopArtifact,
    install: DesktopInstallManifest,
) -> None:
    """Reject any external and internal manifest disagreement."""
    if artifact not in release.artifacts:
        _mismatch("Artifact is not part of the release manifest")
    if (
        release.release_version != install.release_version
        or release.compatibility != install.compatibility
        or artifact.platform != install.platform
        or artifact.package_kind is not install.package_kind
        or artifact.ownership is not install.ownership
        or artifact.extraction_limits != install.extraction_limits
        or install.launcher_path != FIXED_LAUNCHER_PATH
    ):
        _mismatch("Release and install manifests disagree")


def validate_artifact_archive(
    document: bytes,
    archive_name: str,
    release: DesktopReleaseManifest,
    artifact_id: str,
) -> ValidatedArtifactContract:
    """Validate exact archive bytes against both manifests and layout rules."""
    matches = tuple(
        artifact
        for artifact in release.artifacts
        if artifact.artifact_id == artifact_id
    )
    if len(matches) != 1:
        _mismatch("Release artifact identity is missing or ambiguous")
    artifact = matches[0]
    if artifact.package_kind is not PackageKind.USER_ARCHIVE:
        _mismatch("Selected artifact is not a per-user archive")
    if (
        archive_name != artifact.name
        or len(document) != artifact.byte_count
        or hashlib.sha256(document).hexdigest() != artifact.sha256
    ):
        _mismatch("Archive name, length, or digest disagrees with the release manifest")
    inspection = inspect_archive(document, artifact.extraction_limits)
    install = parse_install_manifest(inspection.install_document)
    validate_manifest_pair(release, artifact, install)
    layout = validate_archive_layout(inspection.entries, install)
    return ValidatedArtifactContract(release, artifact, install, layout)


def _entry_type(member: tarfile.TarInfo) -> ArchiveEntryType:
    if member.isfile():
        return ArchiveEntryType.FILE
    if member.isdir():
        return ArchiveEntryType.DIRECTORY
    if member.issym():
        return ArchiveEntryType.SYMLINK
    if member.islnk():
        return ArchiveEntryType.HARDLINK
    if member.ischr():
        return ArchiveEntryType.CHARACTER_DEVICE
    if member.isblk():
        return ArchiveEntryType.BLOCK_DEVICE
    if member.isfifo():
        return ArchiveEntryType.FIFO
    return ArchiveEntryType.OTHER


def _validate_extraction_limits(limits: ExtractionLimits) -> None:
    validate_limits(
        limits.max_entries,
        limits.max_total_bytes,
        limits.max_file_bytes,
        limits.max_path_bytes,
    )


def _invalid_archive(message: str) -> None:
    raise ArtifactContractError(ArtifactContractErrorCode.INVALID_ARCHIVE, message)


def _layout_error(message: str) -> None:
    raise ArtifactContractError(ArtifactContractErrorCode.INVALID_LAYOUT, message)


def _limit(message: str) -> None:
    raise ArtifactContractError(ArtifactContractErrorCode.LIMIT_EXCEEDED, message)


def _mismatch(message: str) -> None:
    raise ArtifactContractError(ArtifactContractErrorCode.ARTIFACT_MISMATCH, message)
