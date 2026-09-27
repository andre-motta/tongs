"""Map diff lines to forge-agnostic positions for inline comments.

A `DiffPosition` records the paths, old and new line numbers and side
(LEFT for the old file, RIGHT for the new file) of a diff line. The TUI
turns it into a review mutation `DiffAnchor`, and each forge client maps
that anchor to its own API format.
"""

from __future__ import annotations

from dataclasses import dataclass

from tongs.diff.models import DiffFile, DiffLine, LineType


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
