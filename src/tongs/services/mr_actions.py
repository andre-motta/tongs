"""Revision-bound merge request lifecycle actions with retained outcomes."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from enum import Enum

from tongs.errors import (
    AuthError,
    ConflictError,
    ForgePermissionError,
    NotFoundError,
    RateLimitError,
)
from tongs.forges.base import ForgeClient
from tongs.forges.models import (
    ForgeMergeResult,
    ForgeMutationResult,
    MRState,
    SourceCleanupStatus,
)
from tongs.services.errors import ServiceError, ServiceErrorCode, translate_error
from tongs.services.models import (
    RepositoryRef,
    ReviewRef,
    ReviewRevision,
    ReviewSnapshot,
    ServiceEventKind,
)

_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_KNOWN_DISPATCH_ERRORS = (
    AuthError,
    ForgePermissionError,
    NotFoundError,
    ConflictError,
    RateLimitError,
    NotImplementedError,
)
_KNOWN_SERVICE_CODES = frozenset(
    {
        ServiceErrorCode.AUTHENTICATION_FAILED,
        ServiceErrorCode.PERMISSION_DENIED,
        ServiceErrorCode.NOT_FOUND,
        ServiceErrorCode.CONFLICT,
        ServiceErrorCode.RATE_LIMITED,
    }
)


class MRAction(str, Enum):
    """Supported merge request lifecycle actions."""

    MERGE = "merge"
    CLOSE = "close"
    REOPEN = "reopen"
    UNAPPROVE = "unapprove"


class MRActionOutcome(str, Enum):
    """Whether the remote action result is known."""

    KNOWN = "known"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class MRActionCapabilities:
    """State-aware native actions available for one admitted review."""

    merge: bool
    close: bool
    reopen: bool
    unapprove: bool

    def supports(self, action: MRAction) -> bool:
        """Return whether this capability snapshot supports ``action``."""
        return {
            MRAction.MERGE: self.merge,
            MRAction.CLOSE: self.close,
            MRAction.REOPEN: self.reopen,
            MRAction.UNAPPROVE: self.unapprove,
        }[action]


@dataclass(frozen=True, slots=True)
class ReviewActionTarget:
    """A review action bound to one full revision and expected state."""

    review: ReviewRef
    revision: ReviewRevision
    expected_state: MRState

    def __post_init__(self) -> None:
        if not isinstance(self.review, ReviewRef):
            raise TypeError("review must be a ReviewRef")
        if not isinstance(self.revision, ReviewRevision):
            raise TypeError("revision must be a ReviewRevision")
        if not isinstance(self.expected_state, MRState):
            raise TypeError("expected_state must be an MRState")


@dataclass(frozen=True, slots=True)
class SourceBranchTarget:
    """An exact same-repository source branch approved for optional cleanup."""

    repository: RepositoryRef
    branch: str

    def __post_init__(self) -> None:
        if not isinstance(self.repository, RepositoryRef):
            raise TypeError("repository must be a RepositoryRef")
        if not isinstance(self.branch, str) or not self.branch:
            raise ValueError("branch must be a non-empty string")


@dataclass(frozen=True, slots=True)
class MergeReviewCommand:
    """Merge an open review at one exact head with optional source cleanup."""

    operation_id: str
    target: ReviewActionTarget
    squash: bool = False
    source_cleanup: SourceBranchTarget | None = None


@dataclass(frozen=True, slots=True)
class CloseReviewCommand:
    """Close an open review."""

    operation_id: str
    target: ReviewActionTarget


@dataclass(frozen=True, slots=True)
class ReopenReviewCommand:
    """Reopen a closed review."""

    operation_id: str
    target: ReviewActionTarget


@dataclass(frozen=True, slots=True)
class UnapproveReviewCommand:
    """Remove the current user's approval where the forge supports it."""

    operation_id: str
    target: ReviewActionTarget


type MRActionCommand = (
    MergeReviewCommand
    | CloseReviewCommand
    | ReopenReviewCommand
    | UnapproveReviewCommand
)


