"""Shared data models for forge operations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from tongs.scanner.repo import ForgeType


class MRState(Enum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class CIStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELED = "canceled"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


class ReviewDecision(Enum):
    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    COMMENTED = "commented"
    REVIEW_REQUIRED = "review_required"
    NONE = "none"


@dataclass(frozen=True)
class ForgeHost:
    """Identifies a forge instance."""

    hostname: str
    forge_type: ForgeType
    api_base: str


@dataclass(frozen=True)
class User:
    username: str
    display_name: str = ""


@dataclass(frozen=True)
class MRSummary:
    """Lightweight MR for list views. No diff or comment data."""

    forge_host: ForgeHost
    repo_path: str
    local_path: str
    number: int
    title: str
    author: User
    state: MRState
    is_draft: bool
    source_branch: str
    target_branch: str
    ci_status: CIStatus
    created_at: datetime
    updated_at: datetime
    web_url: str
    comment_count: int = 0
    has_conflicts: bool = False
    labels: tuple[str, ...] = ()
    # GitHub-specific
    review_decision: ReviewDecision | None = None
    additions: int | None = None
    deletions: int | None = None


@dataclass(frozen=True)
class MRDetail(MRSummary):
    """Full MR with description and metadata. Still no diff/comments."""

    description: str = ""
    merge_status: str = ""
    approvals: tuple[User, ...] = ()
    reviewers: tuple[User, ...] = ()
    assignees: tuple[User, ...] = ()
    changes_count: int = 0
    # GitLab-specific
    detailed_merge_status: str | None = None
    draft_notes_count: int | None = None
    # GitHub-specific
    status_check_rollup: str | None = None


@dataclass(frozen=True)
class InlineComment:
    """A comment anchored to a specific diff position."""

    id: str
    author: User
    body: str
    created_at: datetime
    file_path: str
    old_line: int | None = None
    new_line: int | None = None
    is_resolved: bool = False
    replies: tuple["InlineComment", ...] = ()


@dataclass(frozen=True)
class Discussion:
    """A comment thread (may or may not be inline)."""

    id: str
    is_inline: bool
    root_comment: InlineComment
    is_resolved: bool = False
    resolvable: bool = True


@dataclass(frozen=True)
class Pipeline:
    """CI pipeline / workflow run."""

    id: int
    status: CIStatus
    ref: str
    sha: str
    web_url: str
    source: str = ""
    created_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: int | None = None


@dataclass(frozen=True)
class PipelineJob:
    """A job within a pipeline."""

    id: int
    name: str
    stage: str
    status: CIStatus
    web_url: str = ""
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_seconds: int | None = None
    allow_failure: bool = False
