"""Tests for renderer-independent split diff alignment."""

from __future__ import annotations

from tongs.diff.alignment import align_hunk
from tongs.diff.models import DiffHunk, DiffLine, LineType


def _line(
    line_type: LineType,
    content: str,
    old_lineno: int | None = None,
    new_lineno: int | None = None,
) -> DiffLine:
    return DiffLine(
        old_lineno=old_lineno,
        new_lineno=new_lineno,
        content=content,
        line_type=line_type,
    )


def _hunk(*lines: DiffLine) -> DiffHunk:
    return DiffHunk(
        header="@@ -1,1 +1,1 @@",
        old_start=1,
        old_count=1,
        new_start=1,
        new_count=1,
        lines=tuple(lines),
    )


def test_asymmetric_replacement_tracks_marker_to_new_side_ordinal() -> None:
    marker = _line(LineType.NO_NEWLINE, r"\ No newline at end of file")
    old_one = _line(LineType.DELETION, "old one", old_lineno=1)
    old_two = _line(LineType.DELETION, "old two", old_lineno=2)
    new_one = _line(LineType.ADDITION, "new one", new_lineno=1)
    new_two = _line(LineType.ADDITION, "new two", new_lineno=2)

    rows = align_hunk(_hunk(old_one, old_two, new_one, marker, new_two))

    assert rows[0].old is old_one
    assert rows[0].new is new_one
    assert rows[1].old is None
    assert rows[1].new is marker
    assert rows[2].old is old_two
    assert rows[2].new is new_two


def test_markers_on_both_sides_are_kept_without_anchors() -> None:
    marker = _line(LineType.NO_NEWLINE, r"\ No newline at end of file")
    deleted = _line(LineType.DELETION, "old", old_lineno=4)
    added = _line(LineType.ADDITION, "new", new_lineno=5)

    rows = align_hunk(_hunk(deleted, marker, added, marker))

    assert rows[0].old is deleted
    assert rows[0].new is added
    assert rows[1].old is marker
    assert rows[1].new is None
    assert rows[2].old is None
    assert rows[2].new is marker
    assert rows[1].old_anchor is None
    assert rows[2].new_anchor is None


def test_marker_after_context_applies_to_both_cells() -> None:
    context = _line(LineType.CONTEXT, "same", old_lineno=8, new_lineno=8)
    marker = _line(LineType.NO_NEWLINE, r"\ No newline at end of file")

    rows = align_hunk(_hunk(context, marker))

    assert rows[0].old is context
    assert rows[0].new is context
    assert rows[1].old is marker
    assert rows[1].new is marker
    assert rows[1].old_anchor is None
    assert rows[1].new_anchor is None


def test_unmatched_cells_and_hunk_boundaries_are_preserved() -> None:
    deleted = _line(LineType.DELETION, "old", old_lineno=1)
    context = _line(LineType.CONTEXT, "boundary", old_lineno=2, new_lineno=2)
    added = _line(LineType.ADDITION, "new", new_lineno=3)

    rows = align_hunk(_hunk(deleted, context, added))

    assert rows[0].old is deleted
    assert rows[0].new is None
    assert rows[1].old is context
    assert rows[1].new is context
    assert rows[2].old is None
    assert rows[2].new is added


def test_metadata_lines_with_numbers_still_cannot_anchor() -> None:
    marker = _line(LineType.NO_NEWLINE, "marker", old_lineno=10, new_lineno=10)

    row = align_hunk(_hunk(marker))[0]

    assert row.old is marker
    assert row.new is marker
    assert row.old_anchor is None
    assert row.new_anchor is None
