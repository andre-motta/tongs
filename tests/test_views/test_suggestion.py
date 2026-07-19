"""Tests for suggestion comment helpers."""


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

    def test_multiline_original_code(self):
        result = build_suggestion_template("a = 1\nb = 2")
        assert SUGGESTION_SEPARATOR in result
        assert "a = 1\nb = 2" in result


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

    def test_empty_comment_area(self):
        edited = f"\n\n{SUGGESTION_SEPARATOR}\ny = 10"
        comment, code = parse_suggestion_template(edited)
        assert comment == ""
        assert code == "y = 10"

    def test_whitespace_handling(self):
        edited = f"  some comment  \n{SUGGESTION_SEPARATOR}\n  code  "
        comment, code = parse_suggestion_template(edited)
        assert comment == "some comment"
        assert code == "code"


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

    def test_empty_string_returns_triple(self):
        assert compute_backtick_fence("") == "```"


class TestFormatSuggestionBlock:
    def test_gitlab_single_line(self):
        result = format_suggestion_block("new code", 1, ForgeType.GITLAB)
        assert "```suggestion:-0+0" in result
        assert "new code" in result

    def test_gitlab_multi_line_3_lines(self):
        result = format_suggestion_block("replaced", 3, ForgeType.GITLAB)
        assert "```suggestion:-0+2" in result

    def test_github_single_line(self):
        result = format_suggestion_block("new code", 1, ForgeType.GITHUB)
        assert "```suggestion" in result
        # No offset syntax for GitHub
        assert "suggestion:-" not in result
        assert "suggestion:+" not in result

    def test_github_multi_line_same_as_single(self):
        result = format_suggestion_block("replaced", 3, ForgeType.GITHUB)
        # GitHub range is in API params, not in the body
        assert "```suggestion" in result
        assert "suggestion:-" not in result

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

    def test_keeps_context_and_addition(self):
        lines = [
            DiffLine(None, 1, "added", LineType.ADDITION),
            DiffLine(2, 2, "context", LineType.CONTEXT),
        ]
        result = extract_new_side_lines(lines)
        assert len(result) == 2
        assert result[0].line_type == LineType.ADDITION
        assert result[1].line_type == LineType.CONTEXT

    def test_empty_input_returns_empty(self):
        assert extract_new_side_lines([]) == []


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
