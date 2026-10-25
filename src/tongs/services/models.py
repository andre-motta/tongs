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
from tongs.identity import (
    RepositoryRef,
    ReviewRef,
    ReviewRevision,
    validate_hostname,
    validate_project_path,
)
from tongs.scanner.repo import ForgeType
from tongs.services.errors import ServiceError, ServiceErrorCode


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
    hostnames: tuple[str, ...] | None = None
    # When set, an All Open read fetches one page per repository instead of
    # walking every page. ``cursor`` continues a repository-scoped paged read
    # from the ``next_cursor`` a previous page returned.
    paged: bool = False
    cursor: str | None = None
    # An explicit refresh: skip any cached list or first page, store what the
    # forge returns, and look up again a sign-in that failed recently.
    fresh: bool = False
    # False lets a paged All Open read skip the per-review CI requests of
    # forges that support it. Its rows then carry UNKNOWN CI and a head commit
    # for ``ApplicationSession.get_review_ci_status``. Other reads ignore it.
    with_ci: bool = True

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
        if self.hostnames is not None:
            if not isinstance(self.hostnames, tuple):
                raise TypeError("hostnames must be a tuple")
            for hostname in self.hostnames:
                validate_hostname(hostname)
            if len(self.hostnames) != len(set(self.hostnames)):
                raise ValueError("hostnames must not contain duplicates")
        if not isinstance(self.paged, bool):
            raise TypeError("paged must be a boolean")
        if not isinstance(self.fresh, bool):
            raise TypeError("fresh must be a boolean")
        if not isinstance(self.with_ci, bool):
            raise TypeError("with_ci must be a boolean")
        if self.cursor is not None:
            review_cursor_page(self.cursor)
            if (
                not self.paged
                or self.repository is None
                or self.scope != ReviewScope.ALL_OPEN
            ):
                raise ValueError(
                    "a cursor needs a paged All Open read of one repository"
                )


MAX_REVIEW_PAGE = 10_000


def review_cursor_page(cursor: str) -> int:
    """Return the forge page a session review cursor names, or raise."""
    if (
        not isinstance(cursor, str)
        or not 1 <= len(cursor) <= 5
        or not cursor.isascii()
        or not cursor.isdigit()
        or cursor.startswith("0")
    ):
        raise ValueError("the review cursor is invalid")
    page = int(cursor)
    if not 2 <= page <= MAX_REVIEW_PAGE:
        raise ValueError("the review cursor is invalid")
    return page


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
    # Set only for a paged read of one repository that has another page, or
    # whose later page failed and can be read again.
    next_cursor: str | None = None


@dataclass(frozen=True, slots=True)
class RawDiffSnapshot:
    """Unconverted forge change payload bound to a verified stable revision."""

    review: ReviewRef
    revision: ReviewRevision
    changes: tuple[Mapping[str, object], ...]
    # True when the forge capped the changed-file list, so ``changes`` is
    # partial. Appended with a default for positional compatibility.
    changes_overflow: bool = False


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
    "validate_hostname",
    "validate_project_path",
]