@dataclass(frozen=True, slots=True)
class MRActionReceipt:
    """Retained known or unknown result for one admitted review action."""

    operation_id: str
    action: MRAction
    target: ReviewActionTarget
    outcome: MRActionOutcome
    remote_id: str | None = None
    merge_sha: str | None = None
    source_cleanup: SourceCleanupStatus = SourceCleanupStatus.NOT_REQUESTED
    error: ServiceError | None = None
    resync_required: bool = True

    def __post_init__(self) -> None:
        if self.outcome is MRActionOutcome.KNOWN:
            if self.error is not None or not self.remote_id:
                raise ValueError(
                    "known outcomes require a remote identity and no error"
                )
            if self.action is MRAction.MERGE and not self.merge_sha:
                raise ValueError("known merge outcomes require a merge SHA")
            if self.action is not MRAction.MERGE and (
                self.merge_sha is not None
                or self.source_cleanup is not SourceCleanupStatus.NOT_REQUESTED
            ):
                raise ValueError("only merge outcomes contain merge details")
        elif self.error is None:
            raise ValueError("unknown outcomes require a safe error")
        elif (
            self.remote_id is not None
            or self.merge_sha is not None
            or self.source_cleanup is not SourceCleanupStatus.NOT_REQUESTED
            or not self.resync_required
        ):
            raise ValueError("unknown outcomes cannot claim remote results")


type GetClient = Callable[[ReviewRef, str], Awaitable[ForgeClient]]
type GetReview = Callable[[ReviewRef], Awaitable[ReviewSnapshot]]
type EmitChange = Callable[[ServiceEventKind, ReviewRef, ReviewRevision | None], None]


@dataclass(slots=True)
class _OperationRecord:
    command: MRActionCommand
    done: asyncio.Event
    receipt: MRActionReceipt | None = None
    error: ServiceError | None = None


