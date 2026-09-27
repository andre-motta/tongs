"""Responses respect the negotiated JSON value and depth budget (#292)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from importlib.metadata import version
from typing import cast

import pytest

from tongs.config import Config
from tongs.desktop.protocol.messages import (
    DEFAULT_JSON_LIMITS,
    MAX_JSON_DEPTH,
    MAX_JSON_ITEMS,
    MIN_JSON_DEPTH,
    MIN_JSON_ITEMS,
    JsonLimits,
    JsonObject,
    JsonValue,
    encode_response,
)
from tongs.desktop.protocol.server import DesktopSidecarServer, RequestContext
from tongs.desktop.protocol.state import HandleKind
from tongs.forges.models import Discussion, InlineComment, User
from tongs.plugins.desktop import DesktopCancellation
from tongs.plugins.desktop_registry import DesktopPluginRegistry
from tongs.scanner.repo import ForgeType
from tongs.services import RepositoryRef, RepositorySnapshot, ReviewRef, ServiceEvent

_REVIEW = ReviewRef(RepositoryRef("forge.example.com", "team/project"), 9)
_LARGE_THREAD_COUNT = 1_200


def _count_values(value: JsonValue) -> int:
    if isinstance(value, list):
        return 1 + sum(_count_values(item) for item in value)
    if isinstance(value, dict):
        return 1 + sum(_count_values(item) for item in value.values())
    return 1


def _depth(value: JsonValue) -> int:
    if isinstance(value, list):
        return 1 + max((_depth(item) for item in value), default=0)
    if isinstance(value, dict):
        return 1 + max((_depth(item) for item in value.values()), default=0)
    return 0


def _nested(depth: int) -> JsonValue:
    value: JsonValue = "leaf"
    for _ in range(depth):
        value = [value]
    return value


def _discussions(count: int) -> tuple[Discussion, ...]:
    return tuple(
        Discussion(
            id=f"thread-{index}",
            is_inline=True,
            root_comment=InlineComment(
                id=f"note-{index}",
                author=User("reviewer", "Reviewer"),
                body="Looks good.",
                created_at=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
                file_path="src/module.py",
                old_line=None,
                new_line=index + 1,
            ),
        )
        for index in range(count)
    )


class _Session:
    def __init__(self, discussions: tuple[Discussion, ...]) -> None:
        self.config = Config()
        self.discussions = discussions
        self.closed = False

    async def start(self) -> _Session:
        return self

    async def close(self) -> None:
        self.closed = True

    async def events(self) -> AsyncIterator[ServiceEvent]:
        await asyncio.Future()
        yield cast(ServiceEvent, None)

    async def get_discussions(self, review: ReviewRef) -> tuple[Discussion, ...]:
        assert review == _REVIEW
        return self.discussions

    async def discover_repositories(self) -> tuple[RepositorySnapshot, ...]:
        return (
            RepositorySnapshot(
                RepositoryRef("forge.example.com", "team/project"),
                "project",
                ForgeType.GITLAB,
            ),
        )


class _QueueWriter:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.raw: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.raw.append(data)
        self.frames.put_nowait(json.loads(data))

    async def drain(self) -> None:
        await asyncio.sleep(0)


def _registry() -> DesktopPluginRegistry:
    return DesktopPluginRegistry(
        entry_point_source=lambda _group: (), host_version="1.0"
    )


def _frame(request_id: str, method: str, params: JsonObject) -> bytes:
    document = {
        "v": 1,
        "type": "request",
        "id": request_id,
        "method": method,
        "params": params,
    }
    return json.dumps(document).encode() + b"\n"


def _handshake(limits: JsonValue | None = None) -> bytes:
    params: JsonObject = {
        "protocol_major": 1,
        "core_version": version("tongs"),
        "capabilities": [],
    }
    if limits is not None:
        params["limits"] = limits
    return _frame("handshake", "handshake", params)


async def _discussion_result(count: int) -> object:
    server = DesktopSidecarServer(session=cast(object, _Session(_discussions(count))))
    handle = server._handles.issue(HandleKind.REVIEW, _REVIEW)
    return await server._discussions_list(
        {"review": handle}, RequestContext("threads", DesktopCancellation())
    )


@pytest.mark.asyncio
async def test_large_discussion_list_becomes_a_bounded_error_frame() -> None:
    result = await _discussion_result(_LARGE_THREAD_COUNT)
    unbounded = {"v": 1, "type": "response", "id": "threads", "result": result}
    assert _count_values(cast(JsonValue, unbounded)) > MAX_JSON_ITEMS

    encoded = encode_response("threads", result=result)
    document = json.loads(encoded)

    assert document["id"] == "threads"
    assert "result" not in document
    assert document["error"]["code"] == "response_too_large"
    assert document["error"]["retryable"] is False
    assert "too large" in document["error"]["message"]
    assert document["error"]["details"] == {
        "value_limit": MAX_JSON_ITEMS,
        "depth_limit": MAX_JSON_DEPTH,
    }
    assert _count_values(document) <= MAX_JSON_ITEMS
    assert len(encoded) < 1024


@pytest.mark.asyncio
async def test_small_discussion_list_still_encodes_its_result() -> None:
    result = await _discussion_result(3)

    document = json.loads(encode_response("threads", result=result))

    assert "error" not in document
    assert len(document["result"]["discussions"]) == 3


def test_value_budget_boundary_counts_the_whole_frame() -> None:
    # The envelope holds the object, v, type, id, and the result list itself.
    envelope = 5
    fits = [0] * (MAX_JSON_ITEMS - envelope)
    over = [0] * (MAX_JSON_ITEMS - envelope + 1)

    accepted = json.loads(encode_response("a", result=fits))
    rejected = json.loads(encode_response("a", result=over))

    assert _count_values(accepted) == MAX_JSON_ITEMS
    assert len(accepted["result"]) == len(fits)
    assert rejected["error"]["code"] == "response_too_large"


def test_depth_budget_boundary_counts_the_whole_frame() -> None:
    # The result sits one level below the response envelope.
    accepted = json.loads(encode_response("a", result=_nested(MAX_JSON_DEPTH - 1)))
    rejected = json.loads(encode_response("a", result=_nested(MAX_JSON_DEPTH)))

    assert _depth(accepted) == MAX_JSON_DEPTH
    assert "result" in accepted
    assert rejected["error"]["code"] == "response_too_large"


def test_negotiated_limits_tighten_the_response_budget() -> None:
    limits = JsonLimits(values=MIN_JSON_ITEMS, depth=MIN_JSON_DEPTH)

    by_values = json.loads(
        encode_response("a", result=[0] * MIN_JSON_ITEMS, limits=limits)
    )
    by_depth = json.loads(
        encode_response("a", result=_nested(MIN_JSON_DEPTH), limits=limits)
    )
    default = json.loads(encode_response("a", result=[0] * MIN_JSON_ITEMS))

    assert by_values["error"]["code"] == "response_too_large"
    assert by_values["error"]["details"]["value_limit"] == MIN_JSON_ITEMS
    assert by_depth["error"]["code"] == "response_too_large"
    assert "result" in default


def test_negotiation_keeps_the_tighter_limit_on_each_axis() -> None:
    assert DEFAULT_JSON_LIMITS.negotiate(MAX_JSON_ITEMS + 5, 10) == JsonLimits(
        MAX_JSON_ITEMS, 10
    )


@pytest.mark.asyncio
async def test_oversized_response_fails_only_its_request_and_the_service_stays_up() -> (
    None
):
    session = _Session(_discussions(_LARGE_THREAD_COUNT))
    server = DesktopSidecarServer(
        session=cast(object, session),
        plugin_registry=_registry(),
        shutdown_timeout=0.2,
    )
    handle = server._handles.issue(HandleKind.REVIEW, _REVIEW)
    reader = asyncio.StreamReader()
    writer = _QueueWriter()
    task = asyncio.create_task(server.run(reader, writer))

    reader.feed_data(
        _handshake({"json_values": MAX_JSON_ITEMS, "json_depth": MAX_JSON_DEPTH})
    )
    handshake = await asyncio.wait_for(writer.frames.get(), 1)
    limits = cast(JsonObject, cast(JsonObject, handshake["result"])["limits"])
    assert limits["json_values"] == MAX_JSON_ITEMS
    assert limits["json_depth"] == MAX_JSON_DEPTH

    reader.feed_data(_frame("threads", "discussions.list", {"review": handle}))
    threads = await asyncio.wait_for(writer.frames.get(), 1)
    reader.feed_data(_frame("repos", "repositories.discover", {}))
    repositories = await asyncio.wait_for(writer.frames.get(), 1)
    reader.feed_data(_frame("shutdown", "shutdown", {}))
    assert (await asyncio.wait_for(writer.frames.get(), 1))["result"] == {
        "accepted": True
    }
    await task

    assert threads["id"] == "threads"
    assert cast(JsonObject, threads["error"])["code"] == "response_too_large"
    assert repositories["id"] == "repos"
    assert "result" in repositories
    assert all(_count_values(json.loads(raw)) <= MAX_JSON_ITEMS for raw in writer.raw)


@pytest.mark.asyncio
async def test_handshake_negotiates_tighter_client_limits() -> None:
    session = _Session(_discussions(200))
    server = DesktopSidecarServer(
        session=cast(object, session),
        plugin_registry=_registry(),
        shutdown_timeout=0.2,
    )
    handle = server._handles.issue(HandleKind.REVIEW, _REVIEW)
    reader = asyncio.StreamReader()
    writer = _QueueWriter()
    task = asyncio.create_task(server.run(reader, writer))

    reader.feed_data(_handshake({"json_values": MIN_JSON_ITEMS, "json_depth": 99}))
    handshake = await asyncio.wait_for(writer.frames.get(), 1)
    reader.feed_data(_frame("threads", "discussions.list", {"review": handle}))
    threads = await asyncio.wait_for(writer.frames.get(), 1)
    reader.feed_eof()
    await task

    limits = cast(JsonObject, cast(JsonObject, handshake["result"])["limits"])
    assert limits["json_values"] == MIN_JSON_ITEMS
    assert limits["json_depth"] == MAX_JSON_DEPTH
    assert cast(JsonObject, threads["error"])["code"] == "response_too_large"


@pytest.mark.asyncio
async def test_handshake_without_limits_uses_the_sidecar_defaults() -> None:
    server = DesktopSidecarServer(
        session=cast(object, _Session(())),
        plugin_registry=_registry(),
        shutdown_timeout=0.2,
    )
    reader = asyncio.StreamReader()
    writer = _QueueWriter()
    task = asyncio.create_task(server.run(reader, writer))

    reader.feed_data(_handshake())
    handshake = await asyncio.wait_for(writer.frames.get(), 1)
    reader.feed_eof()
    await task

    limits = cast(JsonObject, cast(JsonObject, handshake["result"])["limits"])
    assert limits["json_values"] == MAX_JSON_ITEMS
    assert limits["json_depth"] == MAX_JSON_DEPTH


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("limits", "code"),
    [
        ({"json_values": MIN_JSON_ITEMS - 1, "json_depth": 24}, "unsupported_protocol"),
        (
            {"json_values": 20_000, "json_depth": MIN_JSON_DEPTH - 1},
            "unsupported_protocol",
        ),
        ({"json_values": "20000", "json_depth": 24}, "unsupported_protocol"),
        ({"json_values": 20_000, "json_depth": True}, "unsupported_protocol"),
        ({"json_values": 20_000}, "invalid_params"),
        ({"json_values": 20_000, "json_depth": 24, "extra": 1}, "invalid_params"),
        ([20_000, 24], "invalid_params"),
    ],
)
async def test_handshake_rejects_incompatible_limits(
    limits: JsonValue, code: str
) -> None:
    server = DesktopSidecarServer(
        session=cast(object, _Session(())),
        plugin_registry=_registry(),
        shutdown_timeout=0.2,
    )
    reader = asyncio.StreamReader()
    writer = _QueueWriter()
    task = asyncio.create_task(server.run(reader, writer))

    reader.feed_data(_handshake(limits))
    response = await asyncio.wait_for(writer.frames.get(), 1)
    reader.feed_data(_frame("repos", "repositories.discover", {}))
    follow_up = await asyncio.wait_for(writer.frames.get(), 1)
    reader.feed_eof()
    await task

    assert cast(JsonObject, response["error"])["code"] == code
    assert cast(JsonObject, follow_up["error"])["code"] == "handshake_required"
