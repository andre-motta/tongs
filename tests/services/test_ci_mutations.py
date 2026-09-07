"""Tests for capability-gated CI mutation services."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import cast

import httpx
import pytest

from tongs.errors import (
    AuthError,
    ConflictError,
    ForgeError,
    NetworkError,
    NotFoundError,
)
from tongs.forges.base import ForgeClient
from tongs.forges.github import GitHubClient
from tongs.forges.gitlab import GitLabClient
from tongs.forges.models import CIStatus, ForgeHost, PipelineJob
from tongs.scanner.repo import ForgeType
from tongs.services.ci_mutations import (
    CancelJobCommand,
    CancelPipelineCommand,
    CIMutationOutcome,
    CIMutationService,
    JobMutationTarget,
    PipelineMutationTarget,
    RetryJobCommand,
    RetryPipelineCommand,
)
from tongs.services.errors import ServiceError, ServiceErrorCode
from tongs.services.models import JobRef, PipelineRef, RepositoryRef, ServiceEventKind

REPOSITORY = RepositoryRef("github.com", "acme/widgets")
OTHER_REPOSITORY = RepositoryRef("gitlab.com", "other/widgets")
PIPELINE = PipelineRef(REPOSITORY, 101)
JOB = JobRef(REPOSITORY, 202)
PIPELINE_TARGET = PipelineMutationTarget(PIPELINE)
JOB_TARGET = JobMutationTarget(PIPELINE, JOB)


class FakeClient:
    def __init__(self, *, supports_job_cancel: bool = False) -> None:
        self.supports_job_cancel = supports_job_cancel
        self.calls: list[tuple[str, str, int]] = []
        self.error: BaseException | None = None
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def retry_pipeline(self, project: str, pipeline_id: int) -> None:
        await self._mutate("retry_pipeline", project, pipeline_id)

    async def cancel_pipeline(self, project: str, pipeline_id: int) -> None:
        await self._mutate("cancel_pipeline", project, pipeline_id)

    async def retry_job(self, project: str, job_id: int) -> None:
        await self._mutate("retry_job", project, job_id)

    async def cancel_job(self, project: str, job_id: int) -> None:
        await self._mutate("cancel_job", project, job_id)

    async def _mutate(self, action: str, project: str, item_id: int) -> None:
        self.calls.append((action, project, item_id))
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error


class DefaultPipelineRetryClient(FakeClient):
    retry_pipeline = ForgeClient.retry_pipeline


class Harness:
    def __init__(
        self,
        client: FakeClient | None = None,
        *,
        jobs: Sequence[PipelineJob] | None = None,
    ) -> None:
        self.client = client or FakeClient()
        self.jobs = list(jobs if jobs is not None else [self._job(JOB.job_id)])
        self.get_client_calls: list[tuple[RepositoryRef, str]] = []
        self.job_reads: list[PipelineRef] = []
        self.invalidations: list[PipelineRef] = []
        self.events: list[tuple[ServiceEventKind, PipelineRef | None]] = []
        self.get_client_error: Exception | None = None
        self.jobs_error: Exception | None = None
        self.invalidate_error: Exception | None = None
        self.emit_error: Exception | None = None
        self.invalidate_started = asyncio.Event()
        self.invalidate_release: asyncio.Event | None = None
        self.resist_invalidate_cancellation = False

    @staticmethod
    def _job(job_id: int) -> PipelineJob:
        return PipelineJob(job_id, "test", "verify", CIStatus.RUNNING)

    async def get_client(
        self, repository: RepositoryRef, operation: str
    ) -> ForgeClient:
        self.get_client_calls.append((repository, operation))
        if self.get_client_error is not None:
            raise self.get_client_error
        if repository != REPOSITORY:
            raise ServiceError(
                ServiceErrorCode.RESOURCE_NOT_ISSUED,
                "The repository was not issued by this session.",
            )
        return cast(ForgeClient, self.client)

    async def get_pipeline_jobs(self, pipeline: PipelineRef) -> Sequence[PipelineJob]:
        self.job_reads.append(pipeline)
        if self.jobs_error is not None:
            raise self.jobs_error
        return tuple(self.jobs)

    async def invalidate_pipeline(self, pipeline: PipelineRef) -> None:
        self.invalidations.append(pipeline)
        self.invalidate_started.set()
        if self.invalidate_release is not None:
            try:
                await self.invalidate_release.wait()
            except asyncio.CancelledError:
                if not self.resist_invalidate_cancellation:
                    raise
                await self.invalidate_release.wait()
        if self.invalidate_error is not None:
            raise self.invalidate_error

    def emit_change(self, kind: ServiceEventKind, pipeline: PipelineRef | None) -> None:
        self.events.append((kind, pipeline))
        if self.emit_error is not None:
            raise self.emit_error

    def service(
        self, *, max_operations: int = 256, hint_timeout: float = 5.0
    ) -> CIMutationService:
        return CIMutationService(
            get_client=self.get_client,
            get_pipeline_jobs=self.get_pipeline_jobs,
            invalidate_pipeline=self.invalidate_pipeline,
            emit_change=self.emit_change,
            max_operations=max_operations,
            hint_timeout=hint_timeout,
        )


@pytest.mark.asyncio
async def test_capabilities_preserve_native_job_cancel_difference() -> None:
    github = Harness(FakeClient(supports_job_cancel=False)).service()
    gitlab = Harness(FakeClient(supports_job_cancel=True)).service()

    github_capabilities = await github.capabilities(REPOSITORY)
    gitlab_capabilities = await gitlab.capabilities(REPOSITORY)

    assert github_capabilities.retry_pipeline is True
    assert github_capabilities.cancel_pipeline is True
    assert github_capabilities.retry_job is True
    assert github_capabilities.cancel_job is False
    assert gitlab_capabilities.cancel_job is True


@pytest.mark.asyncio
async def test_actual_github_and_gitlab_capability_matrix_uses_native_flags() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(500, json={"message": "unused"})
    )
    async with (
        httpx.AsyncClient(transport=transport) as github_http,
        httpx.AsyncClient(transport=transport) as gitlab_http,
    ):
        github_client = GitHubClient(
            ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com"),
            github_http,
        )
        gitlab_client = GitLabClient(
            ForgeHost("gitlab.com", ForgeType.GITLAB, "https://gitlab.com/api/v4"),
            gitlab_http,
        )

        async def get_jobs(_pipeline: PipelineRef) -> Sequence[PipelineJob]:
            return ()

        def emit_change(_kind: ServiceEventKind, _resource: PipelineRef | None) -> None:
            return None

        async def github_get_client(
            _repository: RepositoryRef, _operation: str
        ) -> ForgeClient:
            return github_client

        async def gitlab_get_client(
            _repository: RepositoryRef, _operation: str
        ) -> ForgeClient:
            return gitlab_client

        github = CIMutationService(
            get_client=github_get_client,
            get_pipeline_jobs=get_jobs,
            emit_change=emit_change,
        )
        gitlab = CIMutationService(
            get_client=gitlab_get_client,
            get_pipeline_jobs=get_jobs,
            emit_change=emit_change,
        )

        assert (await github.capabilities(REPOSITORY)).cancel_job is False
        assert (await gitlab.capabilities(REPOSITORY)).cancel_job is True


@pytest.mark.asyncio
async def test_default_pipeline_retry_is_reported_unsupported() -> None:
    service = Harness(DefaultPipelineRetryClient()).service()

    capabilities = await service.capabilities(REPOSITORY)

    assert capabilities.retry_pipeline is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected_call"),
    [
        (
            RetryPipelineCommand("retry-pipeline", PIPELINE_TARGET),
            ("retry_pipeline", "acme/widgets", 101),
        ),
        (
            CancelPipelineCommand("cancel-pipeline", PIPELINE_TARGET),
            ("cancel_pipeline", "acme/widgets", 101),
        ),
        (
            RetryJobCommand("retry-job", JOB_TARGET),
            ("retry_job", "acme/widgets", 202),
        ),
        (
            CancelJobCommand("cancel-job", JOB_TARGET),
            ("cancel_job", "acme/widgets", 202),
        ),
    ],
)
async def test_dispatches_supported_typed_commands(
    command, expected_call: tuple[str, str, int]
) -> None:
    harness = Harness(FakeClient(supports_job_cancel=True))

    receipt = await harness.service().execute(command)

    assert receipt.outcome == CIMutationOutcome.KNOWN
    assert receipt.error is None
    assert receipt.resync_required is False
    assert harness.client.calls == [expected_call]
    assert harness.job_reads == [PIPELINE]
    assert harness.invalidations == [PIPELINE]
    assert harness.events == [(ServiceEventKind.PIPELINE_CHANGED, PIPELINE)]


@pytest.mark.asyncio
async def test_unissued_repository_is_rejected_before_read_or_dispatch() -> None:
    harness = Harness()
    target = PipelineMutationTarget(PipelineRef(OTHER_REPOSITORY, 101))

    with pytest.raises(ServiceError) as raised:
        await harness.service().execute(RetryPipelineCommand("forged", target))

    assert raised.value.code == ServiceErrorCode.RESOURCE_NOT_ISSUED
    assert harness.job_reads == []
    assert harness.client.calls == []


def test_job_target_rejects_cross_repository_binding() -> None:
    with pytest.raises(ValueError, match="same repository"):
        JobMutationTarget(PIPELINE, JobRef(OTHER_REPOSITORY, 202))


@pytest.mark.asyncio
async def test_pipeline_endpoint_failure_is_known_before_dispatch() -> None:
    harness = Harness()
    harness.jobs_error = NotFoundError("secret response")

    with pytest.raises(ServiceError) as raised:
        await harness.service().execute(
            CancelPipelineCommand("stale-pipeline", PIPELINE_TARGET)
        )

    assert raised.value.code == ServiceErrorCode.NOT_FOUND
    assert "secret" not in raised.value.message
    assert harness.client.calls == []


@pytest.mark.asyncio
async def test_empty_pipeline_still_proves_pipeline_target() -> None:
    harness = Harness(jobs=[])

    receipt = await harness.service().execute(
        RetryPipelineCommand("empty-pipeline", PIPELINE_TARGET)
    )

    assert receipt.outcome == CIMutationOutcome.KNOWN
    assert harness.job_reads == [PIPELINE]
    assert harness.client.calls == [("retry_pipeline", "acme/widgets", 101)]


@pytest.mark.asyncio
async def test_job_requires_fresh_membership_immediately_before_dispatch() -> None:
    harness = Harness(jobs=[Harness._job(999)])

    with pytest.raises(ServiceError) as raised:
        await harness.service().execute(RetryJobCommand("stale-job", JOB_TARGET))

    assert raised.value.code == ServiceErrorCode.RESOURCE_NOT_ISSUED
    assert harness.job_reads == [PIPELINE]
    assert harness.client.calls == []


@pytest.mark.asyncio
async def test_unsupported_job_cancel_stops_before_target_read() -> None:
    harness = Harness(FakeClient(supports_job_cancel=False))

    with pytest.raises(ServiceError) as raised:
        await harness.service().execute(CancelJobCommand("cancel-job", JOB_TARGET))

    assert raised.value.code == ServiceErrorCode.INVALID_INPUT
    assert harness.job_reads == []
    assert harness.client.calls == []


@pytest.mark.asyncio
async def test_exact_duplicate_returns_retained_receipt_without_replay() -> None:
    harness = Harness()
    service = harness.service()
    command = RetryPipelineCommand("same-operation", PIPELINE_TARGET)

    first = await service.execute(command)
    second = await service.execute(command)

    assert second is first
    assert harness.client.calls == [("retry_pipeline", "acme/widgets", 101)]


@pytest.mark.asyncio
async def test_concurrent_duplicate_coalesces_one_remote_dispatch() -> None:
    client = FakeClient()
    client.release = asyncio.Event()
    harness = Harness(client)
    service = harness.service()
    command = RetryPipelineCommand("concurrent", PIPELINE_TARGET)

    first_task = asyncio.create_task(service.execute(command))
    await client.started.wait()
    second_task = asyncio.create_task(service.execute(command))
    await asyncio.sleep(0)
    client.release.set()

    first, second = await asyncio.gather(first_task, second_task)
    assert first is second
    assert client.calls == [("retry_pipeline", "acme/widgets", 101)]


@pytest.mark.asyncio
async def test_operation_id_reuse_for_another_target_is_rejected() -> None:
    harness = Harness()
    service = harness.service()
    await service.execute(RetryPipelineCommand("bound-id", PIPELINE_TARGET))
    other_target = PipelineMutationTarget(PipelineRef(REPOSITORY, 303))

    with pytest.raises(ServiceError) as raised:
        await service.execute(RetryPipelineCommand("bound-id", other_target))

    assert raised.value.code == ServiceErrorCode.CONFLICT
    assert harness.client.calls == [("retry_pipeline", "acme/widgets", 101)]


@pytest.mark.asyncio
async def test_full_ledger_rejects_new_operation_without_eviction() -> None:
    harness = Harness()
    service = harness.service(max_operations=1)
    first = RetryPipelineCommand("first", PIPELINE_TARGET)
    await service.execute(first)

    with pytest.raises(ServiceError) as raised:
        await service.execute(CancelPipelineCommand("second", PIPELINE_TARGET))

    assert raised.value.code == ServiceErrorCode.CONFLICT
    assert raised.value.retryable is True
    assert await service.execute(first) == await service.receipt("first")
    assert len(harness.client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (AuthError("Bearer secret"), ServiceErrorCode.AUTHENTICATION_FAILED),
        (ConflictError("server details"), ServiceErrorCode.CONFLICT),
    ],
)
async def test_known_remote_rejection_is_typed_and_does_not_refresh(
    error: Exception, expected_code: ServiceErrorCode
) -> None:
    client = FakeClient()
    client.error = error
    harness = Harness(client)
    service = harness.service()
    command = CancelPipelineCommand("known-rejection", PIPELINE_TARGET)

    with pytest.raises(ServiceError) as raised:
        await service.execute(command)
    with pytest.raises(ServiceError) as duplicate:
        await service.execute(command)

    assert raised.value.code == expected_code
    assert duplicate.value.code == expected_code
    assert harness.invalidations == []
    assert harness.events == []
    assert len(client.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [NetworkError("Bearer secret"), ForgeError("HTTP 500 private body")],
)
async def test_ambiguous_dispatch_failure_returns_unknown_and_refreshes(
    error: Exception,
) -> None:
    client = FakeClient()
    client.error = error
    harness = Harness(client)
    service = harness.service()
    command = RetryPipelineCommand("ambiguous", PIPELINE_TARGET)

    receipt = await service.execute(command)

    assert receipt.outcome == CIMutationOutcome.UNKNOWN
    assert receipt.resync_required is False
    assert receipt.error is not None
    assert "secret" not in receipt.error.message
    assert "private" not in receipt.error.message
    assert await service.execute(command) is receipt
    assert await service.receipt("ambiguous") is receipt
    assert len(client.calls) == 1
    assert harness.invalidations == [PIPELINE]
    assert harness.events == [(ServiceEventKind.PIPELINE_CHANGED, PIPELINE)]


@pytest.mark.asyncio
async def test_cancellation_after_dispatch_retains_unknown_without_replay() -> None:
    client = FakeClient()
    client.release = asyncio.Event()
    harness = Harness(client)
    service = harness.service()
    command = CancelPipelineCommand("cancelled", PIPELINE_TARGET)
    task = asyncio.create_task(service.execute(command))
    await client.started.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    receipt = await service.receipt("cancelled")
    assert receipt is not None
    assert receipt.outcome == CIMutationOutcome.UNKNOWN
    assert await service.execute(command) is receipt
    assert len(client.calls) == 1
    assert harness.invalidations == [PIPELINE]
    assert harness.events == [(ServiceEventKind.PIPELINE_CHANGED, PIPELINE)]


@pytest.mark.asyncio
async def test_hint_failures_do_not_erase_known_success() -> None:
    harness = Harness()
    harness.invalidate_error = RuntimeError("cache failure")
    harness.emit_error = RuntimeError("event failure")
    service = harness.service()

    receipt = await service.execute(
        RetryPipelineCommand("hint-failures", PIPELINE_TARGET)
    )

    assert receipt.outcome == CIMutationOutcome.KNOWN
    assert receipt.resync_required is True
    assert await service.receipt("hint-failures") is receipt
    assert harness.invalidations == [PIPELINE]
    assert harness.events == [
        (ServiceEventKind.PIPELINE_CHANGED, PIPELINE),
        (ServiceEventKind.RESYNC_REQUIRED, None),
    ]


@pytest.mark.asyncio
async def test_cancellation_resistant_invalidation_cleanup_is_bounded() -> None:
    harness = Harness()
    harness.invalidate_release = asyncio.Event()
    harness.resist_invalidate_cancellation = True
    service = harness.service(hint_timeout=0.01)

    async with asyncio.timeout(0.1):
        receipt = await service.execute(
            CancelPipelineCommand("blocked-invalidation", PIPELINE_TARGET)
        )

    assert receipt.outcome == CIMutationOutcome.KNOWN
    assert receipt.resync_required is True
    assert harness.events == [
        (ServiceEventKind.PIPELINE_CHANGED, PIPELINE),
        (ServiceEventKind.RESYNC_REQUIRED, None),
    ]
    harness.invalidate_release.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation_id", ["", " space", "a" * 129, "bad/slash"])
async def test_invalid_operation_id_is_safe_predispatch_rejection(
    operation_id: str,
) -> None:
    harness = Harness()

    with pytest.raises(ServiceError) as raised:
        await harness.service().execute(
            RetryPipelineCommand(operation_id, PIPELINE_TARGET)
        )

    assert raised.value.code == ServiceErrorCode.INVALID_INPUT
    assert harness.get_client_calls == []


@pytest.mark.asyncio
async def test_runtime_malformed_target_is_safe_predispatch_rejection() -> None:
    harness = Harness()
    command = RetryPipelineCommand("malformed", cast(PipelineMutationTarget, None))

    with pytest.raises(ServiceError) as raised:
        await harness.service().execute(command)

    assert raised.value.code == ServiceErrorCode.INVALID_INPUT
    assert harness.get_client_calls == []
