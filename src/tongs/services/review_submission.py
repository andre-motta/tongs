"""Durable, revision-bound submission of persistent review drafts."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from uuid import UUID
from weakref import WeakValueDictionary

from tongs.forges.models import ReviewDecision
from tongs.scanner.repo import ForgeType
from tongs.services.errors import ServiceError, ServiceErrorCode
from tongs.services.models import ReviewRef, ReviewSnapshot
from tongs.services.review_mutations import (
    DiffAnchor,
    DiffSide,
    GeneralComment,
    InlineComment,
    InlineDraft,
    MutationOutcome,
    MutationStatus,
    Reply,
    ReviewMutationCommand,
    ReviewMutationService,
    ReviewVerdict,
)
from tongs.state.drafts.errors import DraftStoreError
from tongs.state.drafts.models import (
    DraftComment,
    DraftState,
    DraftVerdict,
    GeneralDraftComment,
    InlineDraftComment,
    ReconciliationResolution,
    ReplyDraftComment,
    StepReceipt,
    SubmissionAttempt,
    SubmissionPlanRecord,
    SubmissionPlanStepRecord,
)
from tongs.state.drafts.reconciliation import (
    ConfirmedSubmissionContent,
    editable_remainder,
)
from tongs.state.drafts.store import DraftStore

_CLOSE_TIMEOUT = 1.0


class SubmissionStepKind(str, Enum):
    """One durable unit of remote review submission."""

    GENERAL_COMMENT = "general_comment"
    INLINE_COMMENT = "inline_comment"
    REPLY = "reply"
    BODY = "body"
    VERDICT = "verdict"
    GITHUB_REVIEW = "github_review"


class SubmissionOutcome(str, Enum):
    """Current caller-visible disposition of a submission attempt."""

    SUBMITTED = "submitted"
    PAUSED = "paused"
    UNKNOWN = "unknown"
    EDITABLE = "editable"


@dataclass(frozen=True, slots=True)
class SubmissionStep:
    """A stable planned step and the draft comments it represents."""

    id: str
    kind: SubmissionStepKind
    comment_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class SubmissionFailure:
    """A safe failure that stopped submission before the next step."""

    code: str
    message: str
    retryable: bool
    step_id: str | None = None


@dataclass(frozen=True, slots=True)
class SubmissionProgress:
    """Immutable progress for one frozen draft submission."""

    attempt_id: UUID
    draft_id: UUID
    frozen_version: int
    state: DraftState
    outcome: SubmissionOutcome
    steps: tuple[SubmissionStep, ...]
    receipts: tuple[StepReceipt, ...]
    completed_step_ids: tuple[str, ...]
    unknown_step_ids: tuple[str, ...]
    atomic: bool
    resync_required: bool = False
    failure: SubmissionFailure | None = None
    plan_available: bool = True


@dataclass(frozen=True, slots=True)
class _SubmissionPlan:
    steps: tuple[SubmissionStep, ...]
    atomic: bool
    forge: ForgeType


class ReviewSubmissionService:
    """Advance frozen drafts through S5a mutations with durable receipts."""

    def __init__(
        self,
        *,
        store: DraftStore,
        mutations: ReviewMutationService,
        get_review: Callable[[ReviewRef], Awaitable[ReviewSnapshot]],
        close_timeout: float = _CLOSE_TIMEOUT,
    ) -> None:
        if close_timeout <= 0:
            raise ValueError("close_timeout must be positive")
        self._store = store
        self._mutations = mutations
        self._get_review = get_review
        self._close_timeout = close_timeout
        self._attempt_locks: WeakValueDictionary[UUID, asyncio.Lock] = (
            WeakValueDictionary()
        )
        self._state_lock = asyncio.Lock()
        self._active_tasks: set[asyncio.Task[object]] = set()
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def start(self, draft_id: UUID, expected_version: int) -> SubmissionProgress:
        """Freeze an exact draft version, preflight it, then submit its plan."""
        operation = await self._enter_operation()
        try:
            attempt = await self._store.lock_submission(draft_id, expected_version)
            lock = self._attempt_lock(attempt.id)
            async with lock:
                try:
                    attempt, plan = await self._preflight(attempt)
                except BaseException as error:
                    await self._cancel_before_reraise(attempt.id, error)
                    raise
                return await self._advance(attempt, plan)
        finally:
            self._active_tasks.discard(operation)

    async def resume(self, attempt_id: UUID) -> SubmissionProgress:
        """Explicitly retry the first unconfirmed step of an owned active attempt."""
        operation = await self._enter_operation()
        try:
            lock = self._attempt_lock(attempt_id)
            async with lock:
                attempt = await self._store.get_attempt(attempt_id)
                if attempt.state == DraftState.UNKNOWN:
                    raise ServiceError(
                        ServiceErrorCode.CONFLICT,
                        "Unknown submission outcomes require explicit reconciliation.",
                    )
                if attempt.state == DraftState.SUBMITTED:
                    return self._progress(attempt, None, SubmissionOutcome.SUBMITTED)
                attempt, plan = await self._preflight(attempt)
                remaining = self._remaining_steps(attempt, plan)
                if not remaining:
                    return await self._complete(attempt, plan)
                attempt, cancelled = await _finish_critical_state(
                    self._store.authorize_retry(attempt.id, remaining[0].id)
                )
                if cancelled:
                    raise asyncio.CancelledError
                return await self._advance(attempt, plan)
        finally:
            self._active_tasks.discard(operation)

    async def reconcile(
        self, attempt_id: UUID, resolution: ReconciliationResolution
    ) -> SubmissionProgress:
        """Record an explicit UNKNOWN decision and optionally resume remaining work."""
        operation = await self._enter_operation()
        try:
            lock = self._attempt_lock(attempt_id)
            async with lock:
                attempt = await self._store.get_attempt(attempt_id)
                if attempt.state != DraftState.UNKNOWN:
                    raise ServiceError(
                        ServiceErrorCode.CONFLICT,
                        "Only an outcome-unknown submission can be reconciled.",
                    )
                if resolution == ReconciliationResolution.RETURN_EDITABLE:
                    plan = self._stored_plan(attempt)
                    confirmed = self._confirmed_content(attempt)
                    content = editable_remainder(attempt, confirmed)
                    reconciled = await self._store.reconcile_attempt(
                        attempt_id, resolution, editable_content=content
                    )
                    return self._progress(reconciled, plan, SubmissionOutcome.EDITABLE)
                reconciled = await self._store.reconcile_attempt(attempt_id, resolution)
                if resolution == ReconciliationResolution.MARK_SUBMITTED:
                    return self._progress(
                        reconciled,
                        self._stored_plan(reconciled),
                        SubmissionOutcome.SUBMITTED,
                    )
                reconciled, plan = await self._preflight(reconciled)
                return await self._advance(reconciled, plan)
        finally:
            self._active_tasks.discard(operation)

    async def get(self, attempt_id: UUID) -> SubmissionProgress:
        """Read durable attempt state without dispatching or authorizing work."""
        operation = await self._enter_operation()
        try:
            attempt = await self._store.get_attempt(attempt_id)
            outcome = {
                DraftState.SUBMITTED: SubmissionOutcome.SUBMITTED,
                DraftState.UNKNOWN: SubmissionOutcome.UNKNOWN,
            }.get(attempt.state, SubmissionOutcome.PAUSED)
            return self._progress(attempt, self._stored_plan(attempt), outcome)
        finally:
            self._active_tasks.discard(operation)

    async def close(self) -> None:
        """Reject new work and settle all service-owned calls within a fixed bound."""
        async with self._state_lock:
            if self._close_task is None or (
                self._close_task.done()
                and (
                    self._close_task.cancelled()
                    or self._close_task.exception() is not None
                )
            ):
                self._closed = True
                self._close_task = asyncio.create_task(self._coordinate_close())
            task = self._close_task
        cancelled = False
        while True:
            try:
                await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
                if task.done():
                    task.result()
                    break
        if cancelled:
            raise asyncio.CancelledError

    async def _coordinate_close(self) -> None:
        async with self._state_lock:
            current = asyncio.current_task()
            tasks = {task for task in self._active_tasks if task is not current}
        pending = tasks
        for _attempt in range(2):
            if not pending:
                break
            for task in pending:
                task.cancel()
            _done, pending = await asyncio.wait(pending, timeout=self._close_timeout)
        if pending:
            raise RuntimeError("review submission tasks did not stop")

    async def _enter_operation(self) -> asyncio.Task[object]:
        current = asyncio.current_task()
        if current is None:
            raise RuntimeError("review submission requires an asyncio task")
        async with self._state_lock:
            if self._closed:
                raise ServiceError(
                    ServiceErrorCode.CLOSED, "The review submission service is closed."
                )
            self._active_tasks.add(current)
        return current

    def _attempt_lock(self, attempt_id: UUID) -> asyncio.Lock:
        lock = self._attempt_locks.get(attempt_id)
        if lock is None:
            lock = asyncio.Lock()
            self._attempt_locks[attempt_id] = lock
        return lock

    async def _preflight(
        self, attempt: SubmissionAttempt
    ) -> tuple[SubmissionAttempt, _SubmissionPlan]:
        if attempt.plan is None and (
            attempt.receipts
            or attempt.unknown_outcomes
            or attempt.pending_dispatch is not None
        ):
            raise ServiceError(
                ServiceErrorCode.CONFLICT,
                "The recovered attempt has no durable submission plan. Return it to editing before retrying.",
            )
        snapshot = await self._get_review(attempt.snapshot.review)
        if (
            not isinstance(snapshot, ReviewSnapshot)
            or snapshot.ref != attempt.snapshot.review
        ):
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                "The forge returned invalid review detail for this draft.",
            )
        if snapshot.revision is None:
            raise snapshot.revision_error or ServiceError(
                ServiceErrorCode.REVISION_UNAVAILABLE,
                "The review revision is unavailable.",
            )
        if snapshot.revision != attempt.snapshot.revision:
            raise ServiceError(
                ServiceErrorCode.REVISION_CHANGED,
                "The draft revision changed. Refresh its anchors before submitting.",
                retryable=True,
            )
        forge = snapshot.detail.forge_host.forge_type
        if forge == ForgeType.GITHUB and not snapshot.capabilities.batched_review:
            raise ServiceError(
                ServiceErrorCode.UNSUPPORTED,
                "GitHub review submission is unavailable for this repository.",
            )
        if forge == ForgeType.GITLAB and snapshot.revision.start_sha is None:
            raise ServiceError(
                ServiceErrorCode.REVISION_UNAVAILABLE,
                "GitLab did not provide a complete review revision.",
            )
        for comment in attempt.snapshot.comments:
            if isinstance(comment, InlineDraftComment) and (
                comment.anchor.stale
                or comment.anchor.revision != attempt.snapshot.revision
            ):
                raise ServiceError(
                    ServiceErrorCode.REVISION_CHANGED,
                    "The draft contains a stale inline anchor.",
                    retryable=True,
                )
        plan = self._plan(attempt, snapshot)
        for step in plan.steps:
            await self._mutations.validate(self._command(attempt, step, plan))
        recorded = await self._store.record_plan(attempt.id, self._plan_record(plan))
        return recorded, plan

    def _plan(
        self, attempt: SubmissionAttempt, snapshot: ReviewSnapshot
    ) -> _SubmissionPlan:
        content = attempt.snapshot
        forge = snapshot.detail.forge_host.forge_type
        if (
            forge == ForgeType.GITLAB
            and content.verdict == DraftVerdict.REQUEST_CHANGES
        ):
            raise ServiceError(
                ServiceErrorCode.UNSUPPORTED,
                "GitLab does not support request-changes review verdicts.",
            )
        standalone = any(
            isinstance(comment, (GeneralDraftComment, ReplyDraftComment))
            for comment in content.comments
        )
        if forge == ForgeType.GITHUB and not standalone:
            if content.comments or content.body or content.verdict is not None:
                step = SubmissionStep(
                    "batch",
                    SubmissionStepKind.GITHUB_REVIEW,
                    tuple(comment.id for comment in content.comments),
                )
                return _SubmissionPlan((step,), True, forge)
            raise self._empty_draft()

        steps: list[SubmissionStep] = []
        for comment in content.comments:
            kind = {
                GeneralDraftComment: SubmissionStepKind.GENERAL_COMMENT,
                InlineDraftComment: SubmissionStepKind.INLINE_COMMENT,
                ReplyDraftComment: SubmissionStepKind.REPLY,
            }[type(comment)]
            steps.append(
                SubmissionStep(f"comment:{comment.id.hex}", kind, (comment.id,))
            )
        if forge == ForgeType.GITHUB:
            if content.body or content.verdict in {
                DraftVerdict.APPROVE,
                DraftVerdict.REQUEST_CHANGES,
            }:
                steps.append(SubmissionStep("review", SubmissionStepKind.VERDICT))
        else:
            if content.body:
                steps.append(SubmissionStep("body", SubmissionStepKind.BODY))
            if content.verdict == DraftVerdict.APPROVE:
                steps.append(SubmissionStep("verdict", SubmissionStepKind.VERDICT))
        if not steps:
            raise self._empty_draft()
        return _SubmissionPlan(tuple(steps), False, forge)

    @staticmethod
    def _plan_record(plan: _SubmissionPlan) -> SubmissionPlanRecord:
        return SubmissionPlanRecord(
            plan.forge,
            plan.atomic,
            tuple(
                SubmissionPlanStepRecord(
                    step_id=step.id,
                    kind=step.kind.value,
                    comment_ids=step.comment_ids,
                )
                for step in plan.steps
            ),
        )

    @staticmethod
    def _stored_plan(attempt: SubmissionAttempt) -> _SubmissionPlan | None:
        if attempt.plan is None:
            return None
        try:
            return _SubmissionPlan(
                tuple(
                    SubmissionStep(
                        step.step_id,
                        SubmissionStepKind(step.kind),
                        step.comment_ids,
                    )
                    for step in attempt.plan.steps
                ),
                attempt.plan.atomic,
                attempt.plan.forge,
            )
        except ValueError as error:
            raise DraftStoreError("stored submission plan is invalid") from error

    async def _advance(
        self, attempt: SubmissionAttempt, plan: _SubmissionPlan
    ) -> SubmissionProgress:
        for step in self._remaining_steps(attempt, plan):
            command = self._command(attempt, step, plan)
            try:
                attempt = await self._store.begin_dispatch(
                    attempt.id, step.id, command.operation_id
                )
            except asyncio.CancelledError:
                await self._mark_unknown(attempt, step, "dispatch_journal_cancelled")
                raise
            except DraftStoreError:
                latest = await self._store.get_attempt(attempt.id)
                return self._progress(
                    latest,
                    plan,
                    SubmissionOutcome.PAUSED,
                    failure=SubmissionFailure(
                        "storage_failed",
                        "The next submission step could not be journaled.",
                        True,
                        step.id,
                    ),
                )
            try:
                outcome = await self._mutations.execute(command)
            except asyncio.CancelledError:
                await self._mark_unknown(attempt, step, "cancelled")
                raise
            except ServiceError as error:
                try:
                    latest, cancelled = await _finish_critical_state(
                        self._store.reject_dispatch(
                            attempt.id, step.id, command.operation_id
                        )
                    )
                    if cancelled:
                        raise asyncio.CancelledError
                except DraftStoreError:
                    unknown = await self._mark_unknown(
                        attempt, step, "rejection_storage_failed"
                    )
                    return self._progress(
                        unknown,
                        plan,
                        SubmissionOutcome.UNKNOWN,
                        resync_required=True,
                        failure=SubmissionFailure(
                            "storage_failed",
                            "The definite rejection could not be stored safely.",
                            False,
                            step.id,
                        ),
                    )
                return self._progress(
                    latest,
                    plan,
                    SubmissionOutcome.PAUSED,
                    failure=self._service_failure(error, step.id),
                )
            except Exception as error:  # noqa: BLE001 - Durable ambiguity boundary.
                unknown = await self._mark_unknown(attempt, step, type(error).__name__)
                return self._progress(
                    unknown,
                    plan,
                    SubmissionOutcome.UNKNOWN,
                    resync_required=True,
                    failure=SubmissionFailure(
                        ServiceErrorCode.INTERNAL.value,
                        "The review mutation stopped with an unknown outcome.",
                        False,
                        step.id,
                    ),
                )
            if outcome.status == MutationStatus.UNKNOWN:
                unknown = await self._store.mark_attempt_unknown(
                    attempt.id,
                    step_id=step.id,
                    reason=outcome.reason or "unknown",
                )
                return self._progress(
                    unknown,
                    plan,
                    SubmissionOutcome.UNKNOWN,
                    resync_required=True,
                )
            try:
                attempt, cancelled = await _finish_critical_state(
                    self._store.record_receipt(
                        attempt.id,
                        step.id,
                        self._remote_id(outcome),
                        operation_id=command.operation_id,
                        resync_required=outcome.resync_required,
                    )
                )
                if cancelled:
                    raise asyncio.CancelledError
            except DraftStoreError as error:
                try:
                    unknown = await self._store.mark_attempt_unknown(
                        attempt.id, step_id=step.id, reason="receipt_storage_failed"
                    )
                except DraftStoreError:
                    raise error
                return self._progress(
                    unknown,
                    plan,
                    SubmissionOutcome.UNKNOWN,
                    resync_required=True,
                    failure=SubmissionFailure(
                        "storage_failed",
                        "The confirmed remote receipt could not be stored.",
                        False,
                        step.id,
                    ),
                )
        return await self._complete(attempt, plan)

    async def _complete(
        self, attempt: SubmissionAttempt, plan: _SubmissionPlan
    ) -> SubmissionProgress:
        try:
            completed, cancelled = await _finish_critical_state(
                self._store.complete_submission(attempt.id)
            )
            if cancelled:
                raise asyncio.CancelledError
        except DraftStoreError:
            latest = await self._store.get_attempt(attempt.id)
            return self._progress(
                latest,
                plan,
                SubmissionOutcome.PAUSED,
                failure=SubmissionFailure(
                    "storage_failed",
                    "All remote steps completed, but final state could not be stored.",
                    True,
                ),
            )
        return self._progress(completed, plan, SubmissionOutcome.SUBMITTED)

    async def _mark_unknown(
        self,
        attempt: SubmissionAttempt,
        step: SubmissionStep,
        reason: str,
    ) -> SubmissionAttempt:
        return await _finish_critical(
            self._store.mark_attempt_unknown(attempt.id, step_id=step.id, reason=reason)
        )

    async def _cancel_predispatch(self, attempt_id: UUID) -> None:
        await _finish_critical(self._store.cancel_submission(attempt_id))

    async def _cancel_before_reraise(
        self, attempt_id: UUID, original: BaseException
    ) -> None:
        try:
            await self._cancel_predispatch(attempt_id)
        except BaseException:
            if not isinstance(original, Exception):
                raise original
            raise

    def _command(
        self,
        attempt: SubmissionAttempt,
        step: SubmissionStep,
        plan: _SubmissionPlan,
    ) -> ReviewMutationCommand:
        snapshot = attempt.snapshot
        operation_id = self._operation_id(attempt, step)
        if step.kind == SubmissionStepKind.GENERAL_COMMENT:
            comment = self._comment(snapshot.comments, step.comment_ids[0])
            assert isinstance(comment, GeneralDraftComment)
            return GeneralComment(operation_id, snapshot.review, comment.body)
        if step.kind == SubmissionStepKind.INLINE_COMMENT:
            comment = self._comment(snapshot.comments, step.comment_ids[0])
            assert isinstance(comment, InlineDraftComment)
            return InlineComment(
                operation_id,
                snapshot.review,
                snapshot.revision,
                self._anchor(comment),
                comment.body,
            )
        if step.kind == SubmissionStepKind.REPLY:
            comment = self._comment(snapshot.comments, step.comment_ids[0])
            assert isinstance(comment, ReplyDraftComment)
            return Reply(
                operation_id,
                snapshot.review,
                snapshot.revision,
                comment.thread_id,
                comment.body,
            )
        if step.kind == SubmissionStepKind.BODY:
            return GeneralComment(operation_id, snapshot.review, snapshot.body)
        if step.kind == SubmissionStepKind.VERDICT:
            return ReviewVerdict(
                operation_id,
                snapshot.review,
                snapshot.revision,
                self._decision(snapshot.verdict),
                snapshot.body if plan.forge == ForgeType.GITHUB else "",
            )
        comments = tuple(
            self._comment(snapshot.comments, comment_id)
            for comment_id in step.comment_ids
        )
        return ReviewVerdict(
            operation_id,
            snapshot.review,
            snapshot.revision,
            self._decision(snapshot.verdict),
            snapshot.body,
            tuple(
                InlineDraft(self._anchor(comment), comment.body)
                for comment in comments
                if isinstance(comment, InlineDraftComment)
            ),
        )

    @staticmethod
    def _anchor(comment: InlineDraftComment) -> DiffAnchor:
        anchor = comment.anchor
        side = DiffSide.LEFT if anchor.side.value == "old" else DiffSide.RIGHT
        selected = anchor.old_line if side == DiffSide.LEFT else anchor.new_line
        assert selected is not None
        start_side = (
            DiffSide.LEFT
            if anchor.start_side is not None and anchor.start_side.value == "old"
            else DiffSide.RIGHT
            if anchor.start_side is not None
            else None
        )
        return DiffAnchor(
            anchor.old_path,
            anchor.new_path,
            selected,
            side,
            anchor.start_line,
            start_side,
        )

    @staticmethod
    def _decision(verdict: DraftVerdict | None) -> ReviewDecision:
        return {
            DraftVerdict.APPROVE: ReviewDecision.APPROVED,
            DraftVerdict.REQUEST_CHANGES: ReviewDecision.CHANGES_REQUESTED,
            DraftVerdict.COMMENT: ReviewDecision.COMMENTED,
            None: ReviewDecision.COMMENTED,
        }[verdict]

    @staticmethod
    def _comment(comments: tuple[DraftComment, ...], comment_id: UUID) -> DraftComment:
        return next(comment for comment in comments if comment.id == comment_id)

    @staticmethod
    def _remaining_steps(
        attempt: SubmissionAttempt, plan: _SubmissionPlan
    ) -> tuple[SubmissionStep, ...]:
        completed = {receipt.step_id for receipt in attempt.receipts}
        return tuple(step for step in plan.steps if step.id not in completed)

    @staticmethod
    def _operation_id(attempt: SubmissionAttempt, step: SubmissionStep) -> str:
        retry = (
            attempt.retry_authorizations[-1].ordinal
            if attempt.retry_authorizations
            else 0
        )
        digest = hashlib.sha256(step.id.encode()).hexdigest()[:16]
        return (
            f"draft:{attempt.id.hex}:r{len(attempt.reconciliations)}:t{retry}:{digest}"
        )

    @staticmethod
    def _remote_id(outcome: MutationOutcome) -> str:
        if outcome.receipt is None or not outcome.receipt.remote_id:
            raise DraftStoreError("known mutation outcome lacks a remote receipt")
        return outcome.receipt.remote_id

    @staticmethod
    def _confirmed_content(attempt: SubmissionAttempt) -> ConfirmedSubmissionContent:
        receipt_ids = {receipt.step_id for receipt in attempt.receipts}
        batch = "batch" in receipt_ids
        comments = frozenset(
            comment.id
            for comment in attempt.snapshot.comments
            if batch or f"comment:{comment.id.hex}" in receipt_ids
        )
        return ConfirmedSubmissionContent(
            comments,
            body=batch or "body" in receipt_ids or "review" in receipt_ids,
            verdict=batch or "verdict" in receipt_ids or "review" in receipt_ids,
        )

    @staticmethod
    def _service_failure(error: ServiceError, step_id: str) -> SubmissionFailure:
        return SubmissionFailure(
            error.code.value, error.message, error.retryable, step_id
        )

    @staticmethod
    def _empty_draft() -> ServiceError:
        return ServiceError(
            ServiceErrorCode.INVALID_INPUT,
            "The draft does not contain a review mutation.",
        )

    @staticmethod
    def _progress(
        attempt: SubmissionAttempt,
        plan: _SubmissionPlan | None,
        outcome: SubmissionOutcome,
        *,
        resync_required: bool = False,
        failure: SubmissionFailure | None = None,
    ) -> SubmissionProgress:
        steps = plan.steps if plan is not None else ()
        completed = tuple(receipt.step_id for receipt in attempt.receipts)
        return SubmissionProgress(
            attempt.id,
            attempt.draft_id,
            attempt.frozen_version,
            attempt.state,
            outcome,
            steps,
            attempt.receipts,
            completed,
            tuple(marker.step_id for marker in attempt.unknown_outcomes),
            plan.atomic if plan is not None else False,
            resync_required
            or any(receipt.resync_required for receipt in attempt.receipts)
            or bool(attempt.unknown_outcomes),
            failure,
            plan is not None,
        )


async def _finish_critical[T](awaitable: Awaitable[T]) -> T:
    """Finish a durable state transition despite repeated caller cancellation."""
    result, _cancelled = await _finish_critical_state(awaitable)
    return result


async def _finish_critical_state[T](awaitable: Awaitable[T]) -> tuple[T, bool]:
    """Return a durable transition and whether its caller was cancelled."""
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while True:
        try:
            return await asyncio.shield(task), cancelled
        except asyncio.CancelledError:
            cancelled = True
            if task.done():
                return task.result(), cancelled


__all__ = [
    "ReviewSubmissionService",
    "SubmissionFailure",
    "SubmissionOutcome",
    "SubmissionProgress",
    "SubmissionStep",
    "SubmissionStepKind",
]
