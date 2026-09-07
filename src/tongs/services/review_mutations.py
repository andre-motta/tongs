"""Revision-bound, outcome-aware review mutation primitives."""

from __future__ import annotations

import asyncio
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

from tongs.diff.conversion import convert_forge_changes
from tongs.diff.models import DiffHunk, DiffLine, LineType
from tongs.errors import (
    AuthError,
    ConflictError,
    ForgePermissionError,
    NotFoundError,
    RateLimitError,
)
from tongs.forges.base import ForgeClient
from tongs.forges.models import Discussion, ForgeMutationResult, ReviewDecision
from tongs.scanner.repo import ForgeType
from tongs.services.errors import ServiceError, ServiceErrorCode, translate_error
from tongs.services.models import (
    RawDiffSnapshot,
    ReviewRef,
    ReviewRevision,
    ReviewSnapshot,
    ServiceEventKind,
)

_OPERATION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MAX_BODY_BYTES = 65_536


class DiffSide(str, Enum):
    LEFT = "LEFT"
    RIGHT = "RIGHT"


@dataclass(frozen=True, slots=True)
class DiffAnchor:
    old_path: str
    new_path: str
    line: int
    side: DiffSide
    start_line: int | None = None
    start_side: DiffSide | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.old_path, str) or not isinstance(self.new_path, str):
            raise TypeError("diff paths must be strings")
        if (
            not self.old_path
            or not self.new_path
            or "\n" in self.old_path + self.new_path
        ):
            raise ValueError("old_path and new_path are required")
        if not isinstance(self.side, DiffSide):
            raise TypeError("side must be a DiffSide")
        if (
            not isinstance(self.line, int)
            or isinstance(self.line, bool)
            or self.line <= 0
            or (
                self.start_line is not None
                and (
                    not isinstance(self.start_line, int)
                    or isinstance(self.start_line, bool)
                    or self.start_line <= 0
                )
            )
        ):
            raise ValueError("diff line numbers must be positive")
        if (self.start_line is None) != (self.start_side is None):
            raise ValueError(
                "range start_line and start_side must be supplied together"
            )
        if self.start_side is not None and not isinstance(self.start_side, DiffSide):
            raise TypeError("start_side must be a DiffSide")


@dataclass(frozen=True, slots=True)
class InlineDraft:
    anchor: DiffAnchor
    body: str

    def __post_init__(self) -> None:
        if not isinstance(self.anchor, DiffAnchor):
            raise TypeError("anchor must be a DiffAnchor")
        _validate_body(self.body)


@dataclass(frozen=True, slots=True)
class GeneralComment:
    operation_id: str
    review: ReviewRef
    body: str

    def __post_init__(self) -> None:
        _validate_command(self.operation_id, self.review, self.body)


@dataclass(frozen=True, slots=True)
class InlineComment:
    operation_id: str
    review: ReviewRef
    revision: ReviewRevision
    anchor: DiffAnchor
    body: str

    def __post_init__(self) -> None:
        _validate_command(self.operation_id, self.review, self.body)
        if not isinstance(self.revision, ReviewRevision) or not isinstance(
            self.anchor, DiffAnchor
        ):
            raise TypeError("revision and anchor must use their declared types")


@dataclass(frozen=True, slots=True)
class Reply:
    operation_id: str
    review: ReviewRef
    revision: ReviewRevision
    discussion_id: str
    body: str

    def __post_init__(self) -> None:
        _validate_command(self.operation_id, self.review, self.body)
        if not isinstance(self.revision, ReviewRevision):
            raise TypeError("revision must be a ReviewRevision")
        if not isinstance(self.discussion_id, str) or not self.discussion_id:
            raise ValueError("discussion_id is required")


@dataclass(frozen=True, slots=True)
class Resolve:
    operation_id: str
    review: ReviewRef
    revision: ReviewRevision
    discussion_id: str
    resolved: bool = True

    def __post_init__(self) -> None:
        _validate_command(self.operation_id, self.review)
        if not isinstance(self.revision, ReviewRevision):
            raise TypeError("revision must be a ReviewRevision")
        if not isinstance(self.discussion_id, str) or not self.discussion_id:
            raise ValueError("discussion_id is required")
        if not isinstance(self.resolved, bool):
            raise TypeError("resolved must be a boolean")


