"""Tests for tongs.widgets.discussion_list pure functions and logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch



from tongs.diff.models import DiffFile, DiffHunk, DiffLine, FileStatus, LineType
from tongs.forges.models import Discussion, InlineComment, User
from tongs.widgets.discussion_list import (
    DiscussionPanel,
    DiscussionReplyRequested,
    JumpToDiffDiscussion,
    _relative_time,
    _render_thread,
    render_diff_snippet,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_disc(
    id="d1",
    file_path="test.py",
    new_line=10,
    is_resolved=False,
    is_inline=True,
    body="test",
):
    return Discussion(
        id=id,
        is_inline=is_inline,
        root_comment=InlineComment(
            id=f"c-{id}",
            author=User(username="testuser"),
            body=body,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            file_path=file_path if is_inline else "",
            old_line=None,
            new_line=new_line if is_inline else None,
            is_resolved=is_resolved,
        ),
        is_resolved=is_resolved,
    )


def _ctx(old: int, new: int, content: str = "") -> DiffLine:
    return DiffLine(
        old_lineno=old, new_lineno=new, content=content, line_type=LineType.CONTEXT
    )


def _add(new: int, content: str = "") -> DiffLine:
    return DiffLine(
        old_lineno=None, new_lineno=new, content=content, line_type=LineType.ADDITION
    )


def _del(old: int, content: str = "") -> DiffLine:
    return DiffLine(
        old_lineno=old, new_lineno=None, content=content, line_type=LineType.DELETION
    )


def _make_file(
    hunks: tuple[DiffHunk, ...] = (),
    new_path: str = "file.py",
    language: str = "",
) -> DiffFile:
    return DiffFile(
        old_path=new_path,
        new_path=new_path,
        status=FileStatus.MODIFIED,
        hunks=hunks,
        language=language,
    )


# ===================================================================
# _relative_time
# ===================================================================

class TestRelativeTime:
    """Tests for _relative_time()."""

    def _fixed_now(self, **kwargs):
        """Return a patcher that freezes datetime.now to a fixed offset from the reference dt."""
        ref = datetime(2026, 1, 1, tzinfo=timezone.utc)
        frozen = ref + timedelta(**kwargs)

        original_now = datetime.now

        def fake_now(tz=None):
            if tz is not None:
                return frozen
            return original_now(tz)

        return patch(
            "tongs.widgets.discussion_list.datetime",
            wraps=datetime,
            **{"now": fake_now},
        )

    def test_just_now_zero_seconds(self):
        with self._fixed_now(seconds=0):
            assert _relative_time(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "just now"

    def test_just_now_under_60_seconds(self):
        with self._fixed_now(seconds=59):
            assert _relative_time(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "just now"

    def test_boundary_exactly_60_seconds(self):
        with self._fixed_now(seconds=60):
            assert _relative_time(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "1m ago"

    def test_minutes_plural(self):
        with self._fixed_now(minutes=30):
            assert _relative_time(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "30m ago"

    def test_boundary_exactly_59_minutes(self):
        with self._fixed_now(minutes=59):
            assert _relative_time(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "59m ago"

    def test_boundary_exactly_60_minutes(self):
        with self._fixed_now(minutes=60):
            assert _relative_time(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "1h ago"

    def test_hours_plural(self):
        with self._fixed_now(hours=5):
            assert _relative_time(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "5h ago"

    def test_boundary_exactly_23_hours(self):
        with self._fixed_now(hours=23):
            assert _relative_time(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "23h ago"

    def test_boundary_exactly_24_hours(self):
        with self._fixed_now(hours=24):
            assert _relative_time(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "1d ago"

    def test_days_plural(self):
        with self._fixed_now(days=7):
            assert _relative_time(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "7d ago"

    def test_large_day_count(self):
        with self._fixed_now(days=365):
            assert _relative_time(datetime(2026, 1, 1, tzinfo=timezone.utc)) == "365d ago"


# ===================================================================
# render_diff_snippet
# ===================================================================

class TestRenderDiffSnippet:
    """Tests for render_diff_snippet()."""

    def test_target_line_none_returns_empty(self):
        file = _make_file(hunks=(
            DiffHunk(
                header="@@ -1,3 +1,3 @@", old_start=1, old_count=3,
                new_start=1, new_count=3,
                lines=(_ctx(1, 1, "a"), _ctx(2, 2, "b"), _ctx(3, 3, "c")),
            ),
        ))
        assert render_diff_snippet(file, None) == []

    def test_empty_hunks_returns_empty(self):
        file = _make_file(hunks=())
        assert render_diff_snippet(file, 5) == []

    def test_target_not_found_returns_empty(self):
        file = _make_file(hunks=(
            DiffHunk(
                header="@@ -1,3 +1,3 @@", old_start=1, old_count=3,
                new_start=1, new_count=3,
                lines=(_ctx(1, 1, "a"), _ctx(2, 2, "b"), _ctx(3, 3, "c")),
            ),
        ))
        assert render_diff_snippet(file, 999) == []

    def test_found_target_with_context(self):
        lines = (
            _ctx(1, 1, "line1"),
            _ctx(2, 2, "line2"),
            _ctx(3, 3, "line3"),
            _ctx(4, 4, "line4"),
            _ctx(5, 5, "line5"),
        )
        file = _make_file(hunks=(
            DiffHunk(
                header="@@ -1,5 +1,5 @@", old_start=1, old_count=5,
                new_start=1, new_count=5, lines=lines,
            ),
        ))
        result = render_diff_snippet(file, 3, context=2)
        assert len(result) == 5

    def test_target_marked_with_prefix(self):
        lines = (
            _ctx(1, 1, "line1"),
            _ctx(2, 2, "line2"),
            _ctx(3, 3, "line3"),
        )
        file = _make_file(hunks=(
            DiffHunk(
                header="@@ -1,3 +1,3 @@", old_start=1, old_count=3,
                new_start=1, new_count=3, lines=lines,
            ),
        ))
        result = render_diff_snippet(file, 2, context=1)
        # The target line (line 2) should start with "> "
        texts = [r.plain for r in result]
        target_found = any(t.startswith("> ") for t in texts)
        assert target_found, f"No line starts with '> ' in: {texts}"
        # Non-target lines should start with "  "
        non_targets = [t for t in texts if not t.startswith("> ")]
        for t in non_targets:
            assert t.startswith("  "), f"Non-target line missing '  ' prefix: {t!r}"

    def test_matched_by_old_lineno(self):
        lines = (
            _ctx(1, 1, "keep"),
            _del(2, "removed"),
            _ctx(3, 2, "after"),
        )
        file = _make_file(hunks=(
            DiffHunk(
                header="@@ -1,3 +1,2 @@", old_start=1, old_count=3,
                new_start=1, new_count=2, lines=lines,
            ),
        ))
        # Target line 2 matches old_lineno=2 on the deletion line
        result = render_diff_snippet(file, 2, context=1)
        assert len(result) > 0
        texts = [r.plain for r in result]
        target_found = any(t.startswith("> ") for t in texts)
        assert target_found, f"Old lineno match not found: {texts}"

    def test_context_clamped_at_hunk_start(self):
        lines = (
            _ctx(1, 1, "first"),
            _ctx(2, 2, "second"),
        )
        file = _make_file(hunks=(
            DiffHunk(
                header="@@ -1,2 +1,2 @@", old_start=1, old_count=2,
                new_start=1, new_count=2, lines=lines,
            ),
        ))
        # Target is line 1 (index 0), context=2 should clamp start to 0
        result = render_diff_snippet(file, 1, context=2)
        assert len(result) == 2

    def test_context_clamped_at_hunk_end(self):
        lines = (
            _ctx(1, 1, "first"),
            _ctx(2, 2, "last"),
        )
        file = _make_file(hunks=(
            DiffHunk(
                header="@@ -1,2 +1,2 @@", old_start=1, old_count=2,
                new_start=1, new_count=2, lines=lines,
            ),
        ))
        # Target is line 2 (index 1), context=2 should clamp end to len
        result = render_diff_snippet(file, 2, context=2)
        assert len(result) == 2


# ===================================================================
# _render_thread
# ===================================================================

class TestRenderThread:
    """Tests for _render_thread()."""

    def test_single_comment_no_replies(self):
        disc = _make_disc(body="Hello world")
        lines = _render_thread(disc)
        assert len(lines) > 0
        # First line should contain the author
        assert "@testuser" in lines[0].plain

    def test_comment_with_replies(self):
        reply = InlineComment(
            id="reply-1",
            author=User(username="reviewer"),
            body="Looks good",
            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            file_path="test.py",
        )
        disc = Discussion(
            id="d-threaded",
            is_inline=True,
            root_comment=InlineComment(
                id="root",
                author=User(username="author"),
                body="Please review",
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                file_path="test.py",
                new_line=5,
                replies=(reply,),
            ),
        )
        lines = _render_thread(disc)
        plain_texts = [ln.plain for ln in lines]
        # Root comment author should appear
        assert any("@author" in t for t in plain_texts)
        # Reply author should appear, indented
        reply_lines = [t for t in plain_texts if "@reviewer" in t]
        assert len(reply_lines) > 0
        for rl in reply_lines:
            assert rl.startswith("  "), f"Reply not indented: {rl!r}"

    def test_empty_body(self):
        disc = _make_disc(body="")
        lines = _render_thread(disc)
        assert len(lines) > 0
        # Should still have the author line
        assert "@testuser" in lines[0].plain
        # No rendered body lines beyond the author line (only the author line)
        # With empty body, only 1 line is produced (the author line)
        assert len(lines) == 1


# ===================================================================
# DiscussionPanel._apply_filter
# ===================================================================

class TestApplyFilter:
    """Tests for DiscussionPanel._apply_filter()."""

    def _make_panel(self, filter_mode: str = "all") -> DiscussionPanel:
        panel = DiscussionPanel.__new__(DiscussionPanel)
        panel._filter = filter_mode
        return panel

    def test_filter_all_returns_all(self):
        panel = self._make_panel("all")
        discs = [
            _make_disc(id="d1", is_resolved=False),
            _make_disc(id="d2", is_resolved=True),
        ]
        result = panel._apply_filter(discs)
        assert len(result) == 2

    def test_filter_unresolved(self):
        panel = self._make_panel("unresolved")
        discs = [
            _make_disc(id="d1", is_resolved=False),
            _make_disc(id="d2", is_resolved=True),
            _make_disc(id="d3", is_resolved=False),
        ]
        result = panel._apply_filter(discs)
        assert len(result) == 2
        assert all(not d.is_resolved for d in result)

    def test_filter_resolved(self):
        panel = self._make_panel("resolved")
        discs = [
            _make_disc(id="d1", is_resolved=False),
            _make_disc(id="d2", is_resolved=True),
            _make_disc(id="d3", is_resolved=True),
        ]
        result = panel._apply_filter(discs)
        assert len(result) == 2
        assert all(d.is_resolved for d in result)

    def test_filter_empty_input(self):
        panel = self._make_panel("all")
        assert panel._apply_filter([]) == []

    def test_filter_unresolved_empty_input(self):
        panel = self._make_panel("unresolved")
        assert panel._apply_filter([]) == []

    def test_filter_resolved_none_match(self):
        panel = self._make_panel("resolved")
        discs = [_make_disc(id="d1", is_resolved=False)]
        assert panel._apply_filter(discs) == []

    def test_filter_all_returns_copy(self):
        """_apply_filter('all') returns a new list, not the input."""
        panel = self._make_panel("all")
        discs = [_make_disc()]
        result = panel._apply_filter(discs)
        assert result is not discs


# ===================================================================
# DiscussionPanel._sort_discussions
# ===================================================================

class TestSortDiscussions:
    """Tests for DiscussionPanel._sort_discussions()."""

    def _make_panel(self) -> DiscussionPanel:
        panel = DiscussionPanel.__new__(DiscussionPanel)
        return panel

    def test_unresolved_before_resolved(self):
        panel = self._make_panel()
        discs = [
            _make_disc(id="resolved1", is_resolved=True, file_path="a.py", new_line=1),
            _make_disc(id="unresolved1", is_resolved=False, file_path="a.py", new_line=1),
        ]
        result = panel._sort_discussions(discs)
        assert not result[0].is_resolved
        assert result[1].is_resolved

    def test_sorted_by_file_path_then_line(self):
        panel = self._make_panel()
        discs = [
            _make_disc(id="d3", file_path="z.py", new_line=1),
            _make_disc(id="d1", file_path="a.py", new_line=5),
            _make_disc(id="d2", file_path="a.py", new_line=1),
        ]
        result = panel._sort_discussions(discs)
        assert result[0].id == "d2"  # a.py:1
        assert result[1].id == "d1"  # a.py:5
        assert result[2].id == "d3"  # z.py:1

    def test_generals_after_inlines(self):
        panel = self._make_panel()
        discs = [
            _make_disc(id="general", is_inline=False),
            _make_disc(id="inline", file_path="a.py", new_line=1),
        ]
        result = panel._sort_discussions(discs)
        assert result[0].id == "inline"
        assert result[1].id == "general"

    def test_empty_list(self):
        panel = self._make_panel()
        assert panel._sort_discussions([]) == []

    def test_mixed_resolved_and_file_sort(self):
        """Resolved status takes priority over file path."""
        panel = self._make_panel()
        discs = [
            _make_disc(id="r-a", is_resolved=True, file_path="a.py", new_line=1),
            _make_disc(id="u-z", is_resolved=False, file_path="z.py", new_line=1),
        ]
        result = panel._sort_discussions(discs)
        # Unresolved z.py should come before resolved a.py
        assert result[0].id == "u-z"
        assert result[1].id == "r-a"

    def test_general_unresolved_before_general_resolved(self):
        panel = self._make_panel()
        discs = [
            _make_disc(id="g-resolved", is_inline=False, is_resolved=True),
            _make_disc(id="g-unresolved", is_inline=False, is_resolved=False),
        ]
        result = panel._sort_discussions(discs)
        assert result[0].id == "g-unresolved"
        assert result[1].id == "g-resolved"


# ===================================================================
# Messages
# ===================================================================

class TestMessages:
    """Tests for message dataclasses."""

    def test_jump_to_diff_discussion_stores_fields(self):
        msg = JumpToDiffDiscussion(
            discussion_id="disc-42", file_path="src/main.py", line=17
        )
        assert msg.discussion_id == "disc-42"
        assert msg.file_path == "src/main.py"
        assert msg.line == 17

    def test_jump_to_diff_discussion_line_none(self):
        msg = JumpToDiffDiscussion(
            discussion_id="disc-99", file_path="readme.md", line=None
        )
        assert msg.line is None

    def test_discussion_reply_requested_stores_fields(self):
        msg = DiscussionReplyRequested(
            discussion_id="disc-7",
            file_path="lib/utils.py",
            line=42,
            author="alice",
        )
        assert msg.discussion_id == "disc-7"
        assert msg.file_path == "lib/utils.py"
        assert msg.line == 42
        assert msg.author == "alice"

    def test_discussion_reply_requested_none_file_line(self):
        msg = DiscussionReplyRequested(
            discussion_id="disc-general",
            file_path=None,
            line=None,
            author="bob",
        )
        assert msg.file_path is None
        assert msg.line is None
        assert msg.author == "bob"
