from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from importlib.metadata import version
from pathlib import Path
from typing import cast

import pytest

from tongs.config import Config
from tongs.desktop.protocol.messages import JsonObject
from tongs.desktop.protocol.server import (
    DesktopSidecarServer,
    RequestContext,
    _EventBuffer,
)
from tongs.desktop.protocol.state import HandleKind
from tongs.plugins.desktop import (
    DesktopCallContext,
    DesktopCancellation,
    DesktopCompatibility,
    DesktopMethod,
    DesktopPluginCallResult,
    DesktopPluginManifest,
    DesktopReadKind,
    freeze_json_object,
)
from tongs.plugins.desktop_registry import DesktopPluginRegistry
from tongs.scanner.repo import ForgeType
from tongs.services import JobRef, RepositoryRef, RepositorySnapshot, ServiceEvent


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


class _QueueWriter:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    def write(self, data: bytes) -> None:
        self.frames.put_nowait(json.loads(data))

    async def drain(self) -> None:
        await asyncio.sleep(0)


class _BlockingWriter(_QueueWriter):
    def __init__(self) -> None:
        super().__init__()
        self.block = False
        self.drain_started = asyncio.Event()
        self.drain_completed = asyncio.Event()
        self.release = asyncio.Event()

    async def drain(self) -> None:
        if not self.block:
            self.drain_completed.set()
            return
        self.drain_started.set()
        await self.release.wait()


class _FakeSession:
    def __init__(self) -> None:
        self.config = Config()
        self.started = False
        self.closed = False
        self.read_started = asyncio.Event()
        self.read_cancelled = asyncio.Event()
        self.release_read = asyncio.Event()
        self.log = ""

    async def start(self) -> _FakeSession:
        self.started = True
        return self

    async def close(self) -> None:
        self.closed = True

    async def events(self) -> AsyncIterator[ServiceEvent]:
        await asyncio.Future()
        yield cast(ServiceEvent, None)

    async def discover_repositories(self) -> tuple[RepositorySnapshot, ...]:
        self.read_started.set()
        try:
            await self.release_read.wait()
        except asyncio.CancelledError:
            self.read_cancelled.set()
            raise
        return (
            RepositorySnapshot(
                RepositoryRef("git.example.com", "team/project"),
                "project",
                ForgeType.GITLAB,
            ),
        )

    async def get_job_log(self, _job: JobRef) -> str:
        return self.log


def _registry() -> DesktopPluginRegistry:
    return DesktopPluginRegistry(
        entry_point_source=lambda _group: (), host_version="1.0"
    )


@pytest.mark.asyncio
async def test_read_cancellation_and_shutdown_drain_pending_request() -> None:
    session = _FakeSession()
    server = DesktopSidecarServer(
        session=cast(object, session),
        plugin_registry=_registry(),
        shutdown_timeout=0.2,
    )
    reader = asyncio.StreamReader()
    writer = _QueueWriter()
    task = asyncio.create_task(server.run(reader, writer))

    reader.feed_data(
        _frame(
            "handshake",
            "handshake",
            {
                "protocol_major": 1,
                "core_version": version("tongs"),
                "capabilities": [],
            },
        )
    )
    assert (await writer.frames.get())["id"] == "handshake"
    reader.feed_data(_frame("read", "repositories.discover", {}))
    await session.read_started.wait()
    reader.feed_data(b'{"v":1,"type":"cancel","id":"read"}\n')
    cancelled = await asyncio.wait_for(writer.frames.get(), 1)
    assert cancelled["id"] == "read"
    assert cancelled["error"]["code"] == "request_cancelled"  # type: ignore[index]

    reader.feed_data(_frame("shutdown", "shutdown", {}))
    assert (await writer.frames.get())["result"] == {"accepted": True}
    await task
    assert session.closed
    assert not server._pending


@pytest.mark.asyncio
async def test_eof_cancels_pending_read_and_closes_session() -> None:
    session = _FakeSession()
    server = DesktopSidecarServer(
        session=cast(object, session),
        plugin_registry=_registry(),
        shutdown_timeout=0.2,
    )
    reader = asyncio.StreamReader()
    writer = _QueueWriter()
    task = asyncio.create_task(server.run(reader, writer))
    reader.feed_data(
        _frame(
            "handshake",
            "handshake",
            {
                "protocol_major": 1,
                "core_version": version("tongs"),
                "capabilities": [],
            },
        )
    )
    await writer.frames.get()
    reader.feed_data(_frame("read", "repositories.discover", {}))
    await session.read_started.wait()

    reader.feed_eof()
    cancelled = await asyncio.wait_for(writer.frames.get(), 1)
    assert cancelled["id"] == "read"
    assert cancelled["error"]["code"] == "request_cancelled"  # type: ignore[index]
    await task
    assert session.closed
    assert not server._pending


