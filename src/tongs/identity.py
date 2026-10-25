"""Forge-agnostic identity types shared by services and draft storage.

This module is a leaf: it depends only on the standard library so that
importing the durable draft store does not load the services package.
"""

from __future__ import annotations

from dataclasses import dataclass


def validate_hostname(hostname: str) -> None:
    if not isinstance(hostname, str):
        raise TypeError("hostname must be a string")
    if (
        not hostname
        or hostname != hostname.strip().lower()
        or "://" in hostname
        or "/" in hostname
        or "\\" in hostname
        or any(ord(char) < 33 for char in hostname)
    ):
        raise ValueError("hostname must be a canonical lowercase host name")


def validate_project_path(project_path: str) -> None:
    if not isinstance(project_path, str):
        raise TypeError("project_path must be a string")
    segments = project_path.split("/")
    if (
        project_path != project_path.strip()
        or len(segments) < 2
        or any(segment in {"", ".", ".."} for segment in segments)
        or "\\" in project_path
        or "://" in project_path
        or any(ord(char) < 32 for char in project_path)
    ):
        raise ValueError("project_path must be a canonical forge project path")


@dataclass(frozen=True, slots=True)
class RepositoryRef:
    """Semantic identity for a project on a configured forge host."""

    hostname: str
    project_path: str

    def __post_init__(self) -> None:
        validate_hostname(self.hostname)
        validate_project_path(self.project_path)


@dataclass(frozen=True, slots=True)
class ReviewRef:
    """Semantic identity for a merge or pull request."""

    repository: RepositoryRef
    number: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.repository, RepositoryRef)
            or not isinstance(self.number, int)
            or isinstance(self.number, bool)
            or self.number <= 0
        ):
            raise ValueError("review number must be positive")


@dataclass(frozen=True, slots=True)
class ReviewRevision:
    """Forge revision metadata required for revision-bound diff operations."""

    head_sha: str
    base_sha: str
    start_sha: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.head_sha, str)
            or not self.head_sha
            or not isinstance(self.base_sha, str)
            or not self.base_sha
            or (
                self.start_sha is not None
                and (not isinstance(self.start_sha, str) or not self.start_sha)
            )
        ):
            raise ValueError("head_sha and base_sha are required")


__all__ = [
    "RepositoryRef",
    "ReviewRef",
    "ReviewRevision",
    "validate_hostname",
    "validate_project_path",
]
