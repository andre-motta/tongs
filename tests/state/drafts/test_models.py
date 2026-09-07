"""Tests for immutable draft and anchor models."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from tongs.services import ReviewRevision
from tongs.state.drafts import (
    DiffSide,
    DraftContent,
    GeneralDraftComment,
    InlineAnchor,
    InlineDraftComment,
    ReplyDraftComment,
    context_fingerprint,
)


def make_anchor(revision: ReviewRevision | None = None) -> InlineAnchor:
    return InlineAnchor(
        revision or ReviewRevision("head", "base", "start"),
        "old.py",
        "new.py",
        4,
        5,
        DiffSide.NEW,
        context_fingerprint(("before", "selected", "after")),
        start_line=3,
        start_side=DiffSide.OLD,
    )


def test_comment_payloads_are_frozen_and_discriminated() -> None:
    general = GeneralDraftComment(uuid4(), "general")
    inline = InlineDraftComment(uuid4(), "inline", make_anchor())
    reply = ReplyDraftComment(uuid4(), "reply", "thread-1")

    assert (general.kind, inline.kind, reply.kind) == ("general", "inline", "reply")
    with pytest.raises(FrozenInstanceError):
        general.body = "changed"  # type: ignore[misc]


def test_context_fingerprint_is_stable_and_preserves_line_boundaries() -> None:
    assert context_fingerprint(("ab", "c")) == context_fingerprint(("ab", "c"))
    assert context_fingerprint(("ab", "c")) != context_fingerprint(("a", "bc"))


@pytest.mark.parametrize(
    "current",
    [
        ReviewRevision("other", "base", "start"),
        ReviewRevision("head", "other", "start"),
        ReviewRevision("head", "base", "other"),
        ReviewRevision("head", "base", None),
    ],
)
def test_anchor_staleness_compares_the_complete_revision(
    current: ReviewRevision,
) -> None:
    anchor = make_anchor()

    assert anchor.assessed_against(current).stale is True
    assert anchor.assessed_against(anchor.revision).stale is False


def test_stale_anchor_never_becomes_fresh_again() -> None:
    anchor = make_anchor().assessed_against(ReviewRevision("other", "base", "start"))

    assert anchor.assessed_against(anchor.revision).stale is True


def test_content_rejects_duplicate_stable_comment_ids() -> None:
    identifier = uuid4()
    with pytest.raises(ValueError, match="unique"):
        DraftContent(
            comments=(
                GeneralDraftComment(identifier, "one"),
                GeneralDraftComment(identifier, "two"),
            )
        )


def test_anchor_requires_a_real_line_on_selected_side() -> None:
    with pytest.raises(ValueError, match="selected side"):
        InlineAnchor(
            ReviewRevision("head", "base"),
            "old.py",
            "new.py",
            1,
            None,
            DiffSide.NEW,
            "fingerprint",
        )
