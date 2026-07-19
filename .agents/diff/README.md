# Diff

## Parser Design

`src/tongs/diff/parser.py` parses unified diff text into structured objects. It is forge-agnostic; both GitHub and GitLab produce standard unified diff format.

### Entry Point

```python
from tongs.diff.parser import parse_diff

files: list[DiffFile] = parse_diff(diff_text)
```

`parse_diff()` splits the input into lines and dispatches to format-specific parsers based on what it finds first:

- `diff --git a/... b/...` header -> `_parse_git_diff_file()` (git format)
- `--- ...` followed by `+++ ...` -> `_parse_plain_diff_file()` (plain unified diff)

Both formats are supported in a single diff string (mixed is fine).

### Two Parse Paths

**Git format** (`_parse_git_diff_file`):
1. Reads `diff --git a/{old} b/{new}` to get file paths
2. Scans metadata lines: `new file`, `deleted file`, `rename from/to`, `similarity index`, `index`, `Binary files`
3. When `--- ` is found, delegates to `_parse_hunks_from()` for hunk content
4. Falls through to create a headerless DiffFile for binary/metadata-only entries

**Plain format** (`_parse_plain_diff_file`):
1. Reads `--- {old_path}` and `+++ {new_path}` directly
2. Strips `a/`/`b/` prefixes via `_strip_prefix()`
3. Detects added (`/dev/null` as old) and deleted (`/dev/null` as new) files
4. Delegates to `_parse_hunks_from()` for hunk content

### Hunk Parsing

`_parse_hunks_from()` skips `---`/`+++` lines, then loops looking for `@@ -old_start,old_count +new_start,new_count @@ context_text` headers.

