"""P0 tests for tongs.widgets.diff_panel."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tongs.diff.models import DiffFile, DiffHunk, DiffLine, FileStatus, LineType
from tongs.widgets.diff_panel import (
    CommentMode,
    CommentRequested,
    DiffOptionList,
    DiffRenderer,
    _collect_change_block,
    _is_markdown_file,
    _reconstruct_new_content,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ctx(old: int, new: int, content: str = "") -> DiffLine:
    """Shortcut for a CONTEXT DiffLine."""
    return DiffLine(old_lineno=old, new_lineno=new, content=content, line_type=LineType.CONTEXT)


def _add(new: int, content: str = "") -> DiffLine:
    """Shortcut for an ADDITION DiffLine."""
    return DiffLine(old_lineno=None, new_lineno=new, content=content, line_type=LineType.ADDITION)


def _del(old: int, content: str = "") -> DiffLine:
    """Shortcut for a DELETION DiffLine."""
    return DiffLine(old_lineno=old, new_lineno=None, content=content, line_type=LineType.DELETION)


def _make_file(
    hunks: tuple[DiffHunk, ...] = (),
    new_path: str = "file.py",
    language: str = "",
) -> DiffFile:
    """Build a minimal DiffFile for testing."""
    return DiffFile(
        old_path=new_path,
        new_path=new_path,
        status=FileStatus.MODIFIED,
        hunks=hunks,
        language=language,
    )


# ===================================================================
# 1. CommentMode enum
# ===================================================================

class TestCommentMode:
    def test_comment_value(self):
        assert CommentMode.COMMENT.value == "comment"

    def test_suggest_value(self):
        assert CommentMode.SUGGEST.value == "suggest"

    def test_members_count(self):
        assert len(CommentMode) == 2


# ===================================================================
# 2. CommentRequested message
# ===================================================================

class TestCommentRequested:
    def test_defaults(self):
        msg = CommentRequested()
        assert msg.file is None
        assert msg.line is None
        assert msg.mode is CommentMode.COMMENT
        assert msg.context_lines is None

    def test_all_fields(self):
        file = _make_file()
        line = _ctx(1, 1, "hello")
        ctx = [_ctx(2, 2, "world")]
        msg = CommentRequested(
            file=file,
            line=line,
            mode=CommentMode.SUGGEST,
            context_lines=ctx,
        )
        assert msg.file is file
        assert msg.line is line
        assert msg.mode is CommentMode.SUGGEST
        assert msg.context_lines is ctx

    def test_mode_defaults_to_comment(self):
        msg = CommentRequested(file=_make_file(), line=_ctx(1, 1))
        assert msg.mode is CommentMode.COMMENT


# ===================================================================
# 3. DiffOptionList._in_selection_range
# ===================================================================

class TestInSelectionRange:
    """Test the pure-state selection range check.

    DiffOptionList is instantiated directly and its internal attributes
    are set without mounting in a Textual app.
    """

    @pytest.fixture()
    def widget(self):
        w = DiffOptionList()
        # Add dummy options so highlighted can be set to valid indices.
        from textual.widgets._option_list import Option
        for i in range(10):
            w.add_option(Option(f"line {i}"))
        return w

    def test_no_anchor_returns_false(self, widget):
        widget._selection_anchor = None
        widget.highlighted = 3
        assert widget._in_selection_range(0) is False
        assert widget._in_selection_range(3) is False
        assert widget._in_selection_range(9) is False

    def test_highlighted_none_returns_false(self, widget):
        widget._selection_anchor = 2
        widget.highlighted = None
        assert widget._in_selection_range(2) is False
        assert widget._in_selection_range(0) is False

    def test_anchor_equals_highlighted_single_line(self, widget):
        widget._selection_anchor = 4
        widget.highlighted = 4
        assert widget._in_selection_range(4) is True
        assert widget._in_selection_range(3) is False
        assert widget._in_selection_range(5) is False

    def test_forward_selection(self, widget):
        """anchor < highlighted: indices in [anchor, highlighted] are True."""
        widget._selection_anchor = 2
        widget.highlighted = 5
        assert widget._in_selection_range(1) is False
        assert widget._in_selection_range(2) is True
        assert widget._in_selection_range(3) is True
        assert widget._in_selection_range(4) is True
        assert widget._in_selection_range(5) is True
        assert widget._in_selection_range(6) is False

    def test_backward_selection(self, widget):
        """anchor > highlighted: min/max logic still gives correct range."""
        widget._selection_anchor = 7
        widget.highlighted = 3
        assert widget._in_selection_range(2) is False
        assert widget._in_selection_range(3) is True
        assert widget._in_selection_range(5) is True
        assert widget._in_selection_range(7) is True
        assert widget._in_selection_range(8) is False


# ===================================================================
# 4. DiffOptionList._get_selection_lines
# ===================================================================

class TestGetSelectionLines:
    @pytest.fixture()
    def widget(self):
        w = DiffOptionList()
        from textual.widgets._option_list import Option
        for i in range(10):
            w.add_option(Option(f"line {i}"))
        return w

    def test_no_anchor_returns_empty(self, widget):
        widget._selection_anchor = None
        widget.highlighted = 5
        assert widget._get_selection_lines() == []

    def test_highlighted_none_returns_empty(self, widget):
        widget._selection_anchor = 3
        widget.highlighted = None
        assert widget._get_selection_lines() == []

    def test_single_line_selection(self, widget):
        dl = _ctx(10, 10, "single")
        widget._line_map = {4: dl}
        widget._selection_anchor = 4
        widget.highlighted = 4
        result = widget._get_selection_lines()
        assert result == [dl]

    def test_multi_line_selection(self, widget):
        dl1 = _ctx(10, 10, "a")
        dl2 = _add(11, "b")
        dl3 = _del(12, "c")
        widget._line_map = {2: dl1, 3: dl2, 4: dl3}
        widget._selection_anchor = 2
        widget.highlighted = 4
        result = widget._get_selection_lines()
        assert result == [dl1, dl2, dl3]

    def test_gaps_in_line_map_skipped(self, widget):
        """Disabled options (not in _line_map) are silently skipped."""
        dl1 = _ctx(10, 10, "first")
        dl3 = _add(12, "third")
        # Index 3 has no entry in _line_map (e.g. a hunk header).
        widget._line_map = {2: dl1, 4: dl3}
        widget._selection_anchor = 2
        widget.highlighted = 4
        result = widget._get_selection_lines()
        assert result == [dl1, dl3]

    def test_backward_selection_same_result(self, widget):
        """Backward selection yields the same lines in ascending order."""
        dl1 = _ctx(1, 1, "x")
        dl2 = _add(2, "y")
        widget._line_map = {5: dl1, 6: dl2}
        widget._selection_anchor = 6
        widget.highlighted = 5
        result = widget._get_selection_lines()
        assert result == [dl1, dl2]


# ===================================================================
# 5. DiffRenderer helpers
# ===================================================================

# -- _fold_context -------------------------------------------------------

class TestFoldContext:
    def test_short_run_kept(self):
        """A run of <= 6 context lines is kept entirely (CONTEXT_LINES=3)."""
        renderer = DiffRenderer()
        lines = [_ctx(i, i, f"line {i}") for i in range(1, 7)]
        result = renderer._fold_context(lines)
        assert all(isinstance(r, DiffLine) for r in result)
        assert len(result) == 6

    def test_exact_threshold_kept(self):
        """Exactly 2*CONTEXT_LINES = 6 context lines are kept (no fold)."""
        renderer = DiffRenderer()
        lines = [_ctx(i, i, f"line {i}") for i in range(1, 7)]
        result = renderer._fold_context(lines)
        assert len(result) == 6
        assert all(isinstance(r, DiffLine) for r in result)

    def test_long_run_folded(self):
        """A run of 10 context lines is folded: 3 + marker + 3."""
        renderer = DiffRenderer()
        lines = [_ctx(i, i, f"line {i}") for i in range(1, 11)]
        result = renderer._fold_context(lines)
        # 3 before + fold marker + 3 after = 7 items
        assert len(result) == 7
        # First three are DiffLines
        assert all(isinstance(r, DiffLine) for r in result[:3])
        # Middle is fold marker string
        assert isinstance(result[3], str)
        assert "4 unchanged lines" in result[3]
        # Last three are DiffLines
        assert all(isinstance(r, DiffLine) for r in result[4:])

    def test_fold_between_changes(self):
        """Context between two changes is folded if long enough."""
        renderer = DiffRenderer()
        lines = (
            [_del(1, "old")]
            + [_ctx(i, i, f"c{i}") for i in range(2, 12)]  # 10 context lines
            + [_add(12, "new")]
        )
        result = renderer._fold_context(lines)
        # Deletion, 3 context, fold marker, 3 context, addition = 9 items
        assert len(result) == 9
        strings = [r for r in result if isinstance(r, str)]
        assert len(strings) == 1
        assert "unchanged lines" in strings[0]

    def test_no_context_lines(self):
        """All change lines, no folding needed."""
        renderer = DiffRenderer()
        lines = [_del(1, "a"), _add(1, "b")]
        result = renderer._fold_context(lines)
        assert len(result) == 2
        assert all(isinstance(r, DiffLine) for r in result)


# -- _collect_change_block -----------------------------------------------

class TestCollectChangeBlock:
    def test_del_then_add(self):
        """Consecutive deletion followed by addition forms a change block."""
        lines = [_del(1, "old"), _add(1, "new"), _ctx(2, 2, "c")]
        old, new, consumed = _collect_change_block(lines, 0)
        assert len(old) == 1
        assert old[0].content == "old"
        assert len(new) == 1
        assert new[0].content == "new"
        assert consumed == 2

    def test_del_only(self):
        """Deletion-only block: no additions collected."""
        lines = [_del(1, "gone"), _del(2, "also gone"), _ctx(3, 3)]
        old, new, consumed = _collect_change_block(lines, 0)
        assert len(old) == 2
        assert new == []
        assert consumed == 2

    def test_add_only_starting_at_addition(self):
        """Starting at an addition (not a deletion) yields empty old list."""
        lines = [_add(1, "new line")]
        old, new, consumed = _collect_change_block(lines, 0)
        # Start is addition, not deletion, so old stays empty and additions
        # are collected.
        assert old == []
        assert len(new) == 1
        assert consumed == 1

    def test_multiple_del_multiple_add(self):
        lines = [
            _del(1, "d1"),
            _del(2, "d2"),
            _add(1, "a1"),
            _add(2, "a2"),
            _add(3, "a3"),
        ]
        old, new, consumed = _collect_change_block(lines, 0)
        assert len(old) == 2
        assert len(new) == 3
        assert consumed == 5

    def test_start_offset(self):
        """Starting at an offset into the list works correctly."""
        lines = [_ctx(1, 1), _del(2, "x"), _add(2, "y")]
        old, new, consumed = _collect_change_block(lines, 1)
        assert len(old) == 1
        assert len(new) == 1
        assert consumed == 2

    def test_fold_marker_stops_collection(self):
        """A fold marker (string) in the list stops the collection."""
        lines = [_del(1, "d"), "... 5 unchanged lines ...", _add(1, "a")]
        old, new, consumed = _collect_change_block(lines, 0)
        assert len(old) == 1
        assert new == []
        assert consumed == 1


# -- _gutter -------------------------------------------------------------

class TestGutter:
    def test_both_line_numbers(self):
        renderer = DiffRenderer()
        dl = _ctx(42, 99, "text")
        gutter = renderer._gutter(dl)
        text = gutter.plain
        # old_lineno right-aligned in 4 chars, then space, new_lineno in 4 chars, then space
        assert text == "  42   99 "

    def test_none_old_lineno(self):
        renderer = DiffRenderer()
        dl = _add(7, "added")
        gutter = renderer._gutter(dl)
        text = gutter.plain
        assert text == "        7 "

    def test_none_new_lineno(self):
        renderer = DiffRenderer()
        dl = _del(3, "deleted")
        gutter = renderer._gutter(dl)
        text = gutter.plain
        assert text == "   3      "

    def test_both_none(self):
        renderer = DiffRenderer()
        dl = DiffLine(
            old_lineno=None,
            new_lineno=None,
            content="no newline",
            line_type=LineType.NO_NEWLINE,
        )
        gutter = renderer._gutter(dl)
        text = gutter.plain
        assert text == "          "

    def test_large_numbers(self):
        renderer = DiffRenderer()
        dl = _ctx(1234, 5678, "big")
        gutter = renderer._gutter(dl)
        text = gutter.plain
        assert text == "1234 5678 "


# -- _is_markdown_file ---------------------------------------------------

class TestIsMarkdownFile:
    def test_language_markdown(self):
        f = _make_file(new_path="README.txt", language="markdown")
        assert _is_markdown_file(f) is True

    def test_md_extension(self):
        f = _make_file(new_path="docs/guide.md", language="")
        assert _is_markdown_file(f) is True

    def test_neither(self):
        f = _make_file(new_path="main.py", language="python")
        assert _is_markdown_file(f) is False

    def test_md_extension_with_other_language(self):
        f = _make_file(new_path="notes.md", language="python")
        assert _is_markdown_file(f) is True

    def test_mdx_not_matched(self):
        """Only .md suffix triggers, not .mdx."""
        f = _make_file(new_path="page.mdx", language="")
        assert _is_markdown_file(f) is False


# -- _reconstruct_new_content -------------------------------------------

class TestReconstructNewContent:
    def test_context_and_addition_collected(self):
        hunk = DiffHunk(
            header="@@ -1,3 +1,3 @@",
            old_start=1, old_count=3, new_start=1, new_count=3,
            lines=(
                _ctx(1, 1, "keep"),
                _del(2, "old"),
                _add(2, "new"),
                _ctx(3, 3, "also keep"),
            ),
        )
        f = _make_file(hunks=(hunk,))
        result = _reconstruct_new_content(f)
        assert result == "keep\nnew\nalso keep"

    def test_deletion_excluded(self):
        hunk = DiffHunk(
            header="@@ -1,2 +1,1 @@",
            old_start=1, old_count=2, new_start=1, new_count=1,
            lines=(
                _ctx(1, 1, "stay"),
                _del(2, "gone"),
            ),
        )
        f = _make_file(hunks=(hunk,))
        result = _reconstruct_new_content(f)
        assert result == "stay"

    def test_empty_hunks(self):
        f = _make_file(hunks=())
        result = _reconstruct_new_content(f)
        assert result == ""

    def test_multiple_hunks(self):
        h1 = DiffHunk(
            header="@@ -1,1 +1,1 @@",
            old_start=1, old_count=1, new_start=1, new_count=1,
            lines=(_ctx(1, 1, "a"),),
        )
        h2 = DiffHunk(
            header="@@ -10,1 +10,2 @@",
            old_start=10, old_count=1, new_start=10, new_count=2,
            lines=(
                _ctx(10, 10, "b"),
                _add(11, "c"),
            ),
        )
        f = _make_file(hunks=(h1, h2))
        result = _reconstruct_new_content(f)
        assert result == "a\nb\nc"

    def test_addition_only(self):
        hunk = DiffHunk(
            header="@@ -0,0 +1,2 @@",
            old_start=0, old_count=0, new_start=1, new_count=2,
            lines=(
                _add(1, "first"),
                _add(2, "second"),
            ),
        )
        f = _make_file(hunks=(hunk,))
        result = _reconstruct_new_content(f)
        assert result == "first\nsecond"


# ===================================================================
# 6. action_comment / action_suggest guard logic
# ===================================================================

class TestActionCommentGuards:
    @pytest.fixture()
    def widget(self):
        w = DiffOptionList()
        from textual.widgets._option_list import Option
        for i in range(5):
            w.add_option(Option(f"line {i}"))
        w._current_file = _make_file()
        w._line_map = {1: _ctx(1, 1, "code"), 2: _add(2, "added"), 3: _del(3, "deleted")}
        w._line_types = {1: LineType.CONTEXT, 2: LineType.ADDITION, 3: LineType.DELETION}
        return w

    def test_comment_highlighted_none_no_message(self, widget):
        widget.highlighted = None
        posted = []
        widget.post_message = lambda msg: posted.append(msg)
        widget.action_comment()
        assert posted == []

    def test_comment_on_non_code_line_notifies(self, widget):
        widget.highlighted = 0  # index 0 not in _line_map
        mock_app = MagicMock()
        with patch.object(type(widget), "app", new_callable=lambda: property(lambda self: mock_app)):
            widget.action_comment()
        mock_app.notify.assert_called_once()
        assert "code line" in mock_app.notify.call_args[0][0]

    def test_comment_happy_path_posts_message(self, widget):
        widget.highlighted = 1
        posted = []
        widget.post_message = lambda msg: posted.append(msg)
        widget.action_comment()
        assert len(posted) == 1
        msg = posted[0]
        assert isinstance(msg, CommentRequested)
        assert msg.mode is CommentMode.COMMENT
        assert msg.context_lines is None

    def test_comment_with_selection_includes_context(self, widget):
        widget.highlighted = 2
        widget._selection_anchor = 1
        posted = []
        widget.post_message = lambda msg: posted.append(msg)
        widget.action_comment()
        assert len(posted) == 1
        assert posted[0].context_lines is not None
        assert len(posted[0].context_lines) == 2
        assert widget._selection_anchor is None

    def test_suggest_on_deletion_blocked(self, widget):
        widget.highlighted = 3  # DELETION line
        mock_app = MagicMock()
        with patch.object(type(widget), "app", new_callable=lambda: property(lambda self: mock_app)):
            widget.action_suggest()
        mock_app.notify.assert_called_once()
        assert "deletion" in mock_app.notify.call_args[0][0].lower()

    def test_suggest_happy_path_posts_suggest_mode(self, widget):
        widget.highlighted = 2  # ADDITION line
        posted = []
        widget.post_message = lambda msg: posted.append(msg)
        widget.action_suggest()
        assert len(posted) == 1
        assert posted[0].mode is CommentMode.SUGGEST
