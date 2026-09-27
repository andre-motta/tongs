"""The desktop inbox reads one bounded page per repository (#344)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from typing import cast

import httpx
import pytest
import pytest_asyncio

from tongs.cache.store import CacheStore
from tongs.config import Config
from tongs.desktop.protocol.messages import MAX_JSON_ITEMS, JsonObject, JsonValue
from tongs.desktop.protocol.server import DesktopSidecarServer
from tongs.forges.base import ForgeClient
from tongs.forges.github import GitHubClient
from tongs.forges.models import ForgeHost
from tongs.plugins.desktop_registry import DesktopPluginRegistry
from tongs.scanner.repo import ForgeType
from tongs.services import ApplicationSession
from tongs.state.drafts import DraftStore

_HOST = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
_PROJECTS = ("acme/huge", "acme/small")
_HUGE_COUNT = 3_500
_NEWEST = datetime(2026, 9, 1, tzinfo=UTC)


def _pull(project: str, number: int, updated: datetime) -> dict[str, object]:
    stamp = updated.isoformat().replace("+00:00", "Z")
    return {
        "number": number,
        "title": f"Change {number} with a reasonably descriptive title",
        "state": "open",
        "draft": False,
        "user": {"login": f"author-{number % 17}"},
        "head": {"ref": f"feature/{number}", "sha": f"sha-{project}-{number}"},
        "base": {"ref": "main", "sha": "base"},
        "created_at": stamp,
        "updated_at": stamp,
        "html_url": f"https://github.com/{project}/pull/{number}",
        "comments": 1,
        "review_comments": 2,
        "mergeable_state": "clean",
        "labels": [{"name": "area/core"}, {"name": "needs-review"}],
    }


class _Forge:
    """A mocked GitHub with one 3,500-pull-request repository and a small one."""

    def __init__(self) -> None:
        self.pulls = {
            "acme/huge": [
                _pull("acme/huge", number, _NEWEST - timedelta(minutes=number))
                for number in range(1, _HUGE_COUNT + 1)
            ],
            "acme/small": [
                _pull("acme/small", 9000 + index, _NEWEST - timedelta(hours=index))
                for index in range(3)
            ],
        }
        self.list_requests: list[tuple[str, dict[str, str]]] = []
        self.ci_requests: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for project, pulls in self.pulls.items():
            if path == f"/repos/{project}/pulls":
                params = dict(request.url.params)
                self.list_requests.append((project, params))
                assert params["sort"] == "updated"
                assert params["direction"] == "desc"
                assert params["state"] == "open"
                page = int(params["page"])
                per_page = int(params["per_page"])
                start = (page - 1) * per_page
                # Like GitHub, a paginated list always sends Link; only
                # pages before the last one carry rel="next".
                links = []
                if start + per_page < len(pulls):
                    links.append(
                        f'<{_HOST.api_base}{path}?page={page + 1}>; rel="next"'
                    )
                if page > 1:
                    links.append(f'<{_HOST.api_base}{path}?page=1>; rel="first"')
                headers = {"Link": ", ".join(links)} if links else {}
                return httpx.Response(
                    200, json=pulls[start : start + per_page], headers=headers
                )
            if path.startswith(f"/repos/{project}/commits/") and path.endswith(
                "/check-runs"
            ):
                self.ci_requests.append(path)
                return httpx.Response(
                    200,
                    json={
                        "check_runs": [{"status": "completed", "conclusion": "success"}]
                    },
                )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")


class _Registry:
    def __init__(self, client: GitHubClient) -> None:
        self.client = client

    def active_hostnames(self) -> list[str]:
        return [_HOST.hostname]

    def get_host(self, hostname: str) -> ForgeHost | None:
        return _HOST if hostname == _HOST.hostname else None

    async def get_client(self, _hostname: str) -> ForgeClient:
        return self.client

    async def close_all(self) -> None:
        await self.client.close()


class _Writer:
    def __init__(self) -> None:
        self.frames: asyncio.Queue[dict[str, object]] = asyncio.Queue()

    def write(self, data: bytes) -> None:
        self.frames.put_nowait(json.loads(data))

    async def drain(self) -> None:
        await asyncio.sleep(0)

    async def response(self, request_id: str) -> dict[str, object]:
        while True:
            frame = await asyncio.wait_for(self.frames.get(), 5)
            if frame.get("type") == "response" and frame.get("id") == request_id:
                return frame


class _Wire:
    def __init__(self, reader: asyncio.StreamReader, writer: _Writer) -> None:
        self.reader = reader
        self.writer = writer
        self.count = 0

    async def call(self, method: str, params: JsonObject) -> dict[str, object]:
        self.count += 1
        request_id = f"request-{self.count}"
        self.reader.feed_data(
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
        return await self.writer.response(request_id)


def _count_values(value: JsonValue) -> int:
    if isinstance(value, list):
        return 1 + sum(_count_values(item) for item in value)
    if isinstance(value, dict):
        return 1 + sum(_count_values(item) for item in value.values())
    return 1


def _result(frame: dict[str, object]) -> dict[str, object]:
    assert "error" not in frame, frame.get("error")
    assert _count_values(cast(JsonValue, frame)) < MAX_JSON_ITEMS
    return cast(dict[str, object], frame["result"])


@pytest_asyncio.fixture
async def wired(tmp_path: Path) -> AsyncIterator[tuple[_Wire, _Forge, dict[str, str]]]:
    forge = _Forge()
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(forge.handle), base_url=_HOST.api_base
    )
    session = ApplicationSession(
        config=Config(),
        cache=CacheStore(db_path=tmp_path / "cache.db"),
        draft_store=DraftStore(tmp_path / "drafts.db"),
        forge_registry=cast(object, _Registry(GitHubClient(_HOST, http))),  # type: ignore[arg-type]
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
    wire = _Wire(reader, writer)
    try:
        await wire.call(
            "handshake",
            {
                "protocol_major": 1,
                "core_version": version("tongs"),
                "capabilities": [],
            },
        )
        handles: dict[str, str] = {}
        for project in _PROJECTS:
            opened = _result(
                await wire.call(
                    "repositories.open",
                    {"hostname": _HOST.hostname, "project_path": project},
                )
            )
            handles[project] = cast(str, opened["handle"])
        # Opening a repository reads a single one-item page, with no walk.
        assert [project for project, _ in forge.list_requests] == list(_PROJECTS)
        assert all(params["per_page"] == "1" for _, params in forge.list_requests)
        forge.list_requests.clear()
        forge.ci_requests.clear()
        yield wire, forge, handles
        await wire.call("shutdown", {})
        await asyncio.wait_for(running, 5)
    finally:
        if not running.done():
            reader.feed_eof()
            with suppress(BaseException):
                await asyncio.wait_for(running, 5)


@pytest.mark.asyncio
async def test_huge_repository_first_read_is_one_page_with_page_ci_only(
    wired: tuple[_Wire, _Forge, dict[str, str]],
) -> None:
    wire, forge, handles = wired

    first = _result(
        await wire.call(
            "reviews.list", {"scope": "all_open", "repository": handles["acme/huge"]}
        )
    )

    items = cast(list[dict[str, object]], first["items"])
    assert len(items) == 100
    assert [cast(dict, item["summary"])["number"] for item in items] == list(
        range(1, 101)
    )
    assert isinstance(first["next_cursor"], str)
    assert len(forge.list_requests) == 1
    assert forge.list_requests[0][1]["page"] == "1"
    assert len(forge.ci_requests) == 100
    assert all(cast(dict, item["summary"])["ci_status"] == "success" for item in items)


@pytest.mark.asyncio
async def test_every_page_of_a_huge_repository_stays_under_the_value_budget(
    wired: tuple[_Wire, _Forge, dict[str, str]],
) -> None:
    wire, forge, handles = wired
    params: JsonObject = {"scope": "all_open", "repository": handles["acme/huge"]}
    seen: list[int] = []
    pages = 0
    while True:
        result = _result(await wire.call("reviews.list", params))
        pages += 1
        seen.extend(
            cast(dict, item["summary"])["number"]
            for item in cast(list[dict[str, object]], result["items"])
        )
        # Each read makes one list request and CI lookups for its own page.
        assert len(forge.list_requests) == pages
        assert len(forge.ci_requests) == len(seen)
        if result["next_cursor"] is None:
            break
        params = {**params, "cursor": result["next_cursor"]}
        assert pages < 40

    assert pages == 35
    assert seen == list(range(1, _HUGE_COUNT + 1))


@pytest.mark.asyncio
async def test_small_repository_has_no_next_cursor(
    wired: tuple[_Wire, _Forge, dict[str, str]],
) -> None:
    wire, forge, handles = wired
    result = _result(
        await wire.call(
            "reviews.list", {"scope": "all_open", "repository": handles["acme/small"]}
        )
    )
    assert len(cast(list[object], result["items"])) == 3
    assert result["next_cursor"] is None
    assert len(forge.ci_requests) == 3


@pytest.mark.asyncio
async def test_forged_and_mismatched_cursors_are_invalid_params(
    wired: tuple[_Wire, _Forge, dict[str, str]],
) -> None:
    wire, forge, handles = wired
    huge = handles["acme/huge"]
    first = _result(
        await wire.call("reviews.list", {"scope": "all_open", "repository": huge})
    )
    cursor = cast(str, first["next_cursor"])
    prefix, page, digest = cursor.split(".")
    assert (prefix, page) == ("rc1", "2")
    list_reads = len(forge.list_requests)

    forged = [
        f"{prefix}.3.{digest}",
        f"{prefix}.{page}.{'0' * len(digest)}",
        "2",
        "",
        "x" * 65,
        f"{cursor}.extra",
        17,
    ]
    for value in forged:
        frame = await wire.call(
            "reviews.list",
            {"scope": "all_open", "repository": huge, "cursor": value},
        )
        error = cast(dict[str, object], frame["error"])
        assert error["code"] == "invalid_params", value

    mismatched: list[JsonObject] = [
        {"scope": "all_open", "repository": handles["acme/small"], "cursor": cursor},
        {"scope": "all_open", "repository": huge, "state": "closed", "cursor": cursor},
        {"scope": "all_open", "repository": huge, "per_page": 50, "cursor": cursor},
        {"scope": "my_reviews", "repository": huge, "cursor": cursor},
        {"scope": "all_open", "cursor": cursor},
    ]
    for params in mismatched:
        frame = await wire.call("reviews.list", params)
        error = cast(dict[str, object], frame["error"])
        assert error["code"] == "invalid_params", params

    # No rejected cursor reached the forge.
    assert len(forge.list_requests) == list_reads


@pytest.mark.asyncio
async def test_all_open_without_repository_reads_first_pages_only(
    wired: tuple[_Wire, _Forge, dict[str, str]],
) -> None:
    wire, forge, _handles = wired
    result = _result(await wire.call("reviews.list", {"scope": "all_open"}))
    assert len(cast(list[object], result["items"])) == 103
    assert result["next_cursor"] is None
    assert sorted(project for project, _ in forge.list_requests) == list(_PROJECTS)
    assert len(forge.ci_requests) == 103
