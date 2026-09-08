"""MR detail screen with tabbed interface."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, replace
from typing import ClassVar
from uuid import UUID

from rich.markup import escape
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.screen import Screen
from textual.widgets import (
    Footer,
    Header,
    Markdown,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from tongs.diff.position import DiffPosition
from tongs.forges.models import (
    CIStatus,
    Discussion,
    MRDetail,
    MRState,
    MRSummary,
    Pipeline,
    PipelineJob,
)
from tongs.services.ci_mutations import CIMutationOutcome
from tongs.services.models import ReviewRevision
from tongs.services.mr_actions import MRActionOutcome
from tongs.services.review_mutations import MutationOutcome, MutationStatus
from tongs.services.review_submission import SubmissionOutcome, SubmissionProgress
from tongs.state.drafts import (
    DiffSide,
    DraftComment,
    DraftConflictError,
    DraftContent,
    DraftSnapshot,
    DraftState,
    GeneralDraftComment,
    InlineDraftComment,
    ReconciliationResolution,
    ReplyDraftComment,
    new_general_comment,
    new_inline_comment,
    new_reply_comment,
)
from tongs.tui_services import TUIDiffResult, TUIDraftTarget
from tongs.views.review_submit import (
    ReviewSubmitAction,
    ReviewSubmitActionKind,
    ReviewSubmitScreen,
)
from tongs.views.suggestion import (
    build_suggestion_template,
    extract_new_side_lines,
    format_suggestion_block,
    parse_suggestion_template,
    resolve_suggestion_position,
)
from tongs.widgets.comment_editor import (
    CommentEditor,
    CommentSubmitted,
    DraftCommentEdited,
    GeneralCommentSubmitted,
    ReplySubmitted,
)
from tongs.widgets.diff_panel import (
    CommentMode,
    CommentRequested,
    DiffPanel,
    ReplyRequested,
    ResolveRequested,
)
from tongs.widgets.discussion_list import (
    DiscussionPanel,
    DiscussionReplyRequested,
    JumpToDiffDiscussion,
)
from tongs.widgets.pipeline_panel import (
    CancelJobRequested,
    CancelPipelineRequested,
    LoadJobLogRequested,
    LoadJobsRequested,
    PipelinePanel,
    RetryJobRequested,
    RetryPipelineRequested,
)
from tongs.widgets.review_draft import (
    CapturedDraftAnchor,
    ReviewDraftBar,
    capture_draft_anchor,
    inline_markers,
)
from tongs.widgets.split_diff import DiffSelection


def _ci_label(status: CIStatus) -> str:
    labels = {
        CIStatus.SUCCESS: "[green]passing[/]",
        CIStatus.FAILED: "[red]failed[/]",
        CIStatus.RUNNING: "[yellow]running[/]",
        CIStatus.PENDING: "[dim]pending[/]",
        CIStatus.CANCELED: "[dim]canceled[/]",
        CIStatus.SKIPPED: "[dim]skipped[/]",
        CIStatus.UNKNOWN: "[dim]unknown[/]",
    }
    return labels.get(status, "[dim]unknown[/]")


@dataclass(slots=True)
class _MutationIntent:
    """One complete TUI command retained through a terminal outcome."""

    operation_id: str
    fingerprint: tuple[object, ...]
    action: str
    unknown: bool = False


def _merge_readiness(mr: MRDetail) -> str:
    if mr.state == MRState.MERGED:
        return "[green bold]MERGED[/]"
    if mr.state == MRState.CLOSED:
        return "[red]CLOSED[/]"
    blockers = []
    if mr.is_draft:
        blockers.append("draft")
    if mr.has_conflicts:
        blockers.append("has conflicts")
    if mr.ci_status == CIStatus.FAILED:
        blockers.append("CI failing")
    elif mr.ci_status == CIStatus.RUNNING:
        blockers.append("CI running")
    if mr.detailed_merge_status and mr.detailed_merge_status not in (
        "mergeable",
        "can_be_merged",
    ):
        status = mr.detailed_merge_status.replace("_", " ")
        if status not in " ".join(blockers):
            blockers.append(status)
    if not blockers:
        return "[green]ready[/]"
    return "[yellow]blocked[/] -- " + ", ".join(blockers)


class MROverview(Static):
    """MR overview panel showing metadata and description."""

    def set_mr(self, mr: MRDetail) -> None:
        approvals = ", ".join(u.username for u in mr.approvals) or "none"
        reviewers = ", ".join(u.username for u in mr.reviewers) or "none"
        assignees = ", ".join(u.username for u in mr.assignees) or "none"
        labels = ", ".join(mr.labels) or "none"
        draft = "[yellow]DRAFT[/]  " if mr.is_draft else ""
        conflicts = "[red]HAS CONFLICTS[/]  " if mr.has_conflicts else ""

        meta = (
            f"[bold]!{mr.number} {escape(mr.title)}[/]\n"
            f"{draft}{conflicts}"
            f"{mr.source_branch} -> {mr.target_branch}  "
            f"by @{mr.author.username}\n\n"
            f"CI: {_ci_label(mr.ci_status)}  "
            f"Approvals: {approvals}\n"
            f"Merge: {_merge_readiness(mr)}\n"
            f"Reviewers: {reviewers}  "
            f"Assignees: {assignees}\n"
            f"Labels: {labels}  "
            f"Changes: +{mr.additions or 0} -{mr.deletions or 0}\n"
        )

        self.update(meta)


class MRDetailScreen(Screen):
    """MR detail view with tabs for Overview, Diff, Discussion, Pipeline."""

    BINDINGS: ClassVar[list] = [
        Binding("escape", "go_back", "Back", show=True),
        Binding("q", "go_back", "Back", show=False),
        Binding("1", "focus_tab('overview')", "1 Overview", show=False),
        Binding("2", "focus_tab('diff')", "2 Diff", show=False),
        Binding("3", "focus_tab('commits')", "3 Commits", show=False),
        Binding("4", "focus_tab('discussion')", "4 Discussion", show=False),
        Binding("5", "focus_tab('pipeline')", "5 Pipeline", show=False),
        Binding("c", "add_comment", "Comment", show=True),
        Binding("A", "approve", "Approve", show=True, key_display="A"),
        Binding("U", "unapprove", "Unapprove", show=False, key_display="U"),
        Binding("M", "merge", "Merge", show=True, key_display="M"),
        Binding("X", "close_mr", "Close", show=False, key_display="X"),
        Binding("o", "open_in_browser", "Open", show=False),
        Binding("ctrl+y", "yank_url", "Copy URL", show=True),
        Binding("ctrl+r", "refresh", "Refresh", show=True),
        Binding("ctrl+g", "review_draft", "Review draft", show=True),
        Binding(
            "alt+right_square_bracket", "next_review_draft", "Next draft", show=False
        ),
    ]

    def __init__(self, mr_summary: MRSummary):
        super().__init__()
        self.mr_summary = mr_summary
        self.mr_detail: MRDetail | None = None
        self._diff_loaded = False
        self._discussions_loaded = False
        self._pipeline_loaded = False
        self._cached_diff_files: list | None = None
        self._displayed_diff_revision: ReviewRevision | None = None
        self._current_review_revision: ReviewRevision | None = None
        self._supports_batched_review: bool | None = None
        self._pending_inline_revision: ReviewRevision | None = None
        self._mutation_intents: dict[tuple[object, ...], _MutationIntent] = {}
        self._review_drafts: tuple[DraftSnapshot, ...] = ()
        self._review_draft: DraftSnapshot | None = None
        self._review_draft_target: TUIDraftTarget | None = None
        self._review_progress: SubmissionProgress | None = None
        self._review_progress_by_draft: dict[UUID, SubmissionProgress] = {}
        self._draft_conflict: DraftContent | None = None
        self._draft_busy = False
        self._pending_draft_id: UUID | None = None
        self._pending_draft_anchor: CapturedDraftAnchor | None = None
        self._pending_draft_reply: str | None = None
        self._pending_draft_start_line: int | None = None
        self._pending_draft_start_side: DiffSide | None = None
        self._current_discussion_ids: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Header()
        yield ReviewDraftBar(id="review-draft-bar")
        with TabbedContent(initial="overview"):
            with TabPane("Overview", id="overview"), VerticalScroll():
                yield MROverview(id="mr-overview")
                yield Markdown(id="mr-description")
            with TabPane("Diff", id="diff"):
                yield DiffPanel(id="diff-panel")
            with TabPane("Commits", id="commits"), VerticalScroll(id="commits-scroll"):
                yield Static("[dim]Loading commits...[/]", id="commits-content")
            with TabPane("Discussion", id="discussion"):
                yield Static("", id="disc-status-bar", classes="disc-status-bar")
                yield DiscussionPanel(id="disc-panel")
            with TabPane("Pipeline", id="pipeline"):
                yield Static(
                    "", id="pipeline-status-bar", classes="pipeline-status-bar"
                )
                yield PipelinePanel(id="pipeline-panel")
        yield CommentEditor(id="comment-editor")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"!{self.mr_summary.number} {escape(self.mr_summary.title)}"
        self._load_detail()

    @work(exclusive=True, group="mr-detail")
    async def _load_detail(self) -> None:
        try:
            snapshot = await self.app.services.get_review(self.mr_summary)
            self.mr_detail = snapshot.detail
            self._current_review_revision = snapshot.revision
            self._supports_batched_review = snapshot.capabilities.batched_review
            overview = self.query_one("#mr-overview", MROverview)
            overview.set_mr(self.mr_detail)
            description_widget = self.query_one("#mr-description", Markdown)
            description_widget.update(self.mr_detail.description or "")
            if snapshot.revision is not None:
                self._load_review_drafts(snapshot.revision)
        except Exception as exc:  # noqa: BLE001 - Report background/action failures without terminating the TUI.
            self.notify(
                f"Could not load MR details. Try Ctrl+R to refresh. ({exc})",
                severity="warning",
            )

    @work(exclusive=True, group="review-draft-load")
    async def _load_review_drafts(self, current_revision: ReviewRevision) -> None:
        """Recover durable drafts and exact attempt identities for this review."""
        try:
            current_target = self.app.services.draft_target(
                self.mr_summary, current_revision
            )
            drafts = await self.app.services.list_drafts(current_target)
            recoveries = await self.app.services.list_draft_recoveries(current_target)
            visible = tuple(
                draft for draft in drafts if draft.state is not DraftState.SUBMITTED
            )
            previous_id = self._review_draft.id if self._review_draft else None
            active = next(
                (draft for draft in visible if draft.id == previous_id),
                visible[0] if visible else None,
            )
            self._review_drafts = visible
            self._review_draft = active
            self._review_draft_target = (
                TUIDraftTarget(active.review, active.revision) if active else None
            )
            self._review_progress_by_draft = {
                recovery.progress.draft_id: recovery.progress for recovery in recoveries
            }
            self._review_progress = (
                self._review_progress_by_draft.get(active.id) if active else None
            )
            self._refresh_draft_ui()
        except Exception as exc:  # noqa: BLE001 - Keep MR reads usable if draft recovery fails.
            self.notify(f"Could not recover review drafts. ({exc})", severity="warning")

    def _refresh_draft_ui(self) -> None:
        self.query_one("#review-draft-bar", ReviewDraftBar).show_draft(
            self._review_draft,
            self._current_review_revision,
            progress=self._review_progress,
            busy=self._draft_busy,
            conflict=self._draft_conflict is not None,
        )
        self.query_one("#diff-panel", DiffPanel).set_draft_markers(
            inline_markers(self._review_draft)
        )

    def action_review_draft(self) -> None:
        """Start review mode explicitly or inspect its current durable draft."""
        editor = self.query_one("#comment-editor", CommentEditor)
        if editor.display:
            self.notify(
                "Save or explicitly discard the open editor text first.",
                severity="warning",
            )
            return
        if self._review_draft is None:
            revision = self._current_review_revision
            if revision is None:
                self.notify(
                    "The review revision is unavailable. Refresh before starting a review.",
                    severity="warning",
                )
                return
            self._create_review_draft(
                self.app.services.draft_target(self.mr_summary, revision)
            )
            return
        self._show_review_draft()

    def action_next_review_draft(self) -> None:
        """Cycle recovered drafts without changing their stored revision targets."""
        if self.query_one("#comment-editor", CommentEditor).display:
            self.notify(
                "Save or explicitly discard the open editor text first.",
                severity="warning",
            )
            return
        if len(self._review_drafts) < 2 or self._review_draft is None:
            return
        index = self._review_drafts.index(self._review_draft)
        self._select_review_draft(
            self._review_drafts[(index + 1) % len(self._review_drafts)]
        )

    def _select_review_draft(self, draft: DraftSnapshot) -> None:
        self._review_draft = draft
        self._review_draft_target = TUIDraftTarget(draft.review, draft.revision)
        self._review_progress = self._review_progress_by_draft.get(draft.id)
        self._draft_conflict = None
        self._refresh_draft_ui()

    def _show_review_draft(self) -> None:
        draft = self._review_draft
        if draft is None:
            return
        github = self.mr_summary.forge_host.forge_type.value == "github"
        supports_submission = self._supports_batched_review is not None and (
            not github or self._supports_batched_review
        )
        screen = ReviewSubmitScreen(
            draft,
            self._current_review_revision,
            progress=self._review_progress,
            recovered_content=self._draft_conflict,
            supports_submission=supports_submission,
            submission_unavailable_reason=(
                "GitHub batch review submission is unavailable for this repository. "
                "Quick comments remain available outside review mode."
                if github and self._supports_batched_review is False
                else "Review capabilities are unavailable. Refresh before submitting."
            ),
            supports_request_changes=github,
        )
        self.app.push_screen(screen, self._handle_review_action)

    def _handle_review_action(self, action: ReviewSubmitAction | None) -> None:
        if action is None:
            return
        draft = self._review_draft
        if draft is None or action.draft_id != draft.id:
            self.notify(
                "The review draft changed while the dialog was open. Reopen it.",
                severity="warning",
            )
            return
        if action.expected_version != draft.version:
            if action.kind in {
                ReviewSubmitActionKind.SUBMIT,
                ReviewSubmitActionKind.EDIT,
                ReviewSubmitActionKind.REMOVE,
            }:
                self._draft_conflict = replace(
                    draft.content, body=action.body, verdict=action.verdict
                )
                self._refresh_draft_ui()
                self.notify(
                    "Draft changed while the dialog was open. Review the recovered summary and verdict.",
                    severity="warning",
                )
                self.call_after_refresh(self._show_review_draft)
            else:
                self.notify(
                    "The review draft changed while the dialog was open. Reopen it.",
                    severity="warning",
                )
            return
        if action.kind is ReviewSubmitActionKind.EDIT and action.comment_id:
            self._save_review_content(
                draft,
                replace(draft.content, body=action.body, verdict=action.verdict),
                edit_comment_id=action.comment_id,
                reopen_after_conflict=True,
            )
        elif action.kind is ReviewSubmitActionKind.REMOVE and action.comment_id:
            comments = tuple(
                comment for comment in draft.comments if comment.id != action.comment_id
            )
            self._save_review_content(
                draft,
                replace(
                    draft.content,
                    body=action.body,
                    verdict=action.verdict,
                    comments=comments,
                ),
                reopen_after_conflict=True,
            )
        elif action.kind is ReviewSubmitActionKind.DISCARD:
            self._discard_review_draft(draft)
        elif action.kind is ReviewSubmitActionKind.DISMISS_RECOVERY:
            self._draft_conflict = None
            self._refresh_draft_ui()
        elif action.kind is ReviewSubmitActionKind.NEW_REVISION:
            self._create_current_revision_draft(draft)
        elif action.kind is ReviewSubmitActionKind.SUBMIT:
            self._submit_review_draft(
                draft,
                replace(draft.content, body=action.body, verdict=action.verdict),
            )
        elif action.kind is ReviewSubmitActionKind.RESUME and action.attempt_id:
            self._continue_review_submission(draft, action.attempt_id)
        elif (
            action.kind is ReviewSubmitActionKind.RECONCILE
            and action.attempt_id
            and action.resolution
        ):
            self._continue_review_submission(
                draft, action.attempt_id, resolution=action.resolution
            )

    def _create_review_draft(
        self, target: TUIDraftTarget, content: DraftContent | None = None
    ) -> None:
        if self._draft_busy:
            self.notify(
                "A review draft action is already in progress.", severity="warning"
            )
            return
        self._draft_busy = True
        self._refresh_draft_ui()
        self._do_create_review_draft(target, content)

    @work(group="review-draft-create")
    async def _do_create_review_draft(
        self, target: TUIDraftTarget, content: DraftContent | None = None
    ) -> None:
        try:
            draft = await self.app.services.create_draft(target, content)
            self._review_drafts = (draft, *self._review_drafts)
            self._select_review_draft(draft)
            self.notify("Review mode started. Comments are now saved locally.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - Surface safe durable-store failures.
            self.notify(f"Could not start review mode. ({exc})", severity="error")
        finally:
            self._draft_busy = False
            self._refresh_draft_ui()

    def _create_current_revision_draft(self, old: DraftSnapshot) -> None:
        revision = self._current_review_revision
        if revision is None or revision == old.revision:
            return
        transferable: list[DraftComment] = []
        omitted = 0
        for comment in old.comments:
            if (
                isinstance(comment, GeneralDraftComment)
                or isinstance(comment, ReplyDraftComment)
                and self._reply_is_current(comment.thread_id)
            ):
                transferable.append(comment)
            else:
                omitted += 1
        content = DraftContent(old.body, old.verdict, tuple(transferable))
        self._create_review_draft(
            self.app.services.draft_target(self.mr_summary, revision), content
        )
        if omitted:
            self.notify(
                f"{omitted} old inline or unavailable reply comment(s) remain in the old draft for re-anchoring.",
                severity="warning",
            )

    def _reply_is_current(self, thread_id: str) -> bool:
        return thread_id in getattr(self, "_current_discussion_ids", set())

    def _open_draft_comment(self, comment_id: UUID) -> None:
        draft = self._review_draft
        if draft is None:
            return
        comment = next((item for item in draft.comments if item.id == comment_id), None)
        if comment is None:
            self.notify("That draft comment no longer exists.", severity="warning")
            return
        if isinstance(comment, InlineDraftComment):
            anchor = comment.anchor
            path = anchor.old_path if anchor.side is DiffSide.OLD else anchor.new_path
            line = anchor.old_line if anchor.side is DiffSide.OLD else anchor.new_line
            target = f"{anchor.side.value} {path}:{line}"
        elif isinstance(comment, ReplyDraftComment):
            target = f"reply to {comment.thread_id}"
        else:
            target = "general comment"
        self.query_one("#comment-editor", CommentEditor).open_draft_comment(
            comment.id, comment.body, target
        )
        self._begin_draft_editor(draft)

    def _save_review_content(
        self,
        draft: DraftSnapshot,
        content: DraftContent,
        *,
        acknowledge_editor: bool = False,
        edit_comment_id: UUID | None = None,
        reopen_after_conflict: bool = False,
    ) -> None:
        if self._draft_busy:
            if acknowledge_editor:
                self.query_one("#comment-editor", CommentEditor).reject_submission(
                    "A draft save is already in progress."
                )
            return
        target = TUIDraftTarget(draft.review, draft.revision)
        self._draft_busy = True
        self._refresh_draft_ui()
        self._persist_review_content(
            target,
            draft.id,
            draft.version,
            content,
            acknowledge_editor,
            edit_comment_id,
            reopen_after_conflict,
        )

    @work(group="review-draft-save")
    async def _persist_review_content(
        self,
        target: TUIDraftTarget,
        draft_id: UUID,
        expected_version: int,
        content: DraftContent,
        acknowledge_editor: bool,
        edit_comment_id: UUID | None,
        reopen_after_conflict: bool,
    ) -> None:
        reopen_review = False
        editor = self.query_one("#comment-editor", CommentEditor)
        try:
            current_revision = self._current_review_revision or target.revision
            saved = await self.app.services.save_draft(
                target,
                draft_id,
                expected_version,
                content,
                current_revision=current_revision,
            )
            self._replace_review_draft(saved.assessed_against(current_revision))
            self._draft_conflict = None
            if acknowledge_editor:
                self._clear_pending_draft_editor()
                editor.acknowledge_submission()
            elif edit_comment_id is not None:
                self._open_draft_comment(edit_comment_id)
            self.notify("Draft saved locally.")
        except DraftConflictError as exc:
            current_revision = self._current_review_revision or target.revision
            self._replace_review_draft(exc.current.assessed_against(current_revision))
            self._draft_conflict = exc.caller_content
            if acknowledge_editor:
                editor.reject_submission(
                    "Draft version changed elsewhere. Your text is preserved; submit again to add it to the current version."
                )
            else:
                self.notify(
                    "Draft version changed elsewhere. Review the recovered summary and verdict.",
                    severity="warning",
                )
                reopen_review = reopen_after_conflict
        except asyncio.CancelledError:
            if acknowledge_editor:
                editor.reject_submission(
                    "Draft save was cancelled. Your text is still in the editor."
                )
            raise
        except Exception as exc:  # noqa: BLE001 - Preserve the caller buffer on safe failure.
            if acknowledge_editor:
                editor.reject_submission(f"Draft was not saved. ({exc})")
            else:
                self.notify(f"Draft was not saved. ({exc})", severity="error")
        finally:
            self._draft_busy = False
            self._refresh_draft_ui()
            if reopen_review:
                self.call_after_refresh(self._show_review_draft)

    def _replace_review_draft(self, draft: DraftSnapshot) -> None:
        self._review_drafts = tuple(
            draft if item.id == draft.id else item for item in self._review_drafts
        )
        if not any(item.id == draft.id for item in self._review_drafts):
            self._review_drafts = (draft, *self._review_drafts)
        self._select_review_draft(draft)

    def _discard_review_draft(self, draft: DraftSnapshot) -> None:
        if self._draft_busy:
            return
        self._draft_busy = True
        self._refresh_draft_ui()
        self._do_discard_review_draft(
            TUIDraftTarget(draft.review, draft.revision), draft.id, draft.version
        )

    @work(group="review-draft-discard")
    async def _do_discard_review_draft(
        self, target: TUIDraftTarget, draft_id: UUID, expected_version: int
    ) -> None:
        try:
            await self.app.services.discard_draft(target, draft_id, expected_version)
            self._review_drafts = tuple(
                draft for draft in self._review_drafts if draft.id != draft_id
            )
            replacement = self._review_drafts[0] if self._review_drafts else None
            self._review_draft = replacement
            self._review_draft_target = (
                TUIDraftTarget(replacement.review, replacement.revision)
                if replacement
                else None
            )
            self._review_progress = None
            self._review_progress_by_draft.pop(draft_id, None)
            self._draft_conflict = None
            self.notify("Local review draft discarded.")
        except DraftConflictError as exc:
            current_revision = self._current_review_revision or target.revision
            self._replace_review_draft(exc.current.assessed_against(current_revision))
            self.notify(
                "Draft version changed elsewhere. Review it before discarding.",
                severity="warning",
            )
        except Exception as exc:  # noqa: BLE001 - Surface safe durable-store failures.
            self.notify(f"Draft was not discarded. ({exc})", severity="error")
        finally:
            self._draft_busy = False
            self._refresh_draft_ui()

    def _submit_review_draft(self, draft: DraftSnapshot, content: DraftContent) -> None:
        if self._draft_busy or self._review_progress is not None:
            self.notify(
                "This draft already has submission progress.", severity="warning"
            )
            return
        if self._current_review_revision != draft.revision:
            self.notify("Old-revision drafts cannot be submitted.", severity="warning")
            return
        self._draft_busy = True
        self._refresh_draft_ui()
        self._do_submit_review_draft(
            TUIDraftTarget(draft.review, draft.revision),
            draft.id,
            draft.version,
            draft.content,
            content,
        )

    @work(group="review-draft-submit")
    async def _do_submit_review_draft(
        self,
        target: TUIDraftTarget,
        draft_id: UUID,
        expected_version: int,
        original: DraftContent,
        requested: DraftContent,
    ) -> None:
        reopen_after_conflict = False
        try:
            if requested != original:
                saved = await self.app.services.save_draft(
                    target,
                    draft_id,
                    expected_version,
                    requested,
                    current_revision=target.revision,
                )
                expected_version = saved.version
                self._replace_review_draft(saved)
            progress = await self.app.services.start_draft_submission(
                target, draft_id, expected_version
            )
            self._review_progress = progress
            self._review_progress_by_draft[draft_id] = progress
            await self._refresh_after_submission(target, draft_id, progress)
        except DraftConflictError as exc:
            self._replace_review_draft(exc.current)
            self._draft_conflict = exc.caller_content
            reopen_after_conflict = True
            self.notify(
                "Draft changed elsewhere before submission. Review the recovered summary and verdict.",
                severity="warning",
            )
        except asyncio.CancelledError:
            await self._recover_submission_state(target, draft_id)
            raise
        except Exception as exc:  # noqa: BLE001 - Service errors are safe for the terminal surface.
            await self._recover_submission_state(target, draft_id)
            self.notify(f"Review submission stopped. ({exc})", severity="error")
        finally:
            self._draft_busy = False
            self._refresh_draft_ui()
            if reopen_after_conflict:
                self.call_after_refresh(self._show_review_draft)

    def _continue_review_submission(
        self,
        draft: DraftSnapshot,
        attempt_id: UUID,
        *,
        resolution: ReconciliationResolution | None = None,
    ) -> None:
        if self._draft_busy:
            return
        self._draft_busy = True
        self._refresh_draft_ui()
        self._do_continue_review_submission(
            TUIDraftTarget(draft.review, draft.revision), attempt_id, resolution
        )

    @work(group="review-draft-submit")
    async def _do_continue_review_submission(
        self,
        target: TUIDraftTarget,
        attempt_id: UUID,
        resolution: ReconciliationResolution | None,
    ) -> None:
        try:
            if resolution is None:
                progress = await self.app.services.resume_draft_submission(
                    target, attempt_id
                )
            else:
                progress = await self.app.services.reconcile_draft_submission(
                    target, attempt_id, resolution
                )
            self._review_progress = progress
            self._review_progress_by_draft[progress.draft_id] = progress
            await self._refresh_after_submission(target, progress.draft_id, progress)
        except asyncio.CancelledError:
            await self._recover_submission_state(
                target, self._review_draft.id if self._review_draft else None
            )
            raise
        except Exception as exc:  # noqa: BLE001 - Service errors are safe for the terminal surface.
            self.notify(f"Review recovery stopped. ({exc})", severity="error")
        finally:
            self._draft_busy = False
            self._refresh_draft_ui()

    async def _refresh_after_submission(
        self,
        target: TUIDraftTarget,
        draft_id: UUID,
        progress: SubmissionProgress,
    ) -> None:
        if progress.outcome is SubmissionOutcome.SUBMITTED:
            self._review_drafts = tuple(
                draft for draft in self._review_drafts if draft.id != draft_id
            )
            self._review_draft = self._review_drafts[0] if self._review_drafts else None
            self._review_draft_target = (
                TUIDraftTarget(self._review_draft.review, self._review_draft.revision)
                if self._review_draft
                else None
            )
            self._review_progress = None
            self._review_progress_by_draft.pop(draft_id, None)
            self.notify("Review submitted.")
            self._diff_loaded = False
            self._discussions_loaded = False
            return
        if progress.outcome is SubmissionOutcome.EDITABLE:
            self._review_progress = None
            self._review_progress_by_draft.pop(draft_id, None)
        await self._recover_submission_state(target, draft_id)

    async def _recover_submission_state(
        self, target: TUIDraftTarget, draft_id: UUID | None
    ) -> None:
        current = self._current_review_revision or target.revision
        drafts = await self.app.services.list_drafts(
            self.app.services.draft_target(self.mr_summary, current)
        )
        if draft_id is not None:
            recovered = next((draft for draft in drafts if draft.id == draft_id), None)
            if recovered is not None:
                self._replace_review_draft(recovered)
        recoveries = await self.app.services.list_draft_recoveries(target)
        progress = next(
            (
                recovery.progress
                for recovery in recoveries
                if draft_id is not None and recovery.progress.draft_id == draft_id
            ),
            None,
        )
        if progress is not None:
            self._review_progress = progress
            self._review_progress_by_draft[progress.draft_id] = progress

    def _pipeline_drilled_in(self) -> bool:
        try:
            panel = self.query_one("#pipeline-panel", PipelinePanel)
            return panel._view_level > 0
        except NoMatches:
            return False

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "review_draft":
            try:
                if self.query_one("#comment-editor", CommentEditor).display:
                    return False
            except NoMatches:
                pass
        return not (
            self._pipeline_drilled_in()
            and action
            in ("add_comment", "approve", "unapprove", "merge", "close_mr", "yank_url")
        )

    def action_go_back(self) -> None:
        editor = self.query_one("#comment-editor", CommentEditor)
        if editor.display and editor._defer_close:
            text = editor.query_one("#comment-input", TextArea).text.strip()
            if text:
                self.notify(
                    "Save or explicitly discard the draft editor text before leaving.",
                    severity="warning",
                )
                return
        try:
            panel = self.query_one("#pipeline-panel", PipelinePanel)
            if panel._view_level > 0:
                panel.action_drill_out()
                return
        except NoMatches:
            pass
        screen_stack = self.app.screen_stack
        if len(screen_stack) >= 2:
            parent = screen_stack[-2]
            if hasattr(parent, "_loaded_tabs"):
                parent._loaded_tabs.clear()
        self.app.pop_screen()

    def action_focus_tab(self, tab_id: str) -> None:
        tabbed = self.query_one(TabbedContent)
        tabbed.active = tab_id
        self._on_tab_switch(tab_id)

    def on_tabbed_content_tab_activated(
        self, event: TabbedContent.TabActivated
    ) -> None:
        tab_id = event.pane.id
        if tab_id:
            self._on_tab_switch(tab_id)

    _commits_loaded: bool = False

    def _on_tab_switch(self, tab_id: str) -> None:
        if tab_id == "diff" and not self._diff_loaded:
            self._diff_loaded = True
            self._load_diff()
        elif tab_id == "commits" and not self._commits_loaded:
            self._commits_loaded = True
            self._load_commits()
        elif tab_id == "discussion" and not self._discussions_loaded:
            self._discussions_loaded = True
            self._load_discussions()
        elif tab_id == "pipeline" and not self._pipeline_loaded:
            self._pipeline_loaded = True
            self._load_pipelines()

    @work(exclusive=True, group="mr-diff")
    async def _load_diff(self) -> None:
        panel = self.query_one("#diff-panel", DiffPanel)
        self._displayed_diff_revision = None
        panel.show_placeholder("Loading diff...")
        try:
            diff, discussions = await self._fetch_diff_and_discussions()
            self._current_discussion_ids = {discussion.id for discussion in discussions}
            self._displayed_diff_revision = diff.revision
            self._cached_diff_files = list(diff.files)
            if not diff.files:
                panel.show_placeholder("No changes in this MR")
                return
            panel.set_files(self._cached_diff_files, list(discussions))
        except Exception as exc:  # noqa: BLE001 - Report background/action failures without terminating the TUI.
            self._displayed_diff_revision = None
            panel.show_placeholder(f"Could not load diff. Try Ctrl+R. ({exc})")

    async def _fetch_diff_and_discussions(
        self,
    ) -> tuple[TUIDiffResult, tuple[Discussion, ...]]:
        """Fetch diff changes and discussions in parallel."""
        diff, discussions = await asyncio.gather(
            self.app.services.get_diff(self.mr_summary),
            self.app.services.get_discussions(self.mr_summary),
            return_exceptions=True,
        )
        if isinstance(diff, BaseException):
            raise diff
        if isinstance(discussions, BaseException):
            discussions = ()
        return diff, discussions

    @work(exclusive=True, group="mr-commits")
    async def _load_commits(self) -> None:
        content = self.query_one("#commits-content", Static)
        content.update("[dim]Loading commits...[/]")
        try:
            commits = await self.app.services.get_commits(self.mr_summary)
            if not commits:
                content.update("[dim]No commits[/]")
                return

            lines = []
            for c in commits:
                sha = f"[yellow]{c.short_sha}[/]"
                author = f"[dim]@{c.author.username}[/]"
                title = escape(c.title)
                lines.append(f"{sha} {title}  {author}")
                if c.message and c.message != c.title:
                    body = c.message[len(c.title) :].strip()
                    if body:
                        for body_line in body.split("\n"):
                            lines.append(f"        [dim]{escape(body_line)}[/]")
                lines.append("")

            content.update("\n".join(lines) if lines else "[dim]No commits[/]")
        except Exception as exc:  # noqa: BLE001 - Report background/action failures without terminating the TUI.
            content.update(f"Could not load commits. ({exc})")

    @work(exclusive=True, group="mr-discussions")
    async def _load_discussions(self) -> None:
        status = self.query_one("#disc-status-bar", Static)
        status.update("[dim]Loading discussions...[/]")
        try:
            discussions = await self.app.services.get_discussions(self.mr_summary)
            self._current_discussion_ids = {discussion.id for discussion in discussions}
            if self._cached_diff_files is None:
                try:
                    diff = await self.app.services.get_diff(self.mr_summary)
                    self._cached_diff_files = list(diff.files)
                except Exception:  # noqa: BLE001 - Optional diff failures must not hide discussions.
                    self._cached_diff_files = []

            panel = self.query_one("#disc-panel", DiscussionPanel)
            panel.set_discussions(list(discussions), self._cached_diff_files)
            unresolved = sum(1 for d in discussions if not d.is_resolved)
            resolved = sum(1 for d in discussions if d.is_resolved)
            status.update(
                f"[yellow]{unresolved} unresolved[/]  [dim]{resolved} resolved[/]"
            )
        except Exception as exc:  # noqa: BLE001 - Report background/action failures without terminating the TUI.
            status.update(f"Could not load discussions. ({exc})")

    def on_jump_to_diff_discussion(self, event: JumpToDiffDiscussion) -> None:
        """Switch to Diff tab and navigate to a discussion's location."""
        self.action_focus_tab("diff")
        panel = self.query_one("#diff-panel", DiffPanel)
        panel.jump_to_discussion(event.file_path, event.line, event.discussion_id)

    @work(exclusive=True, group="mr-pipelines")
    async def _load_pipelines(self) -> None:
        status = self.query_one("#pipeline-status-bar", Static)
        status.update("[dim]Loading pipelines...[/]")
        try:
            pipelines = await self.app.services.list_review_pipelines(self.mr_summary)
            panel = self.query_one("#pipeline-panel", PipelinePanel)
            panel.set_pipelines(list(pipelines))
            running = sum(1 for p in pipelines if p.status == CIStatus.RUNNING)
            failed = sum(1 for p in pipelines if p.status == CIStatus.FAILED)
            total = len(pipelines)
            parts = [f"{total} pipeline{'s' if total != 1 else ''}"]
            if running:
                parts.append(f"[yellow]{running} running[/]")
            if failed:
                parts.append(f"[red]{failed} failed[/]")
            status.update("  ".join(parts))
        except Exception as exc:  # noqa: BLE001 - Report background/action failures without terminating the TUI.
            status.update(f"Could not load pipelines. ({exc})")

    def on_load_jobs_requested(self, event: LoadJobsRequested) -> None:
        self._load_pipeline_jobs(event.pipeline)

    @work(exclusive=True, group="mr-pipelines")
    async def _load_pipeline_jobs(self, pipeline: Pipeline) -> None:
        try:
            jobs = await self.app.services.get_pipeline_jobs(
                self.mr_summary, pipeline.id
            )
            panel = self.query_one("#pipeline-panel", PipelinePanel)
            panel.set_jobs(list(jobs), pipeline)
        except Exception as exc:  # noqa: BLE001 - Report background/action failures without terminating the TUI.
            self.notify(f"Could not load jobs: {exc}", severity="error")

    def on_load_job_log_requested(self, event: LoadJobLogRequested) -> None:
        self._load_job_log(event.job, event.pipeline)

    @work(exclusive=True, group="mr-pipelines")
    async def _load_job_log(self, job: PipelineJob, pipeline: Pipeline) -> None:
        try:
            log_text = await self.app.services.get_job_log(self.mr_summary, job.id)
            panel = self.query_one("#pipeline-panel", PipelinePanel)
            panel.set_job_log(log_text, job, pipeline)
        except Exception as exc:  # noqa: BLE001 - Report background/action failures without terminating the TUI.
            self.notify(f"Could not load job log: {exc}", severity="error")

    def on_cancel_pipeline_requested(self, event: CancelPipelineRequested) -> None:
        intent = self._begin_mutation("cancel-pipeline", "Cancel", event.pipeline_id)
        if intent is not None:
            self._do_cancel_pipeline(event.pipeline_id, intent)

    @work(group="mr-pipeline-mutation")
    async def _do_cancel_pipeline(
        self, pipeline_id: int, intent: _MutationIntent
    ) -> None:
        try:
            receipt = await self.app.services.cancel_pipeline(
                self.mr_summary, pipeline_id, operation_id=intent.operation_id
            )
            if receipt.outcome is CIMutationOutcome.UNKNOWN:
                self._retain_unknown(intent)
                self.notify(
                    "Cancel outcome is unknown. Refresh before acting again.",
                    severity="warning",
                )
                self._pipeline_loaded = False
                return
            self._finish_mutation(intent)
            self.notify("[green]Pipeline cancelled[/]")
            self._pipeline_loaded = False
            self._on_tab_switch("pipeline")
        except asyncio.CancelledError:
            self._cancel_mutation(intent)
            raise
        except Exception as exc:  # noqa: BLE001 - Report background/action failures without terminating the TUI.
            self._finish_mutation(intent)
            self.notify(f"Cancel failed: {exc}", severity="error")

    def on_retry_pipeline_requested(self, event: RetryPipelineRequested) -> None:
        intent = self._begin_mutation("retry-pipeline", "Retry", event.pipeline_id)
        if intent is not None:
            self._do_retry_pipeline(event.pipeline_id, intent)

    @work(group="mr-pipeline-mutation")
    async def _do_retry_pipeline(
        self, pipeline_id: int, intent: _MutationIntent
    ) -> None:
        try:
            receipt = await self.app.services.retry_pipeline(
                self.mr_summary, pipeline_id, operation_id=intent.operation_id
            )
            if receipt.outcome is CIMutationOutcome.UNKNOWN:
                self._retain_unknown(intent)
                self.notify(
                    "Retry outcome is unknown. Refresh before acting again.",
                    severity="warning",
                )
                self._pipeline_loaded = False
                return
            self._finish_mutation(intent)
            self.notify("[green]Pipeline retried[/]")
            self._pipeline_loaded = False
            self._on_tab_switch("pipeline")
        except asyncio.CancelledError:
            self._cancel_mutation(intent)
            raise
        except Exception as exc:  # noqa: BLE001 - Report background/action failures without terminating the TUI.
            self._finish_mutation(intent)
            self.notify(f"Retry failed: {exc}", severity="error")

    def on_cancel_job_requested(self, event: CancelJobRequested) -> None:
        intent = self._begin_mutation(
            "cancel-job", "Cancel job", event.pipeline_id, event.job_id
        )
        if intent is not None:
            self._do_cancel_job(event.pipeline_id, event.job_id, intent)

    @work(group="mr-pipeline-mutation")
    async def _do_cancel_job(
        self, pipeline_id: int, job_id: int, intent: _MutationIntent
    ) -> None:
        try:
            receipt = await self.app.services.cancel_job(
                self.mr_summary,
                pipeline_id,
                job_id,
                operation_id=intent.operation_id,
            )
            if receipt.outcome is CIMutationOutcome.UNKNOWN:
                self._retain_unknown(intent)
                self.notify(
                    "Cancel job outcome is unknown. Refresh before acting again.",
                    severity="warning",
                )
                self._pipeline_loaded = False
                return
            self._finish_mutation(intent)
            self.notify("[green]Job cancelled[/]")
        except asyncio.CancelledError:
            self._cancel_mutation(intent)
            raise
        except Exception as exc:  # noqa: BLE001 - Report background/action failures without terminating the TUI.
            self._finish_mutation(intent)
            self.notify(f"Cancel job failed: {exc}", severity="error")

    def on_retry_job_requested(self, event: RetryJobRequested) -> None:
        intent = self._begin_mutation(
            "retry-job", "Retry job", event.pipeline_id, event.job_id
        )
        if intent is not None:
            self._do_retry_job(event.pipeline_id, event.job_id, intent)

    @work(group="mr-pipeline-mutation")
    async def _do_retry_job(
        self, pipeline_id: int, job_id: int, intent: _MutationIntent
    ) -> None:
        try:
            receipt = await self.app.services.retry_job(
                self.mr_summary,
                pipeline_id,
                job_id,
                operation_id=intent.operation_id,
            )
            if receipt.outcome is CIMutationOutcome.UNKNOWN:
                self._retain_unknown(intent)
                self.notify(
                    "Retry job outcome is unknown. Refresh before acting again.",
                    severity="warning",
                )
                self._pipeline_loaded = False
                return
            self._finish_mutation(intent)
            self.notify("[green]Job retried[/]")
        except asyncio.CancelledError:
            self._cancel_mutation(intent)
            raise
        except Exception as exc:  # noqa: BLE001 - Report background/action failures without terminating the TUI.
            self._finish_mutation(intent)
            self.notify(f"Retry job failed: {exc}", severity="error")

    def on_discussion_reply_requested(self, event: DiscussionReplyRequested) -> None:
        """Open reply editor from the Discussion tab."""
        editor = self.query_one("#comment-editor", CommentEditor)
        defer = self._review_draft is not None
        if defer and not self._can_add_current_draft_target("reply"):
            return
        if defer:
            assert self._review_draft is not None
            self._begin_draft_editor(self._review_draft, reply=event.discussion_id)
        else:
            self._clear_pending_draft_editor()
        if event.file_path and event.line is not None:
            from tongs.diff.models import DiffLine, LineType

            dummy_line = DiffLine(
                old_lineno=None,
                new_lineno=event.line,
                content="",
                line_type=LineType.CONTEXT,
            )
            from tongs.diff.models import DiffFile, FileStatus

            dummy_file = DiffFile(
                old_path=event.file_path,
                new_path=event.file_path,
                status=FileStatus.MODIFIED,
                hunks=(),
            )
            editor.open_reply(
                event.discussion_id,
                dummy_file,
                dummy_line,
                event.author,
                defer_close=defer,
            )
        else:
            editor.open_reply_general(
                event.discussion_id, event.author, defer_close=defer
            )

    def action_open_in_browser(self) -> None:
        self.app.open_url(self.mr_summary.web_url)

    def action_yank_url(self) -> None:
        import pyperclip

        try:
            pyperclip.copy(self.mr_summary.web_url)
            self.notify("URL copied to clipboard")
        except (pyperclip.PyperclipException, OSError):
            self.notify(f"URL: {self.mr_summary.web_url}")

    def action_add_comment(self) -> None:
        editor = self.query_one("#comment-editor", CommentEditor)
        draft = self._review_draft
        if draft is not None and draft.state is not DraftState.EDITABLE:
            self.notify("This review draft is not editable.", severity="warning")
            return
        if draft is not None:
            self._begin_draft_editor(draft)
        else:
            self._clear_pending_draft_editor()
        editor.open_general(defer_close=draft is not None)

    _pending_action: str = ""
    _action_taken: bool = False

    def _check_open(self) -> bool:
        if self.mr_detail and self.mr_detail.state != MRState.OPEN:
            self.notify("MR is no longer open", severity="warning")
            return False
        return True

    def action_approve(self) -> None:
        if not self._check_open():
            return
        if self._review_draft is not None:
            self._show_review_draft()
            return
        if self._pending_action == "approve":
            self._pending_action = ""
            intent = self._begin_mutation("approve", "Approval")
            if intent is not None:
                self._do_approve(intent)
        else:
            self._pending_action = "approve"
            self.notify(
                f"Approve !{self.mr_summary.number}? Press A again to confirm.",
                severity="warning",
            )

    def action_unapprove(self) -> None:
        if not self._check_open():
            return
        if self._pending_action == "unapprove":
            self._pending_action = ""
            intent = self._begin_mutation("unapprove", "Unapprove")
            if intent is not None:
                self._do_unapprove(intent)
        else:
            self._pending_action = "unapprove"
            self.notify(
                f"Revoke approval on !{self.mr_summary.number}? Press U again.",
                severity="warning",
            )

    def action_merge(self) -> None:
        if not self._check_open():
            return
        if self._pending_action == "merge":
            self._pending_action = ""
            intent = self._begin_mutation("merge", "Merge")
            if intent is not None:
                self._do_merge(intent)
        else:
            self._pending_action = "merge"
            self.notify(
                f"Merge !{self.mr_summary.number} into {self.mr_summary.target_branch}? "
                "Press M again to confirm.",
                severity="warning",
            )

    def action_close_mr(self) -> None:
        if not self._check_open():
            return
        if self._pending_action == "close":
            self._pending_action = ""
            intent = self._begin_mutation("close", "Close")
            if intent is not None:
                self._do_close(intent)
        else:
            self._pending_action = "close"
            self.notify(
                f"Close !{self.mr_summary.number}? Press X again to confirm.",
                severity="warning",
            )

    @work(group="mr-action-mutation")
    async def _do_approve(self, intent: _MutationIntent) -> None:
        try:
            outcome = await self.app.services.approve(
                self.mr_summary, operation_id=intent.operation_id
            )
            if not self._review_mutation_succeeded(outcome, "Approval"):
                self._retain_unknown(intent)
                self._load_detail()
                return
            self._finish_mutation(intent)
            self.notify(
                f"[green]Approved !{self.mr_summary.number}[/]",
                severity="information",
            )
            self._action_taken = True
            self._load_detail()
        except asyncio.CancelledError:
            self._cancel_mutation(intent)
            raise
        except Exception as exc:  # noqa: BLE001 - Report background/action failures without terminating the TUI.
            self._finish_mutation(intent)
            self.notify(f"Approve failed: {exc}", severity="error")

    @work(group="mr-action-mutation")
    async def _do_unapprove(self, intent: _MutationIntent) -> None:
        try:
            receipt = await self.app.services.unapprove(
                self.mr_summary, operation_id=intent.operation_id
            )
            if receipt.outcome is MRActionOutcome.UNKNOWN:
                self._retain_unknown(intent)
                self._notify_unknown("Unapprove")
                self._load_detail()
                return
            self._finish_mutation(intent)
            self.notify(
                f"[yellow]Approval revoked on !{self.mr_summary.number}[/]",
                severity="information",
            )
            self._action_taken = True
            self._load_detail()
        except asyncio.CancelledError:
            self._cancel_mutation(intent)
            raise
        except Exception as exc:  # noqa: BLE001 - Report background/action failures without terminating the TUI.
            self._finish_mutation(intent)
            self.notify(f"Unapprove failed: {exc}", severity="error")

    @work(group="mr-action-mutation")
    async def _do_merge(self, intent: _MutationIntent) -> None:
        try:
            receipt = await self.app.services.merge(
                self.mr_summary, operation_id=intent.operation_id
            )
            if receipt.outcome is MRActionOutcome.UNKNOWN:
                self._retain_unknown(intent)
                self._notify_unknown("Merge")
                self._load_detail()
                return
            self._finish_mutation(intent)
            self.notify(
                f"[green]Merged !{self.mr_summary.number}[/]",
                severity="information",
            )
            self._action_taken = True
            self._load_detail()
        except asyncio.CancelledError:
            self._cancel_mutation(intent)
            raise
        except Exception as exc:  # noqa: BLE001 - Report background/action failures without terminating the TUI.
            self._finish_mutation(intent)
            self.notify(f"Merge failed: {exc}", severity="error")

    @work(group="mr-action-mutation")
    async def _do_close(self, intent: _MutationIntent) -> None:
        try:
            receipt = await self.app.services.close_review(
                self.mr_summary, operation_id=intent.operation_id
            )
            if receipt.outcome is MRActionOutcome.UNKNOWN:
                self._retain_unknown(intent)
                self._notify_unknown("Close")
                self._load_detail()
                return
            self._finish_mutation(intent)
            self.notify(
                f"[yellow]Closed !{self.mr_summary.number}[/]",
                severity="information",
            )
            self._action_taken = True
            self._load_detail()
        except asyncio.CancelledError:
            self._cancel_mutation(intent)
            raise
        except Exception as exc:  # noqa: BLE001 - Report background/action failures without terminating the TUI.
            self._finish_mutation(intent)
            self.notify(f"Close failed: {exc}", severity="error")

    def _review_mutation_succeeded(self, outcome: MutationOutcome, action: str) -> bool:
        if outcome.status is MutationStatus.KNOWN:
            return True
        self._notify_unknown(action)
        return False

    def _notify_unknown(self, action: str) -> None:
        self.notify(
            f"{action} outcome is unknown. The command will not be retried in this view; "
            "refresh remote state before taking another action.",
            severity="warning",
        )

    def _begin_mutation(
        self, operation: str, action: str, *identity: object
    ) -> _MutationIntent | None:
        """Reserve one operation identity before a Textual worker can run."""
        fingerprint = (operation, *identity)
        existing = self._mutation_intents.get(fingerprint)
        if existing is not None:
            if existing.unknown:
                self._notify_unknown(action)
            else:
                self.notify(f"{action} is already in progress.", severity="warning")
            return None
        intent = _MutationIntent(
            self.app.services.new_operation_id(operation), fingerprint, action
        )
        self._mutation_intents[fingerprint] = intent
        return intent

    def _finish_mutation(self, intent: _MutationIntent) -> None:
        """Release an intent after a known result or safe pre-dispatch error."""
        if self._mutation_intents.get(intent.fingerprint) is intent:
            self._mutation_intents.pop(intent.fingerprint)

    def _retain_unknown(self, intent: _MutationIntent) -> None:
        """Keep an uncertain command identity so it cannot be replayed."""
        intent.unknown = True

    def _cancel_mutation(self, intent: _MutationIntent) -> None:
        """Treat worker cancellation conservatively after service admission."""
        self._retain_unknown(intent)
        self._notify_unknown(intent.action)

    def on_comment_requested(self, event: CommentRequested) -> None:
        """Handle comment request from DiffPanel."""
        if event.mode == CommentMode.SUGGEST and event.file and event.line:
            self._open_suggestion(event)
            return
        editor = self.query_one("#comment-editor", CommentEditor)
        if event.file and event.line:
            if self._displayed_diff_revision is None:
                self.notify(
                    "The displayed diff revision is unavailable. Refresh before commenting.",
                    severity="warning",
                )
                return
            if self._review_draft is not None:
                capture = self._capture_draft_selection(event)
                if capture is None:
                    return
                self._begin_draft_editor(self._review_draft, anchor=capture)
                self._pending_draft_start_line = None
                self._pending_draft_start_side = None
                editor.open_inline(
                    event.file,
                    event.line,
                    side=capture.selection.side,
                    defer_close=True,
                )
                return
            self._clear_pending_draft_editor()
            self._pending_inline_revision = self._displayed_diff_revision
            editor.open_inline(event.file, event.line, side=event.side)
        else:
            self._pending_inline_revision = None
            self.action_add_comment()

    def _capture_draft_selection(
        self, event: CommentRequested
    ) -> CapturedDraftAnchor | None:
        draft = self._review_draft
        revision = self._displayed_diff_revision
        if (
            draft is None
            or revision is None
            or event.file is None
            or event.line is None
        ):
            return None
        if draft.state is not DraftState.EDITABLE:
            self.notify(
                "This draft is not editable while submission is active.",
                severity="warning",
            )
            return None
        if draft.revision != revision:
            self.notify(
                "This draft belongs to an old revision. Create a current-revision draft before adding an anchor.",
                severity="warning",
            )
            return None
        selection = event.selection
        if selection is None:
            side = event.side or (
                DiffSide.OLD if event.line.new_lineno is None else DiffSide.NEW
            )
            try:
                selection = DiffSelection(event.file, side, event.line, (event.line,))
            except ValueError as exc:
                self.notify(str(exc), severity="warning")
                return None
        try:
            target = TUIDraftTarget(draft.review, draft.revision)
            return capture_draft_anchor(target, selection)
        except ValueError as exc:
            self.notify(str(exc), severity="warning")
            return None

    def _open_suggestion(self, event: CommentRequested) -> None:
        """Open external editor for suggesting changes."""
        import os
        import shlex
        import shutil
        import subprocess
        import tempfile

        if event.side is DiffSide.OLD:
            self.notify("Suggestions are available on the new side only")
            return

        revision = self._displayed_diff_revision
        if revision is None:
            self.notify(
                "The displayed diff revision is unavailable. Refresh before suggesting.",
                severity="warning",
            )
            return

        draft_capture: CapturedDraftAnchor | None = None
        if self._review_draft is not None:
            draft_capture = self._capture_draft_selection(event)
            if draft_capture is None:
                return

        editor_cmd = None
        for var in ("VISUAL", "EDITOR"):
            v = os.environ.get(var)
            if v:
                editor_cmd = v
                break
        if not editor_cmd:
            for cmd in ("nvim", "vim", "vi", "nano"):
                if shutil.which(cmd):
                    editor_cmd = cmd
                    break
        if not editor_cmd:
            self.notify("No external editor found. Set $EDITOR.")
            return

        lines = event.context_lines or ([event.line] if event.line else [])
        new_side_lines = extract_new_side_lines(lines)
        if not new_side_lines:
            self.notify("Cannot suggest: no new-side lines in selection")
            return
        original_code = "\n".join(dl.content for dl in new_side_lines)
        file_path = event.file.new_path if event.file else "unknown"

        template = build_suggestion_template(original_code)
        n_original = len(new_side_lines)

        ext = os.path.splitext(file_path)[1] or ".txt"
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix="tongs-suggest-")
            os.chmod(tmp_path, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(template)

            with self.app.suspend():
                subprocess.run([*shlex.split(editor_cmd), tmp_path], check=False)

            with open(tmp_path) as f:
                edited = f.read()

            comment_text, suggested_code = parse_suggestion_template(edited)

            if not suggested_code or suggested_code == original_code.strip():
                self.notify("No changes made, suggestion cancelled.")
                return

            forge_type = self.mr_summary.forge_host.forge_type
            body = format_suggestion_block(
                suggested_code, n_original, forge_type, comment_text
            )

            from tongs.diff.position import position_from_diff_line

            pos_line, start_line, start_side = resolve_suggestion_position(
                new_side_lines, forge_type
            )
            position = position_from_diff_line(event.file, pos_line)
            if draft_capture is not None:
                start_side_value = DiffSide.NEW if start_side is not None else None
                suggestion_selection = DiffSelection(
                    event.file,
                    DiffSide.NEW,
                    pos_line,
                    tuple(new_side_lines),
                )
                draft_capture = capture_draft_anchor(
                    draft_capture.target,
                    suggestion_selection,
                    start_line=start_line,
                    start_side=start_side_value,
                )
                assert self._review_draft is not None
                self._begin_draft_editor(self._review_draft, anchor=draft_capture)
                self._pending_draft_start_line = start_line
                self._pending_draft_start_side = start_side_value
                self.query_one("#comment-editor", CommentEditor).open_inline(
                    event.file,
                    pos_line,
                    side=DiffSide.NEW,
                    initial_body=body,
                    defer_close=True,
                )
                return
            self._start_inline_comment(
                body,
                position,
                revision,
                start_line=start_line,
                start_side=start_side,
            )
        except Exception as exc:  # noqa: BLE001 - Report background/action failures without terminating the TUI.
            self.notify(f"Suggestion failed: {exc}", severity="error")
        finally:
            if tmp_path:
                with suppress(OSError):
                    os.unlink(tmp_path)

    def on_comment_submitted(self, event: CommentSubmitted) -> None:
        """Handle inline comment submission from CommentEditor."""
        if self._pending_draft_anchor is not None:
            captured = self._pending_draft_anchor
            draft = self._draft_for_pending_editor()
            if draft is None:
                return
            if (
                draft.review != captured.target.review
                or draft.revision != captured.target.revision
            ):
                self.query_one("#comment-editor", CommentEditor).reject_submission(
                    "The captured review target changed. Your text is preserved."
                )
                return
            comment = new_inline_comment(event.body, captured.anchor)
            self._save_review_content(
                draft,
                replace(draft.content, comments=(*draft.comments, comment)),
                acknowledge_editor=True,
            )
            return
        revision = self._pending_inline_revision
        self._pending_inline_revision = None
        if revision is None:
            self.notify(
                "The displayed diff revision is unavailable. Refresh before commenting.",
                severity="warning",
            )
            return
        self._start_inline_comment(event.body, event.position, revision)

    def on_general_comment_submitted(self, event: GeneralCommentSubmitted) -> None:
        """Handle general MR comment submission."""
        if self._pending_draft_id is not None:
            draft = self._draft_for_pending_editor()
            if draft is None:
                return
            if draft.state is not DraftState.EDITABLE:
                self.query_one("#comment-editor", CommentEditor).reject_submission(
                    "This review draft is not editable while submission is active."
                )
                return
            comment = new_general_comment(event.body)
            self._save_review_content(
                draft,
                replace(draft.content, comments=(*draft.comments, comment)),
                acknowledge_editor=True,
            )
            return
        intent = self._begin_mutation("comment", "Comment", event.body)
        if intent is not None:
            self._post_general_comment(event.body, intent)

    def on_reply_requested(self, event: ReplyRequested) -> None:
        """Open reply editor for an existing discussion thread."""
        editor = self.query_one("#comment-editor", CommentEditor)
        defer = self._review_draft is not None
        if defer and not self._can_add_current_draft_target("reply"):
            return
        if defer:
            assert self._review_draft is not None
            self._begin_draft_editor(self._review_draft, reply=event.discussion_id)
        else:
            self._clear_pending_draft_editor()
        editor.open_reply(
            event.discussion_id,
            event.file,
            event.line,
            event.author,
            defer_close=defer,
        )

    def on_reply_submitted(self, event: ReplySubmitted) -> None:
        """Post a reply to an existing discussion thread."""
        if self._pending_draft_id is not None:
            draft = self._draft_for_pending_editor()
            if draft is None:
                return
            thread_id = self._pending_draft_reply
            if thread_id != event.discussion_id or not self._reply_is_current(
                thread_id
            ):
                self.query_one("#comment-editor", CommentEditor).reject_submission(
                    "The reply target is no longer in the current discussions. Your text is preserved."
                )
                return
            comment = new_reply_comment(event.body, thread_id)
            self._save_review_content(
                draft,
                replace(draft.content, comments=(*draft.comments, comment)),
                acknowledge_editor=True,
            )
            return
        intent = self._begin_mutation("reply", "Reply", event.discussion_id, event.body)
        if intent is not None:
            self._post_reply(event.discussion_id, event.body, intent)

    def on_draft_comment_edited(self, event: DraftCommentEdited) -> None:
        draft = self._draft_for_pending_editor()
        if draft is None:
            return
        if draft.state is not DraftState.EDITABLE:
            self.query_one("#comment-editor", CommentEditor).reject_submission(
                "This review draft is not editable. Your text is preserved."
            )
            return
        found = False
        comments: list[DraftComment] = []
        for comment in draft.comments:
            if comment.id == event.comment_id:
                comment = replace(comment, body=event.body)
                found = True
            comments.append(comment)
        if not found:
            self.query_one("#comment-editor", CommentEditor).reject_submission(
                "That draft comment changed elsewhere. Your text is preserved."
            )
            return
        self._save_review_content(
            draft,
            replace(draft.content, comments=tuple(comments)),
            acknowledge_editor=True,
        )

    def _begin_draft_editor(
        self,
        draft: DraftSnapshot,
        *,
        anchor: CapturedDraftAnchor | None = None,
        reply: str | None = None,
    ) -> None:
        """Bind one open composer to the exact active durable draft."""
        self._pending_draft_id = draft.id
        self._pending_draft_anchor = anchor
        self._pending_draft_reply = reply
        self._pending_draft_start_line = None
        self._pending_draft_start_side = None

    def _clear_pending_draft_editor(self) -> None:
        self._pending_draft_id = None
        self._pending_draft_anchor = None
        self._pending_draft_reply = None
        self._pending_draft_start_line = None
        self._pending_draft_start_side = None

    def _draft_for_pending_editor(self) -> DraftSnapshot | None:
        draft = self._review_draft
        if draft is None or draft.id != self._pending_draft_id:
            self.query_one("#comment-editor", CommentEditor).reject_submission(
                "The active review draft changed. Your text is preserved."
            )
            return None
        return draft

    def _can_add_current_draft_target(self, kind: str) -> bool:
        draft = self._review_draft
        if draft is None:
            return True
        if draft.state is not DraftState.EDITABLE:
            self.notify(
                f"This draft cannot accept a {kind} while submission is active.",
                severity="warning",
            )
            return False
        if draft.revision != self._current_review_revision:
            self.notify(
                f"This old-revision draft cannot accept a current {kind}. Create a new current-revision draft.",
                severity="warning",
            )
            return False
        return True

    def on_resolve_requested(self, event: ResolveRequested) -> None:
        """Resolve or unresolve a discussion thread."""
        action = "Resolve" if event.resolved else "Reopen"
        intent = self._begin_mutation(
            "resolve", action, event.discussion_id, event.resolved
        )
        if intent is not None:
            self._resolve_thread(event.discussion_id, event.resolved, intent)

    @work(group="mr-comment-mutation")
    async def _post_reply(
        self, discussion_id: str, body: str, intent: _MutationIntent
    ) -> None:
        try:
            outcome = await self.app.services.post_reply(
                self.mr_summary,
                discussion_id,
                body,
                operation_id=intent.operation_id,
            )
            if not self._review_mutation_succeeded(outcome, "Reply"):
                self._retain_unknown(intent)
                self._diff_loaded = False
                self._discussions_loaded = False
                return
            self._finish_mutation(intent)
            self.notify("[green]Reply posted[/]", severity="information")
            self._diff_loaded = False
            self._discussions_loaded = False
        except asyncio.CancelledError:
            self._cancel_mutation(intent)
            raise
        except Exception as exc:  # noqa: BLE001 - Report background/action failures without terminating the TUI.
            self._finish_mutation(intent)
            self.notify(f"Failed to post reply: {exc}", severity="error")

    @work(group="mr-comment-mutation")
    async def _resolve_thread(
        self, discussion_id: str, resolved: bool, intent: _MutationIntent
    ) -> None:
        try:
            outcome = await self.app.services.resolve_discussion(
                self.mr_summary,
                discussion_id,
                resolved,
                operation_id=intent.operation_id,
            )
            action = "Resolve" if resolved else "Reopen"
            if not self._review_mutation_succeeded(outcome, action):
                self._retain_unknown(intent)
                self._diff_loaded = False
                self._discussions_loaded = False
                return
            self._finish_mutation(intent)
            action = "Resolved" if resolved else "Reopened"
            self.notify(f"[green]{action} thread[/]", severity="information")
            self._diff_loaded = False
            self._discussions_loaded = False
        except asyncio.CancelledError:
            self._cancel_mutation(intent)
            raise
        except Exception as exc:  # noqa: BLE001 - Report background/action failures without terminating the TUI.
            self._finish_mutation(intent)
            self.notify(f"Failed to resolve thread: {exc}", severity="error")

    @work(group="mr-comment-mutation")
    async def _post_general_comment(self, body: str, intent: _MutationIntent) -> None:
        try:
            outcome = await self.app.services.post_general_comment(
                self.mr_summary, body, operation_id=intent.operation_id
            )
            if not self._review_mutation_succeeded(outcome, "Comment"):
                self._retain_unknown(intent)
                return
            self._finish_mutation(intent)
            self.notify("[green]Comment posted[/]", severity="information")
        except asyncio.CancelledError:
            self._cancel_mutation(intent)
            raise
        except Exception as exc:  # noqa: BLE001 - Report background/action failures without terminating the TUI.
            self._finish_mutation(intent)
            self.notify(f"Failed to post comment: {exc}", severity="error")

    def _start_inline_comment(
        self,
        body: str,
        position: DiffPosition,
        revision: ReviewRevision,
        *,
        start_line: int | None = None,
        start_side: str | None = None,
    ) -> None:
        intent = self._begin_mutation(
            "inline",
            "Comment",
            body,
            position,
            revision,
            start_line,
            start_side,
        )
        if intent is not None:
            self._post_inline_comment(
                body,
                position,
                revision,
                intent,
                start_line=start_line,
                start_side=start_side,
            )

    @work(group="mr-comment-mutation")
    async def _post_inline_comment(
        self,
        body: str,
        position: DiffPosition,
        revision: ReviewRevision,
        intent: _MutationIntent,
        start_line: int | None = None,
        start_side: str | None = None,
    ) -> None:
        try:
            outcome = await self.app.services.post_inline_comment(
                self.mr_summary,
                body,
                position,
                revision=revision,
                start_line=start_line,
                start_side=start_side,
                operation_id=intent.operation_id,
            )
            if not self._review_mutation_succeeded(outcome, "Comment"):
                self._retain_unknown(intent)
                self._diff_loaded = False
                self._discussions_loaded = False
                return
            self._finish_mutation(intent)
            self.notify("[green]Comment posted[/]", severity="information")
        except asyncio.CancelledError:
            self._cancel_mutation(intent)
            raise
        except Exception as exc:  # noqa: BLE001 - Report background/action failures without terminating the TUI.
            self._finish_mutation(intent)
            self.notify(f"Failed to post comment: {exc}", severity="error")

    def action_refresh(self) -> None:
        self._diff_loaded = False
        self._commits_loaded = False
        self._discussions_loaded = False
        self._pipeline_loaded = False
        self._cached_diff_files = None
        self._displayed_diff_revision = None
        self._load_detail()
