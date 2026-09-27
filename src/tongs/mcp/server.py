"""MCP server exposing tongs forge operations as tools.

Entry point: tongs-mcp
Tools are high-level only. Destructive actions (merge, close, reopen,
cancel) are intentionally excluded as a security boundary.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from tongs.config import load_config
from tongs.errors import ForgeError
from tongs.forges.base import ForgeClient
from tongs.forges.registry import ForgeRegistry
from tongs.scanner.repo import ForgeType
from tongs.services.errors import translate_error

mcp = FastMCP("tongs")

_registry: ForgeRegistry | None = None
_REPO_PATH_RE = re.compile(r"^(?!.*\.\.)[\w.-]+(?:/[\w.-]+){2,}$", re.ASCII)


def _get_registry() -> ForgeRegistry:
    global _registry
    if _registry is None:
        config = load_config()
        _registry = ForgeRegistry(
            extra_gitlab_hosts=config.extra_gitlab_hosts,
            extra_github_hosts=config.extra_github_hosts,
            request_timeout=config.request_timeout,
        )
    return _registry


def _parse_host_repo(repo_path: str) -> tuple[str, str]:
    """Split 'hostname/owner/repo' into (hostname, 'owner/repo').

    Only github.com, gitlab.com and the hosts configured in config.toml are
    accepted, the same set the terminal and desktop apps use. Any other host
    is rejected here, before a client is built, a token is looked up or a
    request is made.
    """
    if not _REPO_PATH_RE.match(repo_path):
        raise ValueError(f"repo_path must be 'hostname/owner/repo', got: {repo_path}")
    hostname, path = repo_path.split("/", 1)
    registry = _get_registry()
    host = (
        registry.get_host(hostname) if hostname in registry.active_hostnames() else None
    )
    if host is None:
        raise ValueError(
            f"Host {hostname!r} is not configured; add it under [hosts.*] in "
            "config.toml to use it with tongs-mcp"
        )
    if host.forge_type == ForgeType.GITHUB and path.count("/") != 1:
        raise ValueError(
            f"repo_path for a GitHub host must be 'hostname/owner/repo', got: {repo_path}"
        )
    return hostname, path


@contextmanager
def _redacted_forge_errors(operation: str) -> Iterator[None]:
    """Replace any forge error with a fixed message before it reaches the client.

    Forge error text can quote credentials, config contents or response
    bodies, so only the category message from :func:`translate_error` is
    returned and the original exception is not chained.
    """
    try:
        yield
    except ForgeError as error:
        message = translate_error(error, operation=operation).message
        raise ToolError(f"{operation} failed: {message}") from None


async def _client_for(repo_path: str) -> tuple[ForgeClient, str]:
    """Return the client for an admitted repo_path and its 'owner/repo' part."""
    hostname, path = _parse_host_repo(repo_path)
    client = await _get_registry().get_client(hostname)
    return client, path


@mcp.tool()
async def list_mrs(repo_path: str, state: str = "open") -> list[dict]:
    """List merge requests for a repository.

    Args:
        repo_path: Repository path as 'hostname/owner/repo' (e.g., 'github.com/acme/app')
        state: MR state filter ('open', 'closed', 'merged')
    """
    with _redacted_forge_errors("list_mrs"):
        client, path = await _client_for(repo_path)
        mrs = await client.list_mrs(path, state=state)
        return [
            {
                "number": mr.number,
                "title": mr.title,
                "author": mr.author.username,
                "source_branch": mr.source_branch,
                "target_branch": mr.target_branch,
                "ci_status": mr.ci_status.value,
                "web_url": mr.web_url,
            }
            for mr in mrs
        ]


@mcp.tool()
async def get_mr(repo_path: str, number: int) -> dict:
    """Get detailed information about a merge request.

    Args:
        repo_path: Repository path as 'hostname/owner/repo'
        number: MR/PR number
    """
    with _redacted_forge_errors("get_mr"):
        client, path = await _client_for(repo_path)
        mr = await client.get_mr(path, number)
        return {
            "number": mr.number,
            "title": mr.title,
            "description": mr.description,
            "author": mr.author.username,
            "state": mr.state.value,
            "source_branch": mr.source_branch,
            "target_branch": mr.target_branch,
            "ci_status": mr.ci_status.value,
            "is_draft": mr.is_draft,
            "has_conflicts": mr.has_conflicts,
            "additions": mr.additions,
            "deletions": mr.deletions,
            "approvals": [u.username for u in mr.approvals],
            "reviewers": [u.username for u in mr.reviewers],
            "labels": list(mr.labels),
            "web_url": mr.web_url,
        }


@mcp.tool()
async def get_mr_diff(repo_path: str, number: int) -> str:
    """Get the diff for a merge request as unified diff text.

    Args:
        repo_path: Repository path as 'hostname/owner/repo'
        number: MR/PR number
    """
    with _redacted_forge_errors("get_mr_diff"):
        client, path = await _client_for(repo_path)
        changes = await client.get_mr_diff(path, number)
        parts = []
        for change in changes:
            old_path = (
                change.get("old_path")
                or change.get("previous_filename")
                or change.get("filename", "")
            )
            new_path = change.get("new_path") or change.get("filename", "")
            diff = (change.get("diff") or change.get("patch") or "").rstrip("\n")
            if diff:
                if not diff.lstrip().startswith("--- "):
                    parts.append(f"--- a/{old_path}")
                    parts.append(f"+++ b/{new_path}")
                parts.append(diff)
        return "\n".join(parts)


@mcp.tool()
async def post_comment(repo_path: str, number: int, body: str) -> str:
    """Post a general comment on a merge request.

    Args:
        repo_path: Repository path as 'hostname/owner/repo'
        number: MR/PR number
        body: Comment text (markdown supported)
    """
    with _redacted_forge_errors("post_comment"):
        client, path = await _client_for(repo_path)
        await client.add_comment(path, number, body)
        return "Comment posted successfully"


@mcp.tool()
async def approve_mr(repo_path: str, number: int) -> str:
    """Approve a merge request.

    Args:
        repo_path: Repository path as 'hostname/owner/repo'
        number: MR/PR number
    """
    with _redacted_forge_errors("approve_mr"):
        client, path = await _client_for(repo_path)
        await client.approve_mr(path, number)
        return "MR approved successfully"


@mcp.tool()
async def list_pipelines(repo_path: str, number: int) -> list[dict]:
    """List pipelines/CI runs for a merge request.

    Args:
        repo_path: Repository path as 'hostname/owner/repo'
        number: MR/PR number
    """
    with _redacted_forge_errors("list_pipelines"):
        client, path = await _client_for(repo_path)
        pipelines = await client.list_mr_pipelines(path, number)
        return [
            {
                "id": p.id,
                "status": p.status.value,
                "ref": p.ref,
                "sha": p.sha[:7],
                "source": p.source,
                "duration_seconds": p.duration_seconds,
                "web_url": p.web_url,
            }
            for p in pipelines
        ]


def main() -> None:
    """Entry point for tongs-mcp."""
    mcp.run()


if __name__ == "__main__":
    main()