@pytest.mark.asyncio
async def test_repository_read_returns_only_session_handle() -> None:
    session = _FakeSession()
    session.release_read.set()
    server = DesktopSidecarServer(
        session=cast(object, session), plugin_registry=_registry()
    )
    result = await server._repositories_discover(
        {}, RequestContext("read", DesktopCancellation())
    )
    repository = cast(dict[str, object], result)["repositories"][0]  # type: ignore[index]

    assert set(repository) == {"handle", "display_name", "forge_type"}
    assert "git.example.com" not in json.dumps(repository)


@pytest.mark.asyncio
async def test_cancel_after_response_commit_does_not_emit_duplicate_response() -> None:
    session = _FakeSession()
    session.release_read.set()
    server = DesktopSidecarServer(
        session=cast(object, session), plugin_registry=_registry(), shutdown_timeout=1
    )
    reader = asyncio.StreamReader()
    writer = _BlockingWriter()
    task = asyncio.create_task(server.run(reader, writer))
    reader.feed_data(
        _frame(
            "handshake",
            "handshake",
            {
                "protocol_major": 1,
                "core_version": version("tongs"),
                "capabilities": [],
            },
        )
    )
    await writer.frames.get()
    await writer.drain_completed.wait()
    writer.block = True
    reader.feed_data(_frame("read", "repositories.discover", {}))
    response = await writer.frames.get()
    assert response["id"] == "read"
    await writer.drain_started.wait()

    reader.feed_data(b'{"v":1,"type":"cancel","id":"read"}\n')
    await asyncio.sleep(0)
    assert server._pending["read"].context.response_started
    assert not server._pending["read"].task.cancelled()
    writer.release.set()
    while "read" in server._pending:
        await asyncio.sleep(0)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(writer.frames.get(), 0.01)

    writer.block = False
    reader.feed_data(_frame("shutdown", "shutdown", {}))
    await writer.frames.get()
    await task


@pytest.mark.asyncio
async def test_event_overflow_replaces_stale_events_with_resync_marker() -> None:
    buffer = _EventBuffer(max_events=1)
    await buffer.publish("service.changed", {"value": 1})
    await buffer.publish("service.changed", {"value": 2})

    event = await buffer.get()
    assert event is not None
    assert event.name == "protocol.resync_required"
    assert event.data == {"reason": "queue_overflow"}


@pytest.mark.asyncio
async def test_job_logs_are_revision_identified_and_paged() -> None:
    session = _FakeSession()
    session.log = "🙂" * 200_000
    server = DesktopSidecarServer(
        session=cast(object, session), plugin_registry=_registry()
    )
    job = JobRef(RepositoryRef("git.example.com", "team/project"), 2)
    handle = server._handles.issue(HandleKind.JOB, job)

    first = cast(
        dict[str, object],
        await server._logs_open(
            {"job": handle, "max_items": 1},
            RequestContext("log", DesktopCancellation()),
        ),
    )

    assert first["resource"] == handle
    assert first["revision"] == {
        "sha256": "5e3dbae7dab449a0a0dbb5d14887c659dd69eff1a6ee58f2a46ef7d7f243fe29",
        "byte_count": 800_000,
    }
    assert first["next_cursor"] == 1
    second = await server._logs_page(
        {"snapshot": first["snapshot_id"], "resource": handle, "cursor": 1},
        RequestContext("page", DesktopCancellation()),
    )
    assert cast(dict[str, object], second)["entries"]


class _BoundRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    async def call(
        self,
        plugin_id: str,
        method: str,
        params: object,
        _context: DesktopCallContext,
    ) -> DesktopPluginCallResult:
        self.calls.append((plugin_id, method, params))
        return DesktopPluginCallResult()


@pytest.mark.asyncio
async def test_plugin_facade_invoke_cannot_redirect_plugin() -> None:
    registry = _BoundRegistry()
    server = DesktopSidecarServer(
        session=cast(object, _FakeSession()),
        plugin_registry=cast(DesktopPluginRegistry, registry),
    )
    manifest = DesktopPluginManifest(
        "alpha",
        "Alpha",
        "1.0",
        DesktopCompatibility(1),
        (),
        (),
        methods=(DesktopMethod("run"),),
    )
    facade = server._facade_for_plugin("alpha", manifest)

    await facade.invoke(
        "run",
        freeze_json_object({"plugin_id": "beta"}),
        DesktopCallContext("invoke", DesktopCancellation()),
    )

    assert registry.calls[0][0:2] == ("alpha", "run")


