"""Tests for diff position mapping to forge-specific formats."""

from tongs.diff.models import DiffFile, DiffLine, FileStatus, LineType
from tongs.diff.position import (
    DiffPosition,
    position_from_diff_line,
    to_forge_position,
    to_github_position,
    to_gitlab_position,
)
from tongs.scanner.repo import ForgeType


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

    def test_preserves_file_and_line_refs(self):
        f = _make_file()
        line = DiffLine(
            old_lineno=1, new_lineno=2, content="x", line_type=LineType.ADDITION
        )
        pos = position_from_diff_line(f, line)
        assert pos.file is f
        assert pos.line is line


class TestToGitlabPosition:
    def _pos(
        self, side: str, old_line: int | None, new_line: int | None
    ) -> DiffPosition:
        return DiffPosition(
            file=_make_file(),
            line=DiffLine(
                old_lineno=old_line,
                new_lineno=new_line,
                content="",
                line_type=LineType.CONTEXT,
            ),
            old_path="src/old.py",
            new_path="src/new.py",
            old_line=old_line,
            new_line=new_line,
            side=side,
        )

    def test_addition_sets_new_line(self):
        result = to_gitlab_position(
            self._pos("RIGHT", None, 42), "base", "start", "head"
        )
        assert result["new_line"] == 42
        assert "old_line" not in result
        assert result["position_type"] == "text"

    def test_deletion_sets_old_line(self):
        result = to_gitlab_position(
            self._pos("LEFT", 10, None), "base", "start", "head"
        )
        assert result["old_line"] == 10
        assert "new_line" not in result

    def test_context_sets_new_line(self):
        result = to_gitlab_position(self._pos("RIGHT", 5, 5), "base", "start", "head")
        assert result["new_line"] == 5

    def test_includes_shas(self):
        result = to_gitlab_position(self._pos("RIGHT", None, 1), "abc", "def", "ghi")
        assert result["base_sha"] == "abc"
        assert result["start_sha"] == "def"
        assert result["head_sha"] == "ghi"

    def test_includes_paths(self):
        result = to_gitlab_position(self._pos("RIGHT", None, 1), "", "", "")
        assert result["old_path"] == "src/old.py"
        assert result["new_path"] == "src/new.py"


class TestToGithubPosition:
    def _pos(
        self, side: str, old_line: int | None, new_line: int | None
    ) -> DiffPosition:
        return DiffPosition(
            file=_make_file(),
            line=DiffLine(
                old_lineno=old_line,
                new_lineno=new_line,
                content="",
                line_type=LineType.CONTEXT,
            ),
            old_path="src/old.py",
            new_path="src/new.py",
            old_line=old_line,
            new_line=new_line,
            side=side,
        )

    def test_addition_uses_new_path_and_line(self):
        result = to_github_position(self._pos("RIGHT", None, 42), "sha123")
        assert result["path"] == "src/new.py"
        assert result["line"] == 42
        assert result["side"] == "RIGHT"
        assert result["commit_id"] == "sha123"

    def test_deletion_uses_old_path_and_line(self):
        result = to_github_position(self._pos("LEFT", 10, None), "sha123")
        assert result["path"] == "src/old.py"
        assert result["line"] == 10
        assert result["side"] == "LEFT"

    def test_context_uses_new_path(self):
        result = to_github_position(self._pos("RIGHT", 5, 5), "sha123")
        assert result["path"] == "src/new.py"
        assert result["line"] == 5


class TestToForgePosition:
    def _pos(self) -> DiffPosition:
        return DiffPosition(
            file=_make_file(),
            line=DiffLine(
                old_lineno=None, new_lineno=10, content="", line_type=LineType.ADDITION
            ),
            old_path="src/old.py",
            new_path="src/new.py",
            old_line=None,
            new_line=10,
            side="RIGHT",
        )

    def test_gitlab_dispatch(self):
        result = to_forge_position(
            self._pos(), ForgeType.GITLAB, base_sha="b", start_sha="s", head_sha="h"
        )
        assert "position_type" in result
        assert result["new_line"] == 10

    def test_github_dispatch(self):
        result = to_forge_position(self._pos(), ForgeType.GITHUB, commit_id="abc")
        assert "commit_id" in result
        assert result["line"] == 10
