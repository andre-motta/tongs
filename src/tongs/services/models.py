"""Immutable models for the shared application service boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from tongs.forges.models import (
    Commit,
    Discussion,
    MRDetail,
    MRState,
    MRSummary,
    Pipeline,
    PipelineJob,
)
from tongs.scanner.repo import ForgeType
from tongs.services.errors import ServiceError, ServiceErrorCode


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


@dataclass(frozen=True, slots=True)
class ForgeCapabilities:
    """Read-only snapshot of forge capabilities relevant to a review."""

    batched_review: bool
    thread_resolution: bool
    draft_notes: bool
    unapprove: bool
    job_cancel: bool


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    """Discovered repository data safe for service consumers."""

    ref: RepositoryRef
    display_name: str
    forge_type: ForgeType


@dataclass(frozen=True, slots=True)
class ReviewSnapshot:
    """Review detail bound to one captured forge revision."""

    ref: ReviewRef
    detail: MRDetail
    revision: ReviewRevision | None
    capabilities: ForgeCapabilities
    revision_error: ServiceError | None = None

    def __post_init__(self) -> None:
        if (self.revision is None) == (self.revision_error is None):
            raise ValueError(
                "exactly one of revision or revision_error must be provided"
            )


@dataclass(frozen=True, slots=True)
class ReviewListItem:
    """A summary paired with its session-issued semantic identity."""

    ref: ReviewRef
    summary: MRSummary


class ReviewScope(str, Enum):
    """Supported review-list queries."""

    ALL_OPEN = "all_open"
    MY_REVIEWS = "my_reviews"
    MY_MRS = "my_mrs"


@dataclass(frozen=True, slots=True)
class ReviewQuery:
    """Typed query for repository or configured-host inbox reads."""

    scope: ReviewScope
    repository: RepositoryRef | None = None
    state: MRState = MRState.OPEN
    per_page: int = 100

    def __post_init__(self) -> None:
        if not isinstance(self.scope, ReviewScope) or not isinstance(
            self.state, MRState
        ):
            raise TypeError("scope and state must use their declared enum types")
        if self.repository is not None and not isinstance(
            self.repository, RepositoryRef
        ):
            raise ValueError("repository must be a RepositoryRef")
        if (
            not isinstance(self.per_page, int)
            or isinstance(self.per_page, bool)
            or not 1 <= self.per_page <= 100
        ):
            raise ValueError("per_page must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class HostFailure:
    """A safe per-host or per-repository failure in a partial result."""

    hostname: str
    code: ServiceErrorCode
    message: str
    retryable: bool
    repository: RepositoryRef | None = None

    def __post_init__(self) -> None:
        validate_hostname(self.hostname)
        if not isinstance(self.code, ServiceErrorCode):
            raise TypeError("code must be a ServiceErrorCode")


@dataclass(frozen=True, slots=True)
class ReviewPage:
    """Review results that preserve successes when another host fails."""

    items: tuple[ReviewListItem, ...]
    failures: tuple[HostFailure, ...] = ()


@dataclass(frozen=True, slots=True)
class RawDiffSnapshot:
    """Unconverted forge change payload bound to a verified stable revision."""

    review: ReviewRef
    revision: ReviewRevision
    changes: tuple[Mapping[str, object], ...]


@dataclass(frozen=True, slots=True)
class PipelineRef:
    """Semantic identity for a repository pipeline."""

    repository: RepositoryRef
    pipeline_id: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.repository, RepositoryRef)
            or not isinstance(self.pipeline_id, int)
            or isinstance(self.pipeline_id, bool)
            or self.pipeline_id <= 0
        ):
            raise ValueError("pipeline_id must be positive")


@dataclass(frozen=True, slots=True)
class JobRef:
    """Semantic identity for a repository job."""

    repository: RepositoryRef
    job_id: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.repository, RepositoryRef)
            or not isinstance(self.job_id, int)
            or isinstance(self.job_id, bool)
            or self.job_id <= 0
        ):
            raise ValueError("job_id must be positive")


class ServiceEventKind(str, Enum):
    """Narrow invalidation hints emitted by the application session."""

    REPOSITORIES_CHANGED = "repositories_changed"
    REVIEW_CHANGED = "review_changed"
    PIPELINE_CHANGED = "pipeline_changed"
    RESYNC_REQUIRED = "resync_required"


@dataclass(frozen=True, slots=True)
class ServiceEvent:
    """Ordered refresh hint without adapter payloads or credentials."""

    sequence: int
    kind: ServiceEventKind
    resource: RepositoryRef | ReviewRef | PipelineRef | None = None
    revision: ReviewRevision | None = None


__all__ = [
    "Commit",
    "Discussion",
    "ForgeCapabilities",
    "HostFailure",
    "JobRef",
    "Pipeline",
    "PipelineJob",
    "PipelineRef",
    "RawDiffSnapshot",
    "RepositoryRef",
    "RepositorySnapshot",
    "ReviewListItem",
    "ReviewPage",
    "ReviewQuery",
    "ReviewRef",
    "ReviewRevision",
    "ReviewScope",
    "ReviewSnapshot",
    "ServiceEvent",
    "ServiceEventKind",
]
