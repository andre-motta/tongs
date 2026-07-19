"""Tests for HTTP transport layer."""

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
)
from tongs.forges.http import map_http_error, paginate, request


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

    def test_409_returns_conflict(self):
        resp = _FakeResponse(409, '{"message": "Merge conflict"}')
        assert isinstance(map_http_error(resp), ConflictError)

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
