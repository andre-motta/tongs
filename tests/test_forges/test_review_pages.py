"""One-page review reads for the desktop inbox (#344)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from tongs.forges.base import ForgeClient, read_mr_page
from tongs.forges.github import GitHubClient
from tongs.forges.gitlab import GitLabClient
from tongs.forges.http import request_page
from tongs.forges.models import CIStatus, ForgeHost, MRPage, MRState, MRSummary, User
from tongs.scanner.repo import ForgeType

_GITHUB = ForgeHost("github.com", ForgeType.GITHUB, "https://api.github.com")
_GITLAB = ForgeHost(
    "gitlab.example.com", ForgeType.GITLAB, "https://gitlab.example.com/api/v4"
)


def _github_pull(number: int) -> dict[str, object]:
    return {
        "number": number,
        "title": f"PR {number}",
        "state": "open",
        "draft": False,
        "user": {"login": "alice"},
        "head": {"ref": "feature", "sha": f"sha-{number}"},
        "base": {"ref": "main", "sha": "base"},
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-02T00:00:00Z",
        "html_url": f"https://github.com/acme/repo/pull/{number}",
        "labels": [],
    }


def _gitlab_mr(iid: int, pipeline: dict | None = None) -> dict[str, object]:
    data: dict[str, object] = {
        "iid": iid,
        "title": f"MR {iid}",
        "state": "opened",
        "draft": False,
        "source_branch": "feature",
        "target_branch": "main",
        "web_url": f"https://gitlab.example.com/acme/repo/-/merge_requests/{iid}",
        "created_at": "2026-09-01T00:00:00Z",
        "updated_at": "2026-09-02T00:00:00Z",
        "author": {"username": "alice", "name": "Alice"},
        "labels": [],
        "references": {"full": f"acme/repo!{iid}"},
    }
    if pipeline is not None:
        data["head_pipeline"] = pipeline
    return data


class TestRequestPage:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("headers", "count", "expected"),
        [
            (
                {
                    "Link": '<https://x/?page=3>; rel="next", <https://x/?page=9>; rel="last"'
                },
                2,
                True,
            ),
            (
                {
                    "Link": '<https://x/?page=1>; rel="first", <https://x/?page=1>; rel="prev"'
                },
                2,
                False,
            ),
            ({"X-Next-Page": "3"}, 2, True),
            ({"X-Next-Page": "", "Link": '<https://x/?page=3>; rel="next"'}, 2, False),
            ({}, 2, True),
            ({}, 1, False),
        ],
    )
    async def test_has_next_follows_forge_headers_then_page_size(
        self, headers: dict[str, str], count: int, expected: bool
    ) -> None:
        seen: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.url.params))
            return httpx.Response(
                200, json=[{"n": i} for i in range(count)], headers=headers
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://x"
        ) as http:
            items, has_next = await request_page(
                http, "/list", page=2, per_page=2, params={"state": "open"}
            )
        assert len(items) == count
        assert has_next is expected
        assert seen == [{"state": "open", "per_page": "2", "page": "2"}]


class TestGitHubPage:
    @pytest.mark.asyncio
    async def test_one_request_sorted_by_update_and_ci_for_page_only(self) -> None:
        list_params: list[dict[str, str]] = []
        ci_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/repos/acme/repo/pulls":
                list_params.append(dict(request.url.params))
                return httpx.Response(
                    200,
                    json=[_github_pull(7), _github_pull(8)],
                    headers={"Link": '<https://api.github.com/x?page=4>; rel="next"'},
                )
            if request.url.path.endswith("/check-runs"):
                ci_paths.append(request.url.path)
                return httpx.Response(
                    200,
                    json={
                        "check_runs": [{"status": "completed", "conclusion": "failure"}]
                    },
                )
            raise AssertionError(str(request.url))

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=_GITHUB.api_base
        ) as http:
            page = await GitHubClient(_GITHUB, http).list_mrs_page(
                "acme/repo", page=3, per_page=2
            )

        assert list_params == [
            {
                "state": "open",
                "sort": "updated",
                "direction": "desc",
                "per_page": "2",
                "page": "3",
            }
        ]
        assert [item.number for item in page.items] == [7, 8]
        assert page.has_next is True
        assert sorted(ci_paths) == [
            "/repos/acme/repo/commits/sha-7/check-runs",
            "/repos/acme/repo/commits/sha-8/check-runs",
        ]
        assert {item.ci_status for item in page.items} == {CIStatus.FAILED}

    @pytest.mark.asyncio
    async def test_list_mrs_still_walks_every_page(self) -> None:
        pages: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/repos/acme/repo/pulls":
                page = request.url.params["page"]
                pages.append(page)
                assert "sort" not in request.url.params
                data = [_github_pull(1), _github_pull(2)] if page == "1" else []
                return httpx.Response(200, json=data)
            return httpx.Response(200, json={"check_runs": []})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=_GITHUB.api_base
        ) as http:
            items = await GitHubClient(_GITHUB, http).list_mrs("acme/repo", per_page=2)
        assert [item.number for item in items] == [1, 2]
        assert pages == ["1", "2"]


class TestGitLabPage:
    @pytest.mark.asyncio
    async def test_one_request_sorted_by_update_and_pipelines_for_page_only(
        self,
    ) -> None:
        list_params: list[dict[str, str]] = []
        pipeline_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.raw_path.decode().split("?")[0]
            if path.endswith("/projects/acme%2Frepo/merge_requests"):
                list_params.append(dict(request.url.params))
                return httpx.Response(
                    200,
                    json=[_gitlab_mr(5, {"status": "success"}), _gitlab_mr(6)],
                    headers={"X-Next-Page": ""},
                )
            if path.endswith("/pipelines"):
                pipeline_paths.append(path)
                return httpx.Response(200, json=[{"status": "running"}])
            raise AssertionError(str(request.url))

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=_GITLAB.api_base
        ) as http:
            page = await GitLabClient(_GITLAB, http).list_mrs_page(
                "acme/repo", state="open", page=2, per_page=2
            )

        assert list_params == [
            {
                "state": "opened",
                "order_by": "updated_at",
                "sort": "desc",
                "per_page": "2",
                "page": "2",
            }
        ]
        assert page.has_next is False
        assert [item.ci_status for item in page.items] == [
            CIStatus.SUCCESS,
            CIStatus.RUNNING,
        ]
        assert len(pipeline_paths) == 1
        assert pipeline_paths[0].endswith("/merge_requests/6/pipelines")


def _summary(number: int, updated: datetime) -> MRSummary:
    return MRSummary(
        forge_host=_GITHUB,
        repo_path="acme/repo",
        local_path="",
        number=number,
        title=f"PR {number}",
        author=User("alice"),
        state=MRState.OPEN,
        is_draft=False,
        source_branch="feature",
        target_branch="main",
        ci_status=CIStatus.UNKNOWN,
        created_at=updated,
        updated_at=updated,
        web_url="https://example.invalid",
    )


class _ListOnlyClient:
    """A duck-typed client that predates paged reads."""

    def __init__(self) -> None:
        base = datetime(2026, 9, 1, tzinfo=UTC)
        self.items = [_summary(n, base + timedelta(minutes=n)) for n in range(1, 6)]

    async def list_mrs(
        self, repo_path: str, state: str = "open", per_page: int = 100
    ) -> list[MRSummary]:
        return list(self.items)


class TestFallbackPage:
    @pytest.mark.asyncio
    async def test_clients_without_paging_are_sliced_newest_first(self) -> None:
        client = _ListOnlyClient()
        first = await read_mr_page(client, "acme/repo", "open", 1, 2)  # type: ignore[arg-type]
        last = await read_mr_page(client, "acme/repo", "open", 3, 2)  # type: ignore[arg-type]
        assert first == MRPage((client.items[4], client.items[3]), has_next=True)
        assert last == MRPage((client.items[0],), has_next=False)

    @pytest.mark.asyncio
    async def test_items_without_an_update_time_sort_last(self) -> None:
        client = _ListOnlyClient()
        undated = [
            replace(client.items[0], number=41, updated_at=None),  # type: ignore[arg-type]
            replace(client.items[0], number=42, updated_at=None),  # type: ignore[arg-type]
        ]
        client.items = [undated[0], *client.items, undated[1]]

        first = await read_mr_page(client, "acme/repo", "open", 1, 3)  # type: ignore[arg-type]
        last = await read_mr_page(client, "acme/repo", "open", 3, 3)  # type: ignore[arg-type]

        assert [item.number for item in first.items] == [5, 4, 3]
        assert [item.number for item in last.items] == [42]
        middle = await read_mr_page(client, "acme/repo", "open", 2, 3)  # type: ignore[arg-type]
        assert [item.number for item in middle.items] == [2, 1, 41]

    @pytest.mark.asyncio
    async def test_invalid_page_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            await ForgeClient.list_mrs_page(_ListOnlyClient(), "acme/repo", page=0)  # type: ignore[arg-type]
