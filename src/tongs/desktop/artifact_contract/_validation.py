"""Semantic and path validation shared across artifact documents."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import total_ordering
from pathlib import PurePosixPath

from tongs.desktop.artifact_contract.models import (
    ArtifactContractError,
    ArtifactContractErrorCode,
)

_SEMVER_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:")

HARD_MAX_ENTRIES = 16_384
HARD_MAX_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
HARD_MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
HARD_MAX_PATH_BYTES = 1024


@total_ordering
@dataclass(frozen=True, slots=True)
class _SemanticVersion:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...]

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, _SemanticVersion):
            return NotImplemented
        own_core = (self.major, self.minor, self.patch)
        other_core = (other.major, other.minor, other.patch)
        if own_core != other_core:
            return own_core < other_core
        if not self.prerelease:
            return False
        if not other.prerelease:
            return True
        for own, theirs in zip(self.prerelease, other.prerelease, strict=False):
            if own == theirs:
                continue
            own_numeric = own.isdigit()
            their_numeric = theirs.isdigit()
            if own_numeric and their_numeric:
                return int(own) < int(theirs)
            if own_numeric != their_numeric:
                return own_numeric
            return own < theirs
        return len(self.prerelease) < len(other.prerelease)


def validate_semantic_version(value: str, label: str) -> _SemanticVersion:
    match = _SEMVER_RE.fullmatch(value)
    if match is None or len(value) > 128:
        _invalid(label)
    prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
    if any(
        identifier.isdigit() and len(identifier) > 1 and identifier[0] == "0"
        for identifier in prerelease
    ):
        _invalid(label)
    return _SemanticVersion(
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        prerelease,
    )


def validate_identifier(value: str, label: str) -> str:
    if len(value) > 128 or _IDENTIFIER_RE.fullmatch(value) is None:
        _invalid(label)
    return value


def validate_source_commit(value: str) -> str:
    if _COMMIT_RE.fullmatch(value) is None:
        _invalid("source_commit")
    return value


def validate_digest(value: str, label: str = "sha256") -> str:
    if _DIGEST_RE.fullmatch(value) is None:
        _invalid(label)
    return value


def validate_artifact_name(value: str) -> str:
    if (
        value in {".", ".."}
        or _encoded_length(value, layout=False) > 255
        or "/" in value
        or "\\" in value
        or any(ord(char) < 32 for char in value)
    ):
        _invalid("artifact name")
    return value


def normalized_name(value: str) -> str:
    """Return the collision key used before writing to common filesystems."""
    return unicodedata.normalize("NFC", value.replace("\\", "/")).casefold()


def validate_archive_path(value: str, max_path_bytes: int) -> str:
    if (
        not value
        or value.endswith("/")
        or _encoded_length(value, layout=True) > max_path_bytes
        or "\\" in value
        or "\x00" in value
        or any(ord(char) < 32 for char in value)
        or value.startswith("/")
        or _DRIVE_RE.match(value) is not None
    ):
        _layout_invalid()
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        _layout_invalid()
    return value


def _encoded_length(value: str, *, layout: bool) -> int:
    try:
        return len(value.encode("utf-8", errors="strict"))
    except UnicodeEncodeError as error:
        if layout:
            _layout_invalid()
        raise ArtifactContractError(
            ArtifactContractErrorCode.INVALID_FIELD,
            "Manifest string is not valid Unicode",
        ) from error


def validate_limits(
    max_entries: int,
    max_total_bytes: int,
    max_file_bytes: int,
    max_path_bytes: int,
) -> None:
    if not 1 <= max_entries <= HARD_MAX_ENTRIES:
        _limit_invalid()
    if not 1 <= max_file_bytes <= HARD_MAX_FILE_BYTES:
        _limit_invalid()
    if not max_file_bytes <= max_total_bytes <= HARD_MAX_TOTAL_BYTES:
        _limit_invalid()
    if not 32 <= max_path_bytes <= HARD_MAX_PATH_BYTES:
        _limit_invalid()


def _invalid(label: str) -> None:
    raise ArtifactContractError(
        ArtifactContractErrorCode.INVALID_FIELD,
        f"Manifest {label} is invalid",
    )


def _layout_invalid() -> None:
    raise ArtifactContractError(
        ArtifactContractErrorCode.INVALID_LAYOUT,
        "Archive contains an invalid path",
    )


def _limit_invalid() -> None:
    raise ArtifactContractError(
        ArtifactContractErrorCode.LIMIT_EXCEEDED,
        "Extraction limits are outside supported bounds",
    )
