"""Async HTTP transport layer for forge API calls."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import httpx

from tongs.errors import (
    AuthError,
    ConflictError,
    ForgeError,
    ForgePermissionError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    ValidationError,
    redact_credentials,
)

log = logging.getLogger(__name__)


class RefreshingTokenAuth(httpx.Auth):
    """Bearer auth that re-resolves the token once when a request gets a 401.

    Short-lived credentials (such as glab OAuth tokens) expire during long
    sessions. ``refresh`` is a blocking callable returning a new token, or
    ``None`` when no better credential is available; it runs in a worker thread.
    """

    def __init__(self, token: str, refresh: Callable[[], str | None]):
        self._token = token
        self._refresh = refresh
        self._lock = asyncio.Lock()

    def sync_auth_flow(self, request: httpx.Request):
        raise RuntimeError("RefreshingTokenAuth only supports async clients")

    async def async_auth_flow(self, request: httpx.Request):
        sent_token = self._token
        request.headers["Authorization"] = f"Bearer {sent_token}"
        response = yield request
        if response.status_code != 401:
            return

        async with self._lock:
            # Another request may already have refreshed the token.
            if self._token == sent_token:
                try:
                    new_token = await asyncio.to_thread(self._refresh)
                except ForgeError as e:
                    log.debug("Token refresh failed: %s", e)
                    new_token = None
                if not new_token or new_token == sent_token:
                    return
                log.info("Retrying request with refreshed credentials")
                self._token = new_token

        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


def create_client(
    base_url: str,
    token: str,
    timeout: float = 30.0,
    refresh: Callable[[], str | None] | None = None,
) -> httpx.AsyncClient:
    """Create an authenticated async HTTP client for a forge API.

    When ``refresh`` is given, a 401 triggers one token refresh and retry.
    """
    headers = {"Accept": "application/json"}
    auth = None
    if refresh is None:
        headers["Authorization"] = f"Bearer {token}"
    else:
        auth = RefreshingTokenAuth(token, refresh)
    return httpx.AsyncClient(
        base_url=base_url,
        auth=auth,
        headers=headers,
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
    if status == 405:
        # GitHub's PR merge endpoint answers a merge attempt it will not
        # perform (merge conflicts, required checks unmet, etc.) with 405
        # and a reason in the body, e.g. {"message": "Pull Request has merge
        # conflicts"}. GitLab's merge endpoint uses the same status when
        # merge_request.mergeable? is false (conflicts, draft, unresolved
        # threads, a failing or pending pipeline), decided before any merge
        # is attempted. Both are a definitive, known-state rejection rather
        # than an ambiguous failure.
        return ConflictError(f"Conflict: {body}")
    if status == 406:
        # Legacy GitLab releases returned 406 on the merge endpoint itself
        # for "branch cannot be merged"; current GitLab reserves 406 for
        # cancel_merge_when_pipeline_succeeds (no auto-merge to cancel).
        # Neither case runs a mutation before returning 406, so it stays a
        # definitive, known-state rejection like 405.
        return ConflictError(f"Conflict: {body}")
    if status == 409:
        return ConflictError(f"Conflict: {body}")
    if status in {400, 422}:
        return ValidationError(f"Validation failed: {body}")
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
    *,
    _retried: bool = False,
    **kwargs,
) -> dict | list:
    """Make an authenticated API request with error mapping.

    Returns parsed JSON response body.
    Raises appropriate ForgeError subclass on failure.
    On 429 (rate limit), waits ``retry_after`` seconds and retries once.
    """
    response = await _send(client, method, path, _retried=_retried, **kwargs)
    if response.status_code == 204:
        return {}
    return response.json()


async def request_page(
    client: httpx.AsyncClient,
    path: str,
    *,
    page: int,
    per_page: int,
    params: dict[str, str | int] | None = None,
) -> tuple[list[dict], bool]:
    """Read exactly one page of a list endpoint.

    Returns the page's items and whether the forge reports another page. The
    ``Link`` header (GitHub and GitLab) and GitLab's ``X-Next-Page`` header are
    authoritative when present; without either, a full page is taken to mean
    another page may follow, the same rule ``paginate`` uses.
    """
    query: dict[str, str | int] = dict(params or {})
    query["per_page"] = per_page
    query["page"] = page
    response = await _send(client, "GET", path, params=query)
    data = [] if response.status_code == 204 else response.json()
    if not isinstance(data, list):
        raise ValidationError("Validation failed: expected a list response")
    return data, _has_next_page(response, len(data), per_page)


def _has_next_page(response: httpx.Response, count: int, per_page: int) -> bool:
    next_page = response.headers.get("X-Next-Page")
    if next_page is not None:
        return bool(next_page.strip())
    link = response.headers.get("Link")
    if link is not None:
        return any(
            'rel="next"' in part.replace(" ", "") or "rel=next" in part
            for part in link.split(",")
        )
    return count >= per_page


async def _send(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    _retried: bool = False,
    **kwargs,
) -> httpx.Response:
    try:
        response = await client.request(method, path, **kwargs)
    except httpx.TimeoutException as e:
        raise NetworkError(f"Request timed out: {redact_credentials(str(e))}") from e
    except httpx.TransportError as e:
        raise NetworkError(f"Transport error: {redact_credentials(str(e))}") from e

    if response.status_code >= 400:
        err = map_http_error(response)
        if isinstance(err, RateLimitError) and not _retried:
            delay = err.retry_after if err.retry_after is not None else 5
            log.warning("Rate limited on %s %s, retrying in %ds", method, path, delay)
            await asyncio.sleep(delay)
            return await _send(client, method, path, _retried=True, **kwargs)
        raise err

    return response


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
    except (ValueError, AttributeError):
        msg = response.text[:200]
    return redact_credentials(str(msg))
