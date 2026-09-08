"""Strict sidecar operations for review mutations and durable drafts."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from typing import Protocol, cast
from uuid import UUID

from tongs.desktop.protocol.messages import (
    JsonObject,
    JsonValue,
    ProtocolError,
    ProtocolErrorCode,
)
from tongs.desktop.protocol.state import HandleKind, HandleRegistry
from tongs.forges.models import MRState, ReviewDecision
from tongs.services import (
    CloseReviewCommand,
    DiffAnchor,
    GeneralComment,
    InlineComment,
    InlineDraft,
    MergeReviewCommand,
    MRAction,
    MRActionCommand,
    MRActionReceipt,
    MRActionService,
    MutationOutcome,
    ReopenReviewCommand,
    Reply,
    Resolve,
    ReviewActionTarget,
    ReviewMutationCommand,
    ReviewMutationService,
    ReviewRef,
    ReviewRevision,
    ReviewSnapshot,
    ReviewSubmissionService,
    ReviewVerdict,
    ServiceError,
    ServiceErrorCode,
    SourceBranchTarget,
    SubmissionProgress,
    UnapproveReviewCommand,
)
from tongs.services import DiffSide as MutationDiffSide
from tongs.state.drafts import (
    DiffSide,
    DraftAttemptOwnedError,
    DraftComment,
    DraftConflictError,
    DraftContent,
    DraftCorruptionError,
    DraftNotFoundError,
    DraftNotOpenError,
    DraftPermissionError,
    DraftSchemaError,
    DraftSnapshot,
    DraftState,
    DraftStateError,
    DraftStore,
    DraftStoreError,
    DraftVerdict,
    GeneralDraftComment,
    InlineAnchor,
    InlineDraftComment,
    ReconciliationResolution,
    ReplyDraftComment,
)

REVIEW_CAPABILITY = "review_mutations"
REVIEW_METHODS = (
    "drafts.create",
    "drafts.discard",
    "drafts.get",
    "drafts.list",
    "drafts.save",
    "review_actions.capabilities",
    "review_actions.close",
    "review_actions.merge",
    "review_actions.receipt",
    "review_actions.reopen",
    "review_actions.unapprove",
    "review_mutations.capabilities",
    "review_mutations.comment",
    "review_mutations.inline_comment",
    "review_mutations.reply",
    "review_mutations.resolve",
    "review_mutations.verdict",
    "review_submissions.list",
    "review_submissions.reconcile",
    "review_submissions.resume",
    "review_submissions.start",
    "review_submissions.status",
)

_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_BODY_BYTES = 65_536
_MAX_COMMENTS = 200
_DEFAULT_PAGE_ITEMS = 50
_MAX_PAGE_ITEMS = 100


class Cancellation(Protocol):
    @property
    def cancelled(self) -> bool: ...

    async def wait(self) -> None: ...


class OperationContext(Protocol):
    cancellation: Cancellation


class SessionWithReviewOperations(Protocol):
    @property
    def review_mutations(self) -> ReviewMutationService: ...

    @property
    def mr_actions(self) -> MRActionService: ...

    @property
    def drafts(self) -> DraftStore: ...

    @property
    def review_submissions(self) -> ReviewSubmissionService: ...

    async def get_review(self, ref: ReviewRef) -> ReviewSnapshot: ...


type ReviewOperationHandler = Callable[
    [JsonObject, OperationContext], Awaitable[object]
]


class ReviewOperations:
    """Resolve opaque review authority before using session-owned services."""

    def __init__(
        self, *, session: SessionWithReviewOperations, handles: HandleRegistry
    ) -> None:
        self._session = session
        self._handles = handles

    @property
    def handlers(self) -> dict[str, tuple[ReviewOperationHandler, bool]]:
        """Return the fixed registry and explicit mutation classification."""
        return {
            "drafts.create": (self.create_draft, True),
            "drafts.discard": (self.discard_draft, True),
            "drafts.get": (self.get_draft, False),
            "drafts.list": (self.list_drafts, False),
            "drafts.save": (self.save_draft, True),
            "review_actions.capabilities": (self.action_capabilities, False),
            "review_actions.close": (self.close_review, True),
            "review_actions.merge": (self.merge_review, True),
            "review_actions.receipt": (self.action_receipt, False),
            "review_actions.reopen": (self.reopen_review, True),
            "review_actions.unapprove": (self.unapprove_review, True),
            "review_mutations.capabilities": (self.mutation_capabilities, False),
            "review_mutations.comment": (self.comment, True),
            "review_mutations.inline_comment": (self.inline_comment, True),
            "review_mutations.reply": (self.reply, True),
            "review_mutations.resolve": (self.resolve, True),
            "review_mutations.verdict": (self.verdict, True),
            "review_submissions.list": (self.list_submissions, False),
            "review_submissions.reconcile": (self.reconcile_submission, True),
            "review_submissions.resume": (self.resume_submission, True),
            "review_submissions.start": (self.start_submission, True),
            "review_submissions.status": (self.submission_status, False),
        }

    async def mutation_capabilities(
        self, params: JsonObject, _context: OperationContext
    ) -> object:
        review_handle, review = self._only_review(params)
        try:
            capabilities = await self._session.review_mutations.capabilities(review)
        except ServiceError as error:
            raise _redact_service_error(error) from None
        return {
            "review": review_handle,
            "capabilities": {
                "general_comment": capabilities.general_comment,
                "inline_comment": capabilities.inline_comment,
                "multiline_comment": capabilities.multiline_comment,
                "reply": capabilities.reply,
                "resolve": capabilities.resolve,
                "approve": capabilities.approve,
                "request_changes": capabilities.request_changes,
                "comment_verdict": capabilities.comment_verdict,
                "atomic_review_batch": capabilities.atomic_review_batch,
            },
        }

    async def action_capabilities(
        self, params: JsonObject, _context: OperationContext
    ) -> object:
        review_handle, review = self._only_review(params)
        try:
            capabilities = await self._session.mr_actions.capabilities(review)
        except ServiceError as error:
            raise _redact_service_error(error) from None
        return {
            "review": review_handle,
            "capabilities": {
                "merge": capabilities.merge,
                "close": capabilities.close,
                "reopen": capabilities.reopen,
                "unapprove": capabilities.unapprove,
            },
        }

    async def comment(self, params: JsonObject, context: OperationContext) -> object:
        _require_params(
            params,
            allowed=frozenset({"operation_id", "review", "body"}),
            required=frozenset({"operation_id", "review", "body"}),
        )
        _review_handle, review = self._resolve_review(params["review"])
        command = GeneralComment(_operation_id(params), review, _body(params, "body"))
        return await self._execute_mutation(command, context)

    async def inline_comment(
        self, params: JsonObject, context: OperationContext
    ) -> object:
        _require_params(
            params,
            allowed=frozenset({"operation_id", "review", "revision", "anchor", "body"}),
            required=frozenset(
                {"operation_id", "review", "revision", "anchor", "body"}
            ),
        )
        _review_handle, review = self._resolve_review(params["review"])
        command = InlineComment(
            _operation_id(params),
            review,
            _revision(params["revision"]),
            _mutation_anchor(params["anchor"]),
            _body(params, "body"),
        )
        return await self._execute_mutation(command, context)

    async def reply(self, params: JsonObject, context: OperationContext) -> object:
        _require_params(
            params,
            allowed=frozenset(
                {"operation_id", "review", "revision", "discussion_id", "body"}
            ),
            required=frozenset(
                {"operation_id", "review", "revision", "discussion_id", "body"}
            ),
        )
        _review_handle, review = self._resolve_review(params["review"])
        command = Reply(
            _operation_id(params),
            review,
            _revision(params["revision"]),
            _text(params, "discussion_id", max_length=512),
            _body(params, "body"),
        )
        return await self._execute_mutation(command, context)

    async def resolve(self, params: JsonObject, context: OperationContext) -> object:
        _require_params(
            params,
            allowed=frozenset(
                {
                    "operation_id",
                    "review",
                    "revision",
                    "discussion_id",
                    "resolved",
                }
            ),
            required=frozenset(
                {
                    "operation_id",
                    "review",
                    "revision",
                    "discussion_id",
                    "resolved",
                }
            ),
        )
        _review_handle, review = self._resolve_review(params["review"])
        command = Resolve(
            _operation_id(params),
            review,
            _revision(params["revision"]),
            _text(params, "discussion_id", max_length=512),
            _boolean(params, "resolved"),
        )
        return await self._execute_mutation(command, context)

    async def verdict(self, params: JsonObject, context: OperationContext) -> object:
        _require_params(
            params,
            allowed=frozenset(
                {
                    "operation_id",
                    "review",
                    "revision",
                    "verdict",
                    "body",
                    "inline_comments",
                }
            ),
            required=frozenset({"operation_id", "review", "revision", "verdict"}),
        )
        _review_handle, review = self._resolve_review(params["review"])
        try:
            decision = ReviewDecision(_text(params, "verdict", max_length=40))
        except ValueError:
            raise _invalid("The verdict parameter is invalid.") from None
        inline_values = params.get("inline_comments", [])
        if not isinstance(inline_values, list) or len(inline_values) > _MAX_COMMENTS:
            raise _invalid("The inline_comments parameter is invalid.")
        inline_comments = tuple(_inline_draft(item) for item in inline_values)
        body = _body(params, "body", allow_empty=True, default="")
        try:
            command = ReviewVerdict(
                _operation_id(params),
                review,
                _revision(params["revision"]),
                decision,
                body,
                inline_comments,
            )
        except (TypeError, ValueError):
            raise _invalid("The review verdict is invalid.") from None
        return await self._execute_mutation(command, context)

    async def merge_review(
        self, params: JsonObject, context: OperationContext
    ) -> object:
        _require_params(
            params,
            allowed=frozenset(
                {
                    "operation_id",
                    "review",
                    "revision",
                    "squash",
                    "source_cleanup",
                }
            ),
            required=frozenset({"operation_id", "review", "revision"}),
        )
        review_handle, review = self._resolve_review(params["review"])
        revision = _revision(params["revision"])
        cleanup = None
        if "source_cleanup" in params and params["source_cleanup"] is not None:
            value = _object(params["source_cleanup"], "source_cleanup")
            _require_params(
                value,
                allowed=frozenset({"branch"}),
                required=frozenset({"branch"}),
            )
            cleanup = SourceBranchTarget(
                review.repository, _text(value, "branch", max_length=500)
            )
        command = MergeReviewCommand(
            _operation_id(params),
            ReviewActionTarget(review, revision, MRState.OPEN),
            _optional_boolean(params, "squash", False),
            cleanup,
        )
        return await self._execute_action(command, review_handle, context)

    async def close_review(
        self, params: JsonObject, context: OperationContext
    ) -> object:
        return await self._simple_action(params, context, MRAction.CLOSE)

    async def reopen_review(
        self, params: JsonObject, context: OperationContext
    ) -> object:
        return await self._simple_action(params, context, MRAction.REOPEN)

    async def unapprove_review(
        self, params: JsonObject, context: OperationContext
    ) -> object:
        return await self._simple_action(params, context, MRAction.UNAPPROVE)

    async def action_receipt(
        self, params: JsonObject, _context: OperationContext
    ) -> object:
        _require_params(
            params,
            allowed=frozenset({"operation_id", "review", "revision", "action"}),
            required=frozenset({"operation_id", "review", "revision", "action"}),
        )
        review_handle, review = self._resolve_review(params["review"])
        revision = _revision(params["revision"])
        action = _action(params)
        try:
            receipt = await self._session.mr_actions.receipt(_operation_id(params))
        except ServiceError as error:
            raise _redact_service_error(error) from None
        if receipt is None:
            return {"receipt": None}
        expected_target = ReviewActionTarget(
            review,
            revision,
            MRState.CLOSED if action is MRAction.REOPEN else MRState.OPEN,
        )
        if receipt.action is not action or receipt.target != expected_target:
            raise ServiceError(
                ServiceErrorCode.CONFLICT,
                "The operation ID is bound to another review action.",
            )
        return {"receipt": _action_wire(receipt, review_handle)}

    async def create_draft(
        self, params: JsonObject, context: OperationContext
    ) -> object:
        _require_params(
            params,
            allowed=frozenset({"review", "revision", "content"}),
            required=frozenset({"review", "revision"}),
        )
        review_handle, review = self._resolve_review(params["review"])
        _check_predispatch(context, "draft mutation")
        requested_revision = _revision(params["revision"])
        await self._fresh_revision(review, requested_revision)
        content = _draft_content(params.get("content", {}), requested_revision)
        try:
            draft = await self._session.drafts.create_draft(
                review, requested_revision, content
            )
        except DraftStoreError as error:
            raise _draft_service_error(error) from None
        return _draft_wire(draft, review_handle)

    async def list_drafts(
        self, params: JsonObject, _context: OperationContext
    ) -> object:
        _require_params(
            params,
            allowed=frozenset({"review", "states", "cursor", "max_items"}),
            required=frozenset({"review"}),
        )
        review_handle, review = self._resolve_review(params["review"])
        states = _draft_states(params.get("states"))
        cursor, max_items = _page(params)
        try:
            drafts = await self._session.drafts.list_drafts(
                review=review, states=states
            )
        except DraftStoreError as error:
            raise _draft_service_error(error) from None
        current_revision = await self._fresh_revision(review)
        page, next_cursor = _slice(drafts, cursor, max_items)
        return {
            "cursor": cursor,
            "next_cursor": next_cursor,
            "drafts": [
                _draft_wire(item.assessed_against(current_revision), review_handle)
                for item in page
            ],
        }

    async def get_draft(self, params: JsonObject, _context: OperationContext) -> object:
        _require_params(
            params,
            allowed=frozenset({"review", "draft_id"}),
            required=frozenset({"review", "draft_id"}),
        )
        review_handle, review = self._resolve_review(params["review"])
        draft = await self._draft_for_review(_uuid(params, "draft_id"), review)
        current_revision = await self._fresh_revision(review)
        return _draft_wire(draft.assessed_against(current_revision), review_handle)

    async def save_draft(self, params: JsonObject, context: OperationContext) -> object:
        _require_params(
            params,
            allowed=frozenset({"review", "draft_id", "expected_version", "content"}),
            required=frozenset({"review", "draft_id", "expected_version", "content"}),
        )
        review_handle, review = self._resolve_review(params["review"])
        draft_id = _uuid(params, "draft_id")
        current = await self._draft_for_review(draft_id, review)
        _check_predispatch(context, "draft mutation")
        current_revision = await self._fresh_revision(review)
        content = _draft_content(params["content"], current.revision)
        content = _preserve_comment_identity(current, content)
        try:
            saved = await self._session.drafts.save_draft(
                draft_id,
                _positive_int(params, "expected_version"),
                content,
                current_revision=current_revision,
            )
        except DraftStoreError as error:
            raise _draft_service_error(error) from None
        return _draft_wire(saved, review_handle)

    async def discard_draft(
        self, params: JsonObject, context: OperationContext
    ) -> object:
        _require_params(
            params,
            allowed=frozenset({"review", "draft_id", "expected_version"}),
            required=frozenset({"review", "draft_id", "expected_version"}),
        )
        review_handle, review = self._resolve_review(params["review"])
        draft_id = _uuid(params, "draft_id")
        await self._draft_for_review(draft_id, review)
        _check_predispatch(context, "draft mutation")
        try:
            discarded = await self._session.drafts.discard_draft(
                draft_id, _positive_int(params, "expected_version")
            )
        except DraftStoreError as error:
            raise _draft_service_error(error) from None
        return {"discarded": _draft_wire(discarded, review_handle)}

    async def start_submission(
        self, params: JsonObject, context: OperationContext
    ) -> object:
        _require_params(
            params,
            allowed=frozenset({"review", "draft_id", "expected_version"}),
            required=frozenset({"review", "draft_id", "expected_version"}),
        )
        review_handle, review = self._resolve_review(params["review"])
        draft_id = _uuid(params, "draft_id")
        await self._draft_for_review(draft_id, review)
        return await self._execute_submission(
            lambda: self._session.review_submissions.start(
                draft_id, _positive_int(params, "expected_version")
            ),
            review_handle,
            context,
            recovery_message=(
                "The submission was cancelled after admission. List recovery "
                "attempts before taking another action."
            ),
        )

    async def submission_status(
        self, params: JsonObject, _context: OperationContext
    ) -> object:
        review_handle, _review, attempt_id = await self._attempt_params(params)
        try:
            progress = await self._session.review_submissions.get(attempt_id)
        except (DraftStoreError, ServiceError) as error:
            raise _safe_operation_error(error) from None
        return _submission_wire(progress, review_handle)

    async def list_submissions(
        self, params: JsonObject, _context: OperationContext
    ) -> object:
        _require_params(
            params,
            allowed=frozenset({"review", "cursor", "max_items"}),
            required=frozenset({"review"}),
        )
        review_handle, review = self._resolve_review(params["review"])
        cursor, max_items = _page(params)
        try:
            attempts = await self._session.drafts.list_recovery_attempts()
        except DraftStoreError as error:
            raise _draft_service_error(error) from None
        admitted = tuple(item for item in attempts if item.snapshot.review == review)
        page, next_cursor = _slice(admitted, cursor, max_items)
        progress: list[JsonObject] = []
        for attempt in page:
            try:
                item = await self._session.review_submissions.get(attempt.id)
            except (DraftStoreError, ServiceError) as error:
                raise _safe_operation_error(error) from None
            progress.append(_submission_wire(item, review_handle))
        return {
            "cursor": cursor,
            "next_cursor": next_cursor,
            "attempts": progress,
        }

    async def resume_submission(
        self, params: JsonObject, context: OperationContext
    ) -> object:
        review_handle, _review, attempt_id = await self._attempt_params(params)
        return await self._execute_submission(
            lambda: self._session.review_submissions.resume(attempt_id),
            review_handle,
            context,
            recovery_message="The submission resume was cancelled; read its status.",
        )

    async def reconcile_submission(
        self, params: JsonObject, context: OperationContext
    ) -> object:
        review_handle, _review, attempt_id = await self._attempt_params(
            params, extra=frozenset({"resolution"})
        )
        try:
            resolution = ReconciliationResolution(
                _text(params, "resolution", max_length=40)
            )
        except ValueError:
            raise _invalid("The reconciliation resolution is invalid.") from None
        return await self._execute_submission(
            lambda: self._session.review_submissions.reconcile(attempt_id, resolution),
            review_handle,
            context,
            recovery_message=(
                "The reconciliation was cancelled; read the attempt status "
                "before another action."
            ),
        )

    async def _execute_mutation(
        self, command: ReviewMutationCommand, context: OperationContext
    ) -> JsonObject:
        async def call() -> MutationOutcome:
            return await self._session.review_mutations.execute(command)

        try:
            outcome = await _cancelable(
                call,
                context,
                cancelled_error=_predispatch_cancellation("review mutation"),
            )
        except ServiceError as error:
            raise _redact_service_error(error) from None
        return _mutation_wire(outcome)

    async def _simple_action(
        self,
        params: JsonObject,
        context: OperationContext,
        action: MRAction,
    ) -> object:
        _require_params(
            params,
            allowed=frozenset({"operation_id", "review", "revision"}),
            required=frozenset({"operation_id", "review", "revision"}),
        )
        review_handle, review = self._resolve_review(params["review"])
        target = ReviewActionTarget(
            review,
            _revision(params["revision"]),
            MRState.CLOSED if action is MRAction.REOPEN else MRState.OPEN,
        )
        command_types = {
            MRAction.CLOSE: CloseReviewCommand,
            MRAction.REOPEN: ReopenReviewCommand,
            MRAction.UNAPPROVE: UnapproveReviewCommand,
        }
        command = command_types[action](_operation_id(params), target)
        return await self._execute_action(command, review_handle, context)

    async def _execute_action(
        self,
        command: MRActionCommand,
        review_handle: str,
        context: OperationContext,
    ) -> JsonObject:
        async def recover() -> MRActionReceipt | None:
            receipt = await self._session.mr_actions.receipt(command.operation_id)
            if receipt is not None and (
                receipt.target != command.target
                or receipt.action is not _command_action(command)
            ):
                raise ServiceError(
                    ServiceErrorCode.CONFLICT,
                    "The operation ID is bound to another review action.",
                )
            return receipt

        try:
            receipt = await _cancelable(
                lambda: self._session.mr_actions.execute(command),
                context,
                recovery=recover,
                cancelled_error=_predispatch_cancellation("review action"),
            )
        except ServiceError as error:
            raise _redact_service_error(error) from None
        return _action_wire(receipt, review_handle)

    async def _execute_submission(
        self,
        call: Callable[[], Awaitable[SubmissionProgress]],
        review_handle: str,
        context: OperationContext,
        *,
        recovery_message: str,
    ) -> JsonObject:
        try:
            progress = await _cancelable(
                call,
                context,
                cancelled_error=ProtocolError(
                    ProtocolErrorCode.REQUEST_CANCELLED,
                    recovery_message,
                    details={"outcome": "recover_durable_state"},
                ),
            )
        except (DraftStoreError, ServiceError) as error:
            raise _safe_operation_error(error) from None
        return _submission_wire(progress, review_handle)

    async def _fresh_revision(
        self, review: ReviewRef, expected: ReviewRevision | None = None
    ) -> ReviewRevision:
        try:
            snapshot = await self._session.get_review(review)
        except ServiceError as error:
            raise _redact_service_error(error) from None
        if not isinstance(snapshot, ReviewSnapshot) or snapshot.ref != review:
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                "The forge returned invalid review detail.",
            )
        if snapshot.revision is None:
            raise snapshot.revision_error or ServiceError(
                ServiceErrorCode.REVISION_UNAVAILABLE,
                "The review revision is unavailable.",
            )
        if expected is not None and snapshot.revision != expected:
            raise ServiceError(
                ServiceErrorCode.REVISION_CHANGED,
                "The review revision changed. Refresh before writing.",
                retryable=True,
            )
        return snapshot.revision

    async def _draft_for_review(
        self, draft_id: UUID, review: ReviewRef
    ) -> DraftSnapshot:
        try:
            draft = await self._session.drafts.get_draft(draft_id)
        except DraftStoreError as error:
            raise _draft_service_error(error) from None
        if draft.review != review:
            raise ServiceError(
                ServiceErrorCode.RESOURCE_NOT_ISSUED,
                "The draft does not belong to the admitted review.",
            )
        return draft

    async def _attempt_params(
        self, params: JsonObject, *, extra: frozenset[str] = frozenset()
    ) -> tuple[str, ReviewRef, UUID]:
        _require_params(
            params,
            allowed=frozenset({"review", "attempt_id"}) | extra,
            required=frozenset({"review", "attempt_id"}) | extra,
        )
        review_handle, review = self._resolve_review(params["review"])
        attempt_id = _uuid(params, "attempt_id")
        try:
            attempt = await self._session.drafts.get_attempt(attempt_id)
        except DraftStoreError as error:
            raise _draft_service_error(error) from None
        if attempt.snapshot.review != review:
            raise ServiceError(
                ServiceErrorCode.RESOURCE_NOT_ISSUED,
                "The submission attempt does not belong to the admitted review.",
            )
        return review_handle, review, attempt_id

    def _only_review(self, params: JsonObject) -> tuple[str, ReviewRef]:
        _require_params(
            params,
            allowed=frozenset({"review"}),
            required=frozenset({"review"}),
        )
        return self._resolve_review(params["review"])

    def _resolve_review(self, value: object) -> tuple[str, ReviewRef]:
        review = self._handles.resolve(value, HandleKind.REVIEW, ReviewRef)
        return cast(str, value), review


async def _cancelable[ResultT](
    call: Callable[[], Awaitable[ResultT]],
    context: OperationContext,
    *,
    recovery: Callable[[], Awaitable[ResultT | None]] | None = None,
    cancelled_error: ProtocolError,
) -> ResultT:
    _check_predispatch(context, "mutation")
    execution = asyncio.create_task(call())
    cancellation = asyncio.create_task(context.cancellation.wait())
    try:
        done, _pending = await asyncio.wait(
            {execution, cancellation}, return_when=asyncio.FIRST_COMPLETED
        )
        if execution in done:
            cancellation.cancel()
            await _drain_cancelled(cancellation)
            return execution.result()
        execution.cancel()
        try:
            return await execution
        except asyncio.CancelledError:
            if recovery is not None:
                recovered = await recovery()
                if recovered is not None:
                    return recovered
            raise cancelled_error from None
    finally:
        if not cancellation.done():
            cancellation.cancel()
        await _drain_cancelled(cancellation)


def _mutation_wire(outcome: MutationOutcome) -> JsonObject:
    receipt = outcome.receipt
    return {
        "operation_id": outcome.operation_id,
        "outcome": outcome.status.value,
        "receipt": (
            {
                "remote_id": receipt.remote_id,
                "comment_id": receipt.comment_id,
                "discussion_id": receipt.discussion_id,
            }
            if receipt is not None
            else None
        ),
        "reason": outcome.reason,
        "resync_required": outcome.resync_required,
    }


def _action_wire(receipt: MRActionReceipt, review_handle: str) -> JsonObject:
    error: JsonObject | None = None
    if receipt.error is not None:
        error = {
            "code": receipt.error.code.value,
            "message": receipt.error.message,
            "retryable": receipt.error.retryable,
        }
    return {
        "operation_id": receipt.operation_id,
        "action": receipt.action.value,
        "review": review_handle,
        "revision": _revision_wire(receipt.target.revision),
        "expected_state": receipt.target.expected_state.value,
        "outcome": receipt.outcome.value,
        "remote_id": receipt.remote_id,
        "merge_sha": receipt.merge_sha,
        "source_cleanup": receipt.source_cleanup.value,
        "error": error,
        "resync_required": receipt.resync_required,
    }


def _draft_wire(draft: DraftSnapshot, review_handle: str) -> JsonObject:
    return {
        "id": str(draft.id),
        "review": review_handle,
        "revision": _revision_wire(draft.revision),
        "version": draft.version,
        "body": draft.body,
        "verdict": draft.verdict.value if draft.verdict is not None else None,
        "comments": [_draft_comment_wire(item) for item in draft.comments],
        "state": draft.state.value,
        "created_at": draft.created_at.isoformat(),
        "updated_at": draft.updated_at.isoformat(),
    }


def _draft_comment_wire(comment: DraftComment) -> JsonObject:
    result: JsonObject = {
        "id": str(comment.id),
        "kind": comment.kind,
        "body": comment.body,
    }
    if isinstance(comment, InlineDraftComment):
        result["anchor"] = _draft_anchor_wire(comment.anchor)
    elif isinstance(comment, ReplyDraftComment):
        result["thread_id"] = comment.thread_id
    return result


def _draft_anchor_wire(anchor: InlineAnchor) -> JsonObject:
    return {
        "revision": _revision_wire(anchor.revision),
        "old_path": anchor.old_path,
        "new_path": anchor.new_path,
        "old_line": anchor.old_line,
        "new_line": anchor.new_line,
        "side": anchor.side.value,
        "context_fingerprint": anchor.context_fingerprint,
        "start_line": anchor.start_line,
        "start_side": anchor.start_side.value if anchor.start_side else None,
        "stale": anchor.stale,
    }


def _submission_wire(progress: SubmissionProgress, review_handle: str) -> JsonObject:
    failure: JsonObject | None = None
    if progress.failure is not None:
        failure = {
            "code": progress.failure.code,
            "message": progress.failure.message,
            "retryable": progress.failure.retryable,
            "step_id": progress.failure.step_id,
        }
    return {
        "attempt_id": str(progress.attempt_id),
        "draft_id": str(progress.draft_id),
        "review": review_handle,
        "frozen_version": progress.frozen_version,
        "state": progress.state.value,
        "outcome": progress.outcome.value,
        "steps": [
            {
                "id": step.id,
                "kind": step.kind.value,
                "comment_ids": [str(item) for item in step.comment_ids],
            }
            for step in progress.steps
        ],
        "receipts": [
            {
                "step_id": receipt.step_id,
                "remote_id": receipt.remote_id,
                "recorded_at": receipt.recorded_at.isoformat(),
                "resync_required": receipt.resync_required,
            }
            for receipt in progress.receipts
        ],
        "completed_step_ids": list(progress.completed_step_ids),
        "unknown_step_ids": list(progress.unknown_step_ids),
        "atomic": progress.atomic,
        "resync_required": progress.resync_required,
        "failure": failure,
        "plan_available": progress.plan_available,
    }


def _draft_content(value: object, revision: ReviewRevision) -> DraftContent:
    params = _object(value, "content")
    _require_params(
        params,
        allowed=frozenset({"body", "verdict", "comments"}),
    )
    body = _body(params, "body", allow_empty=True, default="")
    verdict_value = params.get("verdict")
    verdict = None
    if verdict_value is not None:
        if not isinstance(verdict_value, str):
            raise _invalid("The verdict parameter is invalid.")
        try:
            verdict = DraftVerdict(verdict_value)
        except ValueError:
            raise _invalid("The verdict parameter is invalid.") from None
    comments_value = params.get("comments", [])
    if not isinstance(comments_value, list) or len(comments_value) > _MAX_COMMENTS:
        raise _invalid("The draft comments parameter is invalid.")
    comments = tuple(_draft_comment(item, revision) for item in comments_value)
    try:
        return DraftContent(body, verdict, comments)
    except (TypeError, ValueError):
        raise _invalid("The draft content is invalid.") from None


def _draft_comment(value: object, revision: ReviewRevision) -> DraftComment:
    params = _object(value, "comment")
    kind = params.get("kind")
    if kind == "general":
        _require_params(
            params,
            allowed=frozenset({"id", "kind", "body"}),
            required=frozenset({"id", "kind", "body"}),
        )
        return GeneralDraftComment(_uuid(params, "id"), _body(params, "body"))
    if kind == "reply":
        _require_params(
            params,
            allowed=frozenset({"id", "kind", "body", "thread_id"}),
            required=frozenset({"id", "kind", "body", "thread_id"}),
        )
        return ReplyDraftComment(
            _uuid(params, "id"),
            _body(params, "body"),
            _text(params, "thread_id", max_length=512),
        )
    if kind == "inline":
        _require_params(
            params,
            allowed=frozenset({"id", "kind", "body", "anchor"}),
            required=frozenset({"id", "kind", "body", "anchor"}),
        )
        anchor = _draft_anchor(params["anchor"])
        if anchor.revision != revision:
            raise _invalid("The inline anchor revision is invalid.")
        return InlineDraftComment(_uuid(params, "id"), _body(params, "body"), anchor)
    raise _invalid("The draft comment kind is invalid.")


def _draft_anchor(value: object) -> InlineAnchor:
    params = _object(value, "anchor")
    _require_params(
        params,
        allowed=frozenset(
            {
                "revision",
                "old_path",
                "new_path",
                "old_line",
                "new_line",
                "side",
                "context_fingerprint",
                "start_line",
                "start_side",
                "stale",
            }
        ),
        required=frozenset(
            {
                "revision",
                "old_path",
                "new_path",
                "old_line",
                "new_line",
                "side",
                "context_fingerprint",
            }
        ),
    )
    try:
        side = DiffSide(_text(params, "side", max_length=10))
        start_side = (
            None
            if params.get("start_side") is None
            else DiffSide(_text(params, "start_side", max_length=10))
        )
        fingerprint = _text(params, "context_fingerprint", max_length=64)
        if not _FINGERPRINT_RE.fullmatch(fingerprint):
            raise ValueError
        return InlineAnchor(
            _revision(params["revision"]),
            _text(params, "old_path", max_length=500),
            _text(params, "new_path", max_length=500),
            _nullable_positive_int(params, "old_line"),
            _nullable_positive_int(params, "new_line"),
            side,
            fingerprint,
            _nullable_positive_int(params, "start_line"),
            start_side,
            _optional_boolean(params, "stale", False),
        )
    except (TypeError, ValueError):
        raise _invalid("The inline anchor is invalid.") from None


def _mutation_anchor(value: object) -> DiffAnchor:
    params = _object(value, "anchor")
    _require_params(
        params,
        allowed=frozenset(
            {"old_path", "new_path", "line", "side", "start_line", "start_side"}
        ),
        required=frozenset({"old_path", "new_path", "line", "side"}),
    )
    try:
        side = MutationDiffSide(_text(params, "side", max_length=10))
        start_side = (
            None
            if params.get("start_side") is None
            else MutationDiffSide(_text(params, "start_side", max_length=10))
        )
        return DiffAnchor(
            _text(params, "old_path", max_length=500),
            _text(params, "new_path", max_length=500),
            _positive_int(params, "line"),
            side,
            _nullable_positive_int(params, "start_line"),
            start_side,
        )
    except (TypeError, ValueError):
        raise _invalid("The inline anchor is invalid.") from None


def _inline_draft(value: object) -> InlineDraft:
    params = _object(value, "inline_comment")
    _require_params(
        params,
        allowed=frozenset({"anchor", "body"}),
        required=frozenset({"anchor", "body"}),
    )
    try:
        return InlineDraft(_mutation_anchor(params["anchor"]), _body(params, "body"))
    except (TypeError, ValueError):
        raise _invalid("The inline comment is invalid.") from None


def _preserve_comment_identity(
    current: DraftSnapshot, content: DraftContent
) -> DraftContent:
    existing = {comment.id: comment for comment in current.comments}
    updated: list[DraftComment] = []
    for comment in content.comments:
        previous = existing.get(comment.id)
        if previous is None:
            updated.append(comment)
            continue
        if type(previous) is not type(comment):
            raise _invalid("A draft comment ID cannot change kind.")
        if isinstance(comment, InlineDraftComment):
            assert isinstance(previous, InlineDraftComment)
            prior_anchor = replace(previous.anchor, stale=False)
            next_anchor = replace(comment.anchor, stale=False)
            if prior_anchor != next_anchor:
                raise _invalid("A draft comment ID cannot change its inline anchor.")
            if previous.anchor.stale:
                comment = replace(comment, anchor=replace(comment.anchor, stale=True))
        elif isinstance(comment, ReplyDraftComment):
            assert isinstance(previous, ReplyDraftComment)
            if previous.thread_id != comment.thread_id:
                raise _invalid("A draft comment ID cannot change its reply target.")
        updated.append(comment)
    return replace(content, comments=tuple(updated))


def _revision(value: object) -> ReviewRevision:
    params = _object(value, "revision")
    _require_params(
        params,
        allowed=frozenset({"head_sha", "base_sha", "start_sha"}),
        required=frozenset({"head_sha", "base_sha", "start_sha"}),
    )
    start_sha = params["start_sha"]
    if start_sha is not None and not isinstance(start_sha, str):
        raise _invalid("The revision parameter is invalid.")
    try:
        return ReviewRevision(
            _text(params, "head_sha", max_length=128),
            _text(params, "base_sha", max_length=128),
            None if start_sha is None else _text(params, "start_sha", max_length=128),
        )
    except (TypeError, ValueError):
        raise _invalid("The revision parameter is invalid.") from None


def _revision_wire(revision: ReviewRevision) -> JsonObject:
    return {
        "head_sha": revision.head_sha,
        "base_sha": revision.base_sha,
        "start_sha": revision.start_sha,
    }


def _draft_states(value: JsonValue | None) -> frozenset[DraftState] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > len(DraftState):
        raise _invalid("The draft states parameter is invalid.")
    try:
        states = [DraftState(item) for item in value if isinstance(item, str)]
    except ValueError:
        raise _invalid("The draft states parameter is invalid.") from None
    if len(states) != len(value) or len(states) != len(set(states)):
        raise _invalid("The draft states parameter is invalid.")
    return frozenset(states)


def _page(params: JsonObject) -> tuple[int, int]:
    cursor = _optional_int(params, "cursor", 0, minimum=0, maximum=1_000_000)
    max_items = _optional_int(
        params,
        "max_items",
        _DEFAULT_PAGE_ITEMS,
        minimum=1,
        maximum=_MAX_PAGE_ITEMS,
    )
    return cursor, max_items


def _slice[ResultT](
    values: Sequence[ResultT], cursor: int, max_items: int
) -> tuple[Sequence[ResultT], int | None]:
    if cursor > len(values):
        raise _invalid("The page cursor is invalid.")
    end = min(cursor + max_items, len(values))
    return values[cursor:end], end if end < len(values) else None


def _command_action(command: MRActionCommand) -> MRAction:
    if isinstance(command, MergeReviewCommand):
        return MRAction.MERGE
    if isinstance(command, CloseReviewCommand):
        return MRAction.CLOSE
    if isinstance(command, ReopenReviewCommand):
        return MRAction.REOPEN
    return MRAction.UNAPPROVE


def _action(params: JsonObject) -> MRAction:
    try:
        return MRAction(_text(params, "action", max_length=40))
    except ValueError:
        raise _invalid("The action parameter is invalid.") from None


def _require_params(
    params: JsonObject,
    *,
    allowed: frozenset[str],
    required: frozenset[str] = frozenset(),
) -> None:
    keys = frozenset(params)
    if not required <= keys or not keys <= allowed:
        raise _invalid("The request parameters have unknown or missing fields.")


def _object(value: object, name: str) -> JsonObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise _invalid(f"The {name} parameter is invalid.")
    return cast(JsonObject, value)


def _operation_id(params: JsonObject) -> str:
    value = _text(params, "operation_id", max_length=128)
    if not _OPERATION_ID_RE.fullmatch(value):
        raise _invalid("The operation_id parameter is invalid.")
    return value


def _uuid(params: JsonObject, key: str) -> UUID:
    value = _text(params, key, max_length=36)
    try:
        parsed = UUID(value)
    except ValueError:
        raise _invalid(f"The {key} parameter is invalid.") from None
    if str(parsed) != value:
        raise _invalid(f"The {key} parameter is invalid.")
    return parsed


def _text(params: JsonObject, key: str, *, max_length: int) -> str:
    value = params.get(key)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > max_length
        or any(ord(character) < 32 for character in value)
    ):
        raise _invalid(f"The {key} parameter is invalid.")
    return value


def _body(
    params: JsonObject,
    key: str,
    *,
    allow_empty: bool = False,
    default: str | None = None,
) -> str:
    value = params.get(key, default)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise _invalid(f"The {key} parameter is invalid.")
    if any(ord(character) < 32 and character not in "\t\n" for character in value):
        raise _invalid(f"The {key} parameter is invalid.")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise _invalid(f"The {key} parameter is invalid.") from None
    if size > _MAX_BODY_BYTES:
        raise _invalid(f"The {key} parameter is invalid.")
    return value


def _positive_int(params: JsonObject, key: str) -> int:
    value = params.get(key)
    if type(value) is not int or value <= 0:
        raise _invalid(f"The {key} parameter is invalid.")
    return value


def _nullable_positive_int(params: JsonObject, key: str) -> int | None:
    value = params.get(key)
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise _invalid(f"The {key} parameter is invalid.")
    return value


def _optional_int(
    params: JsonObject,
    key: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = params.get(key, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise _invalid(f"The {key} parameter is invalid.")
    return value


def _boolean(params: JsonObject, key: str) -> bool:
    value = params.get(key)
    if type(value) is not bool:
        raise _invalid(f"The {key} parameter is invalid.")
    return value


def _optional_boolean(params: JsonObject, key: str, default: bool) -> bool:
    if key not in params:
        return default
    return _boolean(params, key)


def _check_predispatch(context: OperationContext, operation: str) -> None:
    if context.cancellation.cancelled:
        raise _predispatch_cancellation(operation)


def _predispatch_cancellation(operation: str) -> ProtocolError:
    return ProtocolError(
        ProtocolErrorCode.REQUEST_CANCELLED,
        f"The {operation} was cancelled before dispatch.",
        details={"outcome": "not_dispatched"},
    )


def _redact_service_error(error: ServiceError) -> ServiceError:
    return ServiceError(error.code, error.message, retryable=error.retryable)


def _draft_service_error(error: DraftStoreError) -> ServiceError:
    if isinstance(error, DraftNotFoundError):
        return ServiceError(
            ServiceErrorCode.NOT_FOUND, "The draft record was not found."
        )
    if isinstance(error, DraftConflictError):
        return ServiceError(
            ServiceErrorCode.CONFLICT,
            "The draft changed before it could be saved.",
            retryable=True,
            details=(
                ("draft_id", str(error.current.id)),
                ("expected_version", str(error.expected_version)),
                ("current_version", str(error.current.version)),
            ),
        )
    if isinstance(error, DraftStateError):
        return ServiceError(
            ServiceErrorCode.CONFLICT,
            "The draft state does not allow this operation.",
            retryable=True,
            details=(
                ("draft_id", str(error.current.id)),
                ("current_version", str(error.current.version)),
                ("current_state", error.current.state.value),
            ),
        )
    if isinstance(error, DraftAttemptOwnedError):
        return ServiceError(
            ServiceErrorCode.CONFLICT,
            "The submission attempt is active in another process.",
            retryable=True,
        )
    if isinstance(error, DraftNotOpenError):
        return ServiceError(ServiceErrorCode.CLOSED, "Draft storage is not open.")
    if isinstance(
        error, (DraftPermissionError, DraftSchemaError, DraftCorruptionError)
    ):
        return ServiceError(
            ServiceErrorCode.CONFIGURATION_INVALID,
            "Draft storage is unavailable.",
        )
    return ServiceError(ServiceErrorCode.INTERNAL, "The draft operation failed.")


def _safe_operation_error(error: DraftStoreError | ServiceError) -> ServiceError:
    if isinstance(error, ServiceError):
        return _redact_service_error(error)
    return _draft_service_error(error)


def _invalid(message: str) -> ProtocolError:
    return ProtocolError(ProtocolErrorCode.INVALID_PARAMS, message)


async def _drain_cancelled(task: asyncio.Task[None]) -> None:
    try:
        await task
    except asyncio.CancelledError:
        pass


__all__ = ["REVIEW_CAPABILITY", "REVIEW_METHODS", "ReviewOperations"]
