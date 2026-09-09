"""Wire coverage for values a forge legitimately leaves absent.

The desktop client validates every read against an exact DTO, and one row that
breaks the contract makes it reject the whole response.  The core models spell
an absent optional text as the empty string, so the sidecar has to translate
that to null on the wire.  These tests pin both known instances: the language of
a file with no detected lexer, and the inline position of a review-level note.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

from tongs.desktop.protocol.messages import JsonObject
from tongs.desktop.protocol.server import (
    DesktopSidecarServer,
    RequestContext,
    _log_entries,
)
from tongs.desktop.protocol.state import HandleKind
from tongs.forges.models import Discussion, InlineComment, User
from tongs.plugins.desktop import DesktopCancellation
from tongs.services import RawDiffSnapshot, RepositoryRef, ReviewRef, ReviewRevision

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "diff-shapes.json"
# Text fields the desktop DTO requires to be present and non-empty.
_REQUIRED_FILE_TEXT = ("old_path", "new_path", "status")
_EXPECTED_SHAPES = (
    ("assets/icon.bin", "modified"),
    ("docs/guide.md", "modified"),
    ("src/calc.py", "modified"),
    ("src/empty_placeholder.py", "added"),
    ("src/no_newline.txt", "modified"),
    ("src/obsolete.py", "deleted"),
    ("src/renamed_module.py", "renamed"),
    ("src/tool.sh", "modified"),
)


def _fixture() -> JsonObject:
    return cast(JsonObject, json.loads(_FIXTURE.read_text()))


class _DiffSession:
    def __init__(self, raw: RawDiffSnapshot) -> None:
        self.raw = raw

    async def get_raw_diff(self, review: ReviewRef) -> RawDiffSnapshot:
        assert review == self.raw.review
        return self.raw


class _DiscussionSession:
    def __init__(self, discussions: tuple[Discussion, ...]) -> None:
        self.discussions = discussions

    async def get_discussions(self, _review: ReviewRef) -> tuple[Discussion, ...]:
        return self.discussions


def _server_for_diff(changes: object) -> tuple[DesktopSidecarServer, str]:
    review = ReviewRef(RepositoryRef("forge.example.com", "team/project"), 9)
    raw = RawDiffSnapshot(
        review,
        ReviewRevision("head", "base", None),
        tuple(cast(list[JsonObject], changes)),
    )
    server = DesktopSidecarServer(session=cast(object, _DiffSession(raw)))
    return server, server._handles.issue(HandleKind.REVIEW, review)


def _comment(identifier: str, file_path: str, line: int | None) -> InlineComment:
    return InlineComment(
        id=identifier,
        author=User("reviewer", ""),
        body="",
        created_at=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
        file_path=file_path,
        old_line=None,
        new_line=line,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("forge", ["github", "gitlab"])
async def test_every_diff_shape_projects_a_wire_conformant_file_row(
    forge: str,
) -> None:
    """All eight sandbox shapes must survive one read, on either forge.

    GitHub omits ``patch`` for the binary, added-empty, rename-only and
    mode-only files, and GitLab omits ``diff`` for three of them, so this drives
    the shapes the acceptance fixture exists to exercise.
    """
    server, handle = _server_for_diff(_fixture()[f"{forge}_changes"])

    page = cast(
        JsonObject,
        await server._diff_open(
            {"review": handle}, RequestContext("diff", DesktopCancellation())
        ),
    )

    files = [
        cast(JsonObject, entry)
        for entry in cast(list[object], page["entries"])
        if cast(JsonObject, entry)["kind"] == "file"
    ]
    assert [(file["new_path"], file["status"]) for file in files] == list(
        _EXPECTED_SHAPES
    )
    for file in files:
        for key in _REQUIRED_FILE_TEXT:
            assert file[key] != "", f"{key} must never be empty on the wire"
        language = file["language"]
        assert language is None or (isinstance(language, str) and language != "")


@pytest.mark.asyncio
@pytest.mark.parametrize("forge", ["github", "gitlab"])
async def test_file_with_no_detected_lexer_projects_a_null_language(
    forge: str,
) -> None:
    """``assets/icon.bin`` has no lexer, and null is its wire form.

    An empty string here made the desktop client reject the entire diff page,
    so a review whose files were all readable showed one generic read failure.
    """
    server, handle = _server_for_diff(_fixture()[f"{forge}_changes"])

    page = cast(
        JsonObject,
        await server._diff_open(
            {"review": handle}, RequestContext("diff", DesktopCancellation())
        ),
    )

    binary = cast(JsonObject, cast(list[object], page["entries"])[0])
    assert binary["new_path"] == "assets/icon.bin"
    assert binary["language"] is None


@pytest.mark.asyncio
async def test_review_level_note_projects_a_null_inline_path() -> None:
    """A note with no position keeps the thread readable, path absent."""
    review = ReviewRef(RepositoryRef("forge.example.com", "team/project"), 9)
    threads = (
        Discussion("53f505e47aca", False, _comment("1", "", None)),
        Discussion(
            "b689d5db45fc",
            True,
            InlineComment(
                id="2",
                author=User("reviewer", "Reviewer"),
                body="inline",
                created_at=datetime(2026, 9, 8, 12, 0, tzinfo=UTC),
                file_path="src/calc.py",
                new_line=26,
                replies=(_comment("3", "src/calc.py", 26),),
            ),
        ),
    )
    server = DesktopSidecarServer(session=cast(object, _DiscussionSession(threads)))
    handle = server._handles.issue(HandleKind.REVIEW, review)

    result = cast(
        JsonObject,
        await server._discussions_list(
            {"review": handle}, RequestContext("threads", DesktopCancellation())
        ),
    )

    discussions = [
        cast(JsonObject, item) for item in cast(list[object], result["discussions"])
    ]
    unpositioned = cast(JsonObject, discussions[0]["root_comment"])
    positioned = cast(JsonObject, discussions[1]["root_comment"])
    assert unpositioned["file_path"] is None
    assert unpositioned["old_line"] is None
    assert unpositioned["new_line"] is None
    assert positioned["file_path"] == "src/calc.py"
    reply = cast(JsonObject, cast(list[object], positioned["replies"])[0])
    assert reply["file_path"] == "src/calc.py"


def test_empty_job_log_projects_one_empty_row() -> None:
    """An empty log is one empty row, which the client must accept as content."""
    assert _log_entries(b"") == ({"text": ""},)
