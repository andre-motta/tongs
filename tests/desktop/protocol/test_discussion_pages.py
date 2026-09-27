"""Discussions are paged through the snapshot store within the value budget (#292)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from importlib.metadata import version
from typing import cast

import pytest

from tongs.config import Config
from tongs.desktop.protocol.messages import (
    MAX_JSON_ITEMS,
    MIN_JSON_ITEMS,
    JsonObject,
    JsonValue,
    ProtocolError,
    to_json_value,
)
from tongs.desktop.protocol.server import DesktopSidecarServer, RequestContext
from tongs.desktop.protocol.state import HandleKind, SnapshotStore
from tongs.forges.models import Discussion, InlineComment, User
from tongs.plugins.desktop import DesktopCancellation, DesktopReadKind, freeze_json
from tongs.plugins.desktop_registry import DesktopPluginRegistry
from tongs.scanner.repo import ForgeType
from tongs.services import RepositoryRef, RepositorySnapshot, ReviewRef, ServiceEvent

_REVIEW = ReviewRef(RepositoryRef("forge.example.com", "team/project"), 9)
_LARGE_THREAD_COUNT = 1_200


def _count_values(value: object) -> int:
    if isinstance(value, list):
        return 1 + sum(_count_values(item) for item in value)
    if isinstance(value, dict):
        return 1 + sum(_count_values(item) for item in value.values())
    return 1


def _comment(index: int, suffix: str, replies: int = 0) -> InlineComment:
    return InlineComment(
        id=f"note-{index}-{suffix}",
        author=User("reviewer", "Reviewer"),
        body="Looks good.",
        created_at=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
        file_path="src/module.py",
        old_line=None,
        new_line=index + 1,
        replies=tuple(_comment(index, f"{suffix}-{reply}") for reply in range(replies)),
    )


def _discussions(count: int, *, replies: int = 0) -> tuple[Discussion, ...]:
    return tuple(
        Discussion(
            id=f"thread-{index}",
            is_inline=True,
            root_comment=_comment(index, "root", replies),
        )
        for index in range(count)
    )


class _Session:
    def __init__(self, discussions: tuple[Discussion, ...]) -> None:
        self.config = Config()
        self.discussions = discussions
        self.reads = 0

    async def start(self) -> _Session:
        return self

    async def close(self) -> None:
        return None

    async def events(self) -> AsyncIterator[ServiceEvent]:
        await asyncio.Future()
        yield cast(ServiceEvent, None)

    async def get_discussions(self, review: ReviewRef) -> tuple[Discussion, ...]:
        assert review == _REVIEW
        self.reads += 1
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


def _frame(request_id: str, method: str, params: JsonObject) -> bytes:
    document = {
        "v": 1,
        "type": "request",
        "id": request_id,
        "method": method,
        "params": params,
    }
    return json.dumps(document).encode() + b"\n"


def _handshake(values: int = MAX_JSON_ITEMS) -> bytes:
    return _frame(
        "handshake",
        "handshake",
        {
            "protocol_major": 1,
            "core_version": version("tongs"),
            "capabilities": [],
            "limits": {"json_values": values, "json_depth": 24},
        },
    )


class _Connection:
    """A running sidecar fed frames the way Electron sends them."""

    def __init__(self, session: _Session) -> None:
        self.server = DesktopSidecarServer(
            session=cast(object, session),
            plugin_registry=DesktopPluginRegistry(
                entry_point_source=lambda _group: (), host_version="1.0"
            ),
            shutdown_timeout=0.2,
        )
        self.review = self.server._handles.issue(HandleKind.REVIEW, _REVIEW)
        self.reader = asyncio.StreamReader()
        self.writer = _QueueWriter()
        self.task: asyncio.Task[None] | None = None
        self._sequence = 0

    async def open(self, values: int = MAX_JSON_ITEMS) -> dict[str, object]:
        self.task = asyncio.create_task(self.server.run(self.reader, self.writer))
        return await self.request_raw(_handshake(values))

    async def request(self, method: str, params: JsonObject) -> dict[str, object]:
        self._sequence += 1
        return await self.request_raw(_frame(f"r{self._sequence}", method, params))

    async def request_raw(self, frame: bytes) -> dict[str, object]:
        self.reader.feed_data(frame)
        return await asyncio.wait_for(self.writer.frames.get(), 2)

    async def close(self) -> None:
        self.reader.feed_eof()
        assert self.task is not None
        await self.task

    async def all_pages(self) -> list[JsonObject]:
        first = await self.request("discussions.list", {"review": self.review})
        pages = [cast(JsonObject, first["result"])]
        while pages[-1]["next_cursor"] is not None:
            response = await self.request(
                "discussions.page",
                {
                    "snapshot": pages[-1]["snapshot_id"],
                    "resource": pages[-1]["resource"],
                    "cursor": pages[-1]["next_cursor"],
                },
            )
            pages.append(cast(JsonObject, response["result"]))
        return pages


def _threads(pages: list[JsonObject]) -> list[JsonValue]:
    return [
        thread
        for page in pages
        for thread in cast(list[JsonValue], page["discussions"])
    ]


def _expected(discussions: tuple[Discussion, ...]) -> list[str]:
    return [discussion.id for discussion in discussions]


@pytest.mark.asyncio
async def test_a_1200_thread_review_arrives_in_pages_inside_the_value_budget() -> None:
    discussions = _discussions(_LARGE_THREAD_COUNT)
    whole = {"discussions": [to_json_value(item) for item in discussions]}
    # The unpaged list is the frame 1.0.2 could not send.
    assert _count_values(whole) > MAX_JSON_ITEMS
    session = _Session(discussions)
    connection = _Connection(session)
    handshake = await connection.open()
    result = cast(JsonObject, handshake["result"])
    assert "paged_discussions" in cast(list[str], result["capabilities"])
    assert "discussions.page" in cast(list[str], result["methods"])

    pages = await connection.all_pages()
    await connection.close()

    assert len(pages) > 1
    assert session.reads == 1
    assert [page["cursor"] for page in pages] == [
        0,
        *[page["next_cursor"] for page in pages[:-1]],
    ]
    assert {page["snapshot_id"] for page in pages} == {pages[0]["snapshot_id"]}
    assert all(
        page["revision"] == {"discussion_count": _LARGE_THREAD_COUNT} for page in pages
    )
    threads = _threads(pages)
    assert [cast(JsonObject, thread)["id"] for thread in threads] == _expected(
        discussions
    )
    assert (
        cast(JsonObject, threads[0])["root_comment"]
        == cast(JsonObject, whole["discussions"][0])["root_comment"]
    )
    for raw in connection.writer.raw:
        document = json.loads(raw)
        assert "error" not in document
        # Every frame, envelope included, stays well inside the budget.
        assert _count_values(document) <= MAX_JSON_ITEMS // 2 + 32


@pytest.mark.asyncio
async def test_long_threads_shrink_the_page_instead_of_overflowing_it() -> None:
    discussions = _discussions(60, replies=60)
    connection = _Connection(_Session(discussions))
    await connection.open()

    pages = await connection.all_pages()
    await connection.close()

    # Each thread holds about 800 values, so a page holds about a dozen of them
    # even though the item limit would allow all sixty.
    assert len(pages) >= 4
    assert [cast(JsonObject, thread)["id"] for thread in _threads(pages)] == (
        _expected(discussions)
    )
    for raw in connection.writer.raw:
        assert _count_values(json.loads(raw)) <= MAX_JSON_ITEMS // 2 + 32


@pytest.mark.asyncio
async def test_a_tighter_negotiated_budget_gives_smaller_pages() -> None:
    discussions = _discussions(300)
    connection = _Connection(_Session(discussions))
    await connection.open(MIN_JSON_ITEMS)

    pages = await connection.all_pages()
    await connection.close()

    assert len(pages) > 5
    assert len(_threads(pages)) == 300
    for raw in connection.writer.raw:
        assert _count_values(json.loads(raw)) <= MIN_JSON_ITEMS


@pytest.mark.asyncio
async def test_one_thread_over_the_budget_fails_only_its_request() -> None:
    connection = _Connection(_Session(_discussions(1, replies=1_000)))
    await connection.open()

    threads = await connection.request(
        "discussions.list", {"review": connection.review}
    )
    repositories = await connection.request("repositories.discover", {})
    await connection.close()

    assert cast(JsonObject, threads["error"])["code"] == "response_too_large"
    assert "result" in repositories


@pytest.mark.asyncio
async def test_an_empty_review_is_one_final_page() -> None:
    connection = _Connection(_Session(()))
    await connection.open()

    pages = await connection.all_pages()
    await connection.close()

    assert len(pages) == 1
    assert pages[0]["discussions"] == []
    assert pages[0]["next_cursor"] is None
    assert pages[0]["revision"] == {"discussion_count": 0}


@pytest.mark.asyncio
async def test_a_diff_page_cannot_read_a_discussions_snapshot() -> None:
    connection = _Connection(_Session(_discussions(500)))
    await connection.open()

    first = await connection.request("discussions.list", {"review": connection.review})
    page = cast(JsonObject, first["result"])
    crossed = await connection.request(
        "diff.page",
        {
            "snapshot": page["snapshot_id"],
            "resource": page["resource"],
            "cursor": page["next_cursor"],
        },
    )
    await connection.close()

    assert cast(JsonObject, crossed["error"])["code"] == "snapshot_expired"


@pytest.mark.asyncio
async def test_a_discussions_page_rejects_unknown_fields() -> None:
    connection = _Connection(_Session(_discussions(3)))
    await connection.open()

    response = await connection.request(
        "discussions.list", {"review": connection.review, "cursor": 1}
    )
    await connection.close()

    assert cast(JsonObject, response["error"])["code"] == "invalid_params"


@pytest.mark.asyncio
async def test_plugin_reads_keep_the_whole_list_shape() -> None:
    server = DesktopSidecarServer(session=cast(object, _Session(_discussions(3))))
    handle = server._handles.issue(HandleKind.REVIEW, _REVIEW)

    result = await server._dispatch_plugin_read(
        DesktopReadKind.DISCUSSIONS,
        cast(Mapping[str, object], freeze_json({"review": handle})),
        DesktopCancellation(),
    )

    assert isinstance(result, Mapping)
    assert set(result) == {"discussions"}
    assert len(result["discussions"]) == 3


@pytest.mark.asyncio
async def test_the_first_page_request_is_direct_and_bounded() -> None:
    server = DesktopSidecarServer(
        session=cast(object, _Session(_discussions(_LARGE_THREAD_COUNT)))
    )
    handle = server._handles.issue(HandleKind.REVIEW, _REVIEW)

    page = cast(
        JsonObject,
        await server._discussions_list(
            {"review": handle, "max_items": 50},
            RequestContext("threads", DesktopCancellation()),
        ),
    )

    assert len(cast(list[JsonValue], page["discussions"])) == 50
    assert page["next_cursor"] == 50


@pytest.mark.asyncio
async def test_single_page_reads_do_not_retain_snapshots() -> None:
    server = DesktopSidecarServer(session=cast(object, _Session(_discussions(3))))
    handle = server._handles.issue(HandleKind.REVIEW, _REVIEW)
    context = RequestContext("threads", DesktopCancellation())

    # More single-page reads than the 32-snapshot cap leave nothing retained.
    for _ in range(40):
        page = cast(
            JsonObject, await server._discussions_list({"review": handle}, context)
        )
        assert page["next_cursor"] is None
        assert len(cast(list[JsonValue], page["discussions"])) == 3
    assert len(server._snapshots._snapshots) == 0


@pytest.mark.asyncio
async def test_multi_page_reads_keep_their_snapshot_and_failures_release_it() -> None:
    session = _Session(_discussions(_LARGE_THREAD_COUNT))
    server = DesktopSidecarServer(session=cast(object, session))
    handle = server._handles.issue(HandleKind.REVIEW, _REVIEW)
    context = RequestContext("threads", DesktopCancellation())

    page = cast(
        JsonObject,
        await server._discussions_list({"review": handle, "max_items": 50}, context),
    )
    assert page["next_cursor"] == 50
    assert list(server._snapshots._snapshots) == [page["snapshot_id"]]

    session.discussions = _discussions(1, replies=1_000)
    with pytest.raises(ProtocolError) as oversized:
        await server._discussions_list({"review": handle}, context)
    assert oversized.value.code.value == "response_too_large"
    assert list(server._snapshots._snapshots) == [page["snapshot_id"]]


def test_snapshot_pages_stop_before_the_value_limit() -> None:
    store = SnapshotStore()
    entries = [{"id": str(index), "values": [1, 2, 3]} for index in range(10)]
    snapshot = store.create("review", {"count": 10}, entries, kind="rows")

    # Each entry is six values: the object, the id, the list and its three items.
    page = store.page(snapshot, "review", 0, 10, max_values=20, kind="rows")

    assert len(page.entries) == 3
    assert page.next_cursor == 3
    with pytest.raises(ProtocolError) as oversized:
        store.page(snapshot, "review", 0, 10, max_values=5, kind="rows")
    assert oversized.value.code.value == "response_too_large"
    with pytest.raises(ProtocolError) as crossed:
        store.page(snapshot, "review", 0, 10, kind="other")
    assert crossed.value.code.value == "snapshot_expired"


@pytest.mark.asyncio
async def test_the_last_page_of_a_multi_page_read_releases_its_snapshot() -> None:
    server = DesktopSidecarServer(
        session=cast(object, _Session(_discussions(_LARGE_THREAD_COUNT)))
    )
    handle = server._handles.issue(HandleKind.REVIEW, _REVIEW)
    context = RequestContext("threads", DesktopCancellation())

    page = cast(
        JsonObject,
        await server._discussions_list({"review": handle, "max_items": 500}, context),
    )
    snapshot_id = page["snapshot_id"]
    pages = 1
    threads = len(cast(list[JsonValue], page["discussions"]))
    while page["next_cursor"] is not None:
        # Every page before the last one keeps the snapshot for the next read.
        assert list(server._snapshots._snapshots) == [snapshot_id]
        page = cast(
            JsonObject,
            await server._discussions_page(
                {
                    "snapshot": snapshot_id,
                    "resource": handle,
                    "cursor": page["next_cursor"],
                    "max_items": 500,
                },
                context,
            ),
        )
        pages += 1
        threads += len(cast(list[JsonValue], page["discussions"]))

    assert pages > 1
    assert threads == _LARGE_THREAD_COUNT
    assert len(server._snapshots._snapshots) == 0
    with pytest.raises(ProtocolError) as expired:
        await server._discussions_page(
            {"snapshot": snapshot_id, "resource": handle, "cursor": 0}, context
        )
    assert expired.value.code.value == "snapshot_expired"
