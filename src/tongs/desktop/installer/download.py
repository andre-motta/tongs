"""Bounded HTTPS downloads for fixed-repository desktop release assets."""

from __future__ import annotations

import asyncio
import hashlib
from urllib.parse import urljoin, urlsplit

import httpx

from tongs.desktop.installer.models import (
    InstallerError,
    InstallerErrorCode,
    InstallerLimits,
    ReleaseAsset,
)

GITHUB_API_ORIGIN = "https://api.github.com"
_ALLOWED_DOWNLOAD_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "github-releases.githubusercontent.com",
    }
)


async def download_asset_bytes(
    client: httpx.AsyncClient,
    asset: ReleaseAsset,
    *,
    limits: InstallerLimits,
    maximum_bytes: int,
    expected_sha256: str | None = None,
) -> bytes:
    """Download one validated release asset into a single immutable buffer."""
    if maximum_bytes <= 0:
        raise InstallerError(
            InstallerErrorCode.LIMIT_EXCEEDED,
            "The requested desktop download limit is invalid.",
        )
    if asset.byte_count > maximum_bytes:
        raise InstallerError(
            InstallerErrorCode.LIMIT_EXCEEDED,
            "The desktop release asset exceeds the installer download limit.",
        )
    return await download_bytes(
        client,
        asset.api_url,
        limits=limits,
        maximum_bytes=maximum_bytes,
        expected_byte_count=asset.byte_count,
        expected_sha256=expected_sha256,
        accept="application/octet-stream",
    )


async def download_bytes(
    client: httpx.AsyncClient,
    url: str,
    *,
    limits: InstallerLimits,
    maximum_bytes: int,
    expected_byte_count: int | None = None,
    expected_sha256: str | None = None,
    accept: str = "application/vnd.github+json",
) -> bytes:
    """Stream one HTTPS response with explicit redirects, size and digest bounds."""
    if maximum_bytes <= 0:
        raise InstallerError(
            InstallerErrorCode.LIMIT_EXCEEDED,
            "The desktop download limit is invalid.",
        )
    current_url = url
    for redirect_count in range(limits.max_redirects + 1):
        _validate_download_url(current_url)
        request = httpx.Request(
            "GET",
            current_url,
            headers={
                "Accept": accept,
                "User-Agent": "tongs-desktop-installer",
            },
        )
        response: httpx.Response | None = None
        try:
            response = await client.send(
                request,
                stream=True,
                auth=None,
                follow_redirects=False,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                if redirect_count >= limits.max_redirects:
                    _download_error(
                        "Desktop release download redirected too many times."
                    )
                location = response.headers.get("location")
                if not location:
                    _download_error(
                        "Desktop release download returned an invalid redirect."
                    )
                current_url = urljoin(current_url, location)
                continue
            if response.status_code != 200:
                _download_error("Desktop release download was rejected by its source.")
            content_encoding = response.headers.get("content-encoding", "identity")
            if content_encoding.lower() not in {"", "identity"}:
                _download_error("Encoded desktop release responses are unsupported.")
            _validate_content_length(
                response.headers.get("content-length"),
                maximum_bytes,
                expected_byte_count,
            )
            return await _consume_response(
                response,
                maximum_bytes=maximum_bytes,
                expected_byte_count=expected_byte_count,
                expected_sha256=expected_sha256,
                chunk_bytes=limits.stream_chunk_bytes,
            )
        except asyncio.CancelledError:
            raise
        except InstallerError:
            raise
        except httpx.TransportError:
            raise InstallerError(
                InstallerErrorCode.DOWNLOAD_FAILED,
                "The desktop release download did not complete.",
                retryable=True,
            ) from None
        finally:
            if response is not None:
                await response.aclose()
    raise AssertionError("redirect loop did not return")


async def _consume_response(
    response: httpx.Response,
    *,
    maximum_bytes: int,
    expected_byte_count: int | None,
    expected_sha256: str | None,
    chunk_bytes: int,
) -> bytes:
    content = bytearray()
    byte_count = 0
    digest = hashlib.sha256()
    async for chunk in response.aiter_bytes(chunk_size=chunk_bytes):
        byte_count += len(chunk)
        if byte_count > maximum_bytes:
            raise InstallerError(
                InstallerErrorCode.LIMIT_EXCEEDED,
                "The desktop release response exceeds the download limit.",
            )
        content.extend(chunk)
        digest.update(chunk)
    if expected_byte_count is not None and byte_count != expected_byte_count:
        raise InstallerError(
            InstallerErrorCode.INTEGRITY_FAILED,
            "The desktop release response length is incorrect.",
        )
    if expected_sha256 is not None and digest.hexdigest() != expected_sha256:
        raise InstallerError(
            InstallerErrorCode.INTEGRITY_FAILED,
            "The desktop release response digest is incorrect.",
        )
    return bytes(content)


def _validate_download_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        _download_error("Desktop release source URL is invalid.")
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _ALLOWED_DOWNLOAD_HOSTS
        or port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        _download_error("Desktop release source URL is not trusted.")


def _validate_content_length(
    value: str | None, maximum_bytes: int, expected_byte_count: int | None
) -> None:
    if value is None:
        return
    try:
        if not value.isascii() or not value.isdigit():
            raise ValueError
        byte_count = int(value)
    except ValueError:
        _download_error("Desktop release response length is invalid.")
    if byte_count < 0 or byte_count > maximum_bytes:
        raise InstallerError(
            InstallerErrorCode.LIMIT_EXCEEDED,
            "The desktop release response exceeds the download limit.",
        )
    if expected_byte_count is not None and byte_count != expected_byte_count:
        raise InstallerError(
            InstallerErrorCode.INTEGRITY_FAILED,
            "The desktop release response length is incorrect.",
        )


def _download_error(message: str) -> None:
    raise InstallerError(InstallerErrorCode.DOWNLOAD_FAILED, message)


__all__ = ["GITHUB_API_ORIGIN", "download_asset_bytes", "download_bytes"]
