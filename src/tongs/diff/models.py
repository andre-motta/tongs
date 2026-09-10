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
    # These flags are additive so existing parser and renderer callers keep
    # their original construction contract.  A forge can describe a file
    # without returning patch text, and the conversion layer preserves that
    # distinction instead of dropping the file or treating it as binary.
    is_truncated: bool = False
    is_empty: bool = False
    is_mode_only: bool = False
    # A rename or copy the forge describes with no content change.  It is a
    # stronger statement than an empty file, and it is the one shape GitHub's
    # files endpoint always determines on its own.
    is_rename_only: bool = False
    # The forge returned neither content nor a reason.  On GitHub this is the
    # residue of a withheld patch that could be either binary content or a
    # mode-only change, which its files endpoint does not distinguish.
    is_unavailable: bool = False

    @property
    def is_metadata_only(self) -> bool:
        """Whether this file has no content hunks for a metadata reason."""
        return (
            self.is_mode_only
            or self.is_empty
            or self.is_binary
            or self.is_rename_only
            or self.is_unavailable
        )


@dataclass(frozen=True)
class SplitDiffRow:
    """One renderer-independent row in a side-by-side diff.

    ``old`` and ``new`` are independent references to the original
    :class:`DiffLine` objects.  A missing cell is represented by ``None`` and
    therefore cannot provide a comment anchor.
    """

    old: DiffLine | None = None
    new: DiffLine | None = None

    @property
    def old_anchor(self) -> DiffLine | None:
        """Old-side line usable as an anchor, or ``None`` for markers."""
        if (
            self.old is None
            or self.old.line_type not in (LineType.CONTEXT, LineType.DELETION)
            or self.old.old_lineno is None
        ):
            return None
        return self.old

    @property
    def new_anchor(self) -> DiffLine | None:
        """New-side line usable as an anchor, or ``None`` for markers."""
        if (
            self.new is None
            or self.new.line_type not in (LineType.CONTEXT, LineType.ADDITION)
            or self.new.new_lineno is None
        ):
            return None
        return self.new
