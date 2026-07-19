"""Tests for the unified diff parser and diff data models."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from tongs.diff.models import DiffFile, DiffHunk, DiffLine, FileStatus, LineType
from tongs.diff.parser import parse_diff

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


# ---------------------------------------------------------------------------
# Real fixture tests
# ---------------------------------------------------------------------------


class TestParseBuilderMR3113:
    """Parse builder_mr_3113.diff (plain format, no diff --git prefix)."""

    @pytest.fixture()
    def files(self) -> list[DiffFile]:
        text = (FIXTURES / "builder_mr_3113.diff").read_text()
        return parse_diff(text)

    def test_file_count(self, files: list[DiffFile]) -> None:
        assert len(files) == 2

    def test_first_file_paths(self, files: list[DiffFile]) -> None:
        assert files[0].old_path == "package_plugins/hooks/upload_after_build_wheel.py"
        assert files[0].new_path == "package_plugins/hooks/upload_after_build_wheel.py"

    def test_second_file_paths(self, files: list[DiffFile]) -> None:
        assert files[1].old_path == "test/test_upload_after_build_wheel.py"
        assert files[1].new_path == "test/test_upload_after_build_wheel.py"

    def test_first_file_hunk_count(self, files: list[DiffFile]) -> None:
        assert len(files[0].hunks) == 2

    def test_second_file_hunk_count(self, files: list[DiffFile]) -> None:
        assert len(files[1].hunks) == 1

    def test_first_file_additions_deletions(self, files: list[DiffFile]) -> None:
        assert files[0].additions == 36
        assert files[0].deletions == 19

    def test_second_file_additions_only(self, files: list[DiffFile]) -> None:
        assert files[1].additions == 72
        assert files[1].deletions == 0

    def test_status_modified(self, files: list[DiffFile]) -> None:
        assert files[0].status == FileStatus.MODIFIED
        assert files[1].status == FileStatus.MODIFIED

    def test_language_detected_as_python(self, files: list[DiffFile]) -> None:
        assert files[0].language == "python"
        assert files[1].language == "python"

    def test_not_binary(self, files: list[DiffFile]) -> None:
        assert not files[0].is_binary
        assert not files[1].is_binary

    def test_first_hunk_header_values(self, files: list[DiffFile]) -> None:
        hunk = files[0].hunks[0]
        assert hunk.old_start == 3
        assert hunk.old_count == 6
        assert hunk.new_start == 3
        assert hunk.new_count == 7

    def test_first_hunk_context_text(self, files: list[DiffFile]) -> None:
        assert files[0].hunks[0].context_text == "import hashlib"

    def test_second_hunk_context_text(self, files: list[DiffFile]) -> None:
        assert "upload_python_package_to_gitlab" in files[0].hunks[1].context_text


class TestParseFromagerPR1258:
    """Parse fromager_pr_1258.diff (diff --git format)."""

    @pytest.fixture()
    def files(self) -> list[DiffFile]:
        text = (FIXTURES / "fromager_pr_1258.diff").read_text()
        return parse_diff(text)

    def test_file_count(self, files: list[DiffFile]) -> None:
        assert len(files) == 2

    def test_first_file_paths(self, files: list[DiffFile]) -> None:
        f = files[0]
        assert f.old_path == "src/fromager/bootstrapper/_bootstrapper.py"
        assert f.new_path == "src/fromager/bootstrapper/_bootstrapper.py"

    def test_second_file_paths(self, files: list[DiffFile]) -> None:
        f = files[1]
        assert f.old_path == "tests/test_bootstrapper.py"
        assert f.new_path == "tests/test_bootstrapper.py"

    def test_first_file_has_four_hunks(self, files: list[DiffFile]) -> None:
        assert len(files[0].hunks) == 4

    def test_second_file_has_two_hunks(self, files: list[DiffFile]) -> None:
        assert len(files[1].hunks) == 2

    def test_first_file_additions_deletions(self, files: list[DiffFile]) -> None:
        assert files[0].additions == 17
        assert files[0].deletions == 4

    def test_second_file_additions_only(self, files: list[DiffFile]) -> None:
        assert files[1].additions == 26
        assert files[1].deletions == 0

    def test_status_modified(self, files: list[DiffFile]) -> None:
        assert files[0].status == FileStatus.MODIFIED
        assert files[1].status == FileStatus.MODIFIED

    def test_language_detected_as_python(self, files: list[DiffFile]) -> None:
        assert files[0].language == "python"
        assert files[1].language == "python"

    def test_hunk_context_text_captures_function_name(
        self, files: list[DiffFile]
    ) -> None:
        assert "add_to_build_order" in files[0].hunks[0].context_text
        assert "finalize" in files[0].hunks[2].context_text


# ---------------------------------------------------------------------------
# Synthetic diff tests
# ---------------------------------------------------------------------------


class TestSyntheticDiffs:
    """Parse hand-crafted diff strings covering each structural variant."""

    def test_simple_addition_only(self) -> None:
        diff = (
            "--- a/hello.py\n"
            "+++ b/hello.py\n"
            "@@ -1,3 +1,4 @@\n"
            " line1\n"
            " line2\n"
            "+new_line\n"
            " line3\n"
        )
        files = parse_diff(diff)
        assert len(files) == 1
        assert files[0].additions == 1
        assert files[0].deletions == 0

    def test_simple_deletion_only(self) -> None:
        diff = (
            "--- a/hello.py\n"
            "+++ b/hello.py\n"
            "@@ -1,4 +1,3 @@\n"
            " line1\n"
            "-removed_line\n"
            " line2\n"
            " line3\n"
        )
        files = parse_diff(diff)
        assert len(files) == 1
        assert files[0].additions == 0
        assert files[0].deletions == 1

    def test_mixed_additions_and_deletions(self) -> None:
        diff = (
            "--- a/hello.py\n"
            "+++ b/hello.py\n"
            "@@ -1,4 +1,4 @@\n"
            " line1\n"
            "-old_line\n"
            "+new_line\n"
            " line2\n"
            " line3\n"
        )
        files = parse_diff(diff)
        assert len(files) == 1
        assert files[0].additions == 1
        assert files[0].deletions == 1

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

    def test_context_lines_have_correct_line_numbers(self) -> None:
        diff = (
            "--- a/f.py\n"
            "+++ b/f.py\n"
            "@@ -5,3 +5,4 @@\n"
            " ctx_line5\n"
            "+added\n"
            " ctx_line6\n"
            " ctx_line7\n"
        )
        files = parse_diff(diff)
        lines = files[0].hunks[0].lines
        # First context line: old=5, new=5
        assert lines[0].line_type == LineType.CONTEXT
        assert lines[0].old_lineno == 5
        assert lines[0].new_lineno == 5
        # Addition: old=None, new=6
        assert lines[1].line_type == LineType.ADDITION
        assert lines[1].old_lineno is None
        assert lines[1].new_lineno == 6
        # Next context: old=6, new=7 (shifted by the addition)
        assert lines[2].line_type == LineType.CONTEXT
        assert lines[2].old_lineno == 6
        assert lines[2].new_lineno == 7

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

    def test_empty_diff(self) -> None:
        assert parse_diff("") == []

    def test_whitespace_only_diff(self) -> None:
        assert parse_diff("   \n\n  \t  \n") == []

    def test_diff_git_format_with_index(self) -> None:
        diff = (
            "diff --git a/src/lib.rs b/src/lib.rs\n"
            "index abc1234..def5678 100644\n"
            "--- a/src/lib.rs\n"
            "+++ b/src/lib.rs\n"
            "@@ -1,3 +1,4 @@\n"
            " use std::io;\n"
            "+use std::fs;\n"
            " fn main() {}\n"
            " // end\n"
        )
        files = parse_diff(diff)
        assert len(files) == 1
        assert files[0].old_path == "src/lib.rs"
        assert files[0].new_path == "src/lib.rs"
        assert files[0].language == "rust"
        assert files[0].additions == 1

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

    def test_plain_format_no_git_prefix(self) -> None:
        """Diffs without 'diff --git' header, just --- and +++ lines."""
        diff = "--- utils.py\n+++ utils.py\n@@ -1,2 +1,3 @@\n existing\n+added\n end\n"
        files = parse_diff(diff)
        assert len(files) == 1
        assert files[0].old_path == "utils.py"
        assert files[0].new_path == "utils.py"

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

    _CASES = [
        (".py", "python"),
        (".js", "javascript"),
        (".ts", "typescript"),
        (".tsx", "typescript"),
        (".jsx", "javascript"),
        (".rs", "rust"),
        (".go", "go"),
        (".rb", "ruby"),
        (".java", "java"),
        (".c", "c"),
        (".cpp", "cpp"),
        (".h", "c"),
        (".hpp", "cpp"),
        (".cs", "csharp"),
        (".sh", "bash"),
        (".yaml", "yaml"),
        (".yml", "yaml"),
        (".json", "json"),
        (".toml", "toml"),
        (".md", "markdown"),
        (".html", "html"),
        (".css", "css"),
        (".sql", "sql"),
        (".xml", "xml"),
        (".dockerfile", "dockerfile"),
        (".tf", "hcl"),
    ]

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
        assert files[0].language == "dockerfile"

    def test_dockerfile_variant_by_name(self) -> None:
        diff = (
            "--- a/Dockerfile.prod\n"
            "+++ b/Dockerfile.prod\n"
            "@@ -1,1 +1,2 @@\n"
            " FROM alpine\n"
            "+RUN echo hi\n"
        )
        files = parse_diff(diff)
        assert files[0].language == "dockerfile"

    def test_makefile_by_name(self) -> None:
        diff = "--- a/Makefile\n+++ b/Makefile\n@@ -1,1 +1,2 @@\n all:\n+\t@echo done\n"
        files = parse_diff(diff)
        assert files[0].language == "makefile"

    def test_jenkinsfile_by_name(self) -> None:
        diff = (
            "--- a/Jenkinsfile\n"
            "+++ b/Jenkinsfile\n"
            "@@ -1,1 +1,2 @@\n"
            " pipeline {\n"
            "+  agent any\n"
        )
        files = parse_diff(diff)
        assert files[0].language == "groovy"

    def test_deleted_file_uses_old_path_for_language(self) -> None:
        diff = "--- a/module.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-line1\n-line2\n"
        files = parse_diff(diff)
        assert files[0].language == "python"


# ---------------------------------------------------------------------------
# Model tests
# ---------------------------------------------------------------------------


class TestDiffModels:
    """Verify frozen semantics and enum values for diff data models."""

    def test_diff_line_frozen(self) -> None:
        line = DiffLine(
            old_lineno=1, new_lineno=1, content="x", line_type=LineType.CONTEXT
        )
        with pytest.raises(FrozenInstanceError):
            line.content = "changed"

    def test_diff_hunk_frozen(self) -> None:
        hunk = DiffHunk(
            header="@@ -1,1 +1,1 @@",
            old_start=1,
            old_count=1,
            new_start=1,
            new_count=1,
            lines=(),
        )
        with pytest.raises(FrozenInstanceError):
            hunk.old_start = 99

    def test_diff_file_frozen(self) -> None:
        f = DiffFile(
            old_path="a.py",
            new_path="a.py",
            status=FileStatus.MODIFIED,
            hunks=(),
        )
        with pytest.raises(FrozenInstanceError):
            f.old_path = "changed.py"

    def test_line_type_enum_values(self) -> None:
        assert LineType.CONTEXT.value == "context"
        assert LineType.ADDITION.value == "addition"
        assert LineType.DELETION.value == "deletion"
        assert LineType.HUNK_HEADER.value == "hunk_header"
        assert LineType.NO_NEWLINE.value == "no_newline"

    def test_file_status_enum_values(self) -> None:
        assert FileStatus.MODIFIED.value == "modified"
        assert FileStatus.ADDED.value == "added"
        assert FileStatus.DELETED.value == "deleted"
        assert FileStatus.RENAMED.value == "renamed"

    def test_diff_file_default_values(self) -> None:
        f = DiffFile(
            old_path="x.py",
            new_path="x.py",
            status=FileStatus.MODIFIED,
            hunks=(),
        )
        assert f.additions == 0
        assert f.deletions == 0
        assert f.is_binary is False
        assert f.language == ""

    def test_diff_hunk_default_context_text(self) -> None:
        hunk = DiffHunk(
            header="@@ -1,1 +1,1 @@",
            old_start=1,
            old_count=1,
            new_start=1,
            new_count=1,
            lines=(),
        )
        assert hunk.context_text == ""


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

    def test_empty_hunk_zero_additions_deletions(self) -> None:
        """A hunk header with 0,0 counts produces an empty hunk."""
        diff = "--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,3 @@\n line1\n line2\n line3\n"
        files = parse_diff(diff)
        assert files[0].additions == 0
        assert files[0].deletions == 0
        # All lines are context
        assert all(ln.line_type == LineType.CONTEXT for ln in files[0].hunks[0].lines)

    def test_very_long_line_content(self) -> None:
        long_content = "x" * 10_000
        diff = f"--- a/f.txt\n+++ b/f.txt\n@@ -1,1 +1,2 @@\n short\n+{long_content}\n"
        files = parse_diff(diff)
        added = [
            ln for ln in files[0].hunks[0].lines if ln.line_type == LineType.ADDITION
        ]
        assert len(added) == 1
        assert added[0].content == long_content

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

    def test_multiple_files_git_format(self) -> None:
        diff = (
            "diff --git a/one.py b/one.py\n"
            "--- a/one.py\n"
            "+++ b/one.py\n"
            "@@ -1,1 +1,2 @@\n"
            " first\n"
            "+added_to_one\n"
            "diff --git a/two.py b/two.py\n"
            "--- a/two.py\n"
            "+++ b/two.py\n"
            "@@ -1,1 +1,2 @@\n"
            " second\n"
            "+added_to_two\n"
        )
        files = parse_diff(diff)
        assert len(files) == 2
        assert files[0].new_path == "one.py"
        assert files[1].new_path == "two.py"
        assert files[0].additions == 1
        assert files[1].additions == 1

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

    def test_hunk_lines_tuple_type(self) -> None:
        """DiffHunk.lines is a tuple, not a list."""
        diff = "--- a/f.py\n+++ b/f.py\n@@ -1,1 +1,2 @@\n x\n+y\n"
        files = parse_diff(diff)
        assert isinstance(files[0].hunks[0].lines, tuple)

    def test_hunks_tuple_type(self) -> None:
        """DiffFile.hunks is a tuple, not a list."""
        diff = "--- a/f.py\n+++ b/f.py\n@@ -1,1 +1,2 @@\n x\n+y\n"
        files = parse_diff(diff)
        assert isinstance(files[0].hunks, tuple)

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
