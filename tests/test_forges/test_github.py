"""Tests for GitHub client."""

import json

import httpx
import pytest

from tongs.forges.github import GitHubClient
from tongs.forges.models import ForgeHost
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
        "base": {"ref": "main", "repo": {"full_name": "acme/repo"}},
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

        post_req = [r for r in requests_made if r.method == "POST"][0]
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

        post_req = [r for r in requests_made if r.method == "POST"][0]
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

        post_req = [r for r in requests_made if r.method == "POST"][0]
        payload = json.loads(post_req.content)
        assert payload["start_line"] == 45
        assert payload["start_side"] == "RIGHT"
