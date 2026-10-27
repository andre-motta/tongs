"""Tests for immutable draft and anchor models."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from uuid import uuid4

import pytest

from tongs.services import ReviewRevision
from tongs.state.drafts import (
    MAX_DRAFT_BODY_BYTES,
    MAX_DRAFT_COMMENTS,
    DiffSide,
    DraftContent,
    DraftLimitError,
    DraftStoreError,
    GeneralDraftComment,
    InlineAnchor,
    InlineDraftComment,
    ReplyDraftComment,
    context_fingerprint,
    draft_text_problem,
    validate_draft_content,
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


def test_stale_anchor_is_derived_from_revisions_alone() -> None:
    changed = ReviewRevision("other", "base", "start")
    stale = make_anchor().assessed_against(changed)
    assert stale.stale is True

    # A stale assessment never sticks: an anchor whose captured revision
    # matches the current one is fresh, and one that does not stays stale.
    assert stale.assessed_against(stale.revision).stale is False
    assert stale.assessed_against(changed).stale is True
    persisted = replace(make_anchor(), stale=True)
    assert persisted.assessed_against(persisted.revision).stale is False
    assert persisted.assessed_against(changed).stale is True


def test_content_staleness_is_derived_for_every_inline_anchor() -> None:
    changed = ReviewRevision("other", "base", "start")
    stored = DraftContent(
        comments=(
            GeneralDraftComment(uuid4(), "general"),
            InlineDraftComment(uuid4(), "inline", replace(make_anchor(), stale=True)),
        )
    )

    fresh = stored.assessed_against(make_anchor().revision)
    refused = stored.assessed_against(changed)

    assert [
        comment.anchor.stale
        for comment in fresh.comments
        if isinstance(comment, InlineDraftComment)
    ] == [False]
    assert [
        comment.anchor.stale
        for comment in refused.comments
        if isinstance(comment, InlineDraftComment)
    ] == [True]


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


def test_anchor_rejects_non_integer_line_numbers() -> None:
    with pytest.raises(ValueError, match="positive integers"):
        InlineAnchor(
            ReviewRevision("head", "base"),
            "old.py",
            "new.py",
            1,
            12.0,  # type: ignore[arg-type]
            DiffSide.NEW,
            "fingerprint",
        )


def test_draft_limits_match_the_documented_values() -> None:
    assert MAX_DRAFT_BODY_BYTES == 65_536
    assert MAX_DRAFT_COMMENTS == 200


@pytest.mark.parametrize(
    "text",
    [
        "",
        "plain",
        "tab\tand\nnewline",
        "x" * MAX_DRAFT_BODY_BYTES,
        "\u00e9" * (MAX_DRAFT_BODY_BYTES // 2),
        "\u00a0 non-breaking and \u2028 separator",
    ],
)
def test_text_within_the_limits_has_no_problem(text: str) -> None:
    assert draft_text_problem(text) is None


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("x" * (MAX_DRAFT_BODY_BYTES + 1), "is over the 65,536-byte limit"),
        ("\u00e9" * (MAX_DRAFT_BODY_BYTES // 2) + "x", "is over the 65,536-byte limit"),
        ("carriage\rreturn", "contains a control character"),
        ("nul\x00", "contains a control character"),
        ("escape\x1b[0m", "contains a control character"),
        ("lone \ud800 surrogate", "contains text that cannot be stored"),
        (b"bytes", "is not text"),
    ],
)
def test_text_breaking_a_limit_names_the_problem(text: object, problem: str) -> None:
    found = draft_text_problem(text)
    assert found is not None
    assert found.startswith(problem)


def test_content_over_the_limits_still_constructs_but_fails_validation() -> None:
    # Stored drafts written before the limits existed must keep loading.
    oversized = DraftContent(
        body="x" * 70_000,
        comments=tuple(
            GeneralDraftComment(uuid4(), "note") for _ in range(MAX_DRAFT_COMMENTS + 5)
        ),
    )
    assert len(oversized.comments) == MAX_DRAFT_COMMENTS + 5
    with pytest.raises(DraftLimitError) as raised:
        validate_draft_content(oversized)
    assert isinstance(raised.value, DraftStoreError)
    assert str(raised.value) == "A draft can hold at most 200 comments."


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (
            DraftContent(body="x" * 70_000),
            "The review summary is over the 65,536-byte limit.",
        ),
        (
            DraftContent(comments=(ReplyDraftComment(uuid4(), "y" * 70_000, "t-1"),)),
            "A comment is over the 65,536-byte limit.",
        ),
        (
            DraftContent(
                comments=(InlineDraftComment(uuid4(), "a\x07", make_anchor()),)
            ),
            "A comment contains a control character; only tab and newline are allowed.",
        ),
    ],
)
def test_validation_message_is_plain_and_never_quotes_the_text(
    content: DraftContent, message: str
) -> None:
    with pytest.raises(DraftLimitError) as raised:
        validate_draft_content(content)
    assert str(raised.value) == message


def test_content_at_the_limits_passes_validation() -> None:
    validate_draft_content(
        DraftContent(
            body="x" * MAX_DRAFT_BODY_BYTES,
            comments=tuple(
                GeneralDraftComment(uuid4(), "z" * MAX_DRAFT_BODY_BYTES)
                for _ in range(MAX_DRAFT_COMMENTS)
            ),
        )
    )


def test_importing_drafts_does_not_load_the_services_package() -> None:
    # The draft store must depend only on leaf modules, so a fresh
    # interpreter can import it without pulling in ``tongs.services``.
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import sys\n"
                "import tongs.state.drafts\n"
                'assert "tongs.services" not in sys.modules, sorted(\n'
                '    name for name in sys.modules if name.startswith("tongs")\n'
                ")\n"
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert completed.returncode == 0