class MRActionService:
    """Validate, dispatch once, and retain merge request lifecycle outcomes."""

    def __init__(
        self,
        *,
        get_client: GetClient,
        get_review: GetReview,
        emit_change: EmitChange,
        max_operations: int = 256,
        close_timeout: float = 1.0,
    ) -> None:
        if not isinstance(max_operations, int) or isinstance(max_operations, bool):
            raise TypeError("max_operations must be an integer")
        if max_operations <= 0:
            raise ValueError("max_operations must be positive")
        if close_timeout <= 0:
            raise ValueError("close_timeout must be positive")
        self._get_client = get_client
        self._get_review = get_review
        self._emit_change = emit_change
        self._max_operations = max_operations
        self._close_timeout = close_timeout
        self._operations: dict[str, _OperationRecord] = {}
        self._operation_lock = asyncio.Lock()
        self._owner_tasks: set[asyncio.Task[object]] = set()
        self._closed = False

    async def capabilities(self, review: ReviewRef) -> MRActionCapabilities:
        """Return native action capabilities for the review's current state."""
        self._require_open()
        snapshot = await self._fresh_snapshot(review)
        client = await self._resolve_client(review, "mr_action_capabilities")
        state = snapshot.detail.state
        try:
            return MRActionCapabilities(
                merge=state is MRState.OPEN,
                close=state is MRState.OPEN,
                reopen=state is MRState.CLOSED,
                unapprove=state is MRState.OPEN and bool(client.supports_unapprove),
            )
        except Exception as error:  # noqa: BLE001 - Sanitize adapter capability.
            raise translate_error(
                error,
                operation="mr_action_capabilities",
                hostname=review.repository.hostname,
            ) from None

    async def execute(self, command: MRActionCommand) -> MRActionReceipt:
        """Execute once per operation ID and retain every terminal disposition."""
        action, target = self._command_parts(command)
        self._validate_operation_id(command.operation_id)
        record, owner = await self._reserve(command)
        if not owner:
            await record.done.wait()
            return self._record_result(record)

        owner_task = asyncio.current_task()
        if owner_task is None:
            raise RuntimeError("MR action execution requires an asyncio task")
        try:
            return await self._execute_owner(command, record, action, target)
        finally:
            self._owner_tasks.discard(owner_task)

    async def receipt(self, operation_id: str) -> MRActionReceipt | None:
        """Return a completed receipt, or ``None`` for an absent/pending action."""
        self._require_open()
        self._validate_operation_id(operation_id)
        async with self._operation_lock:
            record = self._operations.get(operation_id)
            return None if record is None else record.receipt

    async def close(self) -> None:
        """Reject admission, then boundedly cancel and drain active owners."""
        async with self._operation_lock:
            self._closed = True
            owners = set(self._owner_tasks)
        pending = owners
        for _attempt in range(2):
            if not pending:
                break
            for task in pending:
                task.cancel()
            _done, pending = await asyncio.wait(pending, timeout=self._close_timeout)
        if pending:
            raise RuntimeError("MR action tasks did not stop")

    async def _execute_owner(
        self,
        command: MRActionCommand,
        record: _OperationRecord,
        action: MRAction,
        target: ReviewActionTarget,
    ) -> MRActionReceipt:
        dispatch_started = False
        try:
            snapshot = await self._validate_fresh_target(command, action, target)
            client = await self._resolve_client(target.review, f"mr_{action.value}")
            if action is MRAction.UNAPPROVE and not bool(client.supports_unapprove):
                raise ServiceError(
                    ServiceErrorCode.UNSUPPORTED,
                    "This forge does not support removing an approval.",
                )
            dispatch_started = True
            result = await self._dispatch(client, command, snapshot)
            receipt = self._known_receipt(command.operation_id, action, target, result)
        except asyncio.CancelledError:
            if dispatch_started:
                receipt = self._unknown_receipt(command.operation_id, action, target)
                self._retain_receipt_now(record, receipt)
                self._finish_receipt(record, receipt, emit_resync=True)
            else:
                self._complete_error_now(
                    record,
                    ServiceError(
                        ServiceErrorCode.INTERNAL,
                        "The review action was cancelled before dispatch.",
                        retryable=True,
                    ),
                )
            raise
        except BaseException as error:
            if not isinstance(error, Exception):
                if dispatch_started:
                    receipt = self._unknown_receipt(
                        command.operation_id, action, target
                    )
                    self._retain_receipt_now(record, receipt)
                    self._finish_receipt(record, receipt, emit_resync=True)
                else:
                    self._complete_error_now(
                        record,
                        ServiceError(
                            ServiceErrorCode.INTERNAL,
                            "The review action stopped before dispatch.",
                        ),
                    )
                raise
            if not dispatch_started or self._is_known_rejection(error):
                safe = self._safe_error(error, action, target.review.repository)
                self._complete_error_now(record, safe)
                raise safe from None
            receipt = self._unknown_receipt(
                command.operation_id,
                action,
                target,
                self._safe_error(error, action, target.review.repository),
            )
            self._retain_receipt_now(record, receipt)
            return self._finish_receipt(record, receipt, emit_resync=True)

        self._retain_receipt_now(record, receipt)
        emit_resync = not result.cache_invalidated or (
            isinstance(result, ForgeMergeResult)
            and result.source_cleanup is SourceCleanupStatus.UNKNOWN
        )
        finished = self._finish_receipt(record, receipt, emit_resync=emit_resync)
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise asyncio.CancelledError
        return finished

    async def _reserve(self, command: MRActionCommand) -> tuple[_OperationRecord, bool]:
        async with self._operation_lock:
            if self._closed:
                raise ServiceError(
                    ServiceErrorCode.CLOSED,
                    "The MR action service is closed.",
                )
            existing = self._operations.get(command.operation_id)
            if existing is not None:
                if existing.command != command:
                    raise ServiceError(
                        ServiceErrorCode.CONFLICT,
                        "The operation ID is already bound to another review action.",
                    )
                return existing, False
            if len(self._operations) >= self._max_operations:
                raise ServiceError(
                    ServiceErrorCode.CONFLICT,
                    "The MR action operation ledger is full.",
                    retryable=True,
                )
            record = _OperationRecord(command, asyncio.Event())
            self._operations[command.operation_id] = record
            owner = asyncio.current_task()
            if owner is None:
                raise RuntimeError("MR action execution requires an asyncio task")
            self._owner_tasks.add(owner)
            return record, True

    async def _validate_fresh_target(
        self,
        command: MRActionCommand,
        action: MRAction,
        target: ReviewActionTarget,
    ) -> ReviewSnapshot:
        expected_for_action = (
            MRState.CLOSED if action is MRAction.REOPEN else MRState.OPEN
        )
        if target.expected_state is not expected_for_action:
            raise ServiceError(
                ServiceErrorCode.INVALID_INPUT,
                "The review action has an invalid expected state.",
            )
        snapshot = await self._fresh_snapshot(target.review)
        if snapshot.revision is None:
            raise snapshot.revision_error or ServiceError(
                ServiceErrorCode.REVISION_UNAVAILABLE,
                "The review revision is unavailable.",
            )
        if snapshot.revision != target.revision:
            raise ServiceError(
                ServiceErrorCode.REVISION_CHANGED,
                "The review revision changed. Refresh before writing.",
                retryable=True,
            )
        if snapshot.detail.state is not target.expected_state:
            raise ServiceError(
                ServiceErrorCode.CONFLICT,
                "The review state changed. Refresh before writing.",
                retryable=True,
            )
        if isinstance(command, MergeReviewCommand):
            if not isinstance(command.squash, bool):
                raise ServiceError(
                    ServiceErrorCode.INVALID_INPUT,
                    "The merge squash option is invalid.",
                )
            source = command.source_cleanup
            if source is not None:
                if not isinstance(source, SourceBranchTarget):
                    raise ServiceError(
                        ServiceErrorCode.INVALID_INPUT,
                        "The source cleanup target is invalid.",
                    )
                if (
                    source.repository != target.review.repository
                    or source.branch != snapshot.detail.source_branch
                    or source.branch == snapshot.detail.target_branch
                ):
                    raise ServiceError(
                        ServiceErrorCode.CONFLICT,
                        "The source cleanup target changed or is not safely removable.",
                    )
        return snapshot

    async def _fresh_snapshot(self, review: ReviewRef) -> ReviewSnapshot:
        try:
            snapshot = await self._get_review(review)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - Sanitize session callback.
            raise translate_error(
                error,
                operation="read_review_for_action",
                hostname=review.repository.hostname,
            ) from None
        if not isinstance(snapshot, ReviewSnapshot) or snapshot.ref != review:
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                "The forge returned invalid review detail for this action.",
            )
        detail = snapshot.detail
        if (
            detail.repo_path != review.repository.project_path
            or type(detail.number) is not int
            or detail.number != review.number
            or detail.forge_host.hostname != review.repository.hostname
            or not isinstance(detail.source_branch, str)
            or not detail.source_branch
            or not isinstance(detail.target_branch, str)
            or not detail.target_branch
            or (
                snapshot.revision is not None
                and (
                    detail.head_sha != snapshot.revision.head_sha
                    or detail.base_sha != snapshot.revision.base_sha
                    or detail.start_sha != snapshot.revision.start_sha
                )
            )
        ):
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                "The forge returned invalid review detail for this action.",
            )
        return snapshot

    async def _resolve_client(self, review: ReviewRef, operation: str) -> ForgeClient:
        try:
            return await self._get_client(review, operation)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - Sanitize session callback.
            raise translate_error(
                error,
                operation=operation,
                hostname=review.repository.hostname,
            ) from None

    @staticmethod
    async def _dispatch(
        client: ForgeClient,
        command: MRActionCommand,
        snapshot: ReviewSnapshot,
    ) -> ForgeMutationResult | ForgeMergeResult:
        review = command.target.review
        project = review.repository.project_path
        if isinstance(command, MergeReviewCommand):
            cleanup = command.source_cleanup
            return await client.merge_mr(
                project,
                review.number,
                command.squash,
                cleanup is not None,
                head_sha=command.target.revision.head_sha,
                expected_source_repository=(
                    cleanup.repository.project_path if cleanup is not None else None
                ),
                expected_source_branch=cleanup.branch if cleanup is not None else None,
                expected_target_branch=snapshot.detail.target_branch,
            )
        if isinstance(command, CloseReviewCommand):
            return await client.close_mr(project, review.number)
        if isinstance(command, ReopenReviewCommand):
            return await client.reopen_mr(project, review.number)
        return await client.unapprove_mr(project, review.number)

    def _finish_receipt(
        self,
        record: _OperationRecord,
        receipt: MRActionReceipt,
        *,
        emit_resync: bool,
    ) -> MRActionReceipt:
        resync = emit_resync
        try:
            if receipt.outcome is MRActionOutcome.KNOWN:
                self._emit_change(
                    ServiceEventKind.REVIEW_CHANGED,
                    receipt.target.review,
                    receipt.target.revision,
                )
            if resync:
                self._emit_change(
                    ServiceEventKind.RESYNC_REQUIRED,
                    receipt.target.review,
                    receipt.target.revision,
                )
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            resync = True
        finished = replace(receipt, resync_required=resync)
        record.receipt = finished
        record.done.set()
        return finished

    @staticmethod
    def _retain_receipt_now(record: _OperationRecord, receipt: MRActionReceipt) -> None:
        if not record.done.is_set():
            record.receipt = receipt

    @staticmethod
    def _complete_error_now(record: _OperationRecord, error: ServiceError) -> None:
        if not record.done.is_set():
            record.error = error
            record.done.set()

    @staticmethod
    def _record_result(record: _OperationRecord) -> MRActionReceipt:
        if record.error is not None:
            raise record.error
        if record.receipt is None:
            raise RuntimeError("completed operation has no result")
        return record.receipt

    @staticmethod
    def _known_receipt(
        operation_id: str,
        action: MRAction,
        target: ReviewActionTarget,
        result: ForgeMutationResult | ForgeMergeResult,
    ) -> MRActionReceipt:
        if isinstance(result, ForgeMergeResult):
            if action is not MRAction.MERGE:
                raise TypeError("non-merge action returned merge details")
            return MRActionReceipt(
                operation_id,
                action,
                target,
                MRActionOutcome.KNOWN,
                result.remote_id,
                result.merge_sha,
                result.source_cleanup,
                resync_required=not result.cache_invalidated,
            )
        if not isinstance(result, ForgeMutationResult):
            raise TypeError("forge returned an invalid review action receipt")
        return MRActionReceipt(
            operation_id,
            action,
            target,
            MRActionOutcome.KNOWN,
            result.remote_id,
            resync_required=not result.cache_invalidated,
        )

    @staticmethod
    def _unknown_receipt(
        operation_id: str,
        action: MRAction,
        target: ReviewActionTarget,
        error: ServiceError | None = None,
    ) -> MRActionReceipt:
        return MRActionReceipt(
            operation_id,
            action,
            target,
            MRActionOutcome.UNKNOWN,
            error=error
            or ServiceError(
                ServiceErrorCode.INTERNAL,
                "The remote review action outcome is unknown.",
            ),
            resync_required=True,
        )

    @staticmethod
    def _command_parts(
        command: MRActionCommand,
    ) -> tuple[MRAction, ReviewActionTarget]:
        if isinstance(command, MergeReviewCommand):
            action = MRAction.MERGE
        elif isinstance(command, CloseReviewCommand):
            action = MRAction.CLOSE
        elif isinstance(command, ReopenReviewCommand):
            action = MRAction.REOPEN
        elif isinstance(command, UnapproveReviewCommand):
            action = MRAction.UNAPPROVE
        else:
            raise ServiceError(
                ServiceErrorCode.INVALID_INPUT,
                "The review action command is invalid.",
            )
        if not isinstance(command.target, ReviewActionTarget):
            raise ServiceError(
                ServiceErrorCode.INVALID_INPUT,
                "The review action target is invalid.",
            )
        return action, command.target

    @staticmethod
    def _validate_operation_id(operation_id: str) -> None:
        if not isinstance(operation_id, str) or not _OPERATION_ID_RE.fullmatch(
            operation_id
        ):
            raise ServiceError(
                ServiceErrorCode.INVALID_INPUT,
                "The review action operation ID is invalid.",
            )

    @staticmethod
    def _is_known_rejection(error: Exception) -> bool:
        if isinstance(error, _KNOWN_DISPATCH_ERRORS):
            return True
        return isinstance(error, ServiceError) and error.code in _KNOWN_SERVICE_CODES

    @staticmethod
    def _safe_error(
        error: Exception,
        action: MRAction,
        repository: RepositoryRef,
    ) -> ServiceError:
        if isinstance(error, ServiceError):
            return error
        if isinstance(error, NotImplementedError):
            return ServiceError(
                ServiceErrorCode.UNSUPPORTED,
                "This forge does not support the requested review action.",
            )
        return translate_error(
            error,
            operation=f"mr_{action.value}",
            hostname=repository.hostname,
        )

    def _require_open(self) -> None:
        if self._closed:
            raise ServiceError(
                ServiceErrorCode.CLOSED,
                "The MR action service is closed.",
            )


__all__ = [
    "CloseReviewCommand",
    "EmitChange",
    "GetClient",
    "GetReview",
    "MRAction",
    "MRActionCapabilities",
    "MRActionCommand",
    "MRActionOutcome",
    "MRActionReceipt",
    "MRActionService",
    "MergeReviewCommand",
    "ReopenReviewCommand",
    "ReviewActionTarget",
    "SourceBranchTarget",
    "UnapproveReviewCommand",
]
