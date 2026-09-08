"""Review-draft presentation and source-backed anchor capture."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from rich.markup import escape
from textual.widgets import Static

from tongs.diff.models import DiffFile, DiffLine
from tongs.services.models import ReviewRevision
from tongs.services.review_submission import SubmissionProgress
from tongs.state.drafts import (
    DiffSide,
    DraftSnapshot,
    InlineAnchor,
    InlineDraftComment,
    context_fingerprint,
)
from tongs.tui_services import TUIDraftTarget
from tongs.widgets.split_diff import DiffSelection, is_actionable


@dataclass(frozen=True, slots=True)
class CapturedDraftAnchor:
    """One immutable review, revision, selection, and fingerprint bundle."""

    target: TUIDraftTarget
    selection: DiffSelection
    anchor: InlineAnchor


@dataclass(frozen=True, slots=True)
class DraftMarker:
    """Small immutable projection used by unified and split renderers."""

    comment_id: UUID
    body: str
    old_path: str
    new_path: str
    old_line: int | None
    new_line: int | None
    side: DiffSide
    stale: bool

    @classmethod
    def from_comment(cls, comment: InlineDraftComment) -> DraftMarker:
        anchor = comment.anchor
        return cls(
            comment.id,
            comment.body,
            anchor.old_path,
            anchor.new_path,
            anchor.old_line,
            anchor.new_line,
            anchor.side,
            anchor.stale,
        )

    def matches(self, file: DiffFile, line: DiffLine, side: DiffSide) -> bool:
        if (
            self.side is not side
            or self.old_path != file.old_path
            or self.new_path != file.new_path
        ):
            return False
        coordinate = line.old_lineno if side is DiffSide.OLD else line.new_lineno
        expected = self.old_line if side is DiffSide.OLD else self.new_line
        return coordinate is not None and coordinate == expected


def capture_draft_anchor(
    target: TUIDraftTarget,
    selection: DiffSelection,
    *,
    start_line: int | None = None,
    start_side: DiffSide | None = None,
) -> CapturedDraftAnchor:
    """Freeze an original-coordinate selection using its complete ±2 source window."""
    context = _source_context(selection)
    line = selection.line
    anchor = InlineAnchor(
        revision=target.revision,
        old_path=selection.file.old_path,
        new_path=selection.file.new_path,
        old_line=line.old_lineno,
        new_line=line.new_lineno,
        side=selection.side,
        context_fingerprint=context_fingerprint(context),
        start_line=start_line,
        start_side=start_side,
    )
    return CapturedDraftAnchor(target, selection, anchor)


def _source_context(selection: DiffSelection) -> tuple[str, ...]:
    file = selection.file
    if file.is_truncated or file.is_unavailable:
        raise ValueError(
            "The selected diff context is partial. Refresh before drafting inline feedback."
        )
    for hunk in file.hunks:
        candidates = tuple(
            line for line in hunk.lines if is_actionable(line, selection.side)
        )
        try:
            index = next(
                index for index, line in enumerate(candidates) if line is selection.line
            )
        except StopIteration:
            continue
        return tuple(line.content for line in candidates[max(0, index - 2) : index + 3])
    raise ValueError("The selected line is no longer present in its source hunk.")


def inline_markers(draft: DraftSnapshot | None) -> tuple[DraftMarker, ...]:
    """Project only durable inline comments into non-actionable renderer markers."""
    if draft is None:
        return ()
    return tuple(
        DraftMarker.from_comment(comment)
        for comment in draft.comments
        if isinstance(comment, InlineDraftComment)
    )


class ReviewDraftBar(Static):
    """Persistent review-mode status, count, staleness, and progress indicator."""

    DEFAULT_CSS = """
    ReviewDraftBar {
        height: 1;
        padding: 0 1;
        background: $warning 18%;
        color: $foreground;
        display: none;
    }
    """

    def show_draft(
        self,
        draft: DraftSnapshot | None,
        current_revision: ReviewRevision | None,
        *,
        progress: SubmissionProgress | None = None,
        busy: bool = False,
        conflict: bool = False,
    ) -> None:
        if draft is None:
            self.display = False
            self.update("")
            return
        self.display = True
        stale = current_revision is None or draft.revision != current_revision
        count = len(draft.comments)
        state = progress.outcome.value if progress is not None else draft.state.value
        flags = []
        if stale:
            flags.append("old revision")
        if conflict:
            flags.append("version conflict")
        if busy:
            flags.append("saving")
        suffix = f" | [yellow]{escape(', '.join(flags))}[/]" if flags else ""
        self.update(
            f"[bold yellow]REVIEW DRAFT[/] | {count} comment"
            f"{'s' if count != 1 else ''} | v{draft.version} | {escape(state)}"
            f"{suffix} | [bold]Ctrl+G[/] review"
        )


__all__ = [
    "CapturedDraftAnchor",
    "DraftMarker",
    "ReviewDraftBar",
    "capture_draft_anchor",
    "inline_markers",
]
