#!/usr/bin/env python3
"""Exercise the installed tongs-mcp command through the MCP client API."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def _verify() -> dict[str, object]:
    parameters = StdioServerParameters(command="/usr/bin/tongs-mcp")
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialization = await session.initialize()
            tools = await session.list_tools()
    names = sorted(tool.name for tool in tools.tools)
    expected = [
        "approve_mr",
        "get_mr",
        "get_mr_diff",
        "list_mrs",
        "list_pipelines",
        "post_comment",
    ]
    if names != expected:
        raise RuntimeError(f"installed MCP tool set mismatch: {names}")
    return {
        "command": parameters.command,
        "protocol_version": initialization.protocolVersion,
        "tools": names,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = asyncio.run(_verify())
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
