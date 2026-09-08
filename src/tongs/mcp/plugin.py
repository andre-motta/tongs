"""MCP server plugin for tongs."""

from __future__ import annotations

import subprocess
import sys
from importlib.util import find_spec

from tongs.plugins.base import TongsPlugin


def _mcp_available() -> bool:
    """Return whether the optional MCP server dependency can be imported."""
    try:
        return find_spec("mcp.server.fastmcp") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


class MCPPlugin(TongsPlugin):
    """Registers the MCP server as a command in the tongs TUI."""

    @property
    def name(self) -> str:
        return "mcp"

    @property
    def version(self) -> str:
        return "0.2.0"

    def get_commands(self) -> list[tuple[str, str, object]]:
        if not _mcp_available():
            return []
        return [
            (
                "Start MCP Server",
                "Launch tongs-mcp server for AI agent integration",
                self._start_server,
            ),
        ]

    def _start_server(self) -> None:
        if not _mcp_available():
            raise RuntimeError("the optional MCP dependency is not installed")
        subprocess.Popen(
            [sys.executable, "-m", "tongs.mcp.server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
