"""MCP server exposing tongs forge operations as tools.

Entry point: tongs-mcp
Tools are high-level only. Destructive actions (merge, close, reopen,
cancel) are intentionally excluded as a security boundary.
"""

from __future__ import annotations

import re

from mcp.server.fastmcp import FastMCP

from tongs.config import load_config
from tongs.forges.registry import ForgeRegistry

mcp = FastMCP("tongs")

_registry: ForgeRegistry | None = None
_REPO_PATH_RE = re.compile(r"^(?!.*\.\.)[\w.-]+(?:/[\w.-]+){2,}$")


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
    """Split 'hostname/owner/repo' into (hostname, 'owner/repo')."""
    if not _REPO_PATH_RE.match(repo_path):
        raise ValueError(f"repo_path must be 'hostname/owner/repo', got: {repo_path}")
    parts = repo_path.split("/", 1)
    return parts[0], parts[1]


@mcp.tool()
async def list_mrs(repo_path: str, state: str = "open") -> list[dict]:
    """List merge requests for a repository.

    Args:
        repo_path: Repository path as 'hostname/owner/repo' (e.g., 'github.com/acme/app')
        state: MR state filter ('open', 'closed', 'merged')
    """
    hostname, path = _parse_host_repo(repo_path)
    registry = _get_registry()
    client = await registry.get_client(hostname)
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
    hostname, path = _parse_host_repo(repo_path)
    registry = _get_registry()
    client = await registry.get_client(hostname)
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
    hostname, path = _parse_host_repo(repo_path)
    registry = _get_registry()
    client = await registry.get_client(hostname)
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
    hostname, path = _parse_host_repo(repo_path)
    registry = _get_registry()
    client = await registry.get_client(hostname)
    await client.add_comment(path, number, body)
    return "Comment posted successfully"


@mcp.tool()
async def approve_mr(repo_path: str, number: int) -> str:
    """Approve a merge request.

    Args:
        repo_path: Repository path as 'hostname/owner/repo'
        number: MR/PR number
    """
    hostname, path = _parse_host_repo(repo_path)
    registry = _get_registry()
    client = await registry.get_client(hostname)
    await client.approve_mr(path, number)
    return "MR approved successfully"


@mcp.tool()
async def list_pipelines(repo_path: str, number: int) -> list[dict]:
    """List pipelines/CI runs for a merge request.

    Args:
        repo_path: Repository path as 'hostname/owner/repo'
        number: MR/PR number
    """
    hostname, path = _parse_host_repo(repo_path)
    registry = _get_registry()
    client = await registry.get_client(hostname)
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
