"""End-to-end Textual coverage for durable review mode."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from textual.widgets import OptionList, TextArea

from tests.test_tui_mr_services import _app, _settle, _wait_until
from tongs.errors import RateLimitError
from tongs.forges.models import ReviewDecision
from tongs.state.drafts import (
    DiffSide,
    DraftContent,
    DraftState,
    DraftVerdict,
    InlineDraftComment,
    ReplyDraftComment,
    context_fingerprint,
)
from tongs.views.mr_detail import MRDetailScreen
from tongs.views.review_submit import ReviewSubmitScreen
from tongs.widgets.comment_editor import CommentEditor
from tongs.widgets.diff_panel import CommentRequested, DiffOptionList, DiffPanel
from tongs.widgets.discussion_list import DiscussionReplyRequested
from tongs.widgets.split_diff import (
    DiffSelection,
    DiffViewMode,
    SplitDiffColumn,
    SplitDiffView,
)


async def _open_detail(app, pilot) -> MRDetailScreen:
    await _settle(app)
    table = app.screen.query_one("#reviews-table")
    table.focus()
    await pilot.press("enter")
    await _wait_until(
        app,
        lambda: (
            isinstance(app.screen, MRDetailScreen)
            and cast(MRDetailScreen, app.screen)._current_review_revision is not None
        ),
    )
    return cast(MRDetailScreen, app.screen)


@pytest.mark.asyncio
async def test_review_mode_persists_locally_and_recovers_after_restart(
    tmp_path: Path,
) -> None:
    app, forge = _app(tmp_path)

    async with app.run_test(notifications=True) as pilot:
        screen = await _open_detail(app, pilot)
        screen.action_review_draft()
        await _wait_until(app, lambda: screen._review_draft is not None)

        screen.action_add_comment()
        editor = screen.query_one("#comment-editor", CommentEditor)
        editor.query_one("#comment-input", TextArea).text = "durable local note"
        editor.action_submit()
        await _wait_until(
            app,
            lambda: (
                screen._review_draft is not None
                and len(screen._review_draft.comments) == 1
            ),
        )

        assert editor.display is False
        assert not any(call[0] == "comment" for call in forge.calls)
        draft_id = screen._review_draft.id
        assert screen._review_draft.comments[0].body == "durable local note"

    restarted, _forge = _app(tmp_path)
    async with restarted.run_test(notifications=True) as pilot:
        screen = await _open_detail(restarted, pilot)
        await _wait_until(
            restarted,
            lambda: (
                screen._review_draft is not None and screen._review_draft.id == draft_id
            ),
        )
        stored = await restarted.session.drafts.get_draft(draft_id)
        assert stored.comments[0].body == "durable local note"
        assert stored.state is DraftState.EDITABLE


@pytest.mark.asyncio
async def test_review_binding_preserves_text_area_ctrl_d_and_open_buffer(
    tmp_path: Path,
) -> None:
    app, _forge = _app(tmp_path)

    async with app.run_test(notifications=True) as pilot:
        screen = await _open_detail(app, pilot)
        screen.action_review_draft()
        await _wait_until(app, lambda: screen._review_draft is not None)
        screen.action_add_comment()
        editor = screen.query_one("#comment-editor", CommentEditor)
        text_area = editor.query_one("#comment-input", TextArea)
        text_area.text = "ab"
        text_area.cursor_location = (0, 0)

        await pilot.press("ctrl+d")
        assert text_area.text == "b"
        assert app.screen is screen

        await pilot.press("ctrl+g")
        assert app.screen is screen
        assert editor.display is True
        assert text_area.text == "b"

        editor.action_cancel()
        editor.action_cancel()
        await pilot.press("ctrl+g")
        await _wait_until(app, lambda: isinstance(app.screen, ReviewSubmitScreen))


@pytest.mark.asyncio
async def test_version_conflict_keeps_editor_buffer_until_deliberate_retry(
    tmp_path: Path,
) -> None:
    app, _forge = _app(tmp_path)

    async with app.run_test(notifications=True) as pilot:
        screen = await _open_detail(app, pilot)
        screen.action_review_draft()
        await _wait_until(app, lambda: screen._review_draft is not None)
        draft = screen._review_draft
        assert draft is not None

        screen.action_add_comment()
        editor = screen.query_one("#comment-editor", CommentEditor)
        text_area = editor.query_one("#comment-input", TextArea)
        text_area.text = "my unsaved comment"
        await app.session.drafts.save_draft(
            draft.id,
            draft.version,
            DraftContent(body="external summary"),
            current_revision=draft.revision,
        )

        editor.action_submit()
        await _wait_until(app, lambda: screen._draft_conflict is not None)
        assert editor.display is True
        assert text_area.disabled is False
        assert text_area.text == "my unsaved comment"
        assert screen._review_draft is not None
        assert screen._review_draft.body == "external summary"

        editor.action_submit()
        await _wait_until(
            app,
            lambda: (
                screen._review_draft is not None
                and len(screen._review_draft.comments) == 1
            ),
        )
        assert editor.display is False
        assert screen._review_draft.body == "external summary"
        assert screen._review_draft.comments[0].body == "my unsaved comment"


@pytest.mark.asyncio
async def test_split_old_anchor_uses_complete_source_window_and_renders_marker(
    tmp_path: Path,
) -> None:
    app, forge = _app(tmp_path)

    async def renamed_diff(repo_path: str, number: int) -> list[dict[str, object]]:
        forge.calls.append(("get_diff", repo_path, number))
        return [
            {
                "filename": "after/widget.py",
                "previous_filename": "before/widget.py",
                "status": "renamed",
                "patch": "@@ -10,5 +20,5 @@\n c0\n c1\n-old\n+new\n c3\n c4",
                "additions": 1,
                "deletions": 1,
            }
        ]

    forge.get_mr_diff_fresh = renamed_diff  # type: ignore[method-assign]

    async with app.run_test(size=(160, 40), notifications=True) as pilot:
        screen = await _open_detail(app, pilot)
        screen.action_review_draft()
        await _wait_until(app, lambda: screen._review_draft is not None)
        await pilot.press("2")
        await _wait_until(app, lambda: screen._displayed_diff_revision is not None)

        panel = screen.query_one("#diff-panel", DiffPanel)
        assert panel.request_mode(DiffViewMode.SPLIT).effective is DiffViewMode.SPLIT
        split = panel.query_one(SplitDiffView)
        assert split.jump_to(12, DiffSide.OLD)
        panel.query_one("#split-old", SplitDiffColumn).action_comment()
        await pilot.pause()

        editor = screen.query_one("#comment-editor", CommentEditor)
        editor.query_one("#comment-input", TextArea).text = "keep the old behavior"
        editor.action_submit()
        await _wait_until(
            app,
            lambda: (
                screen._review_draft is not None
                and len(screen._review_draft.comments) == 1
            ),
        )

        comment = screen._review_draft.comments[0]
        assert isinstance(comment, InlineDraftComment)
        assert comment.anchor.side is DiffSide.OLD
        assert comment.anchor.old_path == "before/widget.py"
        assert comment.anchor.old_line == 12
        assert comment.anchor.context_fingerprint == context_fingerprint(
            ("c0", "c1", "old", "c3", "c4")
        )
        assert not any(call[0] == "inline" for call in forge.calls)

        old_column = panel.query_one("#split-old", SplitDiffColumn)
        assert any("DRAFT old" in str(option.prompt) for option in old_column.options)
        await pilot.resize_terminal(80, 40)
        assert panel.mode_state.effective is DiffViewMode.UNIFIED
        unified = panel.query_one("#diff-option-list", OptionList)
        assert any("DRAFT old" in str(option.prompt) for option in unified.options)
        await pilot.resize_terminal(160, 40)
        assert panel.mode_state.effective is DiffViewMode.SPLIT
        assert any("DRAFT old" in str(option.prompt) for option in old_column.options)


@pytest.mark.asyncio
async def test_submit_dialog_uses_shared_native_review_and_exact_frozen_version(
    tmp_path: Path,
) -> None:
    app, forge = _app(tmp_path)

    async with app.run_test(size=(160, 40), notifications=True) as pilot:
        screen = await _open_detail(app, pilot)
        screen.action_review_draft()
        await _wait_until(app, lambda: screen._review_draft is not None)
        await pilot.press("2")
        await _wait_until(app, lambda: screen._displayed_diff_revision is not None)

        panel = screen.query_one("#diff-panel", DiffPanel)
        selection = panel.selection
        assert selection is not None
        panel.query_one("#diff-option-list", DiffOptionList).action_comment()
        await pilot.pause()
        editor = screen.query_one("#comment-editor", CommentEditor)
        editor.query_one("#comment-input", TextArea).text = "reviewed inline"
        editor.action_submit()
        await _wait_until(
            app,
            lambda: (
                screen._review_draft is not None
                and len(screen._review_draft.comments) == 1
            ),
        )
        frozen_version = screen._review_draft.version

        screen.action_review_draft()
        await _wait_until(app, lambda: isinstance(app.screen, ReviewSubmitScreen))
        await pilot.press("v")
        await pilot.press("ctrl+s")
        await _wait_until(
            app, lambda: any(call[0] == "verdict" for call in forge.calls)
        )

        verdict_call = next(call for call in forge.calls if call[0] == "verdict")
        assert verdict_call[3] is ReviewDecision.APPROVED
        assert verdict_call[4] == "head-7"
        assert not any(call[0] == "inline" for call in forge.calls)
        attempts = tuple(app.session.drafts._held_attempt_locks)
        assert attempts == ()
        submitted = await app.session.drafts.list_drafts()
        assert submitted[0].version == frozen_version + 2
        assert submitted[0].state is DraftState.SUBMITTED


@pytest.mark.asyncio
async def test_cancelled_submission_recovers_unknown_without_replaying_known_step(
    tmp_path: Path,
) -> None:
    app, forge = _app(tmp_path)
    forge.blocked_mutation = "verdict"

    async with app.run_test(size=(160, 40), notifications=True) as pilot:
        screen = await _open_detail(app, pilot)
        screen.action_review_draft()
        await _wait_until(app, lambda: screen._review_draft is not None)
        screen.action_add_comment()
        editor = screen.query_one("#comment-editor", CommentEditor)
        editor.query_one("#comment-input", TextArea).text = "known first step"
        editor.action_submit()
        await _wait_until(
            app,
            lambda: (
                screen._review_draft is not None
                and len(screen._review_draft.comments) == 1
            ),
        )

        screen.action_review_draft()
        await _wait_until(app, lambda: isinstance(app.screen, ReviewSubmitScreen))
        await pilot.press("v")
        await pilot.press("ctrl+s")
        await _wait_until(app, forge.mutation_started.is_set)
        cancelled = app.workers.cancel_group(screen, "review-draft-submit")
        assert len(cancelled) == 1
        await _wait_until(
            app,
            lambda: (
                screen._review_progress is not None
                and screen._review_progress.outcome.value == "unknown"
            ),
        )

        assert len([call for call in forge.calls if call[0] == "comment"]) == 1
        assert len([call for call in forge.calls if call[0] == "verdict"]) == 1
        assert screen._review_draft is not None
        assert screen._review_draft.state is DraftState.UNKNOWN

        screen.action_review_draft()
        await _wait_until(app, lambda: isinstance(app.screen, ReviewSubmitScreen))
        await pilot.press("2")
        await pilot.press("2")
        await _wait_until(
            app,
            lambda: (
                screen._review_draft is not None
                and screen._review_draft.state is DraftState.EDITABLE
                and screen._review_progress is None
            ),
        )

        assert screen._review_draft.comments == ()
        assert screen._review_draft.verdict is DraftVerdict.APPROVE
        assert len([call for call in forge.calls if call[0] == "comment"]) == 1
        assert len([call for call in forge.calls if call[0] == "verdict"]) == 1


@pytest.mark.asyncio
async def test_partial_submission_resumes_remaining_step_without_replaying_receipt(
    tmp_path: Path,
) -> None:
    app, forge = _app(tmp_path)
    forge.verdict_error = RateLimitError("controlled rate limit", retry_after=1)

    async with app.run_test(size=(160, 40), notifications=True) as pilot:
        screen = await _open_detail(app, pilot)
        screen.action_review_draft()
        await _wait_until(app, lambda: screen._review_draft is not None)
        screen.action_add_comment()
        editor = screen.query_one("#comment-editor", CommentEditor)
        editor.query_one("#comment-input", TextArea).text = "confirmed comment"
        editor.action_submit()
        await _wait_until(
            app,
            lambda: (
                screen._review_draft is not None
                and len(screen._review_draft.comments) == 1
            ),
        )

        screen.action_review_draft()
        await _wait_until(app, lambda: isinstance(app.screen, ReviewSubmitScreen))
        await pilot.press("v")
        await pilot.press("ctrl+s")
        await _wait_until(
            app,
            lambda: (
                screen._review_progress is not None
                and screen._review_progress.outcome.value == "paused"
            ),
        )
        assert len([call for call in forge.calls if call[0] == "comment"]) == 1
        assert len([call for call in forge.calls if call[0] == "verdict"]) == 1

        forge.verdict_error = None
        screen.action_review_draft()
        await _wait_until(app, lambda: isinstance(app.screen, ReviewSubmitScreen))
        await pilot.press("r")
        await _wait_until(
            app,
            lambda: screen._review_draft is None and screen._review_progress is None,
        )
        assert len([call for call in forge.calls if call[0] == "comment"]) == 1
        assert len([call for call in forge.calls if call[0] == "verdict"]) == 2


@pytest.mark.asyncio
async def test_discard_requires_confirmation_and_never_calls_forge(
    tmp_path: Path,
) -> None:
    app, forge = _app(tmp_path)

    async with app.run_test(notifications=True) as pilot:
        screen = await _open_detail(app, pilot)
        screen.action_review_draft()
        await _wait_until(app, lambda: screen._review_draft is not None)
        screen.action_review_draft()
        await _wait_until(app, lambda: isinstance(app.screen, ReviewSubmitScreen))

        await pilot.press("D")
        assert isinstance(app.screen, ReviewSubmitScreen)
        await pilot.press("D")
        await _wait_until(app, lambda: screen._review_draft is None)

        assert await app.session.drafts.list_drafts() == ()
        assert not any(
            call[0] in {"comment", "inline", "reply", "verdict"} for call in forge.calls
        )


@pytest.mark.asyncio
async def test_old_revision_text_can_be_edited_but_new_anchor_is_blocked(
    tmp_path: Path,
) -> None:
    app, forge = _app(tmp_path)

    async with app.run_test(size=(160, 40), notifications=True) as pilot:
        screen = await _open_detail(app, pilot)
        screen.action_review_draft()
        await _wait_until(app, lambda: screen._review_draft is not None)
        screen.action_add_comment()
        editor = screen.query_one("#comment-editor", CommentEditor)
        editor.query_one("#comment-input", TextArea).text = "preserve me"
        editor.action_submit()
        await _wait_until(
            app,
            lambda: (
                screen._review_draft is not None
                and len(screen._review_draft.comments) == 1
            ),
        )
        comment_id = screen._review_draft.comments[0].id

        forge.detail = replace(forge.detail, head_sha="head-8")
        screen._load_detail()
        await _wait_until(
            app,
            lambda: (
                screen._current_review_revision is not None
                and screen._current_review_revision.head_sha == "head-8"
            ),
        )
        screen._open_draft_comment(comment_id)
        editor.query_one("#comment-input", TextArea).text = "preserved and edited"
        editor.action_submit()
        await _wait_until(
            app,
            lambda: (
                screen._review_draft is not None
                and screen._review_draft.comments[0].body == "preserved and edited"
            ),
        )
        assert screen._review_draft.revision.head_sha == "head-7"

        await pilot.press("2")
        await _settle(app)
        screen._diff_loaded = False
        screen._load_diff()
        await _wait_until(
            app,
            lambda: (
                screen._displayed_diff_revision is not None
                and screen._displayed_diff_revision.head_sha == "head-8"
            ),
        )
        screen.on_comment_requested(
            # This legacy-shaped event is still converted from actual parsed source.
            CommentRequested(
                file=screen._cached_diff_files[0],
                line=screen._cached_diff_files[0].hunks[0].lines[-1],
            )
        )
        assert editor.display is False
        assert screen._pending_draft_anchor is None
        assert len(screen._review_draft.comments) == 1


@pytest.mark.asyncio
async def test_open_composer_keeps_navigation_buffer_and_exact_draft_identity(
    tmp_path: Path,
) -> None:
    app, _forge = _app(tmp_path)

    async with app.run_test(size=(160, 40), notifications=True) as pilot:
        screen = await _open_detail(app, pilot)
        screen.action_review_draft()
        await _wait_until(app, lambda: screen._review_draft is not None)
        original = screen._review_draft
        assert original is not None
        assert screen._current_review_revision is not None
        second = await app.services.create_draft(
            app.services.draft_target(
                screen.mr_summary, screen._current_review_revision
            )
        )
        screen._review_drafts = (original, second)

        screen.action_add_comment()
        editor = screen.query_one("#comment-editor", CommentEditor)
        text_area = editor.query_one("#comment-input", TextArea)
        text_area.text = "  exact local spacing  "

        screen.action_focus_tab("diff")
        await pilot.resize_terminal(80, 30)
        screen.action_focus_tab("overview")
        screen.action_next_review_draft()
        screen.action_review_draft()
        assert screen._review_draft.id == original.id
        assert editor.display is True
        assert text_area.text == "  exact local spacing  "

        screen._select_review_draft(second)
        editor.action_submit()
        await _wait_until(app, lambda: text_area.disabled is False)
        assert editor.display is True
        assert text_area.text == "  exact local spacing  "
        assert (await app.session.drafts.get_draft(second.id)).comments == ()

        screen._select_review_draft(original)
        editor.action_submit()
        await _wait_until(
            app,
            lambda: (
                screen._review_draft is not None
                and len(screen._review_draft.comments) == 1
            ),
        )
        assert screen._review_draft.comments[0].body == "  exact local spacing  "


@pytest.mark.asyncio
async def test_inline_conflict_keeps_captured_anchor_for_deliberate_retry(
    tmp_path: Path,
) -> None:
    app, forge = _app(tmp_path)

    async with app.run_test(size=(160, 40), notifications=True) as pilot:
        screen = await _open_detail(app, pilot)
        screen.action_review_draft()
        await _wait_until(app, lambda: screen._review_draft is not None)
        await pilot.press("2")
        await _wait_until(app, lambda: screen._displayed_diff_revision is not None)
        draft = screen._review_draft
        assert draft is not None

        panel = screen.query_one("#diff-panel", DiffPanel)
        panel.query_one("#diff-option-list", DiffOptionList).action_comment()
        await pilot.pause()
        editor = screen.query_one("#comment-editor", CommentEditor)
        editor.query_one("#comment-input", TextArea).text = "retry this anchor"
        await app.session.drafts.save_draft(
            draft.id,
            draft.version,
            DraftContent(body="external summary"),
            current_revision=draft.revision,
        )

        editor.action_submit()
        await _wait_until(app, lambda: screen._draft_conflict is not None)
        assert screen._pending_draft_anchor is not None
        assert editor.display is True

        editor.action_submit()
        await _wait_until(
            app,
            lambda: (
                screen._review_draft is not None
                and len(screen._review_draft.comments) == 1
            ),
        )
        comment = screen._review_draft.comments[0]
        assert isinstance(comment, InlineDraftComment)
        assert comment.body == "retry this anchor"
        assert screen._pending_draft_anchor is None
        assert not any(call[0] == "inline" for call in forge.calls)


@pytest.mark.asyncio
async def test_context_draft_marker_survives_folding_and_multiline_split_pairing(
    tmp_path: Path,
) -> None:
    app, forge = _app(tmp_path)

    async def context_diff(repo_path: str, number: int) -> list[dict[str, object]]:
        forge.calls.append(("get_diff", repo_path, number))
        patch = "@@ -1,12 +1,12 @@\n" + "\n".join(
            f" line {number}" for number in range(1, 13)
        )
        return [
            {
                "filename": "widget.py",
                "status": "modified",
                "patch": patch,
                "additions": 0,
                "deletions": 0,
            }
        ]

    forge.get_mr_diff_fresh = context_diff  # type: ignore[method-assign]

    async with app.run_test(size=(160, 40), notifications=True) as pilot:
        screen = await _open_detail(app, pilot)
        screen.action_review_draft()
        await _wait_until(app, lambda: screen._review_draft is not None)
        await pilot.press("2")
        await _wait_until(app, lambda: screen._displayed_diff_revision is not None)
        file = screen._cached_diff_files[0]
        line = next(item for item in file.hunks[0].lines if item.new_lineno == 7)
        selection = DiffSelection(file, DiffSide.NEW, line, (line,))
        screen.on_comment_requested(
            CommentRequested(
                file=file, line=line, side=DiffSide.NEW, selection=selection
            )
        )
        editor = screen.query_one("#comment-editor", CommentEditor)
        editor.query_one("#comment-input", TextArea).text = "first line\nsecond line"
        editor.action_submit()
        await _wait_until(
            app,
            lambda: (
                screen._review_draft is not None
                and len(screen._review_draft.comments) == 1
            ),
        )

        panel = screen.query_one("#diff-panel", DiffPanel)
        unified = panel.query_one("#diff-option-list", DiffOptionList)
        assert any(
            candidate.new_lineno == 7 for candidate in unified._line_map.values()
        )
        assert any(
            "DRAFT new" in str(option.prompt) and "second line" in str(option.prompt)
            for option in unified.options
        )

        assert panel.request_mode(DiffViewMode.SPLIT).effective is DiffViewMode.SPLIT
        old_column = panel.query_one("#split-old", SplitDiffColumn)
        new_column = panel.query_one("#split-new", SplitDiffColumn)
        assert len(old_column.options) == len(new_column.options)
        marker_indices = [
            index
            for index, option in enumerate(new_column.options)
            if "DRAFT new" in str(option.prompt) or "second line" in str(option.prompt)
        ]
        assert len(marker_indices) == 2
        assert all(index not in new_column._line_map for index in marker_indices)


@pytest.mark.asyncio
async def test_reply_is_immediate_outside_review_and_durable_inside_review(
    tmp_path: Path,
) -> None:
    app, forge = _app(tmp_path)

    async with app.run_test(notifications=True) as pilot:
        screen = await _open_detail(app, pilot)
        await pilot.press("4")
        await _wait_until(app, lambda: "discussion-1" in screen._current_discussion_ids)
        editor = screen.query_one("#comment-editor", CommentEditor)
        event = DiscussionReplyRequested("discussion-1", None, None, "bob")

        screen.on_discussion_reply_requested(event)
        editor.query_one("#comment-input", TextArea).text = "immediate reply"
        editor.action_submit()
        await _wait_until(
            app,
            lambda: any(
                call[0] == "reply" and call[2] == "immediate reply"
                for call in forge.calls
            ),
        )

        screen.action_review_draft()
        await _wait_until(app, lambda: screen._review_draft is not None)
        screen.on_discussion_reply_requested(event)
        editor.query_one("#comment-input", TextArea).text = "durable reply"
        editor.action_submit()
        await _wait_until(
            app,
            lambda: (
                screen._review_draft is not None
                and len(screen._review_draft.comments) == 1
            ),
        )
        comment = screen._review_draft.comments[0]
        assert isinstance(comment, ReplyDraftComment)
        assert comment.body == "durable reply"
        assert len([call for call in forge.calls if call[0] == "reply"]) == 1


@pytest.mark.asyncio
async def test_submit_modal_edits_removes_and_validates_exact_summary(
    tmp_path: Path,
) -> None:
    app, forge = _app(tmp_path)

    async with app.run_test(notifications=True) as pilot:
        screen = await _open_detail(app, pilot)
        screen.action_review_draft()
        await _wait_until(app, lambda: screen._review_draft is not None)
        screen.action_add_comment()
        editor = screen.query_one("#comment-editor", CommentEditor)
        editor.query_one("#comment-input", TextArea).text = "draft comment"
        editor.action_submit()
        await _wait_until(
            app,
            lambda: (
                screen._review_draft is not None
                and len(screen._review_draft.comments) == 1
            ),
        )

        screen.action_review_draft()
        await _wait_until(app, lambda: isinstance(app.screen, ReviewSubmitScreen))
        await pilot.pause()
        await pilot.press("e")
        await _wait_until(app, lambda: app.screen is screen and editor.display)
        text_area = editor.query_one("#comment-input", TextArea)
        text_area.text = " \n "
        editor.action_submit()
        assert editor.display is True
        assert text_area.text == " \n "
        text_area.text = "edited draft comment"
        editor.action_submit()
        await _wait_until(
            app,
            lambda: (
                screen._review_draft is not None
                and screen._review_draft.comments[0].body == "edited draft comment"
            ),
        )

        screen.action_review_draft()
        await _wait_until(app, lambda: isinstance(app.screen, ReviewSubmitScreen))
        await pilot.pause()
        await pilot.press("x")
        assert isinstance(app.screen, ReviewSubmitScreen)
        await pilot.press("x")
        await _wait_until(
            app,
            lambda: (
                screen._review_draft is not None and screen._review_draft.comments == ()
            ),
        )

        screen.action_review_draft()
        await _wait_until(app, lambda: isinstance(app.screen, ReviewSubmitScreen))
        await pilot.pause()
        modal = cast(ReviewSubmitScreen, app.screen)
        await pilot.press("v")
        await pilot.press("v")
        summary = modal.query_one("#review-submit-body", TextArea)
        summary.text = " \n "
        await pilot.press("ctrl+s")
        assert app.screen is modal
        assert summary.text == " \n "

        summary.text = "\n  exact review summary  \n"
        await pilot.press("ctrl+s")
        await _wait_until(
            app,
            lambda: any(call[0] == "verdict" for call in forge.calls),
        )
        verdict = next(call for call in forge.calls if call[0] == "verdict")
        assert verdict[3] is ReviewDecision.CHANGES_REQUESTED
        stored = await app.session.drafts.list_drafts()
        assert stored[0].body == "\n  exact review summary  \n"


@pytest.mark.asyncio
async def test_paused_attempt_recovers_unknown_after_restart_and_can_be_asserted_sent(
    tmp_path: Path,
) -> None:
    app, forge = _app(tmp_path)
    forge.verdict_error = RateLimitError("controlled rate limit", retry_after=1)

    async with app.run_test(notifications=True) as pilot:
        screen = await _open_detail(app, pilot)
        screen.action_review_draft()
        await _wait_until(app, lambda: screen._review_draft is not None)
        screen.action_add_comment()
        editor = screen.query_one("#comment-editor", CommentEditor)
        editor.query_one("#comment-input", TextArea).text = "confirmed before restart"
        editor.action_submit()
        await _wait_until(
            app,
            lambda: (
                screen._review_draft is not None
                and len(screen._review_draft.comments) == 1
            ),
        )
        screen.action_review_draft()
        await _wait_until(app, lambda: isinstance(app.screen, ReviewSubmitScreen))
        await pilot.press("v")
        await pilot.press("ctrl+s")
        await _wait_until(
            app,
            lambda: (
                screen._review_progress is not None
                and screen._review_progress.outcome.value == "paused"
            ),
        )
        attempt_id = screen._review_progress.attempt_id
        draft_id = screen._review_progress.draft_id

    restarted, restarted_forge = _app(tmp_path)
    async with restarted.run_test(notifications=True) as pilot:
        screen = await _open_detail(restarted, pilot)
        await _wait_until(
            restarted,
            lambda: (
                screen._review_progress is not None
                and screen._review_progress.outcome.value == "unknown"
            ),
        )
        assert screen._review_progress.attempt_id == attempt_id
        assert screen._review_progress.draft_id == draft_id
        screen.action_review_draft()
        await _wait_until(
            restarted, lambda: isinstance(restarted.screen, ReviewSubmitScreen)
        )
        await pilot.press("3")
        assert isinstance(restarted.screen, ReviewSubmitScreen)
        await pilot.press("3")
        await _wait_until(
            restarted,
            lambda: screen._review_draft is None and screen._review_progress is None,
        )
        assert not any(
            call[0] in {"comment", "inline", "reply", "verdict"}
            for call in restarted_forge.calls
        )
        stored = await restarted.session.drafts.get_draft(draft_id)
        assert stored.state is DraftState.SUBMITTED
