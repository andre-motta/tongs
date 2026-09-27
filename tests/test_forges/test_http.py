"""Tests for HTTP transport layer."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from tongs.errors import (
    AuthError,
    ConflictError,
    ForgeError,
    ForgePermissionError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    ValidationError,
)
from tongs.forges.http import (
    RefreshingTokenAuth,
    create_client,
    map_http_error,
    paginate,
    request,
)


class _FakeResponse:
    """Minimal response mock for testing error mapping."""

    def __init__(self, status_code: int, body: str = "{}", headers: dict | None = None):
        self.status_code = status_code
        self._body = body
        self.text = body
        self.headers = headers or {}

    def json(self):
        import json

        return json.loads(self._body)


class TestMapHttpError:
    def test_401_returns_auth_error(self):
        resp = _FakeResponse(401, '{"message": "Unauthorized"}')
        assert isinstance(map_http_error(resp), AuthError)

    def test_403_returns_permission_error(self):
        resp = _FakeResponse(403, '{"message": "Forbidden"}')
        assert isinstance(map_http_error(resp), ForgePermissionError)

    def test_404_returns_not_found(self):
        resp = _FakeResponse(404, '{"message": "Not Found"}')
        assert isinstance(map_http_error(resp), NotFoundError)

    def test_405_returns_conflict_and_carries_the_forge_reason(self):
        resp = _FakeResponse(405, '{"message": "Pull Request has merge conflicts"}')
        err = map_http_error(resp)
        assert isinstance(err, ConflictError)
        assert "Pull Request has merge conflicts" in str(err)

    def test_406_returns_conflict_and_carries_the_forge_reason(self):
        resp = _FakeResponse(406, '{"message": "Branch cannot be merged"}')
        err = map_http_error(resp)
        assert isinstance(err, ConflictError)
        assert "Branch cannot be merged" in str(err)

    def test_409_returns_conflict(self):
        resp = _FakeResponse(409, '{"message": "Merge conflict"}')
        assert isinstance(map_http_error(resp), ConflictError)

    @pytest.mark.parametrize("status", [400, 422])
    def test_validation_rejections_are_definite_not_conflicts(self, status):
        resp = _FakeResponse(status, '{"message": "Validation Failed"}')
        err = map_http_error(resp)
        assert isinstance(err, ValidationError)
        assert not isinstance(err, ConflictError)
        assert "Validation Failed" in str(err)

    def test_429_returns_rate_limit(self):
        resp = _FakeResponse(
            429,
            '{"message": "Too Many Requests"}',
            headers={"Retry-After": "60"},
        )
        err = map_http_error(resp)
        assert isinstance(err, RateLimitError)
        assert err.retry_after == 60

    def test_429_without_retry_after(self):
        resp = _FakeResponse(429, '{"message": "Too Many Requests"}')
        err = map_http_error(resp)
        assert isinstance(err, RateLimitError)
        assert err.retry_after is None

    def test_500_returns_generic_forge_error(self):
        resp = _FakeResponse(500, '{"error": "Internal Server Error"}')
        err = map_http_error(resp)
        assert isinstance(err, ForgeError)
        assert not isinstance(err, AuthError)

    def test_redacts_tokens_in_error_body(self):
        resp = _FakeResponse(401, '{"message": "Token glpat-secret123 invalid"}')
        err = map_http_error(resp)
        assert "glpat-secret123" not in str(err)
        assert "[REDACTED]" in str(err)

    def test_handles_non_json_body(self):
        resp = _FakeResponse(502, "Bad Gateway")
        resp.json = lambda: (_ for _ in ()).throw(ValueError("not json"))
        err = map_http_error(resp)
        assert isinstance(err, ForgeError)
        assert "Bad Gateway" in str(err)

    @pytest.mark.parametrize(
        "body", ['["glpat-secret123"]', '"glpat-secret123"', "null"]
    )
    def test_non_object_json_errors_still_redact_credentials(self, body: str) -> None:
        err = map_http_error(_FakeResponse(502, body))
        assert isinstance(err, ForgeError)
        assert "glpat-secret123" not in str(err)
        if "glpat-" in body:
            assert "[REDACTED]" in str(err)


def _make_async_client(handler) -> httpx.AsyncClient:
    """Create an AsyncClient backed by a MockTransport."""
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(
        transport=transport,
        base_url="https://gitlab.example.com/api/v4",
    )


class TestRequest:
    @pytest.mark.asyncio
    async def test_request_returns_parsed_json(self):
        payload = {"id": 1, "name": "test-project"}

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        client = _make_async_client(handler)
        async with client:
            result = await request(client, "GET", "/projects/1")
        assert result == payload

    @pytest.mark.asyncio
    async def test_request_204_returns_empty_dict(self):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(204)

        client = _make_async_client(handler)
        async with client:
            result = await request(client, "POST", "/projects/1/approve")
        assert result == {}

    @pytest.mark.asyncio
    async def test_request_connect_error_raises_network_error(self):
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = _make_async_client(handler)
        async with client:
            with pytest.raises(NetworkError, match="connection refused"):
                await request(client, "GET", "/projects/1")

    @pytest.mark.asyncio
    async def test_request_timeout_raises_network_error(self):
        def handler(req: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timed out")

        client = _make_async_client(handler)
        async with client:
            with pytest.raises(NetworkError, match="timed out"):
                await request(client, "GET", "/projects/1")

    @pytest.mark.asyncio
    async def test_request_http_error_raises_mapped_forge_error(self):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        client = _make_async_client(handler)
        async with client:
            with pytest.raises(NotFoundError):
                await request(client, "GET", "/projects/999")


class TestRateLimitRetry:
    @pytest.mark.asyncio
    async def test_429_with_retry_after_retries_and_succeeds(self):
        """A 429 with Retry-After header triggers a single retry that succeeds."""
        call_count = 0

        def handler(req: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(
                    429,
                    json={"message": "Too Many Requests"},
                    headers={"Retry-After": "0"},
                )
            return httpx.Response(200, json={"ok": True})

        client = _make_async_client(handler)
        async with client:
            result = await request(client, "GET", "/projects/1")
        assert result == {"ok": True}
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_429_twice_fails_on_second_attempt(self):
        """If the retry also returns 429, the error is raised (only one retry)."""

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                json={"message": "Too Many Requests"},
                headers={"Retry-After": "0"},
            )

        client = _make_async_client(handler)
        async with client:
            with pytest.raises(RateLimitError):
                await request(client, "GET", "/projects/1")


class TestPaginate:
    @pytest.mark.asyncio
    async def test_paginate_single_page(self):
        items = [{"id": 1}, {"id": 2}]

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=items)

        client = _make_async_client(handler)
        async with client:
            result = await paginate(client, "/items", per_page=20)
        assert result == items

    @pytest.mark.asyncio
    async def test_paginate_multi_page_collects_all(self):
        pages = {
            1: [{"id": i} for i in range(1, 4)],
            2: [{"id": i} for i in range(4, 7)],
            3: [{"id": 7}],
        }

        def handler(req: httpx.Request) -> httpx.Response:
            page = int(req.url.params.get("page", "1"))
            return httpx.Response(200, json=pages.get(page, []))

        client = _make_async_client(handler)
        async with client:
            result = await paginate(client, "/items", per_page=3)
        assert len(result) == 7
        assert [r["id"] for r in result] == list(range(1, 8))

    @pytest.mark.asyncio
    async def test_paginate_stops_on_partial_page(self):
        """When a page returns fewer items than per_page, pagination stops."""

        def handler(req: httpx.Request) -> httpx.Response:
            page = int(req.url.params.get("page", "1"))
            if page == 1:
                return httpx.Response(200, json=[{"id": 1}, {"id": 2}])
            # Should never reach page 2 because page 1 returned < per_page items
            return httpx.Response(200, json=[{"id": 99}])

        client = _make_async_client(handler)
        async with client:
            result = await paginate(client, "/items", per_page=5)
        assert len(result) == 2
        assert result[0]["id"] == 1

    @pytest.mark.asyncio
    async def test_paginate_respects_max_pages(self):
        """Pagination stops after max_pages even if pages are full."""

        def handler(req: httpx.Request) -> httpx.Response:
            page = int(req.url.params.get("page", "1"))
            # Always return a full page to keep pagination going
            return httpx.Response(200, json=[{"id": page * 10 + i} for i in range(3)])

        client = _make_async_client(handler)
        async with client:
            result = await paginate(client, "/items", per_page=3, max_pages=2)
        # 2 pages * 3 items each = 6 items
        assert len(result) == 6


def _auth_client(handler, auth: httpx.Auth) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://gitlab.example.com/api/v4",
        auth=auth,
    )


class TestRefreshingTokenAuth:
    @pytest.mark.asyncio
    async def test_sends_bearer_token(self):
        seen = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.headers["Authorization"])
            return httpx.Response(200, json={})

        refresh_calls = []
        auth = RefreshingTokenAuth("tok-1", lambda: refresh_calls.append(1))
        async with _auth_client(handler, auth) as client:
            await request(client, "GET", "/user")
        assert seen == ["Bearer tok-1"]
        assert refresh_calls == []

    @pytest.mark.asyncio
    async def test_retries_once_with_refreshed_token_on_401(self):
        seen = []

        def handler(req: httpx.Request) -> httpx.Response:
            seen.append(req.headers["Authorization"])
            if req.headers["Authorization"] == "Bearer expired":
                return httpx.Response(401, json={"message": "401 Unauthorized"})
            return httpx.Response(200, json={"ok": True})

        auth = RefreshingTokenAuth("expired", lambda: "fresh")
        async with _auth_client(handler, auth) as client:
            result = await request(client, "GET", "/user")
            # Later requests keep using the refreshed token.
            await request(client, "GET", "/user")
        assert result == {"ok": True}
        assert seen == ["Bearer expired", "Bearer fresh", "Bearer fresh"]

    @pytest.mark.asyncio
    async def test_retried_post_resends_body(self):
        bodies = []

        def handler(req: httpx.Request) -> httpx.Response:
            bodies.append(req.content)
            if req.headers["Authorization"] == "Bearer expired":
                return httpx.Response(401, json={})
            return httpx.Response(201, json={"id": 1})

        auth = RefreshingTokenAuth("expired", lambda: "fresh")
        async with _auth_client(handler, auth) as client:
            await request(client, "POST", "/notes", json={"body": "hi"})
        assert len(bodies) == 2
        assert bodies[0] == bodies[1] == b'{"body":"hi"}'

    @pytest.mark.asyncio
    @pytest.mark.parametrize("refreshed", [None, "same"])
    async def test_raises_auth_error_when_refresh_gives_nothing_new(self, refreshed):
        calls = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(req.headers["Authorization"])
            return httpx.Response(401, json={"message": "401 Unauthorized"})

        auth = RefreshingTokenAuth("same", lambda: refreshed)
        async with _auth_client(handler, auth) as client:
            with pytest.raises(AuthError):
                await request(client, "GET", "/user")
        assert calls == ["Bearer same"]

    @pytest.mark.asyncio
    async def test_refresh_error_surfaces_original_401(self):
        def refresh():
            raise AuthError("No credentials found")

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"message": "401 Unauthorized"})

        auth = RefreshingTokenAuth("expired", refresh)
        async with _auth_client(handler, auth) as client:
            with pytest.raises(AuthError, match="Authentication failed"):
                await request(client, "GET", "/user")

    @pytest.mark.asyncio
    async def test_gives_up_when_refreshed_token_also_rejected(self):
        calls = []

        def handler(req: httpx.Request) -> httpx.Response:
            calls.append(req.headers["Authorization"])
            return httpx.Response(401, json={})

        auth = RefreshingTokenAuth("expired", lambda: "also-bad")
        async with _auth_client(handler, auth) as client:
            with pytest.raises(AuthError):
                await request(client, "GET", "/user")
        assert calls == ["Bearer expired", "Bearer also-bad"]

    @pytest.mark.asyncio
    async def test_concurrent_401s_refresh_once(self):
        refresh_calls = []

        def refresh():
            refresh_calls.append(1)
            return "fresh"

        def handler(req: httpx.Request) -> httpx.Response:
            if req.headers["Authorization"] == "Bearer expired":
                return httpx.Response(401, json={})
            return httpx.Response(200, json={})

        auth = RefreshingTokenAuth("expired", refresh)
        async with _auth_client(handler, auth) as client:
            await asyncio.gather(*(request(client, "GET", "/user") for _ in range(5)))
        assert refresh_calls == [1]


class TestCreateClient:
    def test_static_token_header_without_refresh(self):
        client = create_client("https://gitlab.example.com/api/v4", "tok")
        assert client.headers["Authorization"] == "Bearer tok"
        assert client.auth is None

    def test_refreshing_auth_with_refresh(self):
        client = create_client(
            "https://gitlab.example.com/api/v4", "tok", refresh=lambda: None
        )
        assert "Authorization" not in client.headers
        assert isinstance(client.auth, RefreshingTokenAuth)
