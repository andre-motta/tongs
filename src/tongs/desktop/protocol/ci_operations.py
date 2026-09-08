"""Strict sidecar operations for session-owned CI mutations."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Protocol, assert_never

from tongs.desktop.protocol.messages import (
    JsonObject,
    ProtocolError,
    ProtocolErrorCode,
)
from tongs.desktop.protocol.state import HandleKind, HandleRegistry
from tongs.services import (
    CancelJobCommand,
    CancelPipelineCommand,
    CIMutationAction,
    CIMutationCommand,
    CIMutationReceipt,
    CIMutationService,
    JobMutationTarget,
    JobRef,
    PipelineMutationTarget,
    PipelineRef,
    RepositoryRef,
    RetryJobCommand,
    RetryPipelineCommand,
    ServiceError,
    ServiceErrorCode,
)

CI_CAPABILITY = "ci_mutations"
CI_METHODS = (
    "ci.capabilities",
    "ci.receipt",
    "jobs.cancel",
    "jobs.retry",
    "pipelines.cancel",
    "pipelines.retry",
)
_OPERATION_ID_MAX_LENGTH = 128
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class Cancellation(Protocol):
    @property
    def cancelled(self) -> bool: ...

    async def wait(self) -> None: ...


class OperationContext(Protocol):
    cancellation: Cancellation


class SessionWithCIMutations(Protocol):
    @property
    def ci_mutations(self) -> CIMutationService: ...


type CIOperationHandler = Callable[[JsonObject, OperationContext], Awaitable[object]]


class CIOperations:
    """Resolve opaque authority and call the session's single CI service."""

    def __init__(
        self, *, session: SessionWithCIMutations, handles: HandleRegistry
    ) -> None:
        self._session = session
        self._handles = handles

    @property
    def handlers(self) -> dict[str, tuple[CIOperationHandler, bool]]:
        """Return the fixed method registry and mutation classification."""
        return {
            "ci.capabilities": (self.capabilities, False),
            "ci.receipt": (self.receipt, False),
            "jobs.cancel": (self.cancel_job, True),
            "jobs.retry": (self.retry_job, True),
            "pipelines.cancel": (self.cancel_pipeline, True),
            "pipelines.retry": (self.retry_pipeline, True),
        }

    async def capabilities(
        self, params: JsonObject, _context: OperationContext
    ) -> object:
        _require_params(
            params,
            allowed=frozenset({"repository"}),
            required=frozenset({"repository"}),
        )
        repository_handle = params["repository"]
        repository = self._handles.resolve(
            repository_handle, HandleKind.REPOSITORY, RepositoryRef
        )
        try:
            capabilities = await self._session.ci_mutations.capabilities(repository)
        except ServiceError as error:
            raise _redact_service_error(error) from None
        return {
            "repository": repository_handle,
            "capabilities": {
                "retry_pipeline": capabilities.retry_pipeline,
                "cancel_pipeline": capabilities.cancel_pipeline,
                "retry_job": capabilities.retry_job,
                "cancel_job": capabilities.cancel_job,
            },
        }

    async def retry_pipeline(
        self, params: JsonObject, context: OperationContext
    ) -> object:
        operation_id, pipeline = self._pipeline_params(params)
        command = RetryPipelineCommand(operation_id, PipelineMutationTarget(pipeline))
        return await self._execute(command, operation_id, context)

    async def cancel_pipeline(
        self, params: JsonObject, context: OperationContext
    ) -> object:
        operation_id, pipeline = self._pipeline_params(params)
        command = CancelPipelineCommand(operation_id, PipelineMutationTarget(pipeline))
        return await self._execute(command, operation_id, context)

    async def retry_job(self, params: JsonObject, context: OperationContext) -> object:
        operation_id, pipeline, job = self._job_params(params)
        command = RetryJobCommand(operation_id, JobMutationTarget(pipeline, job))
        return await self._execute(command, operation_id, context)

    async def cancel_job(self, params: JsonObject, context: OperationContext) -> object:
        operation_id, pipeline, job = self._job_params(params)
        command = CancelJobCommand(operation_id, JobMutationTarget(pipeline, job))
        return await self._execute(command, operation_id, context)

    async def receipt(self, params: JsonObject, _context: OperationContext) -> object:
        operation_id, action, pipeline, job = self._receipt_params(params)
        try:
            receipt = await self._session.ci_mutations.receipt(operation_id)
        except ServiceError as error:
            raise _redact_service_error(error) from None
        if receipt is None:
            return {"receipt": None}
        _require_receipt_authority(receipt, action, pipeline, job)
        return {"receipt": _receipt_wire(receipt)}

    async def _execute(
        self,
        command: CIMutationCommand,
        operation_id: str,
        context: OperationContext,
    ) -> object:
        if context.cancellation.cancelled:
            raise _predispatch_cancellation()
        execute = asyncio.create_task(self._session.ci_mutations.execute(command))
        cancelled = asyncio.create_task(context.cancellation.wait())
        try:
            done, _pending = await asyncio.wait(
                {execute, cancelled}, return_when=asyncio.FIRST_COMPLETED
            )
            if execute in done:
                cancelled.cancel()
                await _drain_cancelled(cancelled)
                return _receipt_wire(execute.result())

            execute.cancel()
            try:
                await execute
            except asyncio.CancelledError:
                receipt = await self._session.ci_mutations.receipt(operation_id)
                if receipt is None:
                    raise _predispatch_cancellation() from None
                _require_receipt_authority(receipt, *_command_authority(command))
                return _receipt_wire(receipt)
            return _receipt_wire(execute.result())
        except ServiceError as error:
            raise _redact_service_error(error) from None
        finally:
            if not cancelled.done():
                cancelled.cancel()
            await _drain_cancelled(cancelled)

    def _pipeline_params(self, params: JsonObject) -> tuple[str, PipelineRef]:
        _require_params(
            params,
            allowed=frozenset({"operation_id", "pipeline"}),
            required=frozenset({"operation_id", "pipeline"}),
        )
        operation_id = _operation_id(params)
        pipeline = self._handles.resolve(
            params["pipeline"], HandleKind.PIPELINE, PipelineRef
        )
        return operation_id, pipeline

    def _job_params(self, params: JsonObject) -> tuple[str, PipelineRef, JobRef]:
        _require_params(
            params,
            allowed=frozenset({"operation_id", "pipeline", "job"}),
            required=frozenset({"operation_id", "pipeline", "job"}),
        )
        operation_id = _operation_id(params)
        pipeline = self._handles.resolve(
            params["pipeline"], HandleKind.PIPELINE, PipelineRef
        )
        job, admitted_pipeline = self._handles.resolve_with_parent(
            params["job"], HandleKind.JOB, JobRef, PipelineRef
        )
        if admitted_pipeline != pipeline:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_HANDLE,
                "The job handle does not belong to the target pipeline.",
            )
        return operation_id, pipeline, job

    def _receipt_params(
        self, params: JsonObject
    ) -> tuple[str, CIMutationAction, PipelineRef, JobRef | None]:
        _require_params(
            params,
            allowed=frozenset({"operation_id", "action", "pipeline", "job"}),
            required=frozenset({"operation_id", "action", "pipeline"}),
        )
        operation_id = _operation_id(params)
        try:
            action = CIMutationAction(_text(params, "action", max_length=40))
        except ValueError:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_PARAMS,
                "The action parameter is invalid.",
            ) from None
        pipeline = self._handles.resolve(
            params["pipeline"], HandleKind.PIPELINE, PipelineRef
        )
        job_actions = frozenset(
            {CIMutationAction.RETRY_JOB, CIMutationAction.CANCEL_JOB}
        )
        if action in job_actions:
            if "job" not in params:
                raise ProtocolError(
                    ProtocolErrorCode.INVALID_PARAMS,
                    "The request parameters have unknown or missing fields.",
                )
            job, admitted_pipeline = self._handles.resolve_with_parent(
                params["job"], HandleKind.JOB, JobRef, PipelineRef
            )
            if admitted_pipeline != pipeline:
                raise ProtocolError(
                    ProtocolErrorCode.INVALID_HANDLE,
                    "The job handle does not belong to the target pipeline.",
                )
            return operation_id, action, pipeline, job
        if "job" in params:
            raise ProtocolError(
                ProtocolErrorCode.INVALID_PARAMS,
                "Pipeline receipt lookups cannot include a job handle.",
            )
        return operation_id, action, pipeline, None


