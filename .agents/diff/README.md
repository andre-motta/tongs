# Diff

## Frontend boundaries

Forge clients return change dictionaries. `ApplicationSession.get_raw_diff()`
binds those changes to a `ReviewRevision` without selecting a UI format. The
terminal calls `TUIServiceAdapter.get_diff()`, which converts them through
`src/tongs/diff/conversion.py` to the models below. The desktop protocol projects
the same admitted snapshot through
`src/tongs/desktop/protocol/diff_projection.py` into bounded unified or split
rows and serves them from expiring connection-local paging snapshots.

Inline mutations must retain the revision and service-issued review identity
that produced the displayed lines. Neither frontend should reconstruct a forge
mutation target from a path or line number alone.

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
- Stops at the next `@@ ` or `diff --git ` line, at an empty line once the declared counts are consumed, at an unrecognized line, or at a `---`/`+++` pair that `_is_file_header_boundary()` accepts

### Boundary Detection

The parser treats `diff --git ` as a new git-format file. It treats `--- ` followed by `+++ ` as a new plain-format file only when `_is_file_header_boundary()` accepts it: always once the current hunk has consumed its declared old and new counts, never while both sides still fit the hunk, and otherwise only when a hunk header follows the pair. Inside a hunk that still has room, the pair stays a deletion and an addition, so content such as SQL comments (`-- DROP TABLE`) is not taken for a header.

### Hunk Header Regex

```python
HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
```

Groups: (1) old_start, (2) old_count (optional, defaults to "1"), (3) new_start, (4) new_count (optional), (5) context text (function name, etc.).

## Models

`src/tongs/diff/models.py` defines four frozen dataclasses:

**DiffLine:**
- `old_lineno: int | None`: line number in old file (None for additions)
- `new_lineno: int | None`: line number in new file (None for deletions)
- `content: str`: line content (prefix character stripped)
- `line_type: LineType`: CONTEXT, ADDITION, DELETION, HUNK_HEADER, NO_NEWLINE

**DiffHunk:**
- `header: str`: raw `@@ ... @@` line
- `old_start`, `old_count`, `new_start`, `new_count`: from hunk header
- `lines: tuple[DiffLine, ...]`: parsed lines in this hunk
- `context_text: str`: function name from hunk header (after `@@`)

**DiffFile:**
- `old_path`, `new_path`: file paths (no `a/`/`b/` prefixes)
- `status: FileStatus`: MODIFIED, ADDED, DELETED, RENAMED
- `hunks: tuple[DiffHunk, ...]`
- `additions`, `deletions`: computed counts
- `is_binary: bool`
- `language: str`: detected from file extension
- `is_truncated`, `is_empty`, `is_mode_only`, `is_rename_only`,
  `is_unavailable`: explicit reasons a forge-described file has incomplete or
  absent content hunks. The states derived from a payload are mutually
  exclusive; a state the forge reports explicitly is passed through as reported,
  so an explicit `is_empty`, `too_large` or mode signal can still set two of
  these at once. No real GitHub or GitLab payload does, and the desktop badge
  map would render both rather than fail.
- `is_metadata_only`: derived property for binary, empty, mode-only,
  rename-only, or unavailable files

**SplitDiffRow:**
- `old: DiffLine | None`, `new: DiffLine | None`: independent references to
  the original `DiffLine` objects; a missing cell is `None`
- `old_anchor`, `new_anchor`: properties returning the respective `DiffLine`
  only when it is a comment-anchorable context/deletion or context/addition
  line with a line number, otherwise `None`

`src/tongs/diff/alignment.py:align_hunk()` pairs a hunk's lines into `SplitDiffRow` tuples for the terminal split view (`v`). It is a pure function covered by `tests/test_diff/test_alignment.py`.

## Language Detection

`_detect_language(path)` calls Pygments `get_lexer_for_filename(path)` and
returns `lexer.aliases[0]` (falling back to `lexer.name.lower()` if the lexer
has no aliases). It returns `""` for `/dev/null` or when Pygments raises for
an unrecognized path. There is no `LANGUAGE_MAP` dict and no project-defined
special-case branch for extensionless files; the supported language set is
whatever Pygments' own filename pattern matching recognizes, which changes
with the installed Pygments version rather than a fixed list maintained here.

Used by the terminal renderer to select Rich syntax highlighting.

