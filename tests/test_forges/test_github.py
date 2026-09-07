"""Tests for GitHub client."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

from tongs.forges.github import GitHubClient
from tongs.forges.models import ForgeHost, ReviewDecision
from tongs.scanner.repo import ForgeType

_TEST_HOST = ForgeHost(
    hostname="github.com",
    forge_type=ForgeType.GITHUB,
    api_base="https://api.github.com",
)


def _make_github_client(handler) -> tuple[GitHubClient, httpx.AsyncClient]:
    """Create a GitHubClient backed by a MockTransport."""
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        transport=transport,
        base_url=_TEST_HOST.api_base,
    )
    return GitHubClient(_TEST_HOST, http), http


def _pr_api_json(overrides: dict | None = None) -> dict:
    """Return a realistic GitHub PR API response dict."""
    data = {
        "number": 10,
        "title": "Add feature",
        "state": "open",
        "draft": False,
        "html_url": "https://github.com/acme/repo/pull/10",
        "created_at": "2026-07-01T09:00:00Z",
        "updated_at": "2026-07-02T14:30:00Z",
        "comments": 2,
        "review_comments": 1,
        "labels": [],
        "user": {"login": "alice", "name": "Alice"},
        "head": {"ref": "feature-branch", "sha": "abc123def456"},
        "base": {
            "ref": "main",
            "sha": "base123def456",
            "repo": {"full_name": "acme/repo"},
        },
        "mergeable_state": "clean",
    }
    if overrides:
        data.update(overrides)
    return data


def _review_comment_json(overrides: dict | None = None) -> dict:
    """Return a GitHub review comment response."""
    data = {
        "id": 999,
        "body": "Looks good",
        "user": {"login": "bob", "name": "Bob"},
        "created_at": "2026-07-02T10:00:00Z",
        "path": "src/main.py",
        "line": 42,
        "side": "RIGHT",
    }
    if overrides:
        data.update(overrides)
    return data


_GHE_HOST = ForgeHost(
    hostname="github.corp.example.com",
    forge_type=ForgeType.GITHUB,
    api_base="https://github.corp.example.com/api/v3",
)


class TestGraphQLProperties:
    def test_graphql_url_github_com(self):
        """github.com uses api.github.com/graphql."""
        transport = httpx.MockTransport(lambda _: httpx.Response(200))
        http = httpx.AsyncClient(transport=transport, base_url=_TEST_HOST.api_base)
        client = GitHubClient(_TEST_HOST, http)
        assert client._graphql_url == "https://api.github.com/graphql"

    def test_graphql_url_ghe(self):
        """GHE hostname uses hostname/api/graphql."""
        transport = httpx.MockTransport(lambda _: httpx.Response(200))
        http = httpx.AsyncClient(transport=transport, base_url=_GHE_HOST.api_base)
        client = GitHubClient(_GHE_HOST, http)
        assert client._graphql_url == "https://github.corp.example.com/api/graphql"

    def test_supports_thread_resolution_true(self):
        """GitHubClient advertises thread resolution support."""
        transport = httpx.MockTransport(lambda _: httpx.Response(200))
        http = httpx.AsyncClient(transport=transport, base_url=_TEST_HOST.api_base)
        client = GitHubClient(_TEST_HOST, http)
        assert client.supports_thread_resolution is True


class TestRevisionMetadata:
    def test_detail_captures_head_and_base_sha(self):
        client, _ = _make_github_client(lambda _: httpx.Response(200))
        detail = client._parse_pr_detail(_pr_api_json(), "acme/repo")
        assert detail.head_sha == "abc123def456"
        assert detail.base_sha == "base123def456"
        assert detail.start_sha is None


class TestCreateInlineComment:
    @pytest.mark.asyncio
    async def test_single_line_no_start_line_or_start_side(self):
        """Single-line comment payload must NOT include start_line/start_side."""
        requests_made = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests_made.append(req)
            if req.method == "GET":
                return httpx.Response(200, json=_pr_api_json())
            return httpx.Response(200, json=_review_comment_json())

        client, http = _make_github_client(handler)
        async with http:
            await client.create_inline_comment(
                repo_path="acme/repo",
                number=10,
                file_path="src/main.py",
                line=42,
                side="RIGHT",
                body="Fix this",
            )

        post_req = next(r for r in requests_made if r.method == "POST")
        payload = json.loads(post_req.content)
        assert payload["body"] == "Fix this"
        assert payload["path"] == "src/main.py"
        assert payload["line"] == 42
        assert payload["side"] == "RIGHT"
        assert payload["commit_id"] == "abc123def456"
        assert "start_line" not in payload
        assert "start_side" not in payload

    @pytest.mark.asyncio
    async def test_multi_line_includes_start_line_and_start_side(self):
        """Multi-line comment payload must include start_line and start_side."""
        requests_made = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests_made.append(req)
            if req.method == "GET":
                return httpx.Response(200, json=_pr_api_json())
            return httpx.Response(200, json=_review_comment_json())

        client, http = _make_github_client(handler)
        async with http:
            await client.create_inline_comment(
                repo_path="acme/repo",
                number=10,
                file_path="src/main.py",
                line=50,
                side="RIGHT",
                body="Refactor this block",
                start_line=45,
                start_side="LEFT",
            )

        post_req = next(r for r in requests_made if r.method == "POST")
        payload = json.loads(post_req.content)
        assert payload["start_line"] == 45
        assert payload["start_side"] == "LEFT"
        assert payload["line"] == 50
        assert payload["side"] == "RIGHT"

    @pytest.mark.asyncio
    async def test_start_side_defaults_to_side_when_none(self):
        """When start_line is set but start_side is None, start_side defaults to side."""
        requests_made = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests_made.append(req)
            if req.method == "GET":
                return httpx.Response(200, json=_pr_api_json())
            return httpx.Response(200, json=_review_comment_json())

        client, http = _make_github_client(handler)
        async with http:
            await client.create_inline_comment(
                repo_path="acme/repo",
                number=10,
                file_path="src/main.py",
                line=50,
                side="RIGHT",
                body="Needs work",
                start_line=45,
                start_side=None,
            )

        post_req = next(r for r in requests_made if r.method == "POST")
        payload = json.loads(post_req.content)
        assert payload["start_line"] == 45
        assert payload["start_side"] == "RIGHT"

    @pytest.mark.asyncio
    async def test_explicit_head_never_refetches_latest_revision(self):
        requests_made = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests_made.append(req)
            assert req.method == "POST"
            return httpx.Response(200, json=_review_comment_json())

        client, http = _make_github_client(handler)
        async with http:
            await client.create_inline_comment(
                "acme/repo",
                10,
                "new.py",
                42,
                "RIGHT",
                "body",
                head_sha="captured-head",
                old_path="old.py",
                new_path="new.py",
            )
        assert [request.method for request in requests_made] == ["POST"]
        assert json.loads(requests_made[0].content)["commit_id"] == "captured-head"


class TestReviewMutationRoutes:
    @pytest.mark.asyncio
    async def test_reply_includes_pull_number_and_top_level_comment(self):
        seen = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return httpx.Response(200, json=_review_comment_json({"id": 1001}))

        client, http = _make_github_client(handler)
        async with http:
            result = await client.reply_to_discussion(
                "acme/repo", 10, "thread", "reply", root_comment_id="999"
            )
        assert seen[0].url.path.endswith("/pulls/10/comments/999/replies")
        assert result.comment_id == "1001"

    @pytest.mark.asyncio
    async def test_discussions_group_replies_under_their_root(self):
        comments = [
            _review_comment_json({"id": 999}),
            _review_comment_json({"id": 1000, "in_reply_to_id": 999}),
        ]
        client, http = _make_github_client(lambda _: httpx.Response(200, json=comments))
        async with http:
            discussions = await client.get_mr_discussions("acme/repo", 10)
        assert len(discussions) == 1
        assert discussions[0].root_comment.replies[0].id == "1000"

    @pytest.mark.asyncio
    async def test_review_payload_binds_top_level_commit_id(self):
        seen = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return httpx.Response(200, json={"id": 77})

        client, http = _make_github_client(handler)
        async with http:
            await client.submit_review(
                "acme/repo",
                10,
                ReviewDecision.APPROVED,
                "",
                head_sha="captured-head",
            )
        assert json.loads(seen[0].content)["commit_id"] == "captured-head"

    @pytest.mark.asyncio
    async def test_thread_lookup_paginates_roots_and_reply_comments(self, monkeypatch):
        client, http = _make_github_client(lambda _: httpx.Response(200))
        pages = [
            {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [],
                            "pageInfo": {"hasNextPage": True, "endCursor": "roots-2"},
                        }
                    }
                }
            },
            {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": [
                                {
                                    "id": "thread-node",
                                    "comments": {
                                        "nodes": [{"databaseId": 1}],
                                        "pageInfo": {
                                            "hasNextPage": True,
                                            "endCursor": "comments-2",
                                        },
                                    },
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            },
            {
                "node": {
                    "comments": {
                        "nodes": [{"databaseId": 999}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            },
        ]
        graphql = AsyncMock(side_effect=pages)
        monkeypatch.setattr(client, "_graphql", graphql)
        assert (
            await client._find_thread_node_id("acme", "repo", 10, 999) == "thread-node"
        )
        assert graphql.await_count == 3
        await http.aclose()
