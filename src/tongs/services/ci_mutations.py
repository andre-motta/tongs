"""Capability-gated CI mutations for session-admitted forge resources."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
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
from tongs.forges.models import PipelineJob
from tongs.services.errors import ServiceError, ServiceErrorCode, translate_error
from tongs.services.models import (
    JobRef,
    PipelineRef,
    RepositoryRef,
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
log = logging.getLogger(__name__)


class CIMutationAction(str, Enum):
    """Supported CI mutation commands."""

    RETRY_PIPELINE = "retry_pipeline"
    CANCEL_PIPELINE = "cancel_pipeline"
    RETRY_JOB = "retry_job"
    CANCEL_JOB = "cancel_job"


class CIMutationOutcome(str, Enum):
    """Whether the remote result of a dispatched mutation is known."""

    KNOWN = "known"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CIMutationCapabilities:
    """Mutation capabilities for one admitted repository."""

    retry_pipeline: bool
    cancel_pipeline: bool
    retry_job: bool
    cancel_job: bool

    def supports(self, action: CIMutationAction) -> bool:
        """Return whether this capability snapshot supports ``action``."""
        return {
            CIMutationAction.RETRY_PIPELINE: self.retry_pipeline,
            CIMutationAction.CANCEL_PIPELINE: self.cancel_pipeline,
            CIMutationAction.RETRY_JOB: self.retry_job,
            CIMutationAction.CANCEL_JOB: self.cancel_job,
        }[action]


@dataclass(frozen=True, slots=True)
class PipelineMutationTarget:
    """A semantic pipeline target under a session-admitted repository."""

    pipeline: PipelineRef

    def __post_init__(self) -> None:
        if not isinstance(self.pipeline, PipelineRef):
            raise TypeError("pipeline must be a PipelineRef")


@dataclass(frozen=True, slots=True)
class JobMutationTarget:
    """A semantic job target bound to its containing pipeline."""

    pipeline: PipelineRef
    job: JobRef

    def __post_init__(self) -> None:
        if not isinstance(self.pipeline, PipelineRef) or not isinstance(
            self.job, JobRef
        ):
            raise TypeError("pipeline and job must use their declared ref types")
        if self.pipeline.repository != self.job.repository:
            raise ValueError("pipeline and job must belong to the same repository")


@dataclass(frozen=True, slots=True)
class RetryPipelineCommand:
    """Retry failed jobs in a pipeline."""

    operation_id: str
    target: PipelineMutationTarget


@dataclass(frozen=True, slots=True)
class CancelPipelineCommand:
    """Cancel a pipeline."""

    operation_id: str
    target: PipelineMutationTarget


@dataclass(frozen=True, slots=True)
class RetryJobCommand:
    """Retry a job after proving current pipeline membership."""

    operation_id: str
    target: JobMutationTarget


@dataclass(frozen=True, slots=True)
class CancelJobCommand:
    """Cancel a job after proving current pipeline membership."""

    operation_id: str
    target: JobMutationTarget


type CIMutationCommand = (
    RetryPipelineCommand | CancelPipelineCommand | RetryJobCommand | CancelJobCommand
)


@dataclass(frozen=True, slots=True)
class CIMutationReceipt:
    """Retained result for a dispatched CI mutation."""

    operation_id: str
    action: CIMutationAction
    pipeline: PipelineRef
    job: JobRef | None
    outcome: CIMutationOutcome
    error: ServiceError | None = None
    resync_required: bool = True

    def __post_init__(self) -> None:
        if self.outcome == CIMutationOutcome.KNOWN and self.error is not None:
            raise ValueError("known successful outcomes cannot contain an error")
        if self.outcome == CIMutationOutcome.UNKNOWN and self.error is None:
            raise ValueError("unknown outcomes require a safe error")


type GetClient = Callable[[RepositoryRef, str], Awaitable[ForgeClient]]
type GetPipelineJobs = Callable[[PipelineRef], Awaitable[Sequence[PipelineJob]]]
type InvalidatePipeline = Callable[[PipelineRef], Awaitable[None]]
type EmitChange = Callable[[ServiceEventKind, PipelineRef | None], None]


@dataclass(slots=True)
class _OperationRecord:
    fingerprint: tuple[CIMutationAction, PipelineRef, JobRef | None]
    done: asyncio.Event
    receipt: CIMutationReceipt | None = None
    error: ServiceError | None = None


class CIMutationService:
    """Validate, dispatch, and retain bounded CI mutation outcomes.

    ``get_client`` and ``get_pipeline_jobs`` must be callbacks bound to the same
    ``ApplicationSession``. The former admits the repository and resolves its
    forge client; the latter proves the exact pipeline endpoint and, for job
    commands, current job membership immediately before dispatch.
    """

    def __init__(
        self,
        *,
        get_client: GetClient,
        get_pipeline_jobs: GetPipelineJobs,
        emit_change: EmitChange,
        invalidate_pipeline: InvalidatePipeline | None = None,
        max_operations: int = 256,
        hint_timeout: float = 5.0,
        close_timeout: float = 1.0,
    ) -> None:
        if not isinstance(max_operations, int) or isinstance(max_operations, bool):
            raise TypeError("max_operations must be an integer")
        if max_operations <= 0:
            raise ValueError("max_operations must be positive")
        if hint_timeout <= 0:
            raise ValueError("hint_timeout must be positive")
        if close_timeout <= 0:
            raise ValueError("close_timeout must be positive")
        self._get_client = get_client
        self._get_pipeline_jobs = get_pipeline_jobs
        self._emit_change = emit_change
        self._invalidate_pipeline = invalidate_pipeline
        self._max_operations = max_operations
        self._hint_timeout = hint_timeout
        self._close_timeout = close_timeout
        self._operations: dict[str, _OperationRecord] = {}
        self._operation_lock = asyncio.Lock()
        self._coordinator_tasks: set[asyncio.Task[CIMutationReceipt]] = set()
        self._invalidation_tasks: set[asyncio.Task[None]] = set()
        self._owner_tasks: set[asyncio.Task[object]] = set()
        self._closed = False

    async def capabilities(self, repository: RepositoryRef) -> CIMutationCapabilities:
        """Return explicit CI mutation capabilities for an admitted repository."""
        self._require_open()
        client = await self._resolve_client(repository, "ci_mutation_capabilities")
        try:
            retry_pipeline = self._overrides_default(
                client, "retry_pipeline", ForgeClient.retry_pipeline
            )
            return CIMutationCapabilities(
                retry_pipeline=retry_pipeline,
                cancel_pipeline=True,
                retry_job=True,
                cancel_job=bool(client.supports_job_cancel),
            )
        except Exception as error:  # noqa: BLE001 - Sanitize adapter boundary.
            raise translate_error(
                error,
                operation="ci_mutation_capabilities",
                hostname=repository.hostname,
            ) from None

    async def execute(self, command: CIMutationCommand) -> CIMutationReceipt:
        """Execute once per operation ID and retain its known or unknown result."""
        action, pipeline, job = self._command_parts(command)
        self._validate_operation_id(command.operation_id)
        fingerprint = (action, pipeline, job)
        record, owner = await self._reserve(command.operation_id, fingerprint)
        if not owner:
            await record.done.wait()
            return self._record_result(record)

        owner_task = asyncio.current_task()
        if owner_task is None:
            raise RuntimeError("CI mutation execution requires an asyncio task")
        try:
            return await self._execute_owner(command, record, action, pipeline, job)
        finally:
            self._owner_tasks.discard(owner_task)

    async def _execute_owner(
        self,
        command: CIMutationCommand,
        record: _OperationRecord,
        action: CIMutationAction,
        pipeline: PipelineRef,
        job: JobRef | None,
    ) -> CIMutationReceipt:

        dispatch_started = False
        try:
            client = await self._resolve_client(
                pipeline.repository, f"ci_{action.value}"
            )
            capabilities = await self._capabilities_for_client(
                client, pipeline.repository
            )
            if not capabilities.supports(action):
                raise ServiceError(
                    ServiceErrorCode.INVALID_INPUT,
                    "This forge does not support the requested CI action.",
                )

            await self._validate_fresh_target(pipeline, job)
            dispatch_started = True
            await self._dispatch(client, action, pipeline, job)
        except asyncio.CancelledError:
            if dispatch_started:
                receipt = self._unknown_receipt(
                    command.operation_id, action, pipeline, job
                )
                self._retain_receipt_now(record, receipt)
                self._release_current_owner()
                await self._finish_hints(record, pipeline, suppress_cancellation=True)
            else:
                self._complete_error_now(
                    record,
                    ServiceError(
                        ServiceErrorCode.INTERNAL,
                        "The CI mutation was cancelled before dispatch.",
                        retryable=True,
                    ),
                )
            raise
        except BaseException as error:
            if not isinstance(error, Exception):
                if dispatch_started:
                    receipt = self._unknown_receipt(
                        command.operation_id, action, pipeline, job
                    )
                    self._retain_receipt_now(record, receipt)
                    self._release_current_owner()
                    await self._finish_hints(
                        record, pipeline, suppress_cancellation=True
                    )
                else:
                    self._complete_error_now(
                        record,
                        ServiceError(
                            ServiceErrorCode.INTERNAL,
                            "The CI mutation stopped before dispatch.",
                        ),
                    )
                raise
            if not dispatch_started or self._is_known_rejection(error):
                safe = self._safe_error(error, action, pipeline.repository)
                self._complete_error_now(record, safe)
                raise safe from None

            receipt = self._unknown_receipt(
                command.operation_id,
                action,
                pipeline,
                job,
                self._safe_error(error, action, pipeline.repository),
            )
            self._retain_receipt_now(record, receipt)
            self._release_current_owner()
            return await self._finish_hints(record, pipeline)

        receipt = CIMutationReceipt(
            operation_id=command.operation_id,
            action=action,
            pipeline=pipeline,
            job=job,
            outcome=CIMutationOutcome.KNOWN,
        )
        self._retain_receipt_now(record, receipt)
        self._release_current_owner()
        return await self._finish_hints(record, pipeline)

    async def receipt(self, operation_id: str) -> CIMutationReceipt | None:
        """Return a completed receipt, or ``None`` for absent/pending operations."""
        self._require_open()
        self._validate_operation_id(operation_id)
        async with self._operation_lock:
            record = self._operations.get(operation_id)
            return None if record is None else record.receipt

    async def _reserve(
        self,
        operation_id: str,
        fingerprint: tuple[CIMutationAction, PipelineRef, JobRef | None],
    ) -> tuple[_OperationRecord, bool]:
        async with self._operation_lock:
            if self._closed:
                raise ServiceError(
                    ServiceErrorCode.CLOSED,
                    "The CI mutation service is closed.",
                )
            existing = self._operations.get(operation_id)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise ServiceError(
                        ServiceErrorCode.CONFLICT,
                        "The operation ID is already bound to another CI action.",
                    )
                return existing, False
            if len(self._operations) >= self._max_operations:
                raise ServiceError(
                    ServiceErrorCode.CONFLICT,
                    "The CI operation ledger is full.",
                    retryable=True,
                )
            record = _OperationRecord(fingerprint, asyncio.Event())
            self._operations[operation_id] = record
            owner_task = asyncio.current_task()
            if owner_task is None:
                raise RuntimeError("CI mutation execution requires an asyncio task")
            self._owner_tasks.add(owner_task)
            return record, True

    @staticmethod
    def _retain_receipt_now(
        record: _OperationRecord, receipt: CIMutationReceipt
    ) -> None:
        if not record.done.is_set():
            record.receipt = receipt

    @staticmethod
    def _complete_error_now(record: _OperationRecord, error: ServiceError) -> None:
        if not record.done.is_set():
            record.error = error
            record.done.set()

    @staticmethod
    def _record_result(record: _OperationRecord) -> CIMutationReceipt:
        if record.error is not None:
            raise record.error
        if record.receipt is None:
            raise RuntimeError("completed operation has no result")
        return record.receipt

    async def _resolve_client(
        self, repository: RepositoryRef, operation: str
    ) -> ForgeClient:
        try:
            return await self._get_client(repository, operation)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - Sanitize session callback.
            raise translate_error(
                error, operation=operation, hostname=repository.hostname
            ) from None

    async def _capabilities_for_client(
        self, client: ForgeClient, repository: RepositoryRef
    ) -> CIMutationCapabilities:
        try:
            return CIMutationCapabilities(
                retry_pipeline=self._overrides_default(
                    client, "retry_pipeline", ForgeClient.retry_pipeline
                ),
                cancel_pipeline=True,
                retry_job=True,
                cancel_job=bool(client.supports_job_cancel),
            )
        except Exception as error:  # noqa: BLE001 - Sanitize forge capability.
            raise translate_error(
                error,
                operation="ci_mutation_capabilities",
                hostname=repository.hostname,
            ) from None

    async def _validate_fresh_target(
        self, pipeline: PipelineRef, job: JobRef | None
    ) -> None:
        try:
            jobs = await self._get_pipeline_jobs(pipeline)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - Sanitize read callback.
            raise translate_error(
                error,
                operation="validate_ci_target",
                hostname=pipeline.repository.hostname,
            ) from None
        if not isinstance(jobs, Sequence) or isinstance(jobs, (str, bytes)):
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                "The forge returned an invalid pipeline job list.",
            )
        if not all(isinstance(item, PipelineJob) for item in jobs):
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                "The forge returned an invalid pipeline job list.",
            )
        job_ids = [item.id for item in jobs]
        if any(type(item_id) is not int or item_id <= 0 for item_id in job_ids):
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                "The forge returned an invalid pipeline job identity.",
            )
        if len(set(job_ids)) != len(job_ids):
            raise ServiceError(
                ServiceErrorCode.INVALID_RESPONSE,
                "The forge returned duplicate pipeline job identities.",
            )
        if job is not None and not any(item.id == job.job_id for item in jobs):
            raise ServiceError(
                ServiceErrorCode.RESOURCE_NOT_ISSUED,
                "The job is not a current member of the target pipeline.",
            )

    @staticmethod
    async def _dispatch(
        client: ForgeClient,
        action: CIMutationAction,
        pipeline: PipelineRef,
        job: JobRef | None,
    ) -> None:
        project = pipeline.repository.project_path
        if action == CIMutationAction.RETRY_PIPELINE:
            await client.retry_pipeline(project, pipeline.pipeline_id)
        elif action == CIMutationAction.CANCEL_PIPELINE:
            await client.cancel_pipeline(project, pipeline.pipeline_id)
        elif action == CIMutationAction.RETRY_JOB:
            assert job is not None
            await client.retry_job(project, job.job_id)
        else:
            assert action == CIMutationAction.CANCEL_JOB and job is not None
            await client.cancel_job(project, job.job_id)

    async def _finish_hints(
        self,
        record: _OperationRecord,
        pipeline: PipelineRef,
        *,
        suppress_cancellation: bool = False,
    ) -> CIMutationReceipt:
        if self._closed:
            return self._finalize_receipt_now(record, resync_required=True)
        task = asyncio.create_task(self._coordinate_hints(record, pipeline))
        self._coordinator_tasks.add(task)
        task.add_done_callback(self._consume_coordinator_task)
        cancelled = False
        while True:
            try:
                receipt = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                cancelled = True
                if task.done():
                    if task.cancelled():
                        receipt = self._finalize_receipt_now(
                            record, resync_required=True
                        )
                    else:
                        receipt = task.result()
                    break
        if cancelled and not suppress_cancellation:
            raise asyncio.CancelledError()
        return receipt

    async def _coordinate_hints(
        self, record: _OperationRecord, pipeline: PipelineRef
    ) -> CIMutationReceipt:
        try:
            hints_succeeded = await self._run_hints(pipeline)
        except BaseException:
            self._finalize_receipt_now(record, resync_required=True)
            raise
        return self._finalize_receipt_now(record, resync_required=not hints_succeeded)

    @staticmethod
    def _finalize_receipt_now(
        record: _OperationRecord, *, resync_required: bool
    ) -> CIMutationReceipt:
        if record.receipt is None:
            raise RuntimeError("operation outcome was not retained")
        if not record.done.is_set():
            record.receipt = replace(record.receipt, resync_required=resync_required)
            record.done.set()
        return record.receipt

    async def _run_hints(self, pipeline: PipelineRef) -> bool:
        failed = not await self._run_invalidation_hint(pipeline)
        try:
            self._emit_change(ServiceEventKind.PIPELINE_CHANGED, pipeline)
        except Exception:  # noqa: BLE001 - Receipt already retained.
            log.warning("CI pipeline change event failed")
            failed = True
        if failed:
            try:
                self._emit_change(ServiceEventKind.RESYNC_REQUIRED, None)
            except Exception:  # noqa: BLE001 - Receipt still exposes resync.
                log.warning("CI resync-required event failed")
        return not failed

    async def _run_invalidation_hint(self, pipeline: PipelineRef) -> bool:
        if self._invalidate_pipeline is None:
            return True
        task = asyncio.create_task(self._invalidate_pipeline(pipeline))
        self._invalidation_tasks.add(task)
        task.add_done_callback(self._consume_invalidation_task)
        try:
            done, _pending = await asyncio.wait({task}, timeout=self._hint_timeout)
            if task in done:
                return self._consume_hint_result(task)

            task.cancel()
            done, _pending = await asyncio.wait({task}, timeout=self._hint_timeout)
            if task in done:
                self._consume_hint_result(task)
            log.warning("CI pipeline cache invalidation hint timed out")
            return False
        except BaseException:
            task.cancel()
            done, _pending = await asyncio.wait({task}, timeout=self._hint_timeout)
            if task in done:
                self._consume_hint_result(task)
            raise

    @staticmethod
    def _consume_hint_result(task: asyncio.Task[None]) -> bool:
        try:
            task.result()
        except asyncio.CancelledError:
            return False
        except Exception:  # noqa: BLE001 - Receipt already retained.
            log.warning("CI pipeline cache invalidation hint failed")
            return False
        return True

    def _consume_invalidation_task(self, task: asyncio.Task[None]) -> None:
        self._invalidation_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - Consume late callback failure safely.
            log.warning("Detached CI invalidation hint failed")

    def _consume_coordinator_task(self, task: asyncio.Task[CIMutationReceipt]) -> None:
        self._coordinator_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.debug("CI hint coordinator failed", exc_info=True)

    async def close(self) -> None:
        """Reject admission, then cancel and boundedly drain owned work."""
        async with self._operation_lock:
            self._closed = True
            owners = set(self._owner_tasks)
        owner_pending = await self._cancel_and_drain(owners)

        hints: set[asyncio.Task[object]] = {
            *self._coordinator_tasks,
            *self._invalidation_tasks,
        }
        hint_pending = await self._cancel_and_drain(hints)
        if owner_pending or hint_pending:
            raise RuntimeError("CI mutation tasks did not stop")

    async def _cancel_and_drain(
        self, tasks: set[asyncio.Task[object]]
    ) -> set[asyncio.Task[object]]:
        pending = tasks
        for _attempt in range(2):
            if not pending:
                break
            for task in pending:
                task.cancel()
            _done, pending = await asyncio.wait(pending, timeout=self._close_timeout)
        return pending

    def _require_open(self) -> None:
        if self._closed:
            raise ServiceError(
                ServiceErrorCode.CLOSED,
                "The CI mutation service is closed.",
            )

    def _release_current_owner(self) -> None:
        current = asyncio.current_task()
        if current is not None:
            self._owner_tasks.discard(current)

    @staticmethod
    def _command_parts(
        command: CIMutationCommand,
    ) -> tuple[CIMutationAction, PipelineRef, JobRef | None]:
        if isinstance(command, RetryPipelineCommand):
            if not isinstance(command.target, PipelineMutationTarget):
                raise ServiceError(
                    ServiceErrorCode.INVALID_INPUT,
                    "The CI mutation target is invalid.",
                )
            return CIMutationAction.RETRY_PIPELINE, command.target.pipeline, None
        if isinstance(command, CancelPipelineCommand):
            if not isinstance(command.target, PipelineMutationTarget):
                raise ServiceError(
                    ServiceErrorCode.INVALID_INPUT,
                    "The CI mutation target is invalid.",
                )
            return CIMutationAction.CANCEL_PIPELINE, command.target.pipeline, None
        if isinstance(command, RetryJobCommand):
            if not isinstance(command.target, JobMutationTarget):
                raise ServiceError(
                    ServiceErrorCode.INVALID_INPUT,
                    "The CI mutation target is invalid.",
                )
            return (
                CIMutationAction.RETRY_JOB,
                command.target.pipeline,
                command.target.job,
            )
        if isinstance(command, CancelJobCommand):
            if not isinstance(command.target, JobMutationTarget):
                raise ServiceError(
                    ServiceErrorCode.INVALID_INPUT,
                    "The CI mutation target is invalid.",
                )
            return (
                CIMutationAction.CANCEL_JOB,
                command.target.pipeline,
                command.target.job,
            )
        raise ServiceError(
            ServiceErrorCode.INVALID_INPUT,
            "The CI mutation command is invalid.",
        )

    @staticmethod
    def _validate_operation_id(operation_id: str) -> None:
        if not isinstance(operation_id, str) or not _OPERATION_ID_RE.fullmatch(
            operation_id
        ):
            raise ServiceError(
                ServiceErrorCode.INVALID_INPUT,
                "The CI mutation operation ID is invalid.",
            )

    @staticmethod
    def _overrides_default(
        client: ForgeClient, name: str, default: Callable[..., object]
    ) -> bool:
        method = getattr(client, name)
        implementation = getattr(method, "__func__", None)
        return implementation is not default

    @staticmethod
    def _is_known_rejection(error: Exception) -> bool:
        if isinstance(error, _KNOWN_DISPATCH_ERRORS):
            return True
        return isinstance(error, ServiceError) and error.code in _KNOWN_SERVICE_CODES

    @staticmethod
    def _safe_error(
        error: Exception,
        action: CIMutationAction,
        repository: RepositoryRef,
    ) -> ServiceError:
        if isinstance(error, ServiceError):
            return error
        if isinstance(error, NotImplementedError):
            return ServiceError(
                ServiceErrorCode.INVALID_INPUT,
                "This forge does not support the requested CI action.",
            )
        return translate_error(
            error,
            operation=f"ci_{action.value}",
            hostname=repository.hostname,
        )

    @staticmethod
    def _unknown_receipt(
        operation_id: str,
        action: CIMutationAction,
        pipeline: PipelineRef,
        job: JobRef | None,
        error: ServiceError | None = None,
    ) -> CIMutationReceipt:
        return CIMutationReceipt(
            operation_id=operation_id,
            action=action,
            pipeline=pipeline,
            job=job,
            outcome=CIMutationOutcome.UNKNOWN,
            error=error
            or ServiceError(
                ServiceErrorCode.INTERNAL,
                "The remote CI mutation outcome is unknown.",
            ),
        )


__all__ = [
    "CIMutationAction",
    "CIMutationCapabilities",
    "CIMutationCommand",
    "CIMutationOutcome",
    "CIMutationReceipt",
    "CIMutationService",
    "CancelJobCommand",
    "CancelPipelineCommand",
    "EmitChange",
    "GetClient",
    "GetPipelineJobs",
    "InvalidatePipeline",
    "JobMutationTarget",
    "PipelineMutationTarget",
    "RetryJobCommand",
    "RetryPipelineCommand",
]