## Known Edge Cases

1. **Empty lines in hunks:** treated as context lines (both counters increment). The `content` is empty string.
2. **SQL comments (`-- ...`):** inside a hunk that still has room on both sides, a `---`/`+++` pair stays a deletion and an addition; see Boundary Detection.
3. **No-newline marker:** `\ No newline at end of file` is preserved as `LineType.NO_NEWLINE` with both linenos as None.
4. **Binary files:** detected via `Binary files` line. DiffFile has `is_binary=True` and empty hunks.
5. **Renamed files without content change:** detected via `rename from`/`rename to` lines. DiffFile has `status=RENAMED` and may have empty hunks. From a forge change payload the same shape sets `is_rename_only`.
6. **Files with only metadata changes** (mode change, no content): results in DiffFile with empty hunks.
7. **Hunk count defaults:** `@@ -1 +1,3 @@` means old_count=1 (omitted comma means count of 1).

## Test Fixtures

Real diff files for testing are in `tests/fixtures/`:
- `builder_mr_3113.diff`: real MR diff from the builder project
- `fromager_pr_1258.diff`: real PR diff from fromager

Tests in `tests/test_diff/test_parser.py` cover both inline diff strings and fixture file parsing.

## Position Mapping

`src/tongs/diff/position.py` records where an inline comment is anchored in a diff, in a forge-agnostic form.

### DiffPosition

Frozen dataclass capturing enough information for any forge:

- `file: DiffFile`: the file this position belongs to
- `line: DiffLine`: the specific diff line
- `old_path`, `new_path`: file paths
- `old_line: int | None`, `new_line: int | None`: line numbers in old/new file
- `side: str`: `"LEFT"` (old file / deletions) or `"RIGHT"` (new file / additions and context)

### Factory

`position_from_diff_line(file, line)` creates a `DiffPosition` from a `DiffFile` and `DiffLine`. Side is determined by `LineType`: ADDITION -> RIGHT, DELETION -> LEFT, everything else -> RIGHT.

### From Position to Forge Payload

`DiffPosition` carries no forge format. `TUIServiceAdapter.post_inline_comment()` in `src/tongs/tui_services.py` turns it into a `DiffAnchor` (paths, the line on the chosen side, and the side), and `ReviewMutationService` in `src/tongs/services/review_mutations.py` passes the anchor fields to the forge client, which builds the GitLab `position` object or the GitHub review comment fields.

## Rendering Pipeline

Diff lines flow through this pipeline from parser to screen:

1. `convert_forge_changes()` (which uses `parse_diff()` for patch text) produces `list[DiffFile]`
2. `DiffContent.show_file()` calls `_render_current_file()`, which sends split mode (`v`) to `SplitDiffView.show_file()` and unified mode to `_show_diff()`
3. `_show_diff()` calls `DiffRenderer.render_lines(hunk)` for each hunk. `DiffRenderer` handles syntax highlighting (`rich.syntax.Syntax`), word-level diffs (`difflib.SequenceMatcher`) and context folding, and returns `list[tuple[DiffLine | None, Text]]` with foreground-only styling
4. Each `(DiffLine, Text)` pair becomes an `Option` in `DiffOptionList`
5. `DiffOptionList.render_line()`, and `SplitDiffColumn.render_line()` in split mode, add background colors (addition/deletion/selection) through `VisualStyle` BEFORE calling `_get_option_render()`. These are the only places backgrounds are set.

The foreground/background split is intentional: `Strip.apply_style()` cannot reliably override backgrounds due to Textual style priority, so backgrounds must be set in the VisualStyle before rendering.

## Suggestion Position Mapping

`src/tongs/views/suggestion.py:resolve_suggestion_position()` extends the position system for multi-line suggestions:

