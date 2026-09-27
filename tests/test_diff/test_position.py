"""Tests for mapping diff lines to forge-agnostic comment positions."""

from tongs.diff.models import DiffFile, DiffLine, FileStatus, LineType
from tongs.diff.position import position_from_diff_line


def _make_file() -> DiffFile:
    return DiffFile(
        old_path="src/old.py",
        new_path="src/new.py",
        status=FileStatus.MODIFIED,
        hunks=(),
    )


class TestPositionFromDiffLine:
    def test_addition_is_right_side(self):
        line = DiffLine(
            old_lineno=None,
            new_lineno=42,
            content="new code",
            line_type=LineType.ADDITION,
        )
        pos = position_from_diff_line(_make_file(), line)
        assert pos.side == "RIGHT"
        assert pos.new_line == 42
        assert pos.old_line is None

    def test_deletion_is_left_side(self):
        line = DiffLine(
            old_lineno=10,
            new_lineno=None,
            content="old code",
            line_type=LineType.DELETION,
        )
        pos = position_from_diff_line(_make_file(), line)
        assert pos.side == "LEFT"
        assert pos.old_line == 10
        assert pos.new_line is None

    def test_context_defaults_to_right(self):
        line = DiffLine(
            old_lineno=5, new_lineno=5, content="context", line_type=LineType.CONTEXT
        )
        pos = position_from_diff_line(_make_file(), line)
        assert pos.side == "RIGHT"
        assert pos.old_line == 5
        assert pos.new_line == 5

    def test_preserves_file_paths(self):
        pos = position_from_diff_line(
            _make_file(),
            DiffLine(
                old_lineno=1, new_lineno=1, content="x", line_type=LineType.CONTEXT
            ),
        )
        assert pos.old_path == "src/old.py"
        assert pos.new_path == "src/new.py"
