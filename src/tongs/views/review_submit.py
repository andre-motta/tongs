"""Review draft inspection, submission, and recovery screen."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar
from uuid import UUID

from rich.markup import escape
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, OptionList, Static, TextArea
from textual.widgets._option_list import Option

from tongs.services.models import ReviewRevision
from tongs.services.review_submission import SubmissionOutcome, SubmissionProgress
from tongs.state.drafts import (
    DraftComment,
    DraftContent,
    DraftSnapshot,
    DraftState,
    DraftVerdict,
    InlineDraftComment,
    ReconciliationResolution,
    ReplyDraftComment,
)


class ReviewSubmitActionKind(str, Enum):
    SUBMIT = "submit"
    EDIT = "edit"
    REMOVE = "remove"
    DISCARD = "discard"
    NEW_REVISION = "new_revision"
    RESUME = "resume"
    RECONCILE = "reconcile"
    DISMISS_RECOVERY = "dismiss_recovery"


@dataclass(frozen=True, slots=True)
class ReviewSubmitAction:
    """One exact user decision returned to the owning review controller."""

    kind: ReviewSubmitActionKind
    draft_id: UUID
    expected_version: int
    body: str = ""
    verdict: DraftVerdict | None = None
    comment_id: UUID | None = None
    attempt_id: UUID | None = None
    resolution: ReconciliationResolution | None = None


class ReviewSubmitScreen(ModalScreen[ReviewSubmitAction | None]):
    """Inspect an exact draft version and choose its next durable transition."""

    DEFAULT_CSS = """
    ReviewSubmitScreen {
        align: center middle;
    }
    ReviewSubmitScreen > Vertical {
        width: 88%;
        max-width: 120;
        height: 88%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    ReviewSubmitScreen #review-submit-title,
    ReviewSubmitScreen #review-submit-status,
    ReviewSubmitScreen #review-submit-verdict,
    ReviewSubmitScreen #review-submit-help {
        height: auto;
    }
    ReviewSubmitScreen #review-submit-comments {
        height: 1fr;
        min-height: 6;
    }
    ReviewSubmitScreen #review-submit-body {
        height: 6;
    }
    """

    BINDINGS: ClassVar[list] = [
        Binding("escape", "close", "Close", show=True),
        Binding("v", "cycle_verdict", "Verdict", show=True),
        Binding("ctrl+s", "submit", "Submit", show=True, priority=True),
        Binding("e", "edit_comment", "Edit", show=True),
        Binding("x", "remove_comment", "Remove", show=True),
        Binding("D", "discard", "Discard", show=True, key_display="D"),
        Binding("N", "new_revision", "New revision", show=False, key_display="N"),
        Binding("r", "resume", "Resume", show=False),
        Binding("1", "reconcile_retry", "Retry unknown", show=False),
        Binding("2", "reconcile_edit", "Return editable", show=False),
        Binding("3", "reconcile_submitted", "Mark submitted", show=False),
    ]

    def __init__(
        self,
        draft: DraftSnapshot,
        current_revision: ReviewRevision | None,
        *,
        progress: SubmissionProgress | None = None,
        recovered_content: DraftContent | None = None,
        supports_submission: bool = True,
        submission_unavailable_reason: str = "Batch review submission is unavailable.",
        supports_request_changes: bool = True,
    ) -> None:
        super().__init__()
        self.draft = draft
        self.current_revision = current_revision
        self.progress = progress
        self.recovered_content = recovered_content
        self.supports_submission = supports_submission
        self.submission_unavailable_reason = submission_unavailable_reason
        self.supports_request_changes = supports_request_changes
        self._verdict = (
            recovered_content.verdict
            if recovered_content is not None
            else draft.verdict
        ) or DraftVerdict.COMMENT
        self._comment_ids: list[UUID] = []
        self._pending_confirmation: str | None = None

    @property
    def stale(self) -> bool:
        return (
            self.current_revision is None
            or self.current_revision != self.draft.revision
        )

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("[bold]Review draft[/]", id="review-submit-title")
            yield Static("", id="review-submit-status")
            yield OptionList(id="review-submit-comments")
            yield Static("", id="review-submit-verdict")
            yield TextArea(
                self.recovered_content.body
                if self.recovered_content is not None
                else self.draft.body,
                id="review-submit-body",
            )
            yield Static("", id="review-submit-help")
            yield Footer()

    def on_mount(self) -> None:
        self.call_after_refresh(self._populate)

    def _populate(self) -> None:
        comments = self.query_one("#review-submit-comments", OptionList)
        options: list[Option] = []
        self._comment_ids.clear()
        for comment in self.draft.comments:
            self._comment_ids.append(comment.id)
            options.append(Option(self._comment_text(comment)))
        if options:
            comments.add_options(options)
        else:
            comments.add_option(
                Option(Text("No draft comments", style="dim"), disabled=True)
            )
        if self._comment_ids:
            comments.highlighted = 0
            comments.focus()
        self._render_state()

    def _comment_text(self, comment: DraftComment) -> Text:
        text = Text()
        if isinstance(comment, InlineDraftComment):
            anchor = comment.anchor
            path = anchor.old_path if anchor.side.value == "old" else anchor.new_path
            line = anchor.old_line if anchor.side.value == "old" else anchor.new_line
            state = " stale" if anchor.stale else ""
            text.append(
                f"DRAFT inline {anchor.side.value} {path}:{line}{state}\n",
                "bold yellow",
            )
        elif isinstance(comment, ReplyDraftComment):
            text.append(f"DRAFT reply to {comment.thread_id}\n", "bold yellow")
        else:
            text.append("DRAFT general\n", "bold yellow")
        text.append(comment.body)
        return text

    def _render_state(self) -> None:
        status = self.query_one("#review-submit-status", Static)
        if self.progress is None:
            state = self.draft.state.value
            if self.stale:
                detail = "old revision; submission and new anchors are blocked"
            elif not self.supports_submission:
                detail = "batch submission unavailable"
            elif self.recovered_content is not None:
                detail = "submission conflict; requested fields recovered"
            else:
                detail = "ready"
            status.update(
                f"Version {self.draft.version} | {escape(state)} | {escape(detail)}"
            )
        else:
            known = len(self.progress.completed_step_ids)
            total = len(self.progress.steps)
            unknown = len(self.progress.unknown_step_ids)
            status.update(
                f"Attempt {self.progress.attempt_id} | {escape(self.progress.outcome.value)} | "
                f"confirmed {known}/{total} | unknown {unknown}"
            )
        self.query_one("#review-submit-verdict", Static).update(
            f"Verdict: [bold]{escape(self._verdict.value)}[/]  (v cycles)"
        )
        if (
            self.progress is not None
            and self.progress.outcome is SubmissionOutcome.UNKNOWN
        ):
            help_text = (
                "Unknown outcome: 1 retry remaining (may duplicate), "
                "2 return editable, 3 mark submitted after inspection. Press twice."
            )
        elif (
            self.progress is not None
            and self.progress.outcome is SubmissionOutcome.PAUSED
        ):
            help_text = (
                "Submission paused. Press r to resume confirmed remaining steps."
            )
        elif self.stale:
            help_text = "Press N for a separate current-revision draft; old inline text stays here."
        elif not self.supports_submission:
            help_text = self.submission_unavailable_reason
        elif self.recovered_content is not None:
            help_text = (
                "Requested summary and verdict recovered. Review current comments, "
                "then Ctrl+S to retry."
            )
        else:
            help_text = "Ctrl+S submit | e edit | x remove | D discard (destructive actions require two presses)"
        self.query_one("#review-submit-help", Static).update(help_text)

    def action_close(self) -> None:
        body = self.query_one("#review-submit-body", TextArea).text
        if (
            body != self.draft.body
            or self._verdict != (self.draft.verdict or DraftVerdict.COMMENT)
        ) and not self._confirm(
            "close", "Press Esc again to discard unsaved summary or verdict changes."
        ):
            return
        self.dismiss(
            self._action(ReviewSubmitActionKind.DISMISS_RECOVERY)
            if self.recovered_content is not None
            else None
        )

    def action_cycle_verdict(self) -> None:
        verdicts = [DraftVerdict.COMMENT, DraftVerdict.APPROVE]
        if self.supports_request_changes:
            verdicts.append(DraftVerdict.REQUEST_CHANGES)
        self._verdict = verdicts[(verdicts.index(self._verdict) + 1) % len(verdicts)]
        self._pending_confirmation = None
        self._render_state()

    def action_submit(self) -> None:
        if self.progress is not None or self.stale:
            self.notify("This draft cannot start a new submission.", severity="warning")
            return
        if not self.supports_submission:
            self.notify(self.submission_unavailable_reason, severity="warning")
            return
        body = self.query_one("#review-submit-body", TextArea).text
        if self._verdict is DraftVerdict.REQUEST_CHANGES and not body.strip():
            self.notify(
                "Request changes requires a review summary.", severity="warning"
            )
            return
        self.dismiss(
            self._action(
                ReviewSubmitActionKind.SUBMIT, body=body, verdict=self._verdict
            )
        )

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        return action != "submit" or self.supports_submission

    def action_edit_comment(self) -> None:
        if self.draft.state is not DraftState.EDITABLE:
            self.notify("This draft is not editable while submission is active.")
            return
        comment_id = self._selected_comment_id()
        if comment_id is not None:
            self.dismiss(
                self._action(
                    ReviewSubmitActionKind.EDIT,
                    body=self.query_one("#review-submit-body", TextArea).text,
                    verdict=self._verdict,
                    comment_id=comment_id,
                )
            )

    def action_remove_comment(self) -> None:
        if self.draft.state is not DraftState.EDITABLE:
            self.notify("This draft is not editable while submission is active.")
            return
        comment_id = self._selected_comment_id()
        if comment_id is None:
            return
        key = f"remove:{comment_id}"
        if not self._confirm(key, "Press x again to remove this local draft comment."):
            return
        self.dismiss(
            self._action(
                ReviewSubmitActionKind.REMOVE,
                body=self.query_one("#review-submit-body", TextArea).text,
                verdict=self._verdict,
                comment_id=comment_id,
            )
        )

    def action_discard(self) -> None:
        if self.draft.state is not DraftState.EDITABLE:
            self.notify("Reconcile the active submission before discarding.")
            return
        if not self._confirm(
            "discard", "Press D again to discard this local review draft."
        ):
            return
        self.dismiss(self._action(ReviewSubmitActionKind.DISCARD))

    def action_new_revision(self) -> None:
        if self.stale and self.progress is None:
            self.dismiss(self._action(ReviewSubmitActionKind.NEW_REVISION))

    def action_resume(self) -> None:
        if (
            self.progress is not None
            and self.progress.outcome is SubmissionOutcome.PAUSED
        ):
            self.dismiss(
                self._action(
                    ReviewSubmitActionKind.RESUME,
                    attempt_id=self.progress.attempt_id,
                )
            )

    def action_reconcile_retry(self) -> None:
        self._reconcile(ReconciliationResolution.RETRY_REMAINING)

    def action_reconcile_edit(self) -> None:
        self._reconcile(ReconciliationResolution.RETURN_EDITABLE)

    def action_reconcile_submitted(self) -> None:
        self._reconcile(ReconciliationResolution.MARK_SUBMITTED)

    def _reconcile(self, resolution: ReconciliationResolution) -> None:
        if (
            self.progress is None
            or self.progress.outcome is not SubmissionOutcome.UNKNOWN
        ):
            return
        warning = {
            ReconciliationResolution.RETRY_REMAINING: "Retry may duplicate an unconfirmed remote write.",
            ReconciliationResolution.RETURN_EDITABLE: "Return remaining content to local editing.",
            ReconciliationResolution.MARK_SUBMITTED: "Mark submitted only after inspecting the forge.",
        }[resolution]
        if not self._confirm(
            f"reconcile:{resolution.value}", f"{warning} Press again to confirm."
        ):
            return
        self.dismiss(
            self._action(
                ReviewSubmitActionKind.RECONCILE,
                attempt_id=self.progress.attempt_id,
                resolution=resolution,
            )
        )

    def _action(
        self, kind: ReviewSubmitActionKind, **kwargs: object
    ) -> ReviewSubmitAction:
        return ReviewSubmitAction(
            kind,
            self.draft.id,
            self.draft.version,
            **kwargs,  # type: ignore[arg-type]
        )

    def _selected_comment_id(self) -> UUID | None:
        highlighted = self.query_one("#review-submit-comments", OptionList).highlighted
        if highlighted is None or highlighted >= len(self._comment_ids):
            return None
        return self._comment_ids[highlighted]

    def _confirm(self, key: str, message: str) -> bool:
        if self._pending_confirmation != key:
            self._pending_confirmation = key
            self.notify(message, severity="warning")
            return False
        self._pending_confirmation = None
        return True


__all__ = ["ReviewSubmitAction", "ReviewSubmitActionKind", "ReviewSubmitScreen"]
