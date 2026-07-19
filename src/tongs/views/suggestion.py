"""Pure helpers for building suggestion comments.

All functions here are forge-agnostic or accept forge_type as a parameter.
They contain no I/O, no Textual dependencies, and are trivially testable.
"""

from __future__ import annotations

from tongs.diff.models import DiffLine, LineType
from tongs.scanner.repo import ForgeType

SUGGESTION_SEPARATOR = ">>>>>>> EDIT CODE BELOW THIS LINE >>>>>>>"
TEMPLATE_INSTRUCTION = "Your comment (everything above the marker line):"


def build_suggestion_template(original_code: str) -> str:
    """Build the editor template with comment area and original code."""
    return f"{TEMPLATE_INSTRUCTION}\n\n\n{SUGGESTION_SEPARATOR}\n{original_code}\n"


def parse_suggestion_template(edited: str) -> tuple[str, str]:
    """Parse editor output into (comment_text, suggested_code).

    Returns the comment text (above separator) and suggested code (below).
    If no separator is found, the entire text is treated as suggested code
    with an empty comment.
    """
    parts = edited.split(SUGGESTION_SEPARATOR, maxsplit=1)
    if len(parts) == 2:
        comment_raw = parts[0].strip()
        suggested_code = parts[1].strip()
    else:
        comment_raw = ""
        suggested_code = edited.strip()

    comment_lines = [
        ln for ln in comment_raw.split("\n") if ln.strip() != TEMPLATE_INSTRUCTION
    ]
    comment_text = "\n".join(comment_lines).strip()
    return comment_text, suggested_code


def compute_backtick_fence(code: str) -> str:
    """Return the minimum backtick fence that won't conflict with code content.

    Scans the code for the longest consecutive run of backticks and returns
    a fence string one longer (minimum 3).
    """
    max_run = 0
    current_run = 0
    for ch in code:
        if ch == "`":
            current_run += 1
            max_run = max(max_run, current_run)
        else:
            current_run = 0
    return "`" * max(3, max_run + 1)


def format_suggestion_block(
    suggested_code: str,
    n_original: int,
    forge_type: ForgeType,
    comment_text: str = "",
) -> str:
    """Format the full comment body with suggestion block.

    Args:
        suggested_code: The user's edited code.
        n_original: Number of original new-side lines being replaced.
        forge_type: Target forge (affects suggestion syntax).
        comment_text: Optional comment text to prepend.

    Returns:
        The complete comment body ready to post.
    """
    fence = compute_backtick_fence(suggested_code)

    if forge_type == ForgeType.GITLAB:
        suggestion_block = (
            f"{fence}suggestion:-0+{n_original - 1}\n{suggested_code}\n{fence}"
        )
    else:
        suggestion_block = f"{fence}suggestion\n{suggested_code}\n{fence}"

    if comment_text:
        return f"{comment_text}\n\n{suggestion_block}"
    return suggestion_block


def extract_new_side_lines(lines: list[DiffLine]) -> list[DiffLine]:
    """Filter to only new-side lines (context + additions, no deletions)."""
    return [dl for dl in lines if dl.line_type != LineType.DELETION]


def resolve_suggestion_position(
    new_side_lines: list[DiffLine],
    forge_type: ForgeType,
) -> tuple[DiffLine, int | None, str | None]:
    """Determine anchor line and optional start_line/start_side for a suggestion.

    Returns (anchor_line, start_line, start_side) where:
    - GitLab: anchor is always the first line, start_line/start_side are None
      (range is encoded in the suggestion fence syntax).
    - GitHub single-line: anchor is the first line, start_line/start_side are None.
    - GitHub multi-line: anchor is the LAST line (GitHub's ``line`` param),
      start_line is the first line's new_lineno, start_side is "RIGHT".
    """
    anchor_line = new_side_lines[0]
    n = len(new_side_lines)
    if n > 1 and forge_type == ForgeType.GITHUB:
        return new_side_lines[-1], anchor_line.new_lineno, "RIGHT"
    return anchor_line, None, None