@dataclass(frozen=True, slots=True)
class ReviewVerdict:
    operation_id: str
    review: ReviewRef
    revision: ReviewRevision
    verdict: ReviewDecision
    body: str = ""
    inline_comments: tuple[InlineDraft, ...] = ()

    def __post_init__(self) -> None:
        _validate_command(self.operation_id, self.review, self.body, allow_empty=True)
        if not isinstance(self.revision, ReviewRevision):
            raise TypeError("revision must be a ReviewRevision")
        if not isinstance(self.verdict, ReviewDecision) or self.verdict not in {
            ReviewDecision.APPROVED,
            ReviewDecision.CHANGES_REQUESTED,
            ReviewDecision.COMMENTED,
        }:
            raise ValueError("unsupported review verdict")
        if (
            self.verdict == ReviewDecision.COMMENTED
            and not self.body
            and not self.inline_comments
        ):
            raise ValueError("comment review requires a body or inline comment")
        if self.verdict == ReviewDecision.CHANGES_REQUESTED and not self.body:
            raise ValueError("request-changes review requires a body")
        if not isinstance(self.inline_comments, tuple) or not all(
            isinstance(comment, InlineDraft) for comment in self.inline_comments
        ):
            raise TypeError("inline_comments must be a tuple of InlineDraft values")


ReviewMutationCommand = GeneralComment | InlineComment | Reply | Resolve | ReviewVerdict


@dataclass(frozen=True, slots=True)
class ReviewMutationCapabilities:
    general_comment: bool
    inline_comment: bool
    multiline_comment: bool
    reply: bool
    resolve: bool
    approve: bool
    request_changes: bool
    comment_verdict: bool
    atomic_review_batch: bool


class MutationStatus(str, Enum):
    KNOWN = "known"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class MutationReceipt:
    operation_id: str
    remote_id: str
    comment_id: str | None = None
    discussion_id: str | None = None


@dataclass(frozen=True, slots=True)
class MutationOutcome:
    operation_id: str
    status: MutationStatus
    receipt: MutationReceipt | None
    reason: str | None = None
    resync_required: bool = False


@dataclass(slots=True)
class _LedgerRecord:
    command: ReviewMutationCommand
    outcome: MutationOutcome | None = None
    error: ServiceError | None = None


@dataclass(frozen=True, slots=True)
class _ResolvedAnchor:
    anchor: DiffAnchor
    line: DiffLine
    start: DiffLine | None


