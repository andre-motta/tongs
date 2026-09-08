"""Wire projections for immutable paged desktop diff snapshots."""

from __future__ import annotations

from enum import StrEnum

from tongs.desktop.protocol.messages import JsonObject
from tongs.diff.alignment import align_hunk
from tongs.diff.models import DiffFile, DiffHunk, DiffLine, SplitDiffRow


class DiffLayout(StrEnum):
    """Supported renderer projections for a diff snapshot."""

    UNIFIED = "unified"
    SPLIT = "split"


def flatten_diff(
    files: tuple[DiffFile, ...], layout: DiffLayout
) -> tuple[JsonObject, ...]:
    """Serialize files using one immutable projection for the whole snapshot."""
    entries: list[JsonObject] = []
    for file_index, file in enumerate(files):
        entries.append(_file_wire(file_index, file))
        for hunk_index, hunk in enumerate(file.hunks):
            entries.append(_hunk_wire(file_index, hunk_index, hunk))
            if layout is DiffLayout.UNIFIED:
                entries.extend(
                    _line_wire(file_index, hunk_index, line) for line in hunk.lines
                )
            else:
                entries.extend(
                    _split_row_wire(file_index, hunk_index, row_index, row)
                    for row_index, row in enumerate(align_hunk(hunk))
                )
    return tuple(entries)


def _file_wire(file_index: int, file: DiffFile) -> JsonObject:
    return {
        "kind": "file",
        "file_index": file_index,
        "old_path": file.old_path,
        "new_path": file.new_path,
        "status": file.status.value,
        "additions": file.additions,
        "deletions": file.deletions,
        "is_binary": file.is_binary,
        "language": file.language,
        "is_truncated": file.is_truncated,
        "is_empty": file.is_empty,
        "is_mode_only": file.is_mode_only,
        "is_unavailable": file.is_unavailable,
    }


def _hunk_wire(file_index: int, hunk_index: int, hunk: DiffHunk) -> JsonObject:
    return {
        "kind": "hunk",
        "file_index": file_index,
        "hunk_index": hunk_index,
        "header": hunk.header,
        "old_start": hunk.old_start,
        "old_count": hunk.old_count,
        "new_start": hunk.new_start,
        "new_count": hunk.new_count,
        "context_text": hunk.context_text,
    }


def _line_wire(file_index: int, hunk_index: int, line: DiffLine) -> JsonObject:
    return {
        "kind": "line",
        "file_index": file_index,
        "hunk_index": hunk_index,
        "old_line": line.old_lineno,
        "new_line": line.new_lineno,
        "content": line.content,
        "line_type": line.line_type.value,
    }


def _split_row_wire(
    file_index: int,
    hunk_index: int,
    row_index: int,
    row: SplitDiffRow,
) -> JsonObject:
    return {
        "kind": "split",
        "file_index": file_index,
        "hunk_index": hunk_index,
        "row_index": row_index,
        "old": _cell_wire(row.old, "old" if row.old_anchor is not None else None),
        "new": _cell_wire(row.new, "new" if row.new_anchor is not None else None),
    }


def _cell_wire(line: DiffLine | None, anchor_side: str | None) -> JsonObject | None:
    if line is None:
        return None
    return {
        "old_line": line.old_lineno,
        "new_line": line.new_lineno,
        "content": line.content,
        "line_type": line.line_type.value,
        "anchor_side": anchor_side,
    }


__all__ = ["DiffLayout", "flatten_diff"]