- **GitLab:** Range is encoded in the suggestion fence syntax (`suggestion:-0+N`). The anchor line is always the first new-side line. No `start_line`/`start_side` API params needed.
- **GitHub single-line:** Anchor is the first line. No extra params.
- **GitHub multi-line:** Anchor is the LAST new-side line (GitHub's `line` parameter). `start_line` is the first line's `new_lineno`, `start_side` is `"RIGHT"`.

The `create_inline_comment` ABC accepts optional `start_line`/`start_side` to support this.

## Comment Anchors in Gutter

The `DiffRenderer._gutter()` method renders a comment marker `*` in the gutter for lines that have discussions:
- Yellow bold `*` for lines with unresolved discussions
- Dim `*` for lines where all discussions are resolved

The gutter lookup uses a `comment_lines: dict[tuple[int | None, int | None], bool]` map (built by `_build_comment_lines()`), where the bool indicates whether all discussions at that position are resolved. The key is `(old_lineno, new_lineno)` matching the DiffLine's line numbers.

## Cross-Tab Navigation

`DiffPanel.jump_to_discussion(file_path, line, discussion_id)` supports cross-tab navigation from the Discussion tab to the Diff tab. When invoked:
1. Finds the file by matching `new_path` or `old_path` against the file list
2. Switches to the file via `_show_file(index)`
3. Picks `DiffSide.OLD` for a discussion anchored only on the old side, otherwise `DiffSide.NEW`
4. Calls `DiffContent.jump_to(line, side, discussion_id)`. In split mode this delegates to `SplitDiffView.jump_to()`. In unified mode it adds `discussion_id` to `DiffOptionList._expanded_threads`, re-renders through `_show_diff()`, and scrolls to the line on that side through `ol.highlighted` and `ol.scroll_to_highlight()`.

This enables the Discussion tab's `Enter` key (via `JumpToDiffDiscussion` message) to jump directly to the code location with the discussion expanded.

## Incomplete and metadata-only files

`TUIServiceAdapter.get_diff()` calls `convert_forge_changes()`. Its
`_convert_change()` keeps forge metadata authoritative for paths, status, and
aggregate counts, then classifies absent or incomplete patch text on each
`DiffFile`:

- `is_truncated` covers explicit truncation flags, incomplete hunks, aggregate
  count shortfalls, or positive change counts without a body.
- `is_empty` covers an explicitly empty patch, and an added or deleted file the
  forge reports with zero changed lines and no patch whose path resolves to a
  text lexer.
- `is_mode_only` covers a reported mode change without content hunks.
- `is_rename_only` covers a rename or copy the forge describes with no content
  change: an explicitly empty patch, or a withheld patch with zero reported
  lines and a path that resolves to a text lexer.
- `is_unavailable` is the conservative fallback when content is absent and the
  payload does not determine which of the states above applies.

Binary is independent of missing content; an absent patch alone is never binary
evidence.

GitHub's pull-request files endpoint sends the same payload (no `patch`,
`additions`/`deletions`/`changes` all zero) for a binary, empty, rename-only or
mode-only file, and for a binary file whether or not its bytes changed. For such
a payload the path is the only remaining signal, and it is read locally with no
extra request: a known binary suffix (`_BINARY_SUFFIXES`) reads as binary and is
settled first, a path that resolves to a text lexer licenses the rename-only and
empty readings, and a path that says neither leaves the file unavailable. Never
let a withheld patch produce a state that asserts there is no content change
unless the path carries that text signal, and never derive a state that
contradicts a flag the forge reported explicitly: derivations yield to reported
flags rather than replacing them. Mode-only is not derivable on
GitHub at all: no cheap endpoint carries file modes, so those files stay
unavailable.

The widget renders all of these files in the tree with paths and counts.
`placeholder_message()` in `src/tongs/widgets/split_diff.py` is the single
source of the one-line message for a file with no content hunks, shared by
`SplitDiffView` and `DiffContent` so both layouts say the same thing. It names
the state (`[Binary file]`, `[Empty file]`, `[File mode changed]`,
`[Renamed with no content change]`) and, for `is_unavailable`, says the forge
did not expose the state rather than inventing a size limit or an empty diff.
The desktop badge map in `desktop/src/renderer/features/diff/index.tsx` mirrors
these states, so a new state must be added to the model, the projection, the DTO
validator, the bridge type, the badge map and this widget together.

## Bulk Pygments Highlighting

`_build_highlight_map(file)` in `src/tongs/widgets/diff_panel.py` performs a single Pygments call per file rather than per-line. It concatenates all diff line contents, highlights the bulk string via `rich.syntax.Syntax`, then splits the highlighted `Text` back into per-line entries keyed by `id(DiffLine)`. The `DiffRenderer` receives this map and uses pre-highlighted text instead of re-highlighting each line individually.

The batch `add_options()` call on `DiffOptionList` replaces the previous per-line `add_option()` loop, reducing Textual widget overhead for large files.
