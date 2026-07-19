"""Async HTTP transport layer for forge API calls."""

from __future__ import annotations

import httpx

from tongs.errors import (
    AuthError,
    ConflictError,
    ForgeError,
    ForgePermissionError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    redact_credentials,
)


def create_client(
    base_url: str,
    token: str,
    timeout: float = 30.0,
) -> httpx.AsyncClient:
    """Create an authenticated async HTTP client for a forge API."""
    return httpx.AsyncClient(
        base_url=base_url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
        timeout=timeout,
        follow_redirects=True,
    )


def map_http_error(response: httpx.Response) -> ForgeError:
    """Map an HTTP error response to the appropriate ForgeError subclass."""
    status = response.status_code
    body = _safe_body(response)

    if status == 401:
        return AuthError(f"Authentication failed: {body}")
    if status == 403:
        return ForgePermissionError(f"Insufficient permissions: {body}")
    if status == 404:
        return NotFoundError(f"Not found: {body}")
    if status == 409:
        return ConflictError(f"Conflict: {body}")
    if status == 429:
        retry_after = response.headers.get("Retry-After")
        retry_seconds = (
            int(retry_after) if retry_after and retry_after.isdigit() else None
        )
        return RateLimitError(f"Rate limited: {body}", retry_after=retry_seconds)

    return ForgeError(f"HTTP {status}: {body}")


async def request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    **kwargs,
) -> dict | list:
    """Make an authenticated API request with error mapping.

    Returns parsed JSON response body.
    Raises appropriate ForgeError subclass on failure.
    """
    try:
        response = await client.request(method, path, **kwargs)
    except httpx.TimeoutException as e:
        raise NetworkError(f"Request timed out: {redact_credentials(str(e))}") from e
    except httpx.TransportError as e:
        raise NetworkError(f"Transport error: {redact_credentials(str(e))}") from e

    if response.status_code >= 400:
        raise map_http_error(response)

    if response.status_code == 204:
        return {}

    return response.json()


async def paginate(
    client: httpx.AsyncClient,
    path: str,
    per_page: int = 20,
    max_pages: int | None = None,
    **kwargs,
) -> list[dict]:
    """Paginate a GET request, collecting all results."""
    params = dict(kwargs.pop("params", {}))
    params["per_page"] = per_page

    results: list[dict] = []
    page = 1

    while True:
        params["page"] = page
        data = await request(client, "GET", path, params=params, **kwargs)

        if isinstance(data, list):
            results.extend(data)
            if len(data) < per_page:
                break
        else:
            results.append(data)
            break

        page += 1
        if max_pages and page > max_pages:
            break

    return results


def _safe_body(response: httpx.Response) -> str:
    """Extract a safe, redacted body string from a response."""
    try:
        data = response.json()
        msg = data.get("message", data.get("error", str(data)))
    except Exception:
        msg = response.text[:200]
    return redact_credentials(str(msg))
