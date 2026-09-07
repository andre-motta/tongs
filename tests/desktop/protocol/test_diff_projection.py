"""Protocol-level tests for immutable unified and split diff projections."""

from __future__ import annotations

from typing import cast

import pytest

from tongs.desktop.protocol.diff_projection import DiffLayout, flatten_diff
from tongs.desktop.protocol.messages import JsonObject, ProtocolError, ProtocolErrorCode
from tongs.desktop.protocol.server import DesktopSidecarServer, RequestContext
from tongs.desktop.protocol.state import HandleKind, SnapshotStore
from tongs.diff.models import DiffFile, DiffHunk, DiffLine, FileStatus, LineType
from tongs.plugins.desktop import DesktopCancellation
from tongs.services import RawDiffSnapshot, RepositoryRef, ReviewRef, ReviewRevision


def _line(
    line_type: LineType,
    content: str,
    old_line: int | None,
    new_line: int | None,
) -> DiffLine:
    return DiffLine(old_line, new_line, content, line_type)


def _file() -> DiffFile:
    marker = _line(LineType.NO_NEWLINE, r"\ No newline at end of file", None, None)
    hunk = DiffHunk(
        "@@ -4,3 +4,3 @@",
        4,
        3,
        4,
        3,
        (
            _line(LineType.CONTEXT, "same", 4, 4),
            _line(LineType.DELETION, "old one", 5, None),
            _line(LineType.DELETION, "old two", 6, None),
            _line(LineType.ADDITION, "new one", None, 5),
            marker,
        ),
        "section",
    )
    return DiffFile("old.py", "new.py", FileStatus.RENAMED, (hunk,), 1, 2)


def test_unified_projection_remains_the_existing_flat_wire_shape() -> None:
    entries = flatten_diff((_file(),), DiffLayout.UNIFIED)

    assert [entry["kind"] for entry in entries] == [
        "file",
        "hunk",
        "line",
        "line",
        "line",
        "line",
        "line",
    ]
    assert entries[2] == {
        "kind": "line",
        "file_index": 0,
        "hunk_index": 0,
        "old_line": 4,
        "new_line": 4,
        "content": "same",
        "line_type": "context",
    }


def test_split_projection_preserves_cells_coordinates_and_anchorability() -> None:
    entries = flatten_diff((_file(),), DiffLayout.SPLIT)
    rows = entries[2:]

    assert [row["row_index"] for row in rows] == [0, 1, 2, 3]
    assert rows[0]["old"] == {
        "old_line": 4,
        "new_line": 4,
        "content": "same",
        "line_type": "context",
        "anchor_side": "old",
    }
    assert rows[0]["new"] == {
        "old_line": 4,
        "new_line": 4,
        "content": "same",
        "line_type": "context",
        "anchor_side": "new",
    }
    assert cast(dict[str, object], rows[1]["old"])["content"] == "old one"
    assert cast(dict[str, object], rows[1]["new"])["content"] == "new one"
    assert rows[3]["new"] is None
    assert cast(dict[str, object], rows[3]["old"])["content"] == "old two"
    assert rows[2]["old"] is None
    marker = rows[2]["new"]
    assert cast(dict[str, object], marker)["anchor_side"] is None


def test_snapshot_retains_projection_without_changing_revision_or_entries() -> None:
    store = SnapshotStore()
    snapshot = store.create(
        "review",
        {"head_sha": "h", "base_sha": "b", "start_sha": None},
        [{"kind": "split", "row_index": 0}],
        projection="split",
    )

    page = store.page(snapshot, "review")

    assert page.projection == "split"
    assert page.revision == {"head_sha": "h", "base_sha": "b", "start_sha": None}
    assert page.entries == ({"kind": "split", "row_index": 0},)


class _DiffSession:
    def __init__(self, raw: RawDiffSnapshot) -> None:
        self.raw = raw

    async def get_raw_diff(self, review: ReviewRef) -> RawDiffSnapshot:
        assert review == self.raw.review
        return self.raw


@pytest.mark.asyncio
async def test_server_defaults_unified_and_pages_one_selected_split_projection() -> (
    None
):
    repository = RepositoryRef("git.example.com", "team/project")
    review = ReviewRef(repository, 7)
    revision = ReviewRevision("head", "base", "start")
    raw = RawDiffSnapshot(
        review,
        revision,
        (
            {
                "old_path": "code.py",
                "new_path": "code.py",
                "diff": "@@ -1,2 +1,2 @@\n same\n-old\n+new",
            },
        ),
    )
    server = DesktopSidecarServer(session=cast(object, _DiffSession(raw)))
    handle = server._handles.issue(HandleKind.REVIEW, review)
    context = RequestContext("diff", DesktopCancellation())

    unified = cast(
        JsonObject,
        await server._diff_open({"review": handle}, context),
    )
    split = cast(
        JsonObject,
        await server._diff_open(
            {"review": handle, "layout": "split", "max_items": 2}, context
        ),
    )

    assert [entry["kind"] for entry in unified["entries"]] == [
        "file",
        "hunk",
        "line",
        "line",
        "line",
    ]
    assert split["next_cursor"] == 2
    continued = cast(
        JsonObject,
        await server._diff_page(
            {
                "snapshot": split["snapshot_id"],
                "resource": handle,
                "cursor": split["next_cursor"],
            },
            RequestContext("page", DesktopCancellation()),
        ),
    )
    assert all(entry["kind"] == "split" for entry in continued["entries"])
    assert continued["revision"] == {
        "head_sha": "head",
        "base_sha": "base",
        "start_sha": "start",
    }
    assert "projection" not in continued


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params", [{"review": "x", "layout": "wide"}, {"review": "x", "layout": True}]
)
async def test_server_rejects_invalid_layout(params: JsonObject) -> None:
    server = DesktopSidecarServer(session=cast(object, object()))

    with pytest.raises(ProtocolError) as caught:
        await server._diff_open(params, RequestContext("diff", DesktopCancellation()))

    assert caught.value.code is ProtocolErrorCode.INVALID_PARAMS


@pytest.mark.asyncio
async def test_page_rejects_attempt_to_change_snapshot_layout() -> None:
    server = DesktopSidecarServer(session=cast(object, object()))

    with pytest.raises(ProtocolError) as caught:
        await server._diff_page(
            {"snapshot": "s", "resource": "r", "cursor": 0, "layout": "unified"},
            RequestContext("page", DesktopCancellation()),
        )

    assert caught.value.code is ProtocolErrorCode.INVALID_PARAMS