class ReviewMutationService:
    """Validate and dispatch single forge mutations without automatic replay."""

    def __init__(
        self,
        *,
        get_client: Callable[[ReviewRef, str], Awaitable[ForgeClient]],
        get_review: Callable[[ReviewRef], Awaitable[ReviewSnapshot]],
        get_diff: Callable[[ReviewRef], Awaitable[RawDiffSnapshot]],
        get_discussions: Callable[[ReviewRef], Awaitable[tuple[Discussion, ...]]],
        emit_change: Callable[
            [ServiceEventKind, ReviewRef, ReviewRevision | None], None
        ],
        timeout: float = 30.0,
        ledger_size: int = 1024,
    ) -> None:
        if timeout <= 0 or ledger_size <= 0:
            raise ValueError("timeout and ledger_size must be positive")
        self._get_client = get_client
        self._get_review = get_review
        self._get_diff = get_diff
        self._get_discussions = get_discussions
        self._emit_change = emit_change
        self._timeout = timeout
        self._ledger_size = ledger_size
        self._ledger: OrderedDict[str, _LedgerRecord] = OrderedDict()
        self._lock = asyncio.Lock()

    async def capabilities(self, review: ReviewRef) -> ReviewMutationCapabilities:
        snapshot = await self._get_review(review)
        return _capabilities(snapshot)

    async def execute(self, command: ReviewMutationCommand) -> MutationOutcome:
        prior = await self._reserve(command)
        if prior is not None:
            return prior
        try:
            client = await self._get_client(command.review, "mutate_review")
            call = await self._prepare(command, client)
        except asyncio.CancelledError:
            await self._remove_pending(command.operation_id)
            raise
        except ServiceError as error:
            await self._finish_error(command.operation_id, error)
            raise
        except Exception as error:  # noqa: BLE001 - Sanitize preparation boundary.
            safe = translate_error(error, operation="prepare_review_mutation")
            await self._finish_error(command.operation_id, safe)
            raise safe from None

        try:
            result = await asyncio.wait_for(call, timeout=self._timeout)
        except (
            AuthError,
            ForgePermissionError,
            NotFoundError,
            ConflictError,
            RateLimitError,
        ) as error:
            safe = translate_error(
                error,
                operation="mutate_review",
                hostname=command.review.repository.hostname,
            )
            await self._finish_error(command.operation_id, safe)
            raise safe from None
        except (asyncio.CancelledError, Exception) as error:  # noqa: BLE001
            outcome = await self._unknown(command, client, type(error).__name__)
            return outcome

        if not isinstance(result, ForgeMutationResult) or not result.remote_id:
            return await self._unknown(command, client, "invalid_receipt")
        receipt = MutationReceipt(
            command.operation_id,
            result.remote_id,
            result.comment_id,
            result.discussion_id,
        )
        resync = not result.cache_invalidated
        resync = not self._emit_known(command, resync) or resync
        outcome = MutationOutcome(
            command.operation_id,
            MutationStatus.KNOWN,
            receipt,
            resync_required=resync,
        )
        await self._finish_outcome(command.operation_id, outcome)
        return outcome

    async def _prepare(
        self, command: ReviewMutationCommand, client: ForgeClient
    ) -> Awaitable[ForgeMutationResult]:
        path = command.review.repository.project_path
        number = command.review.number
        if isinstance(command, GeneralComment):
            return client.add_comment(path, number, command.body)

        snapshot = await self._require_revision(command.review, command.revision)
        capabilities = _capabilities(snapshot)
        if isinstance(command, InlineComment):
            resolved = await self._validate_anchor(
                command.review, command.revision, command.anchor
            )
            await self._require_revision(command.review, command.revision)
            return client.create_inline_comment(
                path,
                number,
                command.anchor.new_path,
                command.anchor.line,
                command.anchor.side.value,
                command.body,
                command.anchor.start_line,
                command.anchor.start_side.value if command.anchor.start_side else None,
                old_path=command.anchor.old_path,
                new_path=command.anchor.new_path,
                head_sha=command.revision.head_sha,
                base_sha=command.revision.base_sha,
                start_sha=command.revision.start_sha,
                old_line=resolved.line.old_lineno,
                new_line=resolved.line.new_lineno,
                start_old_line=resolved.start.old_lineno if resolved.start else None,
                start_new_line=resolved.start.new_lineno if resolved.start else None,
            )
        discussions = (
            await self._get_discussions(command.review)
            if isinstance(command, (Reply, Resolve))
            else ()
        )
        if isinstance(command, Reply):
            discussion = _find_discussion(discussions, command.discussion_id)
            await self._require_revision(command.review, command.revision)
            return client.reply_to_discussion(
                path,
                number,
                discussion.id,
                command.body,
                root_comment_id=discussion.root_comment.id,
            )
        if isinstance(command, Resolve):
            if not capabilities.resolve:
                raise _unsupported("This forge does not support thread resolution.")
            discussion = _find_discussion(discussions, command.discussion_id)
            if not discussion.resolvable:
                raise _unsupported("This discussion cannot be resolved.")
            await self._require_revision(command.review, command.revision)
            return client.resolve_discussion(
                path, number, discussion.id, command.resolved
            )

        if snapshot.detail.forge_host.forge_type == ForgeType.GITLAB:
            if command.verdict == ReviewDecision.CHANGES_REQUESTED:
                raise _unsupported(
                    "GitLab does not provide a request-changes review verdict."
                )
            if command.inline_comments:
                raise _unsupported("GitLab does not provide atomic review batches.")
            if command.verdict == ReviewDecision.APPROVED and command.body:
                raise _unsupported(
                    "GitLab approval and comment require separate operations."
                )
        inline_payloads: list[dict] = []
        for draft in command.inline_comments:
            await self._validate_anchor(command.review, command.revision, draft.anchor)
            inline_payloads.append(_github_review_comment(draft))
        await self._require_revision(command.review, command.revision)
        return client.submit_review(
            path,
            number,
            command.verdict,
            command.body,
            inline_payloads or None,
            head_sha=command.revision.head_sha,
        )

    async def _require_revision(
        self, review: ReviewRef, revision: ReviewRevision
    ) -> ReviewSnapshot:
        snapshot = await self._get_review(review)
        if snapshot.revision != revision:
            raise ServiceError(
                ServiceErrorCode.REVISION_CHANGED,
                "The review revision changed. Refresh before writing.",
                retryable=True,
            )
        return snapshot

    async def _validate_anchor(
        self, review: ReviewRef, revision: ReviewRevision, anchor: DiffAnchor
    ) -> _ResolvedAnchor:
        raw = await self._get_diff(review)
        if raw.revision != revision:
            raise ServiceError(
                ServiceErrorCode.REVISION_CHANGED,
                "The review revision changed. Refresh before writing.",
                retryable=True,
            )
        for file in convert_forge_changes(raw.changes):
            if file.old_path != anchor.old_path or file.new_path != anchor.new_path:
                continue
            if file.is_truncated or file.is_metadata_only or not file.hunks:
                break
            endpoint = _find_line(file.hunks, anchor.line, anchor.side)
            start = None
            if endpoint is not None and anchor.start_line is not None:
                start = _find_line(file.hunks, anchor.start_line, anchor.start_side)
                if start is None or not _same_hunk(file.hunks, start, endpoint):
                    endpoint = None
            if endpoint is not None:
                return _ResolvedAnchor(anchor, endpoint, start)
            break
        raise ServiceError(
            ServiceErrorCode.INVALID_INPUT,
            "The selected diff line is not commentable at this revision.",
        )

    async def _reserve(self, command: ReviewMutationCommand) -> MutationOutcome | None:
        async with self._lock:
            existing = self._ledger.get(command.operation_id)
            if existing is not None:
                if existing.command != command:
                    raise ServiceError(
                        ServiceErrorCode.CONFLICT,
                        "The operation ID is already bound to a different mutation.",
                    )
                if existing.error is not None:
                    raise existing.error
                if existing.outcome is not None:
                    return existing.outcome
                raise ServiceError(
                    ServiceErrorCode.CONFLICT,
                    "The operation is already in progress.",
                    retryable=True,
                )
            if len(self._ledger) >= self._ledger_size:
                raise ServiceError(
                    ServiceErrorCode.CONFLICT,
                    "The mutation ledger is full; start a new service session.",
                )
            self._ledger[command.operation_id] = _LedgerRecord(command)
        return None

    async def _remove_pending(self, operation_id: str) -> None:
        async with self._lock:
            record = self._ledger.get(operation_id)
            if record is not None and record.outcome is None and record.error is None:
                del self._ledger[operation_id]

    async def _finish_outcome(
        self, operation_id: str, outcome: MutationOutcome
    ) -> None:
        async with self._lock:
            self._ledger[operation_id].outcome = outcome

    async def _finish_error(self, operation_id: str, error: ServiceError) -> None:
        async with self._lock:
            self._ledger[operation_id].error = error

    async def _unknown(
        self, command: ReviewMutationCommand, client: ForgeClient, reason: str
    ) -> MutationOutcome:
        try:
            await client.invalidate_review_reads(
                command.review.repository.project_path, command.review.number
            )
        except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110
            pass
        try:
            self._emit_change(
                ServiceEventKind.RESYNC_REQUIRED, command.review, _revision(command)
            )
        except (asyncio.CancelledError, Exception):  # noqa: BLE001, S110
            pass
        outcome = MutationOutcome(
            command.operation_id,
            MutationStatus.UNKNOWN,
            None,
            reason=reason,
            resync_required=True,
        )
        await self._finish_outcome(command.operation_id, outcome)
        return outcome

    def _emit_known(self, command: ReviewMutationCommand, resync: bool) -> bool:
        try:
            self._emit_change(
                ServiceEventKind.REVIEW_CHANGED, command.review, _revision(command)
            )
            if resync:
                self._emit_change(
                    ServiceEventKind.RESYNC_REQUIRED, command.review, _revision(command)
                )
            return True
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            return False


