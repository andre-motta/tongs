"""Tests for the unified diff parser."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from tongs.diff.models import DiffFile, FileStatus, LineType
from tongs.diff.parser import parse_diff

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# Real fixture tests
# ---------------------------------------------------------------------------


def _structure(files: list[DiffFile]) -> list[tuple]:
    """Every parsed file and hunk field that a fixture golden test pins."""
    return [
        (
            f.old_path,
            f.new_path,
            f.status,
            f.additions,
            f.deletions,
            f.language,
            f.is_binary,
            [
                (h.old_start, h.old_count, h.new_start, h.new_count, h.context_text)
                for h in f.hunks
            ],
        )
        for f in files
    ]


def test_parses_builder_mr_3113_plain_format() -> None:
    """builder_mr_3113.diff uses the plain format, with no diff --git prefix."""
    files = parse_diff((FIXTURES / "builder_mr_3113.diff").read_text())

    first = "package_plugins/hooks/upload_after_build_wheel.py"
    second = "test/test_upload_after_build_wheel.py"
    assert _structure(files) == [
        (
            first,
            first,
            FileStatus.MODIFIED,
            36,
            19,
            "python",
            False,
            [
                (3, 6, 3, 7, "import hashlib"),
                (311, 25, 312, 41, "def upload_python_package_to_gitlab("),
            ],
        ),
        (
            second,
            second,
            FileStatus.MODIFIED,
            72,
            0,
            "python",
            False,
            [(198, 6, 198, 78, "def test_upload_package_skip_existing(")],
        ),
    ]


def test_parses_fromager_pr_1258_git_format() -> None:
    """fromager_pr_1258.diff uses the diff --git format."""
    files = parse_diff((FIXTURES / "fromager_pr_1258.diff").read_text())

    first = "src/fromager/bootstrapper/_bootstrapper.py"
    second = "tests/test_bootstrapper.py"
    assert _structure(files) == [
        (
            first,
            first,
            FileStatus.MODIFIED,
            17,
            4,
            "python",
            False,
            [
                (935, 11, 935, 12, "def add_to_build_order("),
                (
                    985,
                    7,
                    986,
                    12,
                    "def _schedule_write(self, path: pathlib.Path, content: str) -> None:",
                ),
                (1269, 6, 1275, 10, "def finalize(self) -> int:"),
                (1334, 5, 1344, 8, "def __exit__("),
            ],
        ),
        (
            second,
            second,
            FileStatus.MODIFIED,
            26,
            0,
            "python",
            False,
            [
                (
                    799,
                    6,
                    799,
                    12,
                    "def test_record_stack_state_throttled_when_called_rapidly(",
                ),
                (
                    825,
                    6,
                    831,
                    26,
                    (
                        "def test_finalize_writes_build_order_and_graph("
                        "tmp_context: WorkContext) -> None"
                    ),
                ),
            ],
        ),
    ]


# ---------------------------------------------------------------------------
# Synthetic diff tests
# ---------------------------------------------------------------------------


class TestSyntheticDiffs:
    """Parse hand-crafted diff strings covering each structural variant."""

    @pytest.mark.parametrize(
        ("hunk", "additions", "deletions"),
        [
            pytest.param(
                "@@ -1,3 +1,4 @@\n line1\n line2\n+new_line\n line3\n",
                1,
                0,
                id="addition",
            ),
            pytest.param(
                "@@ -1,4 +1,3 @@\n line1\n-removed_line\n line2\n line3\n",
                0,
                1,
                id="deletion",
            ),
            pytest.param(
                "@@ -1,4 +1,4 @@\n line1\n-old_line\n+new_line\n line2\n line3\n",
                1,
                1,
                id="mixed",
            ),
        ],
    )
    def test_counts_additions_and_deletions(
        self, hunk: str, additions: int, deletions: int
    ) -> None:
        files = parse_diff(f"--- a/hello.py\n+++ b/hello.py\n{hunk}")
        assert len(files) == 1
        assert files[0].additions == additions
        assert files[0].deletions == deletions

    def test_multiple_hunks_in_one_file(self) -> None:
        diff = (
            "--- a/hello.py\n"
            "+++ b/hello.py\n"
            "@@ -1,3 +1,4 @@\n"
            " line1\n"
            "+inserted\n"
            " line2\n"
            " line3\n"
            "@@ -10,3 +11,4 @@\n"
            " line10\n"
            "+also_inserted\n"
            " line11\n"
            " line12\n"
        )
        files = parse_diff(diff)
        assert len(files) == 1
        assert len(files[0].hunks) == 2
        assert files[0].additions == 2
        assert files[0].deletions == 0

    def test_multiple_files_in_one_diff(self) -> None:
        """Plain format uses bare paths (no a/ b/ prefix) for file boundaries."""
        diff = (
            "--- alpha.py\n"
            "+++ alpha.py\n"
            "@@ -1,2 +1,3 @@\n"
            " a1\n"
            "+a2\n"
            " a3\n"
            "--- beta.py\n"
            "+++ beta.py\n"
            "@@ -1,2 +1,3 @@\n"
            " b1\n"
            "+b2\n"
            " b3\n"
        )
        files = parse_diff(diff)
        assert len(files) == 2
        assert files[0].new_path == "alpha.py"
        assert files[1].new_path == "beta.py"

    def test_hunk_header_with_function_context(self) -> None:
        diff = (
            "--- a/f.py\n"
            "+++ b/f.py\n"
            "@@ -10,5 +10,7 @@ def foo():\n"
            " line10\n"
            "+added1\n"
            "+added2\n"
            " line11\n"
            " line12\n"
            " line13\n"
            " line14\n"
        )
        files = parse_diff(diff)
        hunk = files[0].hunks[0]
        assert hunk.context_text == "def foo():"
        assert hunk.old_start == 10
        assert hunk.new_start == 10

    def test_no_newline_at_end_of_file_marker(self) -> None:
        diff = (
            "--- a/f.txt\n"
            "+++ b/f.txt\n"
            "@@ -1,2 +1,2 @@\n"
            " keep\n"
            "-old_last\n"
            "+new_last\n"
            "\\ No newline at end of file\n"
        )
        files = parse_diff(diff)
        lines = files[0].hunks[0].lines
        no_nl = [ln for ln in lines if ln.line_type == LineType.NO_NEWLINE]
        assert len(no_nl) == 1
        assert "No newline at end of file" in no_nl[0].content

    def test_new_file_dev_null(self) -> None:
        diff = "--- /dev/null\n+++ b/new_file.py\n@@ -0,0 +1,2 @@\n+line1\n+line2\n"
        files = parse_diff(diff)
        assert len(files) == 1
        assert files[0].old_path == "/dev/null"
        assert files[0].new_path == "new_file.py"
        assert files[0].status == FileStatus.ADDED
        assert files[0].additions == 2

    def test_deleted_file_dev_null(self) -> None:
        diff = "--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-line1\n-line2\n"
        files = parse_diff(diff)
        assert len(files) == 1
        assert files[0].new_path == "/dev/null"
        assert files[0].status == FileStatus.DELETED
        assert files[0].deletions == 2

    def test_binary_file_marker(self) -> None:
        diff = (
            "diff --git a/image.png b/image.png\n"
            "new file mode 100644\n"
            "Binary files /dev/null and b/image.png differ\n"
        )
        files = parse_diff(diff)
        assert len(files) == 1
        assert files[0].is_binary
        assert files[0].status == FileStatus.ADDED

    @pytest.mark.parametrize("text", ["", "   \n\n  \t  \n"], ids=["empty", "blank"])
    def test_empty_or_blank_diff(self, text: str) -> None:
        assert parse_diff(text) == []

    def test_diff_git_format_with_rename(self) -> None:
        diff = (
            "diff --git a/old_name.py b/new_name.py\n"
            "similarity index 95%\n"
            "rename from old_name.py\n"
            "rename to new_name.py\n"
            "index abc1234..def5678 100644\n"
            "--- a/old_name.py\n"
            "+++ b/new_name.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-old_content\n"
            "+new_content\n"
            " shared\n"
        )
        files = parse_diff(diff)
        assert len(files) == 1
        assert files[0].status == FileStatus.RENAMED
        assert files[0].old_path == "old_name.py"
        assert files[0].new_path == "new_name.py"

    def test_deeply_nested_file_path(self) -> None:
        diff = (
            "diff --git a/a/b/c/d/e/f/g.py b/a/b/c/d/e/f/g.py\n"
            "--- a/a/b/c/d/e/f/g.py\n"
            "+++ b/a/b/c/d/e/f/g.py\n"
            "@@ -1,1 +1,2 @@\n"
            " deep\n"
            "+deeper\n"
        )
        files = parse_diff(diff)
        assert files[0].new_path == "a/b/c/d/e/f/g.py"


# ---------------------------------------------------------------------------
# Language detection tests
# ---------------------------------------------------------------------------


class TestLanguageDetection:
    """Verify language detection from file extensions."""

    # .py covers the Pygments lookup, .cpp the extension-only fallback path.
    _CASES: ClassVar[list] = [(".py", "python"), (".cpp", "cpp")]

    @pytest.mark.parametrize("ext,expected_lang", _CASES)
    def test_extension_maps_to_language(self, ext: str, expected_lang: str) -> None:
        diff = f"--- a/file{ext}\n+++ b/file{ext}\n@@ -1,1 +1,2 @@\n x\n+y\n"
        files = parse_diff(diff)
        assert files[0].language == expected_lang

    def test_unknown_extension_returns_empty(self) -> None:
        diff = "--- a/data.xyz\n+++ b/data.xyz\n@@ -1,1 +1,2 @@\n x\n+y\n"
        files = parse_diff(diff)
        assert files[0].language == ""

    def test_dockerfile_by_name(self) -> None:
        diff = (
            "--- a/Dockerfile\n"
            "+++ b/Dockerfile\n"
            "@@ -1,1 +1,2 @@\n"
            " FROM alpine\n"
            "+RUN echo hi\n"
        )
        files = parse_diff(diff)
        assert files[0].language == "docker"

    def test_deleted_file_uses_old_path_for_language(self) -> None:
        diff = "--- a/module.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-line1\n-line2\n"
        files = parse_diff(diff)
        assert files[0].language == "python"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Parser edge cases and boundary conditions."""

    def test_hunk_count_one_no_comma(self) -> None:
        """@@ -1 +1 @@ is valid when count is 1 (no comma)."""
        diff = "--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-old\n+new\n"
        files = parse_diff(diff)
        hunk = files[0].hunks[0]
        assert hunk.old_count == 1
        assert hunk.new_count == 1
        assert files[0].additions == 1
        assert files[0].deletions == 1

    def test_line_numbers_correct_across_multiple_hunks(self) -> None:
        """Line numbers must restart from each hunk's header, not carry over."""
        diff = (
            "--- a/f.py\n"
            "+++ b/f.py\n"
            "@@ -1,3 +1,4 @@\n"
            " line1\n"
            "+inserted_early\n"
            " line2\n"
            " line3\n"
            "@@ -20,3 +21,4 @@\n"
            " line20\n"
            "+inserted_late\n"
            " line21\n"
            " line22\n"
        )
        files = parse_diff(diff)
        h0, h1 = files[0].hunks

        # Hunk 0 starts at old=1, new=1
        assert h0.lines[0].old_lineno == 1
        assert h0.lines[0].new_lineno == 1
        # The addition bumps new but not old
        assert h0.lines[1].old_lineno is None
        assert h0.lines[1].new_lineno == 2

        # Hunk 1 starts fresh at old=20, new=21
        assert h1.lines[0].old_lineno == 20
        assert h1.lines[0].new_lineno == 21
        assert h1.lines[1].old_lineno is None
        assert h1.lines[1].new_lineno == 22

    def test_deletion_line_numbers(self) -> None:
        """Deletions have old_lineno set, new_lineno is None."""
        diff = (
            "--- a/f.py\n"
            "+++ b/f.py\n"
            "@@ -5,4 +5,3 @@\n"
            " keep\n"
            "-removed\n"
            " also_keep\n"
            " end\n"
        )
        files = parse_diff(diff)
        lines = files[0].hunks[0].lines

        assert lines[0].old_lineno == 5
        assert lines[0].new_lineno == 5

        assert lines[1].line_type == LineType.DELETION
        assert lines[1].old_lineno == 6
        assert lines[1].new_lineno is None

        # After deletion, new_lineno stays at 6 while old_lineno jumps to 7
        assert lines[2].old_lineno == 7
        assert lines[2].new_lineno == 6

    def test_git_diff_new_file_mode(self) -> None:
        """diff --git with 'new file mode' sets ADDED status."""
        diff = (
            "diff --git a/brand_new.py b/brand_new.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/brand_new.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+#!/usr/bin/env python3\n"
            "+print('hello')\n"
            "+# done\n"
        )
        files = parse_diff(diff)
        assert len(files) == 1
        assert files[0].status == FileStatus.ADDED
        assert files[0].additions == 3

    def test_git_diff_deleted_file_mode(self) -> None:
        """diff --git with 'deleted file mode' sets DELETED status."""
        diff = (
            "diff --git a/obsolete.py b/obsolete.py\n"
            "deleted file mode 100644\n"
            "--- a/obsolete.py\n"
            "+++ /dev/null\n"
            "@@ -1,2 +0,0 @@\n"
            "-line1\n"
            "-line2\n"
        )
        files = parse_diff(diff)
        assert len(files) == 1
        assert files[0].status == FileStatus.DELETED
        assert files[0].deletions == 2

    def test_binary_file_without_hunks(self) -> None:
        """Binary files have is_binary=True and no hunks."""
        diff = (
            "diff --git a/photo.jpg b/photo.jpg\n"
            "Binary files a/photo.jpg and b/photo.jpg differ\n"
        )
        files = parse_diff(diff)
        assert len(files) == 1
        assert files[0].is_binary
        assert files[0].hunks == ()
        assert files[0].additions == 0
        assert files[0].deletions == 0

    def test_no_newline_marker_has_no_line_numbers(self) -> None:
        diff = (
            "--- a/f.txt\n"
            "+++ b/f.txt\n"
            "@@ -1,1 +1,1 @@\n"
            "-old\n"
            "\\ No newline at end of file\n"
            "+new\n"
            "\\ No newline at end of file\n"
        )
        files = parse_diff(diff)
        no_nl_lines = [
            ln for ln in files[0].hunks[0].lines if ln.line_type == LineType.NO_NEWLINE
        ]
        for ln in no_nl_lines:
            assert ln.old_lineno is None
            assert ln.new_lineno is None

    def test_content_strips_diff_prefix(self) -> None:
        """DiffLine.content should not include the leading +/-/space."""
        diff = (
            "--- a/f.py\n"
            "+++ b/f.py\n"
            "@@ -1,2 +1,2 @@\n"
            " context_line\n"
            "-deleted_line\n"
            "+added_line\n"
        )
        files = parse_diff(diff)
        lines = files[0].hunks[0].lines
        assert lines[0].content == "context_line"
        assert lines[1].content == "deleted_line"
        assert lines[2].content == "added_line"

    def test_empty_context_line_preserves_line_numbers(self) -> None:
        """An empty context line (blank source line) must not cause line-number drift."""
        diff = (
            "--- a/f.py\n"
            "+++ b/f.py\n"
            "@@ -1,5 +1,6 @@\n"
            " line1\n"
            " \n"
            " line3\n"
            "+added\n"
            " line4\n"
            " line5\n"
        )
        files = parse_diff(diff)
        lines = files[0].hunks[0].lines

        # line1
        assert lines[0].old_lineno == 1
        assert lines[0].new_lineno == 1

        # empty context line
        assert lines[1].line_type == LineType.CONTEXT
        assert lines[1].old_lineno == 2
        assert lines[1].new_lineno == 2

        # line3
        assert lines[2].old_lineno == 3
        assert lines[2].new_lineno == 3

        # added
        assert lines[3].line_type == LineType.ADDITION
        assert lines[3].old_lineno is None
        assert lines[3].new_lineno == 4

        # line4 -- shifted by the addition
        assert lines[4].old_lineno == 4
        assert lines[4].new_lineno == 5

        # line5
        assert lines[5].old_lineno == 5
        assert lines[5].new_lineno == 6

    def test_incomplete_hunk_stops_before_following_file_headers(self) -> None:
        diff = (
            "--- a/first.py\n"
            "+++ b/first.py\n"
            "@@ -1,1 +1,5 @@\n"
            "-old\n"
            "+new\n"
            "--- a/second.py\n"
            "+++ b/second.py\n"
            "@@ -1 +1 @@\n"
            "-before\n"
            "+after\n"
        )

        files = parse_diff(diff)

        assert [file.new_path for file in files] == ["first.py", "second.py"]
        assert [line.content for line in files[0].hunks[0].lines] == ["old", "new"]
        assert [line.content for line in files[1].hunks[0].lines] == [
            "before",
            "after",
        ]

    def test_header_looking_source_pair_completes_hunk_before_next_hunk(self) -> None:
        diff = (
            "--- a/comments.txt\n"
            "+++ b/comments.txt\n"
            "@@ -1 +1 @@\n"
            "--- comment\n"
            "+++ comment\n"
            "@@ -10 +10 @@\n"
            "-old\n"
            "+new\n"
        )

        files = parse_diff(diff)

        assert len(files) == 1
        assert len(files[0].hunks) == 2
        assert [line.content for line in files[0].hunks[0].lines] == [
            "-- comment",
            "++ comment",
        ]
        assert [line.content for line in files[0].hunks[1].lines] == ["old", "new"]