class _TrackingCancellation(DesktopCancellation):
    def __init__(self) -> None:
        super().__init__()
        self.wait_started = asyncio.Event()
        self.wait_finished = asyncio.Event()

    async def wait(self) -> None:
        self.wait_started.set()
        try:
            await super().wait()
        finally:
            self.wait_finished.set()


@pytest.mark.asyncio
async def test_plugin_read_outer_cancellation_drains_service_and_waiter_tasks() -> None:
    session = _FakeSession()
    server = DesktopSidecarServer(
        session=cast(object, session),
        plugin_registry=_registry(),
        shutdown_timeout=0.2,
    )
    manifest = DesktopPluginManifest(
        "alpha",
        "Alpha",
        "1.0",
        DesktopCompatibility(1),
        (),
        (),
        reads=(DesktopReadKind.REPOSITORIES,),
    )
    facade = server._facade_for_plugin("alpha", manifest)
    cancellation = _TrackingCancellation()
    read = asyncio.create_task(
        facade.read(
            DesktopReadKind.REPOSITORIES,
            freeze_json_object({}),
            cancellation,
        )
    )
    await session.read_started.wait()
    await cancellation.wait_started.wait()

    read.cancel()
    with pytest.raises(asyncio.CancelledError):
        await read

    assert cancellation.cancelled
    assert session.read_cancelled.is_set()
    assert cancellation.wait_finished.is_set()


def test_actual_sidecar_exits_cleanly_on_eof() -> None:
    completed = subprocess.run(
        [sys.executable, "-E", "-P", "-m", "tongs.desktop.sidecar"],
        input=b"",
        capture_output=True,
        cwd="/tmp",
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == b""
    assert completed.stderr == b""


def test_actual_sidecar_redirects_python_and_native_stdout_from_protocol() -> None:
    script = """
import os
from tongs.desktop import sidecar
from tongs.desktop.protocol.messages import encode_response

class FixtureServer:
    async def run(self, _reader, writer):
        os.write(1, b'native-contamination\\n')
        print('python-contamination')
        writer.write(encode_response('probe', result={'ok': True}))
        await writer.drain()

sidecar.DesktopSidecarServer = FixtureServer
raise SystemExit(sidecar.main())
"""
    completed = subprocess.run(
        [sys.executable, "-E", "-P", "-c", script],
        input=b"",
        capture_output=True,
        cwd="/tmp",
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0
    assert json.loads(completed.stdout) == {
        "v": 1,
        "type": "response",
        "id": "probe",
        "result": {"ok": True},
    }
    assert b"native-contamination" in completed.stderr
    assert b"python-contamination" in completed.stderr


@pytest.mark.asyncio
async def test_actual_sidecar_handshake_read_shutdown_and_hostile_frame(
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    environment.update(XDG_CACHE_HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path))
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-E",
        "-P",
        "-m",
        "tongs.desktop.sidecar",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd="/tmp",
        env=environment,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    try:
        process.stdin.write(
            b'{"v":1,"type":"request","id":"a","id":"b","method":"x","params":{}}\n'
        )
        await process.stdin.drain()
        hostile = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
        assert hostile["error"]["code"] == "invalid_frame"

        process.stdin.write(
            _frame(
                "handshake",
                "handshake",
                {
                    "protocol_major": 1,
                    "core_version": version("tongs"),
                    "capabilities": [],
                },
            )
        )
        await process.stdin.drain()
        handshake = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
        assert handshake["result"]["protocol_major"] == 1
        assert handshake["result"]["session_id"]

        process.stdin.write(_frame("assets", "assets.list", {}))
        await process.stdin.drain()
        assets = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
        assert assets["result"] == {"assets": []}

        process.stdin.write(_frame("shutdown", "shutdown", {}))
        await process.stdin.drain()
        shutdown = json.loads(await asyncio.wait_for(process.stdout.readline(), 10))
        assert shutdown["result"] == {"accepted": True}
        process.stdin.close()
        await process.stdin.wait_closed()
        assert await asyncio.wait_for(process.wait(), 10) == 0
        assert await process.stderr.read() == b""
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