def _validate_command(
    operation_id: str,
    review: ReviewRef,
    body: str | None = None,
    *,
    allow_empty: bool = False,
) -> None:
    if not isinstance(operation_id, str) or not _OPERATION_ID.fullmatch(operation_id):
        raise ValueError("operation_id must be a stable printable identifier")
    if not isinstance(review, ReviewRef):
        raise TypeError("review must be a ReviewRef")
    if body is not None:
        _validate_body(body, allow_empty=allow_empty)


def _validate_body(body: str, *, allow_empty: bool = False) -> None:
    if not isinstance(body, str) or (not allow_empty and not body):
        raise ValueError("comment body is empty or exceeds the service limit")
    try:
        size = len(body.encode())
    except UnicodeEncodeError:
        raise ValueError("comment body must contain valid Unicode") from None
    if size > _MAX_BODY_BYTES:
        raise ValueError("comment body is empty or exceeds the service limit")


def _capabilities(snapshot: ReviewSnapshot) -> ReviewMutationCapabilities:
    forge = snapshot.detail.forge_host.forge_type
    github = forge == ForgeType.GITHUB
    return ReviewMutationCapabilities(
        True,
        True,
        True,
        True,
        snapshot.capabilities.thread_resolution,
        True,
        github,
        True,
        snapshot.capabilities.batched_review,
    )


