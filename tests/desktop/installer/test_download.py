"""Bounded fixed-origin release download tests."""

from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest

from tongs.desktop.installer.download import download_bytes
from tongs.desktop.installer.models import (
    InstallerError,
    InstallerErrorCode,
    InstallerLimits,
)


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes], gate: asyncio.Event | None = None) -> None:
        self.chunks = chunks
        self.gate = gate

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.gate is not None:
                await self.gate.wait()
            yield chunk


@pytest.mark.asyncio
async def test_streams_exact_body_without_forwarding_client_auth() -> None:
    body = b"verified bytes"
    seen_authorization: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_authorization.append(request.headers.get("authorization"))
        return httpx.Response(200, content=body)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        auth=("private-user", "private-token"),
    ) as client:
        result = await download_bytes(
            client,
            "https://api.github.com/release",
            limits=InstallerLimits(),
            maximum_bytes=100,
            expected_byte_count=len(body),
            expected_sha256=hashlib.sha256(body).hexdigest(),
        )

    assert result == body
    assert seen_authorization == [None]


@pytest.mark.asyncio
async def test_rejects_redirect_to_untrusted_origin_before_request() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://evil.test/archive"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(InstallerError) as raised:
            await download_bytes(
                client,
                "https://api.github.com/release",
                limits=InstallerLimits(),
                maximum_bytes=100,
            )

    assert raised.value.code is InstallerErrorCode.DOWNLOAD_FAILED
    assert seen == ["https://api.github.com/release"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "chunks", "expected"),
    [
        ({"content-length": "11"}, [b"12345678901"], InstallerErrorCode.LIMIT_EXCEEDED),
        ({}, [b"12345"], InstallerErrorCode.INTEGRITY_FAILED),
        ({"content-encoding": "gzip"}, [b"x"], InstallerErrorCode.DOWNLOAD_FAILED),
    ],
)
async def test_oversized_truncated_and_encoded_responses_are_rejected(
    headers: dict[str, str],
    chunks: list[bytes],
    expected: InstallerErrorCode,
) -> None:
    response = lambda _request: httpx.Response(
        200, headers=headers, stream=ChunkStream(chunks)
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        with pytest.raises(InstallerError) as raised:
            await download_bytes(
                client,
                "https://api.github.com/release",
                limits=InstallerLimits(),
                maximum_bytes=10,
                expected_byte_count=10,
            )

    assert raised.value.code is expected


@pytest.mark.asyncio
async def test_cancellation_propagates_from_blocked_stream() -> None:
    gate = asyncio.Event()
    response = lambda _request: httpx.Response(
        200, stream=ChunkStream([b"content"], gate)
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        task = asyncio.create_task(
            download_bytes(
                client,
                "https://api.github.com/release",
                limits=InstallerLimits(),
                maximum_bytes=100,
            )
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_transport_timeout_is_safe_and_retryable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("remote details", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(InstallerError) as raised:
            await download_bytes(
                client,
                "https://api.github.com/release",
                limits=InstallerLimits(),
                maximum_bytes=100,
            )

    assert raised.value.code is InstallerErrorCode.DOWNLOAD_FAILED
    assert raised.value.retryable is True
    assert "remote details" not in str(raised.value)


@pytest.mark.asyncio
async def test_wrong_digest_is_rejected_after_exact_transfer() -> None:
    body = b"complete but wrong"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, content=body)
        )
    ) as client:
        with pytest.raises(InstallerError) as raised:
            await download_bytes(
                client,
                "https://api.github.com/release",
                limits=InstallerLimits(),
                maximum_bytes=100,
                expected_byte_count=len(body),
                expected_sha256="0" * 64,
            )

    assert raised.value.code is InstallerErrorCode.INTEGRITY_FAILED
