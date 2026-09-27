"""Tests for suggestion comment helpers."""

from __future__ import annotations

import pytest

from tongs.diff.models import DiffLine, LineType
from tongs.scanner.repo import ForgeType
from tongs.views.suggestion import (
    SUGGESTION_SEPARATOR,
    TEMPLATE_INSTRUCTION,
    build_suggestion_template,
    compute_backtick_fence,
    extract_new_side_lines,
    format_suggestion_block,
    parse_suggestion_template,
    resolve_suggestion_position,
)


class TestBuildSuggestionTemplate:
    def test_creates_template_with_instruction_separator_and_code(self):
        result = build_suggestion_template("x = 1")
        lines = result.split("\n")
        assert lines[0] == TEMPLATE_INSTRUCTION
        # blank lines for comment area
        assert lines[1] == ""
        assert lines[2] == ""
        assert lines[3] == SUGGESTION_SEPARATOR
        assert lines[4] == "x = 1"


class TestParseSuggestionTemplate:
    def test_normal_edit_comment_above_code_below(self):
        edited = f"Fix the typo\n{SUGGESTION_SEPARATOR}\nx = 2"
        comment, code = parse_suggestion_template(edited)
        assert comment == "Fix the typo"
        assert code == "x = 2"

    def test_no_separator_entire_text_is_code(self):
        edited = "x = 42"
        comment, code = parse_suggestion_template(edited)
        assert comment == ""
        assert code == "x = 42"

    def test_instruction_line_stripped_from_comment(self):
        edited = (
            f"{TEMPLATE_INSTRUCTION}\nPlease rename\n{SUGGESTION_SEPARATOR}\nfoo = 1"
        )
        comment, code = parse_suggestion_template(edited)
        assert TEMPLATE_INSTRUCTION not in comment
        assert comment == "Please rename"
        assert code == "foo = 1"


class TestComputeBacktickFence:
    def test_no_backticks_returns_triple(self):
        assert compute_backtick_fence("x = 1") == "```"

    def test_code_with_triple_backticks_returns_quad(self):
        assert compute_backtick_fence("some ```code``` here") == "````"

    def test_code_with_five_consecutive_backticks(self):
        assert compute_backtick_fence("text `````more") == "``````"

    def test_non_consecutive_backticks_only_consecutive_runs_count(self):
        # Two separate single backticks are each runs of 1, not 2
        assert compute_backtick_fence("`a`b`") == "```"


class TestFormatSuggestionBlock:
    @pytest.mark.parametrize(
        ("n_original", "header"),
        [(1, "```suggestion:-0+0"), (3, "```suggestion:-0+2")],
    )
    def test_gitlab_header_spans_the_original_lines(self, n_original: int, header: str):
        result = format_suggestion_block("new code", n_original, ForgeType.GITLAB)
        assert header in result
        assert "new code" in result

    @pytest.mark.parametrize("n_original", [1, 3])
    def test_github_header_has_no_offset(self, n_original: int):
        result = format_suggestion_block("new code", n_original, ForgeType.GITHUB)
        assert "```suggestion" in result
        # GitHub carries the range in API parameters, not in the body.
        assert "suggestion:-" not in result
        assert "suggestion:+" not in result

    def test_with_comment_text(self):
        result = format_suggestion_block("code", 1, ForgeType.GITLAB, "Fix this")
        lines = result.split("\n")
        assert lines[0] == "Fix this"
        assert lines[1] == ""
        assert "suggestion" in lines[2]

    def test_without_comment_text(self):
        result = format_suggestion_block("code", 1, ForgeType.GITLAB)
        assert result.startswith("```suggestion")

    def test_code_containing_backticks_fence_adapts(self):
        code_with_backticks = "some ```inner``` block"
        result = format_suggestion_block(code_with_backticks, 1, ForgeType.GITLAB)
        assert result.startswith("````")
        assert "````suggestion" in result


class TestExtractNewSideLines:
    def test_filters_out_deletions(self):
        lines = [
            DiffLine(1, None, "old", LineType.DELETION),
            DiffLine(None, 1, "new", LineType.ADDITION),
            DiffLine(2, 2, "ctx", LineType.CONTEXT),
        ]
        result = extract_new_side_lines(lines)
        assert len(result) == 2
        assert all(dl.line_type != LineType.DELETION for dl in result)


class TestResolveSuggestionPosition:
    def test_single_line_github(self):
        lines = [DiffLine(None, 10, "code", LineType.ADDITION)]
        anchor, start_line, start_side = resolve_suggestion_position(
            lines, ForgeType.GITHUB
        )
        assert anchor is lines[0]
        assert start_line is None
        assert start_side is None

    def test_multi_line_github_swaps_to_last(self):
        lines = [
            DiffLine(None, 10, "first", LineType.ADDITION),
            DiffLine(1, 11, "middle", LineType.CONTEXT),
            DiffLine(None, 12, "last", LineType.ADDITION),
        ]
        anchor, start_line, start_side = resolve_suggestion_position(
            lines, ForgeType.GITHUB
        )
        assert anchor is lines[-1]
        assert start_line == 10
        assert start_side == "RIGHT"

    def test_multi_line_gitlab_no_swap(self):
        lines = [
            DiffLine(None, 10, "first", LineType.ADDITION),
            DiffLine(1, 11, "middle", LineType.CONTEXT),
            DiffLine(None, 12, "last", LineType.ADDITION),
        ]
        anchor, start_line, start_side = resolve_suggestion_position(
            lines, ForgeType.GITLAB
        )
        assert anchor is lines[0]
        assert start_line is None
        assert start_side is None