def _unsupported(message: str) -> ServiceError:
    return ServiceError(ServiceErrorCode.UNSUPPORTED, message)


def _find_discussion(items: tuple[Discussion, ...], discussion_id: str) -> Discussion:
    for item in items:
        if item.id == discussion_id:
            return item
    raise ServiceError(
        ServiceErrorCode.INVALID_INPUT, "The discussion does not belong to this review."
    )


def _find_line(
    hunks: tuple[DiffHunk, ...], number: int, side: DiffSide | None
) -> DiffLine | None:
    if side is None:
        return None
    for hunk in hunks:
        for line in hunk.lines:
            if (
                side == DiffSide.LEFT
                and line.old_lineno == number
                and line.line_type in {LineType.CONTEXT, LineType.DELETION}
            ):
                return line
            if (
                side == DiffSide.RIGHT
                and line.new_lineno == number
                and line.line_type in {LineType.CONTEXT, LineType.ADDITION}
            ):
                return line
    return None


def _same_hunk(hunks: tuple[DiffHunk, ...], first: DiffLine, last: DiffLine) -> bool:
    for hunk in hunks:
        try:
            return hunk.lines.index(first) <= hunk.lines.index(last)
        except ValueError:
            continue
    return False


def _github_review_comment(draft: InlineDraft) -> dict:
    anchor = draft.anchor
    payload: dict = {
        "path": anchor.new_path,
        "line": anchor.line,
        "side": anchor.side.value,
        "body": draft.body,
    }
    if anchor.start_line is not None:
        payload["start_line"] = anchor.start_line
        payload["start_side"] = anchor.start_side.value
    return payload


def _revision(command: ReviewMutationCommand) -> ReviewRevision | None:
    return None if isinstance(command, GeneralComment) else command.revision


__all__ = [
    "DiffAnchor",
    "DiffSide",
    "GeneralComment",
    "InlineComment",
    "InlineDraft",
    "MutationOutcome",
    "MutationReceipt",
    "MutationStatus",
    "Reply",
    "Resolve",
    "ReviewMutationCapabilities",
    "ReviewMutationCommand",
    "ReviewMutationService",
    "ReviewVerdict",
]
