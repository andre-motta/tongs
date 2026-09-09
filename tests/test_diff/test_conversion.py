"""Tests for forge change conversion."""

from __future__ import annotations

from tongs.diff.conversion import convert_forge_changes
from tongs.diff.models import FileStatus, LineType


def test_converts_github_patch_and_preserves_metadata() -> None:
    files = convert_forge_changes(
        (
            {
                "filename": "src/space name.py",
                "status": "modified",
                "additions": 3,
                "deletions": 2,
                "patch": (
                    "@@ -4,2 +4,3 @@ def run():\n"
                    " context\n"
                    "-old\n"
                    "-older\n"
                    "+new\n"
                    "+newer\n"
                    "+newest\n"
                    r"\ No newline at end of file"
                ),
            },
        )
    )

    file = files[0]
    assert file.old_path == "src/space name.py"
    assert file.new_path == "src/space name.py"
    assert file.status is FileStatus.MODIFIED
    assert file.additions == 3
    assert file.deletions == 2
    assert file.language == "python"
    assert file.is_binary is False
    assert file.is_truncated is False
    assert [h.old_start for h in file.hunks] == [4]
    assert file.hunks[0].context_text == "def run():"
    assert file.hunks[0].lines[-1].line_type is LineType.NO_NEWLINE


def test_converts_gitlab_rename_and_preserves_original_paths() -> None:
    files = convert_forge_changes(
        (
            {
                "old_path": "old name.md",
                "new_path": "new name.md",
                "renamed_file": True,
                "diff": ("@@ -1 +1 @@\n-old\n+new\n"),
            },
        )
    )

    file = files[0]
    assert file.old_path == "old name.md"
    assert file.new_path == "new name.md"
    assert file.status is FileStatus.RENAMED
    assert file.hunks[0].lines[0].old_lineno == 1
    assert file.hunks[0].lines[1].new_lineno == 1


def test_metadata_only_states_are_explicit_and_binary_is_never_guessed() -> None:
    files = convert_forge_changes(
        (
            {"filename": "empty.txt", "status": "modified", "patch": ""},
            {"filename": "unknown.txt"},
            {
                "filename": "large.py",
                "status": "modified",
                "additions": 5,
                "deletions": 1,
                "too_large": True,
            },
            {"filename": "image.png", "status": "modified", "is_binary": True},
            {
                "filename": "other.png",
                "status": "modified",
                "patch": "Binary files a/other.png and b/other.png differ",
            },
            {"filename": "script.sh", "a_mode": "100644", "b_mode": "100755"},
        )
    )

    empty, unavailable, truncated, binary, patch_binary, mode_only = files
    assert empty.is_empty is True
    assert empty.is_binary is False
    assert unavailable.is_empty is False
    assert unavailable.is_unavailable is True
    assert truncated.is_truncated is True
    assert truncated.is_binary is False
    assert binary.is_binary is True
    assert binary.is_empty is False
    assert patch_binary.is_binary is True
    assert mode_only.is_mode_only is True
    assert mode_only.is_empty is False


def test_mode_metadata_is_not_mode_only_when_patch_has_content() -> None:
    file = convert_forge_changes(
        (
            {
                "filename": "script.sh",
                "a_mode": "100644",
                "b_mode": "100755",
                "patch": "@@ -1 +1 @@\n-old\n+new\n",
            },
        )
    )[0]

    assert file.hunks
    assert file.is_mode_only is False


def test_newline_in_metadata_path_cannot_break_the_patch() -> None:
    file = convert_forge_changes(
        (
            {
                "filename": "odd\nname.py",
                "patch": "@@ -1 +1 @@\n-old\n+new\n",
            },
        )
    )[0]

    assert file.new_path == "odd\nname.py"
    assert file.hunks
    assert [line.content for line in file.hunks[0].lines] == ["old", "new"]


def test_gitlab_collapsed_change_overrides_false_too_large_flag() -> None:
    file = convert_forge_changes(
        (
            {
                "old_path": "src/large.py",
                "new_path": "src/large.py",
                "collapsed": True,
                "too_large": False,
                "diff": "",
            },
        )
    )[0]

    assert file.is_truncated is True
    assert file.is_empty is False
    assert file.is_unavailable is False


def test_incomplete_hunk_is_truncated_with_authoritative_aggregate_counts() -> None:
    file = convert_forge_changes(
        (
            {
                "filename": "src/partial.py",
                "additions": 5,
                "deletions": 1,
                "patch": "@@ -1,1 +1,5 @@\n-old\n+new\n",
            },
        )
    )[0]

    assert file.is_truncated is True
    assert file.additions == 5
    assert file.deletions == 1
    assert len(file.hunks[0].lines) == 2


def test_omitted_complete_hunks_are_truncated_by_aggregate_count_shortfall() -> None:
    file = convert_forge_changes(
        (
            {
                "filename": "src/partial.py",
                "additions": 5,
                "deletions": 4,
                "patch": "@@ -1 +1 @@\n-old\n+new\n",
            },
        )
    )[0]

    assert file.is_truncated is True
    assert file.additions == 5
    assert file.deletions == 4
    assert len(file.hunks) == 1
    assert [line.line_type for line in file.hunks[0].lines] == [
        LineType.DELETION,
        LineType.ADDITION,
    ]


