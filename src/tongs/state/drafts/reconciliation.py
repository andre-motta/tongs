"""Safe content projection for explicit draft submission reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from tongs.state.drafts.models import DraftContent, SubmissionAttempt


@dataclass(frozen=True, slots=True)
class ConfirmedSubmissionContent:
    """Frozen draft content represented by durable remote receipts."""

    comment_ids: frozenset[UUID] = frozenset()
    body: bool = False
    verdict: bool = False


def editable_remainder(
    attempt: SubmissionAttempt, confirmed: ConfirmedSubmissionContent
) -> DraftContent:
    """Remove only confirmed content while retaining all ambiguous remainder."""
    if not isinstance(attempt, SubmissionAttempt):
        raise TypeError("attempt must be a SubmissionAttempt")
    if not isinstance(confirmed, ConfirmedSubmissionContent):
        raise TypeError("confirmed must be ConfirmedSubmissionContent")
    known_ids = {comment.id for comment in attempt.snapshot.comments}
    if not confirmed.comment_ids <= known_ids:
        raise ValueError("confirmed comment IDs must belong to the frozen draft")
    return DraftContent(
        body="" if confirmed.body else attempt.snapshot.body,
        verdict=None if confirmed.verdict else attempt.snapshot.verdict,
        comments=tuple(
            comment
            for comment in attempt.snapshot.comments
            if comment.id not in confirmed.comment_ids
        ),
    )


__all__ = ["ConfirmedSubmissionContent", "editable_remainder"]
