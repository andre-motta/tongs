"""Map diff lines to forge-specific positions for inline comments.

Each forge has a different API format for specifying where an inline
comment should be anchored:

GitLab requires a `position` object with base_sha, start_sha, head_sha,
old_path, new_path, and either old_line or new_line.

GitHub requires path, line, side (LEFT for old file, RIGHT for new file),
and commit_id.

This module provides a forge-agnostic `DiffPosition` that captures enough
information for either forge, plus conversion functions.
"""

from __future__ import annotations

from dataclasses import dataclass

from tongs.diff.models import DiffFile, DiffLine, LineType
from tongs.scanner.repo import ForgeType


@dataclass(frozen=True)
class DiffPosition:
    """Forge-agnostic position for an inline comment."""

    file: DiffFile
    line: DiffLine
    old_path: str
    new_path: str
    old_line: int | None
    new_line: int | None
    side: str


def position_from_diff_line(file: DiffFile, line: DiffLine) -> DiffPosition:
    """Create a DiffPosition from a DiffFile and DiffLine."""
    if line.line_type == LineType.ADDITION:
        side = "RIGHT"
    elif line.line_type == LineType.DELETION:
        side = "LEFT"
    else:
        side = "RIGHT"

    return DiffPosition(
        file=file,
        line=line,
        old_path=file.old_path,
        new_path=file.new_path,
        old_line=line.old_lineno,
        new_line=line.new_lineno,
        side=side,
    )


def to_gitlab_position(
    pos: DiffPosition,
    base_sha: str,
    start_sha: str,
    head_sha: str,
) -> dict[str, str | int]:
    """Convert a DiffPosition to GitLab API position format."""
    position = {
        "position_type": "text",
        "base_sha": base_sha,
        "start_sha": start_sha,
        "head_sha": head_sha,
        "old_path": pos.old_path,
        "new_path": pos.new_path,
    }
    if pos.side == "LEFT" and pos.old_line is not None:
        position["old_line"] = pos.old_line
    elif pos.new_line is not None:
        position["new_line"] = pos.new_line
    return position


def to_github_position(
    pos: DiffPosition,
    commit_id: str,
) -> dict[str, str | int]:
    """Convert a DiffPosition to GitHub API review comment format."""
    result: dict = {
        "path": pos.new_path if pos.side == "RIGHT" else pos.old_path,
        "side": pos.side,
        "commit_id": commit_id,
    }
    if pos.side == "LEFT" and pos.old_line is not None:
        result["line"] = pos.old_line
    elif pos.new_line is not None:
        result["line"] = pos.new_line
    return result


def to_forge_position(
    pos: DiffPosition,
    forge_type: ForgeType,
    base_sha: str = "",
    start_sha: str = "",
    head_sha: str = "",
    commit_id: str = "",
) -> dict[str, str | int]:
    """Convert a DiffPosition to the appropriate forge API format."""
    if forge_type == ForgeType.GITLAB:
        return to_gitlab_position(pos, base_sha, start_sha, head_sha)
    return to_github_position(pos, commit_id)
