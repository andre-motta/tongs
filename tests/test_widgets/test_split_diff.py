"""Behavioral coverage for the responsive side-by-side diff view."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from textual.app import App, ComposeResult

from tongs.diff.models import DiffFile, DiffHunk, DiffLine, FileStatus, LineType
from tongs.forges.models import Discussion, InlineComment, User
from tongs.state.drafts import DiffSide
from tongs.widgets.diff_panel import (
    CommentMode,
    CommentRequested,
    DiffOptionList,
    DiffPanel,
    ReplyRequested,
    ResolveRequested,
)
from tongs.widgets.split_diff import (
    DiffSelection,
    DiffViewMode,
    SplitDiffColumn,
    SplitDiffView,
    is_actionable,
)


def _file() -> DiffFile:
    context = DiffLine(10, 20, "café = '東京'", LineType.CONTEXT)
    deleted = DiffLine(11, None, "old_value = 'long ' * 30", LineType.DELETION)
    deleted_extra = DiffLine(12, None, "obsolete = True", LineType.DELETION)
    added = DiffLine(None, 21, "new_value = 'long ' * 30", LineType.ADDITION)
    marker = DiffLine(None, None, "\\ No newline at end of file", LineType.NO_NEWLINE)
    return DiffFile(
        old_path="before/renamed.py",
        new_path="after/renamed.py",
        status=FileStatus.RENAMED,
        hunks=(
            DiffHunk(
                "@@ -10,3 +20,2 @@",
                10,
                3,
                20,
                2,
                (context, deleted, deleted_extra, marker, added, marker),
            ),
        ),
        additions=1,
        deletions=2,
        language="python",
    )


def _discussion() -> Discussion:
    return Discussion(
        "thread-1",
        True,
        InlineComment(
            "comment-1",
            User("reviewer"),
            "Use a clearer name",
            datetime(2026, 9, 8, tzinfo=UTC),
            "after/renamed.py",
            None,
            21,
        ),
    )


class _DiffApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        self.comments: list[CommentRequested] = []
        self.replies: list[ReplyRequested] = []
        self.resolutions: list[ResolveRequested] = []

    def compose(self) -> ComposeResult:
        yield DiffPanel()

    def on_comment_requested(self, event: CommentRequested) -> None:
        self.comments.append(event)

    def on_reply_requested(self, event: ReplyRequested) -> None:
        self.replies.append(event)

    def on_resolve_requested(self, event: ResolveRequested) -> None:
        self.resolutions.append(event)


def test_actionable_cells_are_side_specific() -> None:
    file = _file()
    context, deleted, _, marker, added, _ = file.hunks[0].lines

    assert is_actionable(context, DiffSide.OLD)
    assert is_actionable(context, DiffSide.NEW)
    assert is_actionable(deleted, DiffSide.OLD)
    assert not is_actionable(deleted, DiffSide.NEW)
    assert is_actionable(added, DiffSide.NEW)
    assert not is_actionable(added, DiffSide.OLD)
    assert not is_actionable(marker, DiffSide.OLD)
    assert not is_actionable(marker, DiffSide.NEW)
    assert not is_actionable(None, DiffSide.NEW)


def test_selection_rejects_wrong_side_and_non_source_rows() -> None:
    file = _file()
    deleted = file.hunks[0].lines[1]
    marker = file.hunks[0].lines[3]

    with pytest.raises(ValueError, match="non-actionable"):
        DiffSelection(file, DiffSide.NEW, deleted, (deleted,))
    with pytest.raises(ValueError, match="non-actionable"):
        DiffSelection(file, DiffSide.OLD, marker, (marker,))


@pytest.mark.asyncio
async def test_wide_narrow_and_restored_layout_preserves_old_selection() -> None:
    app = _DiffApp()
    file = _file()
    context, deleted = file.hunks[0].lines[:2]

    async with app.run_test(size=(160, 35)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([file], [_discussion()])
        await pilot.pause()

        state = panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        assert state.requested is DiffViewMode.SPLIT
        assert state.effective is DiffViewMode.SPLIT

        split = app.query_one(SplitDiffView)
        assert split.jump_to(11, DiffSide.OLD)
        old = app.query_one("#split-old", SplitDiffColumn)
        old._selection_anchor = old.highlighted
        old.action_extend_up()
        await pilot.pause()
        assert panel.selection == DiffSelection(
            file, DiffSide.OLD, context, (context, deleted)
        )

        await pilot.resize_terminal(80, 35)
        await pilot.pause()
        assert panel.mode_state.requested is DiffViewMode.SPLIT
        assert panel.mode_state.effective is DiffViewMode.UNIFIED
        assert panel.mode_state.narrow_fallback
        assert panel.selection is not None
        assert panel.selection.side is DiffSide.OLD
        assert panel.selection.line is context

        await pilot.resize_terminal(160, 35)
        await pilot.pause()
        assert panel.mode_state.requested is DiffViewMode.SPLIT
        assert panel.mode_state.effective is DiffViewMode.SPLIT
        assert panel.selection == DiffSelection(
            file, DiffSide.OLD, context, (context, deleted)
        )


@pytest.mark.asyncio
async def test_split_comment_publishes_explicit_original_side() -> None:
    app = _DiffApp()
    file = _file()

    async with app.run_test(size=(160, 30)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([file])
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()

        split = app.query_one(SplitDiffView)
        assert split.jump_to(10, DiffSide.OLD)
        app.query_one("#split-old", SplitDiffColumn).action_comment()
        await pilot.pause()

        assert len(app.comments) == 1
        event = app.comments[0]
        assert event.file is file
        assert event.line is file.hunks[0].lines[0]
        assert event.side is DiffSide.OLD
        assert event.mode is CommentMode.COMMENT


@pytest.mark.asyncio
async def test_narrow_fallback_comment_keeps_old_context_side() -> None:
    app = _DiffApp()
    file = _file()

    async with app.run_test(size=(160, 30)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([file])
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        assert app.query_one(SplitDiffView).jump_to(10, DiffSide.OLD)

        await pilot.resize_terminal(80, 30)
        await pilot.pause()
        unified = app.query_one(DiffOptionList)
        assert unified.selection is not None
        assert unified.selection.side is DiffSide.OLD
        unified.action_comment()
        await pilot.pause()

        assert len(app.comments) == 1
        assert app.comments[0].line is file.hunks[0].lines[0]
        assert app.comments[0].side is DiffSide.OLD


@pytest.mark.asyncio
async def test_columns_share_rows_but_empty_and_marker_cells_are_not_anchors() -> None:
    app = _DiffApp()
    file = _file()

    async with app.run_test(size=(160, 30)) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([file])
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()

        old = app.query_one("#split-old", SplitDiffColumn)
        new = app.query_one("#split-new", SplitDiffColumn)
        assert old.option_count == new.option_count
        assert set(old._line_map) != set(new._line_map)
        assert all(is_actionable(line, DiffSide.OLD) for line in old._line_map.values())
        assert all(is_actionable(line, DiffSide.NEW) for line in new._line_map.values())
        marker = file.hunks[0].lines[3]
        assert marker not in old._line_map.values()
        assert marker not in new._line_map.values()


@pytest.mark.asyncio
async def test_old_side_suggestion_is_blocked_and_new_side_is_published() -> None:
    app = _DiffApp()
    file = _file()

    async with app.run_test(size=(160, 30), notifications=True) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([file])
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        split = app.query_one(SplitDiffView)

        assert split.jump_to(10, DiffSide.OLD)
        app.query_one("#split-old", SplitDiffColumn).action_suggest()
        await pilot.pause()
        assert not app.comments
        assert any("new side only" in note.message for note in app._notifications)

        assert split.jump_to(21, DiffSide.NEW)
        app.query_one("#split-new", SplitDiffColumn).action_suggest()
        await pilot.pause()
        assert len(app.comments) == 1
        assert app.comments[0].mode is CommentMode.SUGGEST
        assert app.comments[0].side is DiffSide.NEW


@pytest.mark.asyncio
async def test_discussion_state_and_actions_survive_layout_changes() -> None:
    app = _DiffApp()
    file = _file()
    discussion = _discussion()

    async with app.run_test(size=(160, 35), notifications=True) as pilot:
        panel = app.query_one(DiffPanel)
        panel.set_files([file], [discussion])
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        split = app.query_one(SplitDiffView)
        assert split.jump_to(21, DiffSide.NEW)
        new = app.query_one("#split-new", SplitDiffColumn)
        assert new._current_discussions() == [discussion]

        new.action_toggle_discussion()
        await pilot.pause()
        assert discussion.id in split.expanded_threads

        panel.request_mode(DiffViewMode.UNIFIED)
        await pilot.pause()
        assert discussion.id in app.query_one(DiffOptionList)._expanded_threads
        panel.request_mode(DiffViewMode.SPLIT)
        await pilot.pause()
        assert discussion.id in split.expanded_threads
        assert split.jump_to(21, DiffSide.NEW)

        new.action_reply_discussion()
        new.action_resolve_discussion()
        new.action_resolve_discussion()
        await pilot.pause()
        assert [event.discussion_id for event in app.replies] == [discussion.id]
        assert [(event.discussion_id, event.resolved) for event in app.resolutions] == [
            (discussion.id, True)
        ]
