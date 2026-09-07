"""Production NDJSON stdio entry point for the Tongs desktop sidecar."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import cast

from tongs.desktop.protocol.messages import MAX_REQUEST_FRAME_BYTES
from tongs.desktop.protocol.server import DesktopSidecarServer


class _WritePipeProtocol(asyncio.Protocol):
    """Public-API flow control for the sidecar's stdout pipe."""

    def __init__(self) -> None:
        self.transport: asyncio.WriteTransport | None = None
        self.error: Exception | None = None
        self.resumed = asyncio.Event()
        self.resumed.set()
        self.closed = asyncio.Event()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = cast(asyncio.WriteTransport, transport)

    def pause_writing(self) -> None:
        self.resumed.clear()

    def resume_writing(self) -> None:
        self.resumed.set()

    def connection_lost(self, exc: Exception | None) -> None:
        self.error = exc
        self.resumed.set()
        self.closed.set()

    async def drain(self) -> None:
        await self.resumed.wait()
        if self.error is not None:
            raise self.error


class _PipeWriter:
    def __init__(
        self, transport: asyncio.WriteTransport, protocol: _WritePipeProtocol
    ) -> None:
        self._transport = transport
        self._protocol = protocol

    def write(self, data: bytes) -> None:
        if self._transport.is_closing():
            raise ConnectionError("desktop protocol pipe is closed")
        self._transport.write(data)

    async def drain(self) -> None:
        await self._protocol.drain()

    def close(self) -> None:
        self._transport.close()

    async def wait_closed(self) -> None:
        await self._protocol.closed.wait()


async def run_stdio() -> None:
    """Bind one sidecar server to process stdin and the original stdout pipe."""
    loop = asyncio.get_running_loop()
    protocol_fd = os.dup(sys.stdout.fileno())
    os.set_inheritable(protocol_fd, False)
    protocol_stdout = os.fdopen(protocol_fd, "wb", buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno(), inheritable=True)
    sys.stdout = sys.stderr

    reader = asyncio.StreamReader(limit=MAX_REQUEST_FRAME_BYTES + 1)
    reader_protocol = asyncio.StreamReaderProtocol(reader)
    await loop.connect_read_pipe(lambda: reader_protocol, sys.stdin.buffer)

    writer_protocol = _WritePipeProtocol()
    transport, _ = await loop.connect_write_pipe(
        lambda: writer_protocol, protocol_stdout
    )
    writer = _PipeWriter(cast(asyncio.WriteTransport, transport), writer_protocol)
    try:
        await DesktopSidecarServer().run(reader, writer)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionError):
            pass


def main() -> int:
    """Run the sidecar and return a process status without stdout diagnostics."""
    try:
        asyncio.run(run_stdio())
    except (BrokenPipeError, ConnectionError):
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as error:  # noqa: BLE001 - process boundary stays redacted.
        print(f"tongs desktop sidecar failed: {type(error).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run_stdio"]
