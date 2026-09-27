from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from importlib.metadata import entry_points, version
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from tongs.config import Config
from tongs.desktop import sidecar
from tongs.desktop.protocol.messages import JsonLimits, JsonObject
from tongs.desktop.protocol.server import (
    MAX_RECOVERY_NOTICE_CHARS,
    MAX_RECOVERY_NOTICES,
    DesktopSidecarServer,
    RequestContext,
    _encode_event_or_resync,
    _EventBuffer,
    _QueuedEvent,
    _recovery_notices,
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
from tongs.services import (
    JobRef,
    RepositoryRef,
    RepositorySnapshot,
    ReviewRef,
    ServiceEvent,
)
from tongs.state.drafts import RecoveryWarning
from tongs.state.drafts.models import RECOVERY_CORRUPT_ATTEMPT_MESSAGE

_SOURCE_ROOT = Path(__file__).parents[2] / "src"


def _actual_sidecar_command() -> list[str]:
    source = str(_SOURCE_ROOT)
    script = (
        f"import runpy,sys;sys.path.insert(0,{source!r});"
        "import tongs;"
        f"assert tongs.__file__.startswith({source!r});"
        "runpy.run_module('tongs.desktop.sidecar',run_name='__main__')"
    )
    return [sys.executable, "-E", "-P", "-c", script]


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
    recovery_warnings: tuple[object, ...] = ()
    read_cleanup_delay = 0.0

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
            if self.read_cleanup_delay:
                await asyncio.sleep(self.read_cleanup_delay)
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
async def test_unexpected_operation_logs_method_and_redacted_detail(
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = DesktopSidecarServer(
        session=cast(object, _FakeSession()), plugin_registry=_registry()
    )
    token = "glpat-" + ("b" * 20)

    async def fail(_params: JsonObject, _context: RequestContext) -> JsonObject:
        raise RuntimeError(f"backend rejected {token}")

    server.register_operation("test.failure", fail, mutation=False)
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
    assert (await asyncio.wait_for(writer.frames.get(), 1))["id"] == "handshake"
    reader.feed_data(_frame("failure", "test.failure", {}))
    response = await asyncio.wait_for(writer.frames.get(), 1)
    reader.feed_eof()
    await task

    assert response["error"]["code"] == "internal"  # type: ignore[index]
    stderr = capsys.readouterr().err
    assert "method=test.failure" in stderr
    assert "RuntimeError" in stderr
    assert "[REDACTED]" in stderr
    assert token not in stderr


@pytest.mark.asyncio
async def test_handshake_reports_skipped_recovery_attempt_on_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _FakeSession()
    attempt_id = UUID("11111111-2222-4333-8444-555555555555")
    review = ReviewRef(RepositoryRef("github.com", "acme/widgets"), 12)
    session.recovery_warnings = (RecoveryWarning(attempt_id, review=review),)
    server = DesktopSidecarServer(
        session=cast(object, session), plugin_registry=_registry()
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
    response = await asyncio.wait_for(writer.frames.get(), 1)
    reader.feed_eof()
    await task

    assert response["id"] == "handshake"
    # The desktop shows the same text, naming the review to check on the forge.
    expected = (
        f"{RECOVERY_CORRUPT_ATTEMPT_MESSAGE} Review: github.com/acme/widgets #12. "
        f"Attempt {attempt_id}."
    )
    assert response["result"]["recovery_warnings"] == [expected]  # type: ignore[index]
    stderr = capsys.readouterr().err
    assert "draft recovery warning" in stderr
    assert expected in stderr


def test_handshake_recovery_notices_are_bounded() -> None:
    long_review = ReviewRef(RepositoryRef("github.com", "a/" + "b" * 990), 1)
    warnings = tuple(
        RecoveryWarning(None, review=long_review if index == 0 else None)
        for index in range(MAX_RECOVERY_NOTICES + 5)
    )

    notices = _recovery_notices(warnings)

    assert len(notices) == MAX_RECOVERY_NOTICES
    assert all(isinstance(text, str) for text in notices)
    assert len(cast(str, notices[0])) == MAX_RECOVERY_NOTICE_CHARS
    assert cast(str, notices[0]).endswith("…")
    assert notices[1] == RECOVERY_CORRUPT_ATTEMPT_MESSAGE
    assert _recovery_notices(()) == []


def _js_length(text: str) -> int:
    """Count UTF-16 code units, as the desktop shell's ``String.length`` does."""
    return len(text.encode("utf-16-le")) // 2


@pytest.mark.parametrize("padding", [0, 1, 2, 3])
def test_recovery_notice_bound_counts_utf16_units_for_astral_text(padding: int) -> None:
    # Each astral character is one code point but two UTF-16 units. Near the
    # limit the notice must fit the shell's bound and never split a pair.
    path = "a/" + "b" * padding + "\U0001f600" * 500
    review = ReviewRef(RepositoryRef("github.com", path), 1)

    [notice] = _recovery_notices((RecoveryWarning(None, review=review),))

    text = cast(str, notice)
    assert len(text) < MAX_RECOVERY_NOTICE_CHARS
    assert _js_length(text) <= MAX_RECOVERY_NOTICE_CHARS
    assert _js_length(text) >= MAX_RECOVERY_NOTICE_CHARS - 1
    assert text.endswith("…")
    text.encode("utf-8")  # no lone surrogate


def test_recovery_notice_at_the_utf16_bound_is_kept_whole() -> None:
    prefix = RecoveryWarning(
        None, review=ReviewRef(RepositoryRef("github.com", "a/b"), 1)
    ).describe()
    spare = MAX_RECOVERY_NOTICE_CHARS - _js_length(prefix)
    path = "a/b" + "\U0001f600" * (spare // 2) + "c" * (spare % 2)
    review = ReviewRef(RepositoryRef("github.com", path), 1)
    text = RecoveryWarning(None, review=review).describe()
    assert _js_length(text) == MAX_RECOVERY_NOTICE_CHARS

    assert _recovery_notices((RecoveryWarning(None, review=review),)) == [text]


@pytest.mark.asyncio
async def test_handshake_without_recovery_warnings_sends_an_empty_list() -> None:
    server = DesktopSidecarServer(
        session=cast(object, _FakeSession()), plugin_registry=_registry()
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
    response = await asyncio.wait_for(writer.frames.get(), 1)
    reader.feed_eof()
    await task

    assert response["result"]["recovery_warnings"] == []  # type: ignore[index]


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
async def test_repository_read_returns_handle_and_safe_display_metadata() -> None:
    session = _FakeSession()
    session.release_read.set()
    server = DesktopSidecarServer(
        session=cast(object, session), plugin_registry=_registry()
    )
    result = await server._repositories_discover(
        {}, RequestContext("read", DesktopCancellation())
    )
    repository = cast(dict[str, object], result)["repositories"][0]  # type: ignore[index]

    assert set(repository) == {"handle", "display_name", "forge_type", "hostname"}
    assert repository["hostname"] == "git.example.com"
    assert "team/project" not in json.dumps(repository)


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
async def test_event_over_the_negotiated_budget_becomes_a_resync_marker() -> None:
    buffer = _EventBuffer()
    await buffer.set_limits(JsonLimits(values=1_024, depth=8))
    await buffer.publish("service.changed", {"items": [0] * 2_000})

    event = await buffer.get()
    assert event is not None
    assert event.name == "protocol.resync_required"
    assert event.data == {"reason": "event_too_large"}
    frame = json.loads(
        _encode_event_or_resync(event, JsonLimits(values=1_024, depth=8))
    )
    assert frame["event"] == "protocol.resync_required"


@pytest.mark.asyncio
async def test_tighter_negotiated_budget_replaces_queued_events_with_resync() -> None:
    buffer = _EventBuffer()
    await buffer.publish("service.changed", {"items": [0] * 2_000})
    await buffer.set_limits(JsonLimits(values=1_024, depth=8))

    event = await buffer.get()
    assert event is not None
    assert event.name == "protocol.resync_required"
    assert event.data == {"reason": "event_too_large"}


def test_pump_encoding_replaces_an_over_budget_event_with_resync() -> None:
    event = _QueuedEvent(7, "service.changed", {"items": [0] * 2_000}, None)
    frame = json.loads(
        _encode_event_or_resync(event, JsonLimits(values=1_024, depth=8))
    )

    assert frame["sequence"] == 7
    assert frame["event"] == "protocol.resync_required"
    assert frame["data"] == {"reason": "event_too_large"}


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
            # Cleanup that needs the loop: only a drained waiter finishes it.
            await asyncio.sleep(0.02)
            self.wait_finished.set()


@pytest.mark.asyncio
async def test_plugin_read_outer_cancellation_drains_service_and_waiter_tasks() -> None:
    session = _FakeSession()
    # Slow cleanup: the flags below are set only if the read awaits its tasks.
    session.read_cleanup_delay = 0.02
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


def test_actual_sidecar_exits_cleanly_on_eof(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.update(
        XDG_CACHE_HOME=str(tmp_path / "cache"),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_DATA_HOME=str(tmp_path / "data"),
    )
    completed = subprocess.run(
        _actual_sidecar_command(),
        input=b"",
        capture_output=True,
        cwd="/tmp",
        env=environment,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stdout == b""
    assert completed.stderr == b""


def test_actual_sidecar_redirects_python_and_native_stdout_from_protocol() -> None:
    script = f"""
import os
import sys
sys.path.insert(0, {str(_SOURCE_ROOT)!r})
from tongs.desktop import sidecar
from tongs.desktop.protocol.messages import encode_response

class FixtureServer:
    async def run(self, _reader, writer):
        os.write(1, b'native-contamination\\n')
        print('python-contamination')
        writer.write(encode_response('probe', result={{'ok': True}}))
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
    # This protocol fixture deliberately requests an empty plugin catalog even
    # when the developer has installed desktop plugins in the test environment.
    isolated_config = tmp_path / "config" / "tongs"
    isolated_config.mkdir(parents=True)
    plugin_names = sorted(
        {entry.name for entry in entry_points(group="tongs.desktop_plugins")}
    )
    (isolated_config / "config.toml").write_text(
        "".join(
            f"[plugins.{json.dumps(name)}]\nenabled = false\n" for name in plugin_names
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(
        XDG_CACHE_HOME=str(tmp_path / "cache"),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        XDG_DATA_HOME=str(tmp_path / "data"),
    )
    process = await asyncio.create_subprocess_exec(
        *_actual_sidecar_command(),
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


def test_sidecar_main_logs_redacted_unexpected_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    token = "glpat-" + ("a" * 20)

    async def fail() -> None:
        raise RuntimeError(f"request failed with {token}")

    monkeypatch.setattr(sidecar, "run_stdio", fail)

    assert sidecar.main() == 1
    stderr = capsys.readouterr().err
    assert "RuntimeError" in stderr
    assert "[REDACTED]" in stderr
    assert token not in stderr