`_parse_single_hunk()` processes one hunk:
- Tracks `old_line` and `new_line` counters starting from the hunk header values
- `+` lines: addition (new_lineno set, old_lineno None), increment new_line
- `-` lines: deletion (old_lineno set, new_lineno None), increment old_line
- ` ` lines or empty lines: context (both linenos set), increment both
- `\` lines: no-newline marker (both linenos None)
- Stops at next `@@`, `diff --git`, or `---`/`+++` pair

### Boundary Detection

The parser detects file boundaries by looking for:
- `diff --git ` prefix (new git-format file)
- `--- ` immediately followed by `+++ ` on the next line (new plain-format file)

This means a line starting with `---` inside a hunk is only treated as a boundary if the very next line starts with `+++`. This prevents false matches on content lines like SQL comments (`-- DROP TABLE`).

### Hunk Header Regex

```python
HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
```

Groups: (1) old_start, (2) old_count (optional, defaults to "1"), (3) new_start, (4) new_count (optional), (5) context text (function name, etc.).

## Models

`src/tongs/diff/models.py` defines three frozen dataclasses:

**DiffLine:**
- `old_lineno: int | None` -- line number in old file (None for additions)
- `new_lineno: int | None` -- line number in new file (None for deletions)
- `content: str` -- line content (prefix character stripped)
- `line_type: LineType` -- CONTEXT, ADDITION, DELETION, HUNK_HEADER, NO_NEWLINE

**DiffHunk:**
- `header: str` -- raw `@@ ... @@` line
- `old_start`, `old_count`, `new_start`, `new_count` -- from hunk header
- `lines: tuple[DiffLine, ...]` -- parsed lines in this hunk
- `context_text: str` -- function name from hunk header (after `@@`)

**DiffFile:**
- `old_path`, `new_path` -- file paths (no `a/`/`b/` prefixes)
- `status: FileStatus` -- MODIFIED, ADDED, DELETED, RENAMED
- `hunks: tuple[DiffHunk, ...]`
- `additions`, `deletions` -- computed counts
- `is_binary: bool`
- `language: str` -- detected from file extension

## Language Detection

`_detect_language(path)` maps file extensions to language names via `LANGUAGE_MAP` dict. Also handles extensionless special files: `Dockerfile`, `Makefile`, `Jenkinsfile`.

Supported: python, javascript, typescript, rust, go, ruby, java, c, cpp, csharp, bash, yaml, json, toml, markdown, html, css, sql, xml, dockerfile, hcl, groovy, makefile.

Used for syntax highlighting scoping (planned: viewport-scoped via `rich.syntax.Syntax`).

## Known Edge Cases

1. **Empty lines in hunks:** treated as context lines (both counters increment). The `content` is empty string.
2. **SQL comments (`-- ...`):** only treated as file boundary if next line starts with `+++ `. Inside a hunk, `--` is a deletion line.
3. **No-newline marker:** `\ No newline at end of file` is preserved as `LineType.NO_NEWLINE` with both linenos as None.
4. **Binary files:** detected via `Binary files` line. DiffFile has `is_binary=True` and empty hunks.
5. **Renamed files without content change:** detected via `rename from`/`rename to` lines. DiffFile has `status=RENAMED` and may have empty hunks.
6. **Files with only metadata changes** (mode change, no content): results in DiffFile with empty hunks.
7. **Hunk count defaults:** `@@ -1 +1,3 @@` means old_count=1 (omitted comma means count of 1).

## Test Fixtures

Real diff files for testing are in `tests/fixtures/`:
- `builder_mr_3113.diff` -- real MR diff from the builder project
- `fromager_pr_1258.diff` -- real PR diff from fromager

Tests in `tests/test_diff/test_parser.py` cover both inline diff strings and fixture file parsing.

## Position Mapping

`src/tongs/diff/position.py` translates diff line positions to forge-specific API formats for inline commenting. It is forge-agnostic at the core, with per-forge converters.

### DiffPosition

Frozen dataclass capturing enough information for any forge:

- `file: DiffFile` -- the file this position belongs to
- `line: DiffLine` -- the specific diff line
- `old_path`, `new_path` -- file paths
- `old_line: int | None`, `new_line: int | None` -- line numbers in old/new file
- `side: str` -- `"LEFT"` (old file / deletions) or `"RIGHT"` (new file / additions and context)

### Factory

`position_from_diff_line(file, line)` creates a `DiffPosition` from a `DiffFile` and `DiffLine`. Side is determined by `LineType`: ADDITION -> RIGHT, DELETION -> LEFT, everything else -> RIGHT.

### Forge Converters

**`to_gitlab_position(pos, base_sha, start_sha, head_sha)`** returns a dict matching GitLab's discussions API `position` object:
- Always includes `position_type`, `base_sha`, `start_sha`, `head_sha`, `old_path`, `new_path`
- Sets `old_line` for LEFT-side positions, `new_line` for RIGHT-side

**`to_github_position(pos, commit_id)`** returns a dict matching GitHub's pull request review comments API:
- `path` is `old_path` for LEFT, `new_path` for RIGHT
- Includes `side`, `commit_id`, and the appropriate `line` number

**`to_forge_position(pos, forge_type, ...)`** dispatches to the correct converter based on `ForgeType` enum. Accepts all keyword arguments for both forges; each converter uses only what it needs.

## Rendering Pipeline

Diff lines flow through this pipeline from parser to screen:

1. `parse_diff()` produces `list[DiffFile]` with `DiffHunk` and `DiffLine` objects
2. `DiffContent.show_file()` passes the file to `DiffRenderer.render_lines()`
3. `DiffRenderer` handles syntax highlighting (`rich.syntax.Syntax`), word-level diffs (`difflib.SequenceMatcher`), and context folding. Returns `list[tuple[DiffLine | None, Text]]` with foreground-only styling
4. Each `(DiffLine, Text)` pair becomes an `Option` in `DiffOptionList`
5. `DiffOptionList.render_line()` injects background colors (addition/deletion/selection) via `VisualStyle` BEFORE calling `_get_option_render()`. This is the only place backgrounds are set

The foreground/background split is intentional: `Strip.apply_style()` cannot reliably override backgrounds due to Textual style priority, so backgrounds must be set in the VisualStyle before rendering.

## Suggestion Position Mapping

`src/tongs/views/suggestion.py:resolve_suggestion_position()` extends the position system for multi-line suggestions:

- **GitLab:** Range is encoded in the suggestion fence syntax (`suggestion:-0+N`). The anchor line is always the first new-side line. No `start_line`/`start_side` API params needed.
- **GitHub single-line:** Anchor is the first line. No extra params.
- **GitHub multi-line:** Anchor is the LAST new-side line (GitHub's `line` parameter). `start_line` is the first line's `new_lineno`, `start_side` is `"RIGHT"`.

The `create_inline_comment` ABC accepts optional `start_line`/`start_side` to support this.

## Comment Anchors in Gutter (Phase 4)

The `DiffRenderer._gutter()` method renders a comment marker `*` in the gutter for lines that have discussions:
- Yellow bold `*` for lines with unresolved discussions
- Dim `*` for lines where all discussions are resolved

The gutter lookup uses a `comment_lines: dict[tuple[int | None, int | None], bool]` map (built by `_build_comment_lines()`), where the bool indicates whether all discussions at that position are resolved. The key is `(old_lineno, new_lineno)` matching the DiffLine's line numbers.

## Planned Features

- **Virtual scrolling:** only materialize visible lines as Rich Text objects