def test_binary_aggregate_counts_do_not_imply_text_patch_truncation() -> None:
    file = convert_forge_changes(
        (
            {
                "filename": "image.png",
                "is_binary": True,
                "additions": 8,
                "deletions": 4,
            },
        )
    )[0]

    assert file.is_binary is True
    assert file.is_truncated is False
    assert file.additions == 8
    assert file.deletions == 4


def test_malformed_optional_values_do_not_drop_file() -> None:
    files = convert_forge_changes(
        (
            {
                "filename": "unknown.txt",
                "status": object(),
                "additions": "not a count",
                "deletions": None,
                "patch": object(),
            },
        )
    )

    assert len(files) == 1
    assert files[0].new_path == "unknown.txt"
    assert files[0].additions == 0
    assert files[0].deletions == 0
    assert files[0].is_binary is False


def _github_file(**fields: object) -> dict[str, object]:
    """One GitHub files-endpoint object with no patch, as the endpoint sends it.

    GitHub reports ``additions``, ``deletions`` and ``changes`` for every file
    and omits ``patch`` entirely for a binary, empty, rename-only or mode-only
    change, without saying which of those it is.
    """

    return {"additions": 0, "deletions": 0, "changes": 0, **fields}


def test_github_withheld_patch_derives_every_shape_its_fields_determine() -> None:
    binary, empty, deleted_empty, rename_only, mode_or_binary = convert_forge_changes(
        (
            _github_file(filename="assets/icon.bin", status="modified"),
            _github_file(filename="src/empty_placeholder.py", status="added"),
            _github_file(filename="src/gone.py", status="removed"),
            _github_file(
                filename="src/renamed_module.py",
                status="renamed",
                previous_filename="src/legacy_name.py",
            ),
            _github_file(filename="src/tool.sh", status="modified"),
        )
    )

    assert (binary.is_binary, binary.is_unavailable) == (True, False)
    assert (empty.is_empty, empty.is_unavailable) == (True, False)
    assert (deleted_empty.is_empty, deleted_empty.is_unavailable) == (True, False)
    assert rename_only.is_rename_only is True
    assert (rename_only.is_empty, rename_only.is_unavailable) == (False, False)
    assert rename_only.old_path == "src/legacy_name.py"
    # A modified text-suffixed file with no patch is either binary content or a
    # mode change, and the files endpoint exposes neither.  Saying so is the
    # honest answer; claiming one of them would not be.
    assert mode_or_binary.is_unavailable is True
    assert (mode_or_binary.is_binary, mode_or_binary.is_mode_only) == (False, False)
    assert all(file.is_truncated is False for file in (binary, empty, rename_only))


def test_reported_line_counts_still_mean_a_withheld_patch_is_truncated() -> None:
    """A withheld patch with real counts is truncation, not a derived shape."""

    binary_suffix, text = convert_forge_changes(
        (
            {
                "filename": "assets/icon.bin",
                "status": "modified",
                "additions": 0,
                "deletions": 0,
                "changes": 4,
            },
            {
                "filename": "src/huge.py",
                "status": "modified",
                "additions": 900,
                "deletions": 3,
                "changes": 903,
            },
        )
    )

    assert (binary_suffix.is_binary, binary_suffix.is_truncated) == (False, False)
    assert binary_suffix.is_unavailable is True
    assert (text.is_truncated, text.is_binary) == (True, False)


def test_absent_counts_are_never_read_as_a_reported_zero() -> None:
    """No counts at all is no evidence, so the shape stays unavailable."""

    files = convert_forge_changes(
        (
            {"filename": "assets/icon.bin", "status": "modified"},
            {"filename": "src/empty_placeholder.py", "status": "added"},
            {
                "filename": "src/renamed_module.py",
                "status": "renamed",
                "previous_filename": "src/legacy_name.py",
            },
        )
    )

    assert [file.is_unavailable for file in files] == [True, True, True]
    assert [file.is_binary for file in files] == [False, False, False]
    assert [file.is_rename_only for file in files] == [False, False, False]


def test_gitlab_and_github_agree_on_the_rename_only_shape() -> None:
    """The one shape both forges determine must read the same on each."""

    github = convert_forge_changes(
        (
            _github_file(
                filename="src/renamed_module.py",
                status="renamed",
                previous_filename="src/legacy_name.py",
            ),
        )
    )[0]
    gitlab = convert_forge_changes(
        (
            {
                "old_path": "src/legacy_name.py",
                "new_path": "src/renamed_module.py",
                "renamed_file": True,
                "diff": "",
            },
        )
    )[0]

    for file in (github, gitlab):
        assert file.status is FileStatus.RENAMED
        assert file.is_rename_only is True
        assert file.is_empty is False
        assert file.is_unavailable is False
        assert file.is_metadata_only is True


def test_a_rename_carrying_content_is_not_rename_only() -> None:
    file = convert_forge_changes(
        (
            {
                "filename": "src/renamed_module.py",
                "previous_filename": "src/legacy_name.py",
                "status": "renamed",
                "additions": 1,
                "deletions": 1,
                "changes": 2,
                "patch": "@@ -1 +1 @@\n-old\n+new\n",
            },
        )
    )[0]

    assert file.is_rename_only is False
    assert file.hunks
