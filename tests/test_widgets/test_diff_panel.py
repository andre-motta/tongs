"""P0 tests for tongs.widgets.diff_panel."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from tongs.diff.models import DiffFile, DiffHunk, DiffLine, FileStatus, LineType
from tongs.forges.models import Discussion, InlineComment, User
from tongs.widgets.diff_panel import (
    CommentMode,
    CommentRequested,
    DiffFileTree,
    DiffOptionList,
    DiffRenderer,
    ReplyRequested,
    _build_comment_lines,
    _build_discussion_index,
    _build_highlight_map,
    _collect_change_block,
    _is_markdown_file,
    _match_discussions,
    _reconstruct_new_content,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(old: int, new: int, content: str = "") -> DiffLine:
    """Shortcut for a CONTEXT DiffLine."""
    return DiffLine(
        old_lineno=old, new_lineno=new, content=content, line_type=LineType.CONTEXT
    )


def _add(new: int, content: str = "") -> DiffLine:
    """Shortcut for an ADDITION DiffLine."""
    return DiffLine(
        old_lineno=None, new_lineno=new, content=content, line_type=LineType.ADDITION
    )


def _del(old: int, content: str = "") -> DiffLine:
    """Shortcut for a DELETION DiffLine."""
    return DiffLine(
        old_lineno=old, new_lineno=None, content=content, line_type=LineType.DELETION
    )


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

    @pytest.mark.parametrize(
        ("anchor", "lines"),
        [
            (4, {4: _ctx(10, 10, "single")}),
            (2, {2: _ctx(10, 10, "a"), 3: _add(11, "b"), 4: _del(12, "c")}),
        ],
        ids=["single", "multi"],
    )
    def test_selection_returns_lines_from_anchor_to_cursor(
        self, widget, anchor: int, lines: dict[int, DiffLine]
    ):
        widget._line_map = lines
        widget._selection_anchor = anchor
        widget.highlighted = 4
        assert widget._get_selection_lines() == list(lines.values())

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


# -- _collect_change_block -----------------------------------------------


class TestCollectChangeBlock:
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
        assert text == "  42   99   "

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            (_add(7, "added"), "        7   "),
            (_del(3, "deleted"), "   3        "),
            (
                DiffLine(
                    old_lineno=None,
                    new_lineno=None,
                    content="no newline",
                    line_type=LineType.NO_NEWLINE,
                ),
                "            ",
            ),
        ],
        ids=["no-old", "no-new", "neither"],
    )
    def test_missing_line_numbers_are_blank(self, line: DiffLine, expected: str):
        assert DiffRenderer()._gutter(line).plain == expected

    def test_comment_marker_unresolved(self):
        comment_lines = {(42, 99): False}
        renderer = DiffRenderer(comment_lines=comment_lines)
        dl = _ctx(42, 99, "text")
        gutter = renderer._gutter(dl)
        text = gutter.plain
        assert text == "  42   99 * "

    def test_comment_marker_resolved(self):
        comment_lines = {(42, 99): True}
        renderer = DiffRenderer(comment_lines=comment_lines)
        dl = _ctx(42, 99, "text")
        gutter = renderer._gutter(dl)
        assert "* " in gutter.plain


# -- _is_markdown_file ---------------------------------------------------


class TestIsMarkdownFile:
    def test_language_markdown(self):
        f = _make_file(new_path="README.txt", language="markdown")
        assert _is_markdown_file(f) is True

    @pytest.mark.parametrize("language", ["", "python"])
    def test_md_extension_wins_over_detected_language(self, language: str):
        f = _make_file(new_path="docs/guide.md", language=language)
        assert _is_markdown_file(f) is True

    def test_neither(self):
        f = _make_file(new_path="main.py", language="python")
        assert _is_markdown_file(f) is False

    def test_mdx_not_matched(self):
        """Only .md suffix triggers, not .mdx."""
        f = _make_file(new_path="page.mdx", language="")
        assert _is_markdown_file(f) is False


# -- _reconstruct_new_content -------------------------------------------


class TestReconstructNewContent:
    def test_context_and_addition_collected(self):
        hunk = DiffHunk(
            header="@@ -1,3 +1,3 @@",
            old_start=1,
            old_count=3,
            new_start=1,
            new_count=3,
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
            old_start=1,
            old_count=2,
            new_start=1,
            new_count=1,
            lines=(
                _ctx(1, 1, "stay"),
                _del(2, "gone"),
            ),
        )
        f = _make_file(hunks=(hunk,))
        result = _reconstruct_new_content(f)
        assert result == "stay"

    def test_multiple_hunks(self):
        h1 = DiffHunk(
            header="@@ -1,1 +1,1 @@",
            old_start=1,
            old_count=1,
            new_start=1,
            new_count=1,
            lines=(_ctx(1, 1, "a"),),
        )
        h2 = DiffHunk(
            header="@@ -10,1 +10,2 @@",
            old_start=10,
            old_count=1,
            new_start=10,
            new_count=2,
            lines=(
                _ctx(10, 10, "b"),
                _add(11, "c"),
            ),
        )
        f = _make_file(hunks=(h1, h2))
        result = _reconstruct_new_content(f)
        assert result == "a\nb\nc"


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
        w._line_map = {
            1: _ctx(1, 1, "code"),
            2: _add(2, "added"),
            3: _del(3, "deleted"),
        }
        w._line_types = {
            1: LineType.CONTEXT,
            2: LineType.ADDITION,
            3: LineType.DELETION,
        }
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
        with patch.object(
            type(widget), "app", new_callable=lambda: property(lambda self: mock_app)
        ):
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
        with patch.object(
            type(widget), "app", new_callable=lambda: property(lambda self: mock_app)
        ):
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


# ---------------------------------------------------------------------------
# Discussion fixture helpers
# ---------------------------------------------------------------------------


def _make_discussion(
    id: str = "d1",
    old_line: int | None = None,
    new_line: int | None = 10,
    is_resolved: bool = False,
    is_inline: bool = True,
    file_path: str = "test.py",
) -> Discussion:
    return Discussion(
        id=id,
        is_inline=is_inline,
        root_comment=InlineComment(
            id=f"c-{id}",
            author=User(username="testuser"),
            body="test comment",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            file_path=file_path,
            old_line=old_line,
            new_line=new_line,
            is_resolved=is_resolved,
        ),
        is_resolved=is_resolved,
    )


# ===================================================================
# 7. _build_discussion_index
# ===================================================================


class TestBuildDiscussionIndex:
    def test_inline_keyed_by_old_new(self):
        d = _make_discussion(id="d1", old_line=5, new_line=10)
        index = _build_discussion_index([d])
        assert (5, 10) in index
        assert index[(5, 10)] == [d]

    def test_non_inline_excluded(self):
        d = _make_discussion(id="d1", is_inline=False)
        index = _build_discussion_index([d])
        assert index == {}

    def test_multiple_discussions_same_line(self):
        d1 = _make_discussion(id="d1", old_line=5, new_line=10)
        d2 = _make_discussion(id="d2", old_line=5, new_line=10)
        index = _build_discussion_index([d1, d2])
        assert len(index[(5, 10)]) == 2
        assert index[(5, 10)] == [d1, d2]


# ===================================================================
# 8. _build_comment_lines
# ===================================================================


class TestBuildCommentLines:
    def test_single_unresolved_returns_false(self):
        d = _make_discussion(id="d1", old_line=5, new_line=10, is_resolved=False)
        result = _build_comment_lines([d])
        assert result[(5, 10)] is False

    def test_single_resolved_returns_true(self):
        d = _make_discussion(id="d1", old_line=5, new_line=10, is_resolved=True)
        result = _build_comment_lines([d])
        assert result[(5, 10)] is True

    def test_mixed_on_same_line_returns_false(self):
        d1 = _make_discussion(id="d1", old_line=5, new_line=10, is_resolved=True)
        d2 = _make_discussion(id="d2", old_line=5, new_line=10, is_resolved=False)
        result = _build_comment_lines([d1, d2])
        assert result[(5, 10)] is False


# ===================================================================
# 9. _match_discussions
# ===================================================================


class TestMatchDiscussions:
    def test_exact_old_new_match(self):
        d = _make_discussion(id="d1", old_line=5, new_line=10)
        index = {(5, 10): [d]}
        dl = _ctx(5, 10, "code")
        assert _match_discussions(dl, index) == [d]

    def test_exact_match_takes_priority(self):
        """When both exact and fallback keys exist, exact wins."""
        d_exact = _make_discussion(id="d-exact", old_line=5, new_line=10)
        d_fallback = _make_discussion(id="d-fallback", old_line=None, new_line=10)
        index = {(5, 10): [d_exact], (None, 10): [d_fallback]}
        dl = _ctx(5, 10, "code")
        result = _match_discussions(dl, index)
        assert result == [d_exact]

    def test_fallback_none_new_when_no_exact(self):
        """Fallback to (None, new) when (old, new) is not in index."""
        d = _make_discussion(id="d1", old_line=None, new_line=10)
        index = {(None, 10): [d]}
        dl = _ctx(5, 10, "code")  # (5, 10) not in index
        result = _match_discussions(dl, index)
        assert result == [d]

    def test_fallback_old_none_when_no_exact_or_none_new(self):
        """Fallback to (old, None) when neither (old, new) nor (None, new) exist."""
        d = _make_discussion(id="d1", old_line=5, new_line=None)
        index = {(5, None): [d]}
        dl = _ctx(5, 10, "code")  # (5, 10) not in index, (None, 10) not either
        result = _match_discussions(dl, index)
        assert result == [d]


# ===================================================================
# 10. action_reply_discussion
# ===================================================================


def test_reply_posts_the_thread_line_and_root_author():
    widget = DiffOptionList()
    from textual.widgets._option_list import Option

    for i in range(5):
        widget.add_option(Option(f"line {i}"))
    file = _make_file()
    line = _ctx(1, 10, "code")
    widget._current_file = file
    widget._line_map = {2: line}
    widget._comment_map = {2: [_make_discussion(id="d7", old_line=1, new_line=10)]}
    widget.highlighted = 2
    posted = []
    widget.post_message = posted.append

    widget.action_reply_discussion()

    assert len(posted) == 1
    msg = posted[0]
    assert isinstance(msg, ReplyRequested)
    assert (msg.discussion_id, msg.file, msg.line, msg.author) == (
        "d7",
        file,
        line,
        "testuser",
    )


# ===================================================================
# 11. DiffOptionList._get_target_discussion
# ===================================================================


class TestGetTargetDiscussion:
    @pytest.fixture()
    def widget(self):
        w = DiffOptionList()
        from textual.widgets._option_list import Option

        for i in range(5):
            w.add_option(Option(f"line {i}"))
        return w

    def test_returns_none_when_no_discussions(self, widget):
        widget.highlighted = 2
        widget._comment_map = {}
        assert widget._get_target_discussion() is None

    def test_returns_first_unresolved(self, widget):
        d_resolved = _make_discussion(id="d1", is_resolved=True)
        d_unresolved = _make_discussion(id="d2", is_resolved=False)
        widget.highlighted = 2
        widget._comment_map = {2: [d_resolved, d_unresolved]}
        result = widget._get_target_discussion()
        assert result is d_unresolved

    def test_returns_first_when_all_resolved(self, widget):
        d1 = _make_discussion(id="d1", is_resolved=True)
        d2 = _make_discussion(id="d2", is_resolved=True)
        widget.highlighted = 2
        widget._comment_map = {2: [d1, d2]}
        result = widget._get_target_discussion()
        assert result is d1


# ===================================================================
# 12. DiffOptionList.action_toggle_discussion
# ===================================================================


class TestActionToggleDiscussion:
    @pytest.fixture()
    def widget(self):
        w = DiffOptionList()
        from textual.widgets._option_list import Option

        for i in range(5):
            w.add_option(Option(f"line {i}"))
        # Stub post_message to avoid Textual internals
        w.post_message = MagicMock()
        return w

    def test_no_op_when_no_discussions(self, widget):
        widget.highlighted = 2
        widget._comment_map = {}
        # Reset the mock after highlighted setter posts OptionHighlighted
        widget.post_message.reset_mock()
        widget.action_toggle_discussion()
        widget.post_message.assert_not_called()

    def test_adds_disc_ids_to_expanded_when_collapsed(self, widget):
        d1 = _make_discussion(id="d1")
        d2 = _make_discussion(id="d2")
        widget.highlighted = 2
        widget._comment_map = {2: [d1, d2]}
        widget._expanded_threads = set()
        widget.action_toggle_discussion()
        assert "d1" in widget._expanded_threads
        assert "d2" in widget._expanded_threads

    def test_removes_disc_ids_when_expanded(self, widget):
        d1 = _make_discussion(id="d1")
        widget.highlighted = 2
        widget._comment_map = {2: [d1]}
        widget._expanded_threads = {"d1"}
        widget.action_toggle_discussion()
        assert "d1" not in widget._expanded_threads


# ===================================================================
# 13. Comment navigation: action_next_comment / action_prev_comment
# ===================================================================


class TestCommentNavigation:
    @pytest.fixture()
    def widget(self):
        w = DiffOptionList()
        from textual.widgets._option_list import Option

        for i in range(20):
            w.add_option(Option(f"line {i}"))
        return w

    def test_next_comment_empty_indices_notifies(self, widget):
        widget._comment_indices = []
        mock_app = MagicMock()
        with patch.object(
            type(widget), "app", new_callable=lambda: property(lambda self: mock_app)
        ):
            widget.action_next_comment()
        mock_app.notify.assert_called_once()
        assert "No comments" in mock_app.notify.call_args[0][0]

    def test_prev_comment_empty_indices_notifies(self, widget):
        widget._comment_indices = []
        mock_app = MagicMock()
        with patch.object(
            type(widget), "app", new_callable=lambda: property(lambda self: mock_app)
        ):
            widget.action_prev_comment()
        mock_app.notify.assert_called_once()
        assert "No comments" in mock_app.notify.call_args[0][0]

    def test_next_comment_wraps_forward(self, widget):
        widget._comment_indices = [3, 7, 12]
        widget.highlighted = 12
        widget.scroll_to_highlight = MagicMock()
        widget.action_next_comment()
        # Past the last index (12), bisect_right gives 3 which >= len, wraps to 0
        assert widget.highlighted == 3

    def test_prev_comment_wraps_backward(self, widget):
        widget._comment_indices = [3, 7, 12]
        widget.highlighted = 3
        widget.scroll_to_highlight = MagicMock()
        widget.action_prev_comment()
        # bisect_left(indices, 3) = 0, minus 1 = -1, wraps to last
        assert widget.highlighted == 12

    def test_next_comment_moves_forward(self, widget):
        widget._comment_indices = [3, 7, 12]
        widget.highlighted = 3
        widget.scroll_to_highlight = MagicMock()
        widget.action_next_comment()
        assert widget.highlighted == 7

    def test_prev_comment_moves_backward(self, widget):
        widget._comment_indices = [3, 7, 12]
        widget.highlighted = 12
        widget.scroll_to_highlight = MagicMock()
        widget.action_prev_comment()
        assert widget.highlighted == 7


# ===================================================================
# 14. DiffFileTree.set_files display logic
# ===================================================================


class TestDiffFileTreeSetFiles:
    def test_basename_used_not_full_path(self):
        """Labels show basename, not the full directory path."""
        f = _make_file(new_path="src/deep/nested/component.py")
        tree = DiffFileTree("Files")
        tree.set_files([f])
        # The root label reflects the file count
        assert "1" in tree.root.label.plain
        # First child node label should contain the basename
        children = list(tree.root.children)
        assert len(children) == 1
        label_text = (
            children[0].label.plain
            if hasattr(children[0].label, "plain")
            else str(children[0].label)
        )
        assert "component.py" in label_text
        # Full path should NOT appear
        assert "src/deep/nested/" not in label_text


# ===================================================================
# 15. _build_highlight_map
# ===================================================================


class TestBuildHighlightMap:
    def test_returns_empty_for_text_language(self):
        """Language 'text' skips highlighting entirely."""
        hunk = DiffHunk(
            header="@@ -1,1 +1,1 @@",
            old_start=1,
            old_count=1,
            new_start=1,
            new_count=1,
            lines=(_ctx(1, 1, "hello world"),),
        )
        f = _make_file(hunks=(hunk,), language="text")
        result = _build_highlight_map(f)
        assert result == {}

    def test_highlights_each_line_with_its_own_content(self):
        """A Python file maps each DiffLine id to its own highlighted text."""
        lines = (
            _ctx(1, 1, "def hello():"),
            _add(2, "    return 42"),
            _del(1, "    pass"),
        )
        hunk = DiffHunk(
            header="@@ -1,2 +1,2 @@",
            old_start=1,
            old_count=2,
            new_start=1,
            new_count=2,
            lines=lines,
        )
        f = _make_file(hunks=(hunk,), language="python")
        result = _build_highlight_map(f)
        assert {key: text.plain for key, text in result.items()} == {
            id(line): line.content for line in lines
        }
        assert any(span.style for text in result.values() for span in text.spans)

    def test_handles_pygments_failure_gracefully(self):
        """If Pygments/Syntax raises, returns empty dict."""
        line = _ctx(1, 1, "some code")
        hunk = DiffHunk(
            header="@@ -1,1 +1,1 @@",
            old_start=1,
            old_count=1,
            new_start=1,
            new_count=1,
            lines=(line,),
        )
        f = _make_file(hunks=(hunk,), language="python")
        with patch("tongs.widgets.diff_panel.Syntax", side_effect=Exception("boom")):
            result = _build_highlight_map(f)
        assert result == {}
