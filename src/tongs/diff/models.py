"""Diff data models -- forge-agnostic structured representation of unified diffs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class LineType(Enum):
    CONTEXT = "context"
    ADDITION = "addition"
    DELETION = "deletion"
    HUNK_HEADER = "hunk_header"
    NO_NEWLINE = "no_newline"


class FileStatus(Enum):
    MODIFIED = "modified"
    ADDED = "added"
    DELETED = "deleted"
    RENAMED = "renamed"


@dataclass(frozen=True)
class DiffLine:
    """A single line in a diff."""

    old_lineno: int | None
    new_lineno: int | None
    content: str
    line_type: LineType


@dataclass(frozen=True)
class DiffHunk:
    """A hunk within a diff file."""

    header: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[DiffLine, ...]
    context_text: str = ""


@dataclass(frozen=True)
class DiffFile:
    """A single file's diff."""

    old_path: str
    new_path: str
    status: FileStatus
    hunks: tuple[DiffHunk, ...]
    additions: int = 0
    deletions: int = 0
    is_binary: bool = False
    language: str = ""
