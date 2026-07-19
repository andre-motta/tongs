"""Parse unified diff text into structured DiffFile/DiffHunk/DiffLine objects.

This parser is forge-agnostic. Both GitHub and GitLab produce standard
unified diff format. The parser handles:
- Standard @@ hunk headers with optional function context
- No-newline-at-end-of-file markers
- File mode changes
- Binary file markers
- Renamed files (with similarity index)
- Multiple files in a single diff
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

from tongs.diff.models import DiffFile, DiffHunk, DiffLine, FileStatus, LineType

HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")

FILE_HEADER_RE = re.compile(r"^--- (.+)$")
FILE_HEADER_NEW_RE = re.compile(r"^\+\+\+ (.+)$")

DIFF_GIT_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")

LANGUAGE_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".rb": "ruby",
    ".java": "java",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".md": "markdown",
    ".html": "html",
    ".css": "css",
    ".sql": "sql",
    ".xml": "xml",
    ".dockerfile": "dockerfile",
    ".tf": "hcl",
}


def parse_diff(diff_text: str) -> list[DiffFile]:
    """Parse a unified diff string into a list of DiffFile objects."""
    if not diff_text or not diff_text.strip():
        return []

    lines = diff_text.split("\n")
    files: list[DiffFile] = []
    i = 0

    while i < len(lines):
        line = lines[i]

        git_match = DIFF_GIT_RE.match(line)
        if git_match:
            file, consumed = _parse_git_diff_file(lines, i)
            if file:
                files.append(file)
            i += consumed
            continue

        old_match = FILE_HEADER_RE.match(line)
        if old_match and i + 1 < len(lines):
            new_match = FILE_HEADER_NEW_RE.match(lines[i + 1])
            if new_match:
                file, consumed = _parse_plain_diff_file(lines, i)
                if file:
                    files.append(file)
                i += consumed
                continue

        i += 1

    return files


def _parse_git_diff_file(lines: list[str], start: int) -> tuple[DiffFile | None, int]:
    """Parse a file section starting from 'diff --git ...'."""
    git_match = DIFF_GIT_RE.match(lines[start])
    if not git_match:
        return None, 1

    old_path = git_match.group(1)
    new_path = git_match.group(2)
    i = start + 1
    is_binary = False
    status = FileStatus.MODIFIED

    while i < len(lines):
        line = lines[i]

        if line.startswith("diff --git "):
            break

        if line.startswith("new file"):
            status = FileStatus.ADDED
            i += 1
            continue

        if line.startswith("deleted file"):
            status = FileStatus.DELETED
            i += 1
            continue

        if line.startswith("rename from") or line.startswith("rename to"):
            status = FileStatus.RENAMED
            i += 1
            continue

        if line.startswith("similarity index") or line.startswith(
            "dissimilarity index"
        ):
            i += 1
            continue

        if line.startswith("index "):
            i += 1
            continue

        if line.startswith("Binary files"):
            is_binary = True
            i += 1
            continue

        if line.startswith("--- "):
            hunks, consumed = _parse_hunks_from(lines, i)
            actual_old = _strip_prefix(lines[i][4:])
            actual_new = (
                _strip_prefix(lines[i + 1][4:]) if i + 1 < len(lines) else new_path
            )

            additions = sum(
                1 for h in hunks for dl in h.lines if dl.line_type == LineType.ADDITION
            )
            deletions = sum(
                1 for h in hunks for dl in h.lines if dl.line_type == LineType.DELETION
            )

            lang = _detect_language(
                actual_new if actual_new != "/dev/null" else actual_old
            )

            return (
                DiffFile(
                    old_path=actual_old,
                    new_path=actual_new,
                    status=status,
                    hunks=tuple(hunks),
                    additions=additions,
                    deletions=deletions,
                    is_binary=is_binary,
                    language=lang,
                ),
                i + consumed - start,
            )

        if line.startswith("+++ "):
            i += 1
            continue

        i += 1

    if is_binary or status in (
        FileStatus.ADDED,
        FileStatus.DELETED,
        FileStatus.RENAMED,
    ):
        lang = _detect_language(new_path)
        return (
            DiffFile(
                old_path=old_path,
                new_path=new_path,
                status=status,
                hunks=(),
                is_binary=is_binary,
                language=lang,
            ),
            i - start,
        )

    return None, i - start


def _parse_plain_diff_file(lines: list[str], start: int) -> tuple[DiffFile | None, int]:
    """Parse a file section starting from '--- ...' (no git prefix)."""
    old_path = _strip_prefix(lines[start][4:])
    new_path = _strip_prefix(lines[start + 1][4:])

    hunks, consumed = _parse_hunks_from(lines, start)

    additions = sum(
        1 for h in hunks for dl in h.lines if dl.line_type == LineType.ADDITION
    )
    deletions = sum(
        1 for h in hunks for dl in h.lines if dl.line_type == LineType.DELETION
    )

    status = FileStatus.MODIFIED
    if old_path == "/dev/null":
        status = FileStatus.ADDED
    elif new_path == "/dev/null":
        status = FileStatus.DELETED

    lang = _detect_language(new_path if new_path != "/dev/null" else old_path)

    return (
        DiffFile(
            old_path=old_path,
            new_path=new_path,
            status=status,
            hunks=tuple(hunks),
            additions=additions,
            deletions=deletions,
            language=lang,
        ),
        consumed,
    )


def _parse_hunks_from(lines: list[str], start: int) -> tuple[list[DiffHunk], int]:
    """Parse all hunks starting from the --- line."""
    i = start
    # Skip --- and +++ lines
    if i < len(lines) and lines[i].startswith("--- "):
        i += 1
    if i < len(lines) and lines[i].startswith("+++ "):
        i += 1

    hunks: list[DiffHunk] = []

    while i < len(lines):
        line = lines[i]

        if line.startswith("diff --git "):
            break

        if (
            line.startswith("--- ")
            and i + 1 < len(lines)
            and lines[i + 1].startswith("+++ ")
        ):
            break

        hunk_match = HUNK_HEADER_RE.match(line)
        if hunk_match:
            hunk, consumed = _parse_single_hunk(lines, i, hunk_match)
            hunks.append(hunk)
            i += consumed
            continue

        i += 1

    return hunks, i - start


def _parse_single_hunk(
    lines: list[str], start: int, hunk_match: re.Match
) -> tuple[DiffHunk, int]:
    """Parse a single hunk starting from the @@ line."""
    old_start = int(hunk_match.group(1))
    old_count = int(hunk_match.group(2) or "1")
    new_start = int(hunk_match.group(3))
    new_count = int(hunk_match.group(4) or "1")
    context_text = hunk_match.group(5).strip()

    diff_lines: list[DiffLine] = []
    old_line = old_start
    new_line = new_start
    i = start + 1

    while i < len(lines):
        line = lines[i]

        if line.startswith("@@ ") or line.startswith("diff --git "):
            break

        if (
            line.startswith("--- ")
            and i + 1 < len(lines)
            and lines[i + 1].startswith("+++ ")
        ):
            break

        if line.startswith("+"):
            diff_lines.append(
                DiffLine(
                    old_lineno=None,
                    new_lineno=new_line,
                    content=line[1:],
                    line_type=LineType.ADDITION,
                )
            )
            new_line += 1
        elif line.startswith("-"):
            diff_lines.append(
                DiffLine(
                    old_lineno=old_line,
                    new_lineno=None,
                    content=line[1:],
                    line_type=LineType.DELETION,
                )
            )
            old_line += 1
        elif line.startswith(" ") or not line:
            diff_lines.append(
                DiffLine(
                    old_lineno=old_line,
                    new_lineno=new_line,
                    content=line[1:] if line else "",
                    line_type=LineType.CONTEXT,
                )
            )
            old_line += 1
            new_line += 1
        elif line.startswith("\\"):
            diff_lines.append(
                DiffLine(
                    old_lineno=None,
                    new_lineno=None,
                    content=line,
                    line_type=LineType.NO_NEWLINE,
                )
            )
        else:
            break

        i += 1

    return (
        DiffHunk(
            header=lines[start],
            old_start=old_start,
            old_count=old_count,
            new_start=new_start,
            new_count=new_count,
            lines=tuple(diff_lines),
            context_text=context_text,
        ),
        i - start,
    )


def _strip_prefix(path: str) -> str:
    """Strip a/ or b/ prefix from diff paths."""
    path = path.strip()
    if path.startswith("a/") or path.startswith("b/"):
        return path[2:]
    return path


def _detect_language(path: str) -> str:
    """Detect programming language from file extension."""
    if path == "/dev/null":
        return ""
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in LANGUAGE_MAP:
        return LANGUAGE_MAP[suffix]
    name = PurePosixPath(path).name.lower()
    if name == "dockerfile" or name.startswith("dockerfile."):
        return "dockerfile"
    if name == "makefile":
        return "makefile"
    if name == "jenkinsfile":
        return "groovy"
    return ""
