"""Production sidecar coverage for capability-gated CI mutations."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from pathlib import Path
from typing import cast

import pytest

from tongs.config import Config
from tongs.desktop.protocol.ci_operations import CI_CAPABILITY, CIOperations
from tongs.desktop.protocol.messages import JsonObject, ProtocolError, ProtocolErrorCode
from tongs.desktop.protocol.server import DesktopSidecarServer, RequestContext
from tongs.desktop.protocol.state import HandleKind, HandleRegistry
from tongs.errors import NetworkError, NotFoundError
from tongs.forges.base import ForgeClient
from tongs.forges.models import CIStatus, ForgeHost, Pipeline, PipelineJob
from tongs.plugins.desktop import DesktopCancellation
from tongs.plugins.desktop_registry import DesktopPluginRegistry
from tongs.scanner.repo import ForgeType
from tongs.services import (
    ApplicationSession,
    CIMutationCommand,
    CIMutationReceipt,
    CIMutationService,
    JobRef,
    PipelineRef,
    RepositoryRef,
    ServiceError,
    ServiceErrorCode,
    ServiceEvent,
)

REPOSITORY = RepositoryRef("gitlab.example.com", "team/project")
OTHER_REPOSITORY = RepositoryRef("gitlab.example.com", "team/other")
PIPELINE = PipelineRef(REPOSITORY, 101)
OTHER_PIPELINE = PipelineRef(REPOSITORY, 102)
JOB = JobRef(REPOSITORY, 201)


class _FakeClient:
    def __init__(self, *, supports_job_cancel: bool = True) -> None:
        self.supports_job_cancel = supports_job_cancel
        self.jobs: dict[int, list[PipelineJob]] = {
            PIPELINE.pipeline_id: [
                PipelineJob(JOB.job_id, "test", "verify", CIStatus.RUNNING)
            ],
            OTHER_PIPELINE.pipeline_id: [],
        }
        self.calls: list[tuple[str, str, int]] = []
        self.error: Exception | None = None
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def retry_pipeline(self, project: str, item_id: int) -> None:
        await self._write("retry_pipeline", project, item_id)

    async def cancel_pipeline(self, project: str, item_id: int) -> None:
        await self._write("cancel_pipeline", project, item_id)

    async def retry_job(self, project: str, item_id: int) -> None:
        await self._write("retry_job", project, item_id)

    async def cancel_job(self, project: str, item_id: int) -> None:
        await self._write("cancel_job", project, item_id)

    async def _write(self, action: str, project: str, item_id: int) -> None:
        self.calls.append((action, project, item_id))
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error


class _Session:
    def __init__(self, client: _FakeClient | None = None) -> None:
        self.client = client or _FakeClient()
        self.job_reads: list[PipelineRef] = []
        self.issued = {REPOSITORY}
        self.ci_mutations = CIMutationService(
            get_client=self._get_client,
            get_pipeline_jobs=self._get_pipeline_jobs,
            emit_change=lambda _kind, _resource: None,
        )

    async def _get_client(
        self, repository: RepositoryRef, _operation: str
    ) -> ForgeClient:
        if repository not in self.issued:
            raise ServiceError(
                ServiceErrorCode.RESOURCE_NOT_ISSUED,
                "The repository was not issued by this session.",
            )
        return cast(ForgeClient, self.client)

    async def _get_pipeline_jobs(self, pipeline: PipelineRef) -> Sequence[PipelineJob]:
        self.job_reads.append(pipeline)
        return tuple(self.client.jobs.get(pipeline.pipeline_id, ()))


def _context(request_id: str = "request") -> RequestContext:
    return RequestContext(request_id, DesktopCancellation())


def _operations(
    session: _Session | None = None,
) -> tuple[CIOperations, _Session, dict[str, str]]:
    actual_session = session or _Session()
    handles = HandleRegistry()
    repository = handles.issue(HandleKind.REPOSITORY, REPOSITORY)
    pipeline = handles.issue(HandleKind.PIPELINE, PIPELINE)
    other_pipeline = handles.issue(HandleKind.PIPELINE, OTHER_PIPELINE)
    job = handles.issue(HandleKind.JOB, JOB, parent=PIPELINE)
    operations = CIOperations(
        session=cast(object, actual_session),  # type: ignore[arg-type]
        handles=handles,
    )
    return (
        operations,
        actual_session,
        {
            "repository": repository,
            "pipeline": pipeline,
            "other_pipeline": other_pipeline,
            "job": job,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("job_cancel", [False, True])
async def test_capabilities_expose_only_opaque_repository_and_native_flags(
    job_cancel: bool,
) -> None:
    operations, _session, handles = _operations(
        _Session(_FakeClient(supports_job_cancel=job_cancel))
    )

    result = await operations.capabilities(
        {"repository": handles["repository"]}, _context()
    )

    assert result == {
        "repository": handles["repository"],
        "capabilities": {
            "retry_pipeline": True,
            "cancel_pipeline": True,
            "retry_job": True,
            "cancel_job": job_cancel,
        },
    }
    assert "gitlab.example.com" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "params", "expected_action", "expected_id"),
    [
        ("retry_pipeline", "pipeline", "retry_pipeline", 101),
        ("cancel_pipeline", "pipeline", "cancel_pipeline", 101),
        ("retry_job", "job", "retry_job", 201),
        ("cancel_job", "job", "cancel_job", 201),
    ],
)
async def test_typed_mutations_return_safe_receipts(
    method: str, params: str, expected_action: str, expected_id: int
) -> None:
    operations, session, handles = _operations()
    payload: JsonObject = {
        "operation_id": f"operation:{expected_action}",
        "pipeline": handles["pipeline"],
    }
    if params == "job":
        payload["job"] = handles["job"]

    result = await getattr(operations, method)(payload, _context())

    assert result == {
        "operation_id": f"operation:{expected_action}",
        "action": expected_action,
        "outcome": "known",
        "error": None,
        "resync_required": False,
    }
    assert session.client.calls == [(expected_action, "team/project", expected_id)]
    assert "gitlab.example.com" not in json.dumps(result)


@pytest.mark.asyncio
async def test_job_handle_cannot_be_retargeted_to_same_repository_pipeline() -> None:
    operations, session, handles = _operations()

    with pytest.raises(ProtocolError) as raised:
        await operations.retry_job(
            {
                "operation_id": "retarget",
                "pipeline": handles["other_pipeline"],
                "job": handles["job"],
            },
            _context(),
        )

    assert raised.value.code is ProtocolErrorCode.INVALID_HANDLE
    assert session.job_reads == []
    assert session.client.calls == []


@pytest.mark.asyncio
async def test_fresh_membership_change_rejects_job_before_write() -> None:
    operations, session, handles = _operations()
    session.client.jobs[PIPELINE.pipeline_id] = []

    with pytest.raises(ServiceError) as raised:
        await operations.cancel_job(
            {
                "operation_id": "stale-job",
                "pipeline": handles["pipeline"],
                "job": handles["job"],
            },
            _context(),
        )

    assert raised.value.code is ServiceErrorCode.RESOURCE_NOT_ISSUED
    assert session.job_reads == [PIPELINE]
    assert session.client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("forged", [True, 1.0, 7, "unissued"])
async def test_forged_job_handle_types_fail_before_write(forged: object) -> None:
    operations, session, handles = _operations()

    with pytest.raises(ProtocolError) as raised:
        await operations.retry_job(
            {
                "operation_id": "forged-job",
                "pipeline": handles["pipeline"],
                "job": cast(str, forged),
            },
            _context(),
        )

    assert raised.value.code is ProtocolErrorCode.INVALID_HANDLE
    assert session.job_reads == []
    assert session.client.calls == []


@pytest.mark.asyncio
async def test_cross_session_and_wrong_kind_handles_fail_before_write() -> None:
    operations, session, handles = _operations()
    foreign = HandleRegistry().issue(HandleKind.PIPELINE, PIPELINE)

    with pytest.raises(ProtocolError) as cross_session:
        await operations.retry_pipeline(
            {"operation_id": "cross", "pipeline": foreign}, _context()
        )
    with pytest.raises(ProtocolError) as wrong_kind:
        await operations.retry_pipeline(
            {
                "operation_id": "wrong-kind",
                "pipeline": handles["repository"],
            },
            _context(),
        )

    assert cross_session.value.code is ProtocolErrorCode.INVALID_HANDLE
    assert wrong_kind.value.code is ProtocolErrorCode.WRONG_HANDLE_KIND
    assert session.client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"operation_id": "extra", "pipeline": "x", "project": "secret"},
        {"operation_id": True, "pipeline": "x"},
        {"operation_id": 1.0, "pipeline": "x"},
        {"operation_id": "bad/slash", "pipeline": "x"},
    ],
)
async def test_malformed_payloads_fail_before_handle_or_write(
    payload: JsonObject,
) -> None:
    operations, session, _handles = _operations()

    with pytest.raises((ProtocolError, ServiceError)):
        await operations.retry_pipeline(payload, _context())

    assert session.job_reads == []
    assert session.client.calls == []


@pytest.mark.asyncio
async def test_repeated_id_coalesces_and_conflicting_target_is_rejected() -> None:
    operations, session, handles = _operations()
    params: JsonObject = {
        "operation_id": "stable-id",
        "pipeline": handles["pipeline"],
    }

    first = await operations.retry_pipeline(params, _context("first-request"))
    duplicate = await operations.retry_pipeline(params, _context("other-request"))
    with pytest.raises(ServiceError) as conflict:
        await operations.cancel_pipeline(params, _context("third-request"))

    assert duplicate == first
    assert conflict.value.code is ServiceErrorCode.CONFLICT
    assert session.client.calls == [("retry_pipeline", "team/project", 101)]


@pytest.mark.asyncio
async def test_unknown_receipt_is_retained_and_never_automatically_replayed() -> None:
    operations, session, handles = _operations()
    session.client.error = NetworkError("private upstream detail")
    params: JsonObject = {
        "operation_id": "ambiguous",
        "pipeline": handles["pipeline"],
    }

    first = await operations.retry_pipeline(params, _context("write-request"))
    duplicate = await operations.retry_pipeline(params, _context("retry-request"))
    queried = await operations.receipt(
        {
            "operation_id": "ambiguous",
            "action": "retry_pipeline",
            "pipeline": handles["pipeline"],
        },
        _context("receipt-request"),
    )

    assert first == duplicate == queried["receipt"]  # type: ignore[index]
    assert first["outcome"] == "unknown"  # type: ignore[index]
    assert first["error"]["code"] == "network_unavailable"  # type: ignore[index]
    assert "private" not in json.dumps(first)
    assert len(session.client.calls) == 1


@pytest.mark.asyncio
async def test_known_rejection_removes_internal_service_details() -> None:
    operations, session, handles = _operations()
    session.client.error = NotFoundError("private upstream detail")

    with pytest.raises(ServiceError) as raised:
        await operations.retry_pipeline(
            {"operation_id": "known-rejection", "pipeline": handles["pipeline"]},
            _context(),
        )

    assert raised.value.code is ServiceErrorCode.NOT_FOUND
    assert raised.value.details == ()
    assert "private" not in raised.value.message
    assert len(session.client.calls) == 1


@pytest.mark.asyncio
async def test_receipt_requires_matching_opaque_target_and_action() -> None:
    operations, session, handles = _operations()
    await operations.retry_pipeline(
        {"operation_id": "bound", "pipeline": handles["pipeline"]}, _context()
    )

    with pytest.raises(ServiceError) as wrong_action:
        await operations.receipt(
            {
                "operation_id": "bound",
                "action": "cancel_pipeline",
                "pipeline": handles["pipeline"],
            },
            _context(),
        )
    with pytest.raises(ServiceError) as wrong_target:
        await operations.receipt(
            {
                "operation_id": "bound",
                "action": "retry_pipeline",
                "pipeline": handles["other_pipeline"],
            },
            _context(),
        )

    assert wrong_action.value.code is ServiceErrorCode.CONFLICT
    assert wrong_target.value.code is ServiceErrorCode.CONFLICT
    assert len(session.client.calls) == 1


@pytest.mark.asyncio
async def test_receipt_null_is_absent_or_pending_and_does_not_dispatch() -> None:
    operations, session, handles = _operations()

    result = await operations.receipt(
        {
            "operation_id": "not-retained",
            "action": "retry_pipeline",
            "pipeline": handles["pipeline"],
        },
        _context(),
    )

    assert result == {"receipt": None}
    assert session.client.calls == []


@pytest.mark.asyncio
async def test_predispatch_cancel_does_not_admit_or_write() -> None:
    operations, session, handles = _operations()
    context = _context()
    context.cancellation.cancel()

    with pytest.raises(ProtocolError) as raised:
        await operations.cancel_pipeline(
            {"operation_id": "cancel-before", "pipeline": handles["pipeline"]},
            context,
        )

    assert raised.value.details == {"outcome": "not_dispatched"}
    assert session.client.calls == []
    assert await session.ci_mutations.receipt("cancel-before") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "pipeline_key"),
    [
        pytest.param("cancel_pipeline", "pipeline", id="different-action"),
        pytest.param("retry_pipeline", "other_pipeline", id="different-target"),
    ],
)
async def test_cancelled_conflicting_request_cannot_inherit_retained_receipt(
    method: str,
    pipeline_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operations, session, handles = _operations()
    operation_id = "cancelled-conflict"
    await operations.retry_pipeline(
        {"operation_id": operation_id, "pipeline": handles["pipeline"]},
        _context("original-request"),
    )
    original_execute = session.ci_mutations.execute
    entered_execute = asyncio.Event()

    async def tracked_execute(command: CIMutationCommand) -> CIMutationReceipt:
        entered_execute.set()
        return await original_execute(command)

    monkeypatch.setattr(session.ci_mutations, "execute", tracked_execute)
    await session.ci_mutations._operation_lock.acquire()
    context = _context("conflicting-request")
    task = asyncio.create_task(
        getattr(operations, method)(
            {"operation_id": operation_id, "pipeline": handles[pipeline_key]},
            context,
        )
    )
    try:
        await entered_execute.wait()
        await asyncio.sleep(0)
        context.cancellation.cancel()
        await asyncio.sleep(0)
    finally:
        session.ci_mutations._operation_lock.release()

    with pytest.raises(ServiceError) as conflict:
        await task

    assert conflict.value.code is ServiceErrorCode.CONFLICT
    assert conflict.value.details == ()
    assert session.client.calls == [("retry_pipeline", "team/project", 101)]


@pytest.mark.asyncio
async def test_cancel_after_dispatch_returns_unknown_without_replay() -> None:
    client = _FakeClient()
    client.release = asyncio.Event()
    operations, session, handles = _operations(_Session(client))
    context = _context()
    task = asyncio.create_task(
        operations.cancel_pipeline(
            {"operation_id": "cancel-after", "pipeline": handles["pipeline"]},
            context,
        )
    )
    await client.started.wait()

    context.cancellation.cancel()
    result = await task

    assert result["outcome"] == "unknown"  # type: ignore[index]
    assert len(session.client.calls) == 1
    duplicate = await operations.cancel_pipeline(
        {"operation_id": "cancel-after", "pipeline": handles["pipeline"]},
        _context(),
    )
    assert duplicate == result
    assert len(session.client.calls) == 1


class _EOFSession(_Session):
    config = Config()

    async def start(self) -> _EOFSession:
        return self

    async def close(self) -> None:
        await self.ci_mutations.close()

    async def events(self) -> AsyncIterator[ServiceEvent]:
        await asyncio.Future()
        if False:
            yield cast(ServiceEvent, None)


@pytest.mark.asyncio
async def test_eof_settles_dispatched_mutation_as_unknown_without_replay() -> None:
    client = _FakeClient()
    client.release = asyncio.Event()
    session = _EOFSession(client)
    server = DesktopSidecarServer(
        session=cast(ApplicationSession, session),
        plugin_registry=DesktopPluginRegistry(
            entry_point_source=lambda _group: (), host_version="1.0"
        ),
        shutdown_timeout=0.2,
    )
    pipeline = server._handles.issue(HandleKind.PIPELINE, PIPELINE)
    reader = asyncio.StreamReader()
    writer = _Writer()
    running = asyncio.create_task(server.run(reader, writer))
    reader.feed_data(
        _frame(
            "handshake",
            "handshake",
            {
                "protocol_major": 1,
                "core_version": __import__("importlib.metadata").metadata.version(
                    "tongs"
                ),
                "capabilities": [CI_CAPABILITY],
            },
        )
    )
    await writer.response("handshake")
    reader.feed_data(
        _frame(
            "mutation-request",
            "pipelines.cancel",
            {"operation_id": "eof-operation", "pipeline": pipeline},
        )
    )
    await client.started.wait()

    reader.feed_eof()
    await asyncio.wait_for(running, 1)

    record = session.ci_mutations._operations["eof-operation"]
    assert record.receipt is not None
    assert record.receipt.outcome.value == "unknown"
    assert client.calls == [("cancel_pipeline", "team/project", 101)]


class _RoundtripClient(_FakeClient):
    async def list_mrs(
        self, _project: str, state: str = "open", per_page: int = 1
    ) -> list[object]:
        return []

    async def list_pipelines(self, _project: str, per_page: int = 20) -> list[Pipeline]:
        return [Pipeline(101, CIStatus.RUNNING, "main", "abc", "https://ci")]

    async def get_pipeline_jobs(
        self, _project: str, pipeline_id: int
    ) -> list[PipelineJob]:
        return list(self.jobs.get(pipeline_id, ()))


class _RoundtripRegistry:
    def __init__(self, client: _RoundtripClient) -> None:
        self.client = client

    def active_hostnames(self) -> list[str]:
        return [REPOSITORY.hostname]

    def get_host(self, hostname: str) -> ForgeHost | None:
        if hostname == REPOSITORY.hostname:
            return ForgeHost(hostname, ForgeType.GITLAB, "https://unused.invalid")
        return None

    async def get_client(self, _hostname: str) -> ForgeClient:
        return cast(ForgeClient, self.client)

    async def close_all(self) -> None:
        return None


class _IsolatedCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.opened = False
        self.closed = False

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True


class _IsolatedDraftStore(_IsolatedCache):
    async def recover_incomplete_attempts(self) -> tuple[object, ...]:
        return ()


class _Writer:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    def write(self, data: bytes) -> None:
        self.frames.put_nowait(json.loads(data))

    async def drain(self) -> None:
        await asyncio.sleep(0)

    async def next(self) -> dict[str, object]:
        return await asyncio.wait_for(self.frames.get(), 2)

    async def response(self, request_id: str) -> dict[str, object]:
        while True:
            frame = await self.next()
            if frame.get("type") == "response" and frame.get("id") == request_id:
                return frame


def _frame(request_id: str, method: str, params: JsonObject) -> bytes:
    return (
        json.dumps(
            {
                "v": 1,
                "type": "request",
                "id": request_id,
                "method": method,
                "params": params,
            }
        ).encode()
        + b"\n"
    )


@pytest.mark.asyncio
async def test_composed_session_real_ndjson_roundtrip_uses_isolated_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("cache", "config", "data"):
        monkeypatch.setenv(f"XDG_{name.upper()}_HOME", str(tmp_path / name))
    client = _RoundtripClient()
    cache = _IsolatedCache(tmp_path / "cache" / "cache.db")
    drafts = _IsolatedDraftStore(tmp_path / "data" / "drafts.db")
    session = ApplicationSession(
        config=Config(),
        cache=cache,
        draft_store=cast(object, drafts),  # type: ignore[arg-type]
        forge_registry=cast(object, _RoundtripRegistry(client)),  # type: ignore[arg-type]
    )
    server = DesktopSidecarServer(
        session=session,
        plugin_registry=DesktopPluginRegistry(
            entry_point_source=lambda _group: (), host_version="1.0"
        ),
    )
    reader = asyncio.StreamReader()
    writer = _Writer()
    running = asyncio.create_task(server.run(reader, writer))
    try:
        reader.feed_data(
            _frame(
                "handshake-request",
                "handshake",
                {
                    "protocol_major": 1,
                    "core_version": __import__("importlib.metadata").metadata.version(
                        "tongs"
                    ),
                    "capabilities": [CI_CAPABILITY],
                },
            )
        )
        handshake = await writer.next()
        assert CI_CAPABILITY in handshake["result"]["accepted_capabilities"]  # type: ignore[index]
        methods = handshake["result"]["methods"]  # type: ignore[index]
        assert methods == [*sorted(server._operations), "shutdown"]
        assert server._operations["ci.capabilities"].mutation is False
        assert server._operations["ci.receipt"].mutation is False
        for method in (
            "pipelines.retry",
            "pipelines.cancel",
            "jobs.retry",
            "jobs.cancel",
        ):
            assert server._operations[method].mutation is True

        reader.feed_data(
            _frame(
                "open-request",
                "repositories.open",
                {
                    "hostname": REPOSITORY.hostname,
                    "project_path": REPOSITORY.project_path,
                },
            )
        )
        opened = await writer.next()
        repository_handle = opened["result"]["handle"]  # type: ignore[index]
        reader.feed_data(
            _frame(
                "pipelines-request",
                "pipelines.list",
                {"repository": repository_handle},
            )
        )
        pipelines = await writer.next()
        pipeline_handle = pipelines["result"]["pipelines"][0]["handle"]  # type: ignore[index]
        reader.feed_data(
            _frame("jobs-request", "jobs.list", {"pipeline": pipeline_handle})
        )
        jobs = await writer.next()
        job_handle = jobs["result"]["jobs"][0]["handle"]  # type: ignore[index]
        reader.feed_data(
            _frame(
                "transport-request",
                "jobs.retry",
                {
                    "operation_id": "application-operation",
                    "pipeline": pipeline_handle,
                    "job": job_handle,
                },
            )
        )
        mutation = await writer.response("transport-request")

        assert "result" in mutation, mutation
        assert mutation["result"]["operation_id"] == "application-operation"  # type: ignore[index]
        assert mutation["result"]["outcome"] == "known"  # type: ignore[index]
        assert client.calls == [("retry_job", "team/project", 201)]
        assert cache.path == tmp_path / "cache" / "cache.db"
        assert drafts.path == tmp_path / "data" / "drafts.db"
        assert cache.opened and drafts.opened

        reader.feed_data(_frame("shutdown-request", "shutdown", {}))
        assert (await writer.next())["result"] == {"accepted": True}
        await asyncio.wait_for(running, 2)
        assert cache.closed and drafts.closed
    finally:
        if not running.done():
            reader.feed_eof()
            with suppress(BaseException):
                await asyncio.wait_for(running, 2)
