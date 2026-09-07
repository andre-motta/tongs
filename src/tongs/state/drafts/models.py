"""Immutable models for persistent review drafts and submission recovery."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID, uuid4

from tongs.services import ReviewRef, ReviewRevision


class DraftState(str, Enum):
    """Durable lifecycle of a review draft."""

    EDITABLE = "editable"
    SUBMITTING = "submitting"
    PARTIAL = "partial"
    UNKNOWN = "unknown"
    SUBMITTED = "submitted"


class DraftVerdict(str, Enum):
    """Supported review decisions for a submitted review."""

    COMMENT = "comment"
    APPROVE = "approve"
    REQUEST_CHANGES = "request_changes"


class DiffSide(str, Enum):
    """Original side selected for an inline comment."""

    OLD = "old"
    NEW = "new"


class ReconciliationResolution(str, Enum):
    """Explicit decisions that resolve an outcome-unknown attempt."""

    RETRY_REMAINING = "retry_remaining"
    RETURN_EDITABLE = "return_editable"
    MARK_SUBMITTED = "mark_submitted"


def context_fingerprint(context: Sequence[str]) -> str:
    """Return a stable, unambiguous SHA-256 fingerprint for context lines."""
    digest = hashlib.sha256()
    for line in context:
        encoded = line.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class InlineAnchor:
    """A revision-bound inline location that is never silently retargeted."""

    revision: ReviewRevision
    old_path: str
    new_path: str
    old_line: int | None
    new_line: int | None
    side: DiffSide
    context_fingerprint: str
    start_line: int | None = None
    start_side: DiffSide | None = None
    stale: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.revision, ReviewRevision):
            raise TypeError("revision must be a ReviewRevision")
        if not self.old_path or not self.new_path:
            raise ValueError("old_path and new_path are required")
        if not isinstance(self.side, DiffSide):
            raise TypeError("side must be a DiffSide")
        selected_line = self.old_line if self.side == DiffSide.OLD else self.new_line
        if selected_line is None or selected_line <= 0:
            raise ValueError("the selected side must have a positive line number")
        for line in (self.old_line, self.new_line, self.start_line):
            if line is not None and (
                not isinstance(line, int) or isinstance(line, bool) or line <= 0
            ):
                raise ValueError("line numbers must be positive integers")
        if (self.start_line is None) != (self.start_side is None):
            raise ValueError("start_line and start_side must be provided together")
        if self.start_side is not None and not isinstance(self.start_side, DiffSide):
            raise TypeError("start_side must be a DiffSide")
        if not self.context_fingerprint:
            raise ValueError("context_fingerprint is required")

    def assessed_against(self, current_revision: ReviewRevision) -> InlineAnchor:
        """Mark the anchor stale when any captured revision identity differs."""
        stale = self.stale or current_revision != self.revision
        return replace(self, stale=stale)


@dataclass(frozen=True, slots=True)
class GeneralDraftComment:
    """A general review comment."""

    id: UUID
    body: str
    kind: Literal["general"] = field(default="general", init=False)


@dataclass(frozen=True, slots=True)
class InlineDraftComment:
    """A comment attached to an original inline anchor."""

    id: UUID
    body: str
    anchor: InlineAnchor
    kind: Literal["inline"] = field(default="inline", init=False)


@dataclass(frozen=True, slots=True)
class ReplyDraftComment:
    """A reply attached to a stable remote discussion identity."""

    id: UUID
    body: str
    thread_id: str
    kind: Literal["reply"] = field(default="reply", init=False)

    def __post_init__(self) -> None:
        if not self.thread_id:
            raise ValueError("thread_id is required")


type DraftComment = GeneralDraftComment | InlineDraftComment | ReplyDraftComment


def new_general_comment(body: str) -> GeneralDraftComment:
    """Create a general comment with a stable random identity."""
    return GeneralDraftComment(uuid4(), body)


def new_inline_comment(body: str, anchor: InlineAnchor) -> InlineDraftComment:
    """Create an inline comment with a stable random identity."""
    return InlineDraftComment(uuid4(), body, anchor)


def new_reply_comment(body: str, thread_id: str) -> ReplyDraftComment:
    """Create a reply with a stable random identity."""
    return ReplyDraftComment(uuid4(), body, thread_id)


@dataclass(frozen=True, slots=True)
class DraftContent:
    """Caller-owned editable content retained on optimistic conflicts."""

    body: str = ""
    verdict: DraftVerdict | None = None
    comments: tuple[DraftComment, ...] = ()

    def __post_init__(self) -> None:
        if self.verdict is not None and not isinstance(self.verdict, DraftVerdict):
            raise TypeError("verdict must be a DraftVerdict")
        ids = [comment.id for comment in self.comments]
        if len(ids) != len(set(ids)):
            raise ValueError("draft comment IDs must be unique")

    def assessed_against(self, revision: ReviewRevision) -> DraftContent:
        """Return content whose inline anchors reflect the current review head."""
        comments: list[DraftComment] = []
        for comment in self.comments:
            if isinstance(comment, InlineDraftComment):
                comment = replace(
                    comment, anchor=comment.anchor.assessed_against(revision)
                )
            comments.append(comment)
        return replace(self, comments=tuple(comments))


@dataclass(frozen=True, slots=True)
class DraftSnapshot:
    """One immutable durable version of a review draft."""

    id: UUID
    review: ReviewRef
    revision: ReviewRevision
    version: int
    body: str
    verdict: DraftVerdict | None
    comments: tuple[DraftComment, ...]
    state: DraftState
    created_at: datetime
    updated_at: datetime

    @property
    def content(self) -> DraftContent:
        return DraftContent(self.body, self.verdict, self.comments)

    def assessed_against(self, revision: ReviewRevision) -> DraftSnapshot:
        """Return a read-only staleness assessment for a current review revision."""
        content = self.content.assessed_against(revision)
        return replace(
            self, body=content.body, verdict=content.verdict, comments=content.comments
        )


@dataclass(frozen=True, slots=True)
class StepReceipt:
    """A confirmed remote result for one stable submission step."""

    step_id: str
    remote_id: str
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class ReconciliationRecord:
    """A durable user decision about an outcome-unknown attempt."""

    resolution: ReconciliationResolution
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class SubmissionAttempt:
    """A frozen draft submission and all confirmed durable outcomes."""

    id: UUID
    draft_id: UUID
    frozen_version: int
    snapshot: DraftSnapshot
    state: DraftState
    receipts: tuple[StepReceipt, ...]
    reconciliations: tuple[ReconciliationRecord, ...]
    started_at: datetime
    updated_at: datetime
