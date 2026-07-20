"""MCP server plugin for tongs."""

from __future__ import annotations

import subprocess
import sys

from tongs.plugins.base import TongsPlugin


class MCPPlugin(TongsPlugin):
    """Registers the MCP server as a command in the tongs TUI."""

    @property
    def name(self) -> str:
        return "mcp"

    @property
    def version(self) -> str:
        return "0.2.0"

    def get_commands(self) -> list[tuple[str, str, object]]:
        return [
            (
                "Start MCP Server",
                "Launch tongs-mcp server for AI agent integration",
                self._start_server,
            ),
        ]

    def _start_server(self) -> None:
        subprocess.Popen(
            [sys.executable, "-m", "tongs.mcp.server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