def _command_authority(
    command: CIMutationCommand,
) -> tuple[CIMutationAction, PipelineRef, JobRef | None]:
    if isinstance(command, RetryPipelineCommand):
        return CIMutationAction.RETRY_PIPELINE, command.target.pipeline, None
    if isinstance(command, CancelPipelineCommand):
        return CIMutationAction.CANCEL_PIPELINE, command.target.pipeline, None
    if isinstance(command, RetryJobCommand):
        return CIMutationAction.RETRY_JOB, command.target.pipeline, command.target.job
    if isinstance(command, CancelJobCommand):
        return CIMutationAction.CANCEL_JOB, command.target.pipeline, command.target.job
    assert_never(command)


def _require_receipt_authority(
    receipt: CIMutationReceipt,
    action: CIMutationAction,
    pipeline: PipelineRef,
    job: JobRef | None,
) -> None:
    if (
        receipt.action is not action
        or receipt.pipeline != pipeline
        or receipt.job != job
    ):
        raise ServiceError(
            ServiceErrorCode.CONFLICT,
            "The operation ID is bound to another CI action or target.",
        )


def _receipt_wire(receipt: CIMutationReceipt) -> JsonObject:
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
        "outcome": receipt.outcome.value,
        "error": error,
        "resync_required": receipt.resync_required,
    }


def _require_params(
    params: JsonObject,
    *,
    allowed: frozenset[str],
    required: frozenset[str] = frozenset(),
) -> None:
    keys = frozenset(params)
    if not required <= keys or not keys <= allowed:
        raise ProtocolError(
            ProtocolErrorCode.INVALID_PARAMS,
            "The request parameters have unknown or missing fields.",
        )


def _operation_id(params: JsonObject) -> str:
    operation_id = _text(params, "operation_id", max_length=_OPERATION_ID_MAX_LENGTH)
    if not _OPERATION_ID_RE.fullmatch(operation_id):
        raise ProtocolError(
            ProtocolErrorCode.INVALID_PARAMS,
            "The operation_id parameter is invalid.",
        )
    return operation_id


def _text(params: JsonObject, key: str, *, max_length: int) -> str:
    value = params.get(key)
    if (
        not isinstance(value, str)
        or not value
        or len(value) > max_length
        or any(ord(character) < 32 for character in value)
    ):
        raise ProtocolError(
            ProtocolErrorCode.INVALID_PARAMS,
            f"The {key} parameter is invalid.",
        )
    return value


def _predispatch_cancellation() -> ProtocolError:
    return ProtocolError(
        ProtocolErrorCode.REQUEST_CANCELLED,
        "The CI mutation was cancelled before dispatch.",
        details={"outcome": "not_dispatched"},
    )


def _redact_service_error(error: ServiceError) -> ServiceError:
    return ServiceError(error.code, error.message, retryable=error.retryable)


async def _drain_cancelled(task: asyncio.Task[None]) -> None:
    try:
        await task
    except asyncio.CancelledError:
        pass


__all__ = ["CI_CAPABILITY", "CI_METHODS", "CIOperations"]
