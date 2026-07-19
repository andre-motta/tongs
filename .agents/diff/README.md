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

## Position Mapping (Planned)

`diff/position.py` (not yet implemented) will translate between line numbers and forge-specific position formats for inline commenting:

- **GitLab:** position object with `base_sha`, `start_sha`, `head_sha`, `old_line`/`new_line`, `new_path`/`old_path`
- **GitHub:** `path`, `line`, `side` (LEFT/RIGHT)

The current `GitLabClient.create_inline_comment()` fetches `diff_refs` from the MR and builds the position object inline. This will be refactored to use the position module once it exists.

## Planned Features (Phase 2)

- **Viewport-scoped highlighting:** only highlight visible lines + 100-line buffer via `rich.syntax.Syntax`
- **Word-level diff highlighting:** within changed lines, highlight specific tokens that changed
- **Context folding:** collapse unchanged sections to 3 context lines with expandable separators
- **Virtual scrolling:** only materialize visible lines as Rich Text objects
- **Comment anchors in gutter:** markers for existing comments, drafts, resolved threads
