# TUI

## Textual Patterns

tongs uses Textual 1.0+ with these patterns:

- **Screens** for distinct views (inbox, repo list, MR detail). Pushed/popped via `app.push_screen()`/`app.pop_screen()`.
- **Workers** (`@work` decorator) for async operations. Each data-loading method gets its own named worker group with `exclusive=True` to cancel previous loads on refresh.
- **Reactive attributes** on `TongsApp` for shared state that persists across screen push/pop.
- **DataTable** for list views with row cursor, zebra stripes (inbox, repo list).
- **Tree** widget for hierarchical views (diff file tree with comment counts).
- **TabbedContent** for multi-tab views (inbox tabs: My Reviews, My MRs, All Open).
- **ComposeResult** for declarative widget layout via `compose()`.
- **CommandProvider** for context-aware command palette (`Ctrl+P`).

## TongsApp

`src/tongs/app.py:TongsApp(App)` is the main application class.

Key attributes:
- `config: Config` -- loaded from TOML, injected in constructor (testable)
- `forge_registry: ForgeRegistry` -- manages authenticated forge clients
- `repos: list[Repo]` -- populated by background discovery worker
- Reactive state: `current_repo`, `current_mr_number`, `mr_filter`, `pending_review`, `offline`

Lifecycle:
1. `__init__()` loads config and creates `ForgeRegistry`
2. `on_mount()` pushes InboxScreen and starts `_discover_repos()` worker
3. `_discover_repos()` runs in a background thread (`@work(thread=True)`) to avoid blocking the UI
4. `_on_discovery_complete()` triggers the current screen's `action_refresh()` if it has one
5. `on_unmount()` closes all forge clients via `forge_registry.close_all()`

Registered screens: `"inbox"` -> `InboxScreen`, `"repo_list"` -> `RepoListScreen`.

## Command Palette

`src/tongs/commands.py:TongsCommandProvider(Provider)` implements a context-aware command palette, registered on `TongsApp` via `COMMANDS = {TongsCommandProvider}` and invoked with `Ctrl+P`.

**Context-aware command generation:** `_get_commands()` inspects `type(screen).__name__` to determine the current screen and combines global commands with screen-specific commands. Each command is a `(display, help_text, callback)` tuple.

**Global commands** (always available): Repos, Inbox, Help.

**Screen-specific commands:**
- `InboxScreen`: My Reviews, My MRs, All Open (tab switches), Refresh, Open in Browser
- `RepoListScreen`: Filter Repos, Cycle Forge, Refresh
- `MRDetailScreen`: Overview/Diff/Commits/Discussion/Pipeline (tab switches), Comment, Approve, Unapprove, Merge, Close, Open in Browser, Copy URL, Refresh

**Two query modes:**
- `discover()` yields all commands as `DiscoveryHit` for the default palette view (no query typed)
- `search(query)` uses `self.matcher(query)` for fuzzy matching, returning `Hit` with highlighted match text and relevance score

## AppState Design

`src/tongs/state/app_state.py` contains:

- `MRFilter(state, author, search)` -- filter criteria for MR lists. `state` defaults to `"open"`.
- `ReviewDraft(repo_path, mr_number, verdict, body, inline_comments)` -- accumulates inline comments before submission (GitHub's batched review model).

These are regular (mutable) dataclasses because they represent in-progress state that changes during a session.

## InboxScreen

`src/tongs/views/inbox.py` is the default screen. Supports two modes: global (no repo) and scoped (single repo).

**Scoped mode:** Constructor accepts `repo: Repo | None`. When a repo is provided:
- `sub_title` is set to the repo's display name
- `show_repo=False` is passed to `MRTable.setup_columns()`, hiding the Repo column
- `_get_hostnames()` returns only the scoped repo's hostname
- `_filter_by_repo(mrs)` filters API results to match only the scoped repo's `repo_path`
- `load_all_open()` fetches MRs only for the scoped repo, not all discovered repos
- R key pops back to RepoListScreen instead of pushing it
- q pops back instead of exiting the app; Esc also pops back

Structure:

```
Header
TabbedContent (initial="reviews")
  TabPane "My Reviews" -> MRTable#reviews-table
  TabPane "My MRs"     -> MRTable#my-mrs-table
  TabPane "All Open"   -> MRTable#all-open-table
Footer
```

**MRTable** extends `DataTable`:
- `setup_columns(show_repo=True)` adds CI, #, Title, Author, Updated columns. When `show_repo=True` (default), also adds a Repo column. Scoped inbox passes `show_repo=False` to hide the redundant repo column.
- `add_mr_row(mr, ascii_mode)` adds a row from `MRSummary`, stores the MR data keyed by `"{hostname}:{repo_path}:{number}"`. Conditionally includes repo_path based on `_show_repo`.
- `get_selected_mr()` returns the `MRSummary` for the cursor row using `coordinate_to_cell_key()` and `RowKey.value`

**Lazy loading:** tabs are loaded on first focus, not on mount. `_loaded_tabs` set tracks which tabs have been loaded. `on_tabbed_content_tab_activated()` checks the set before loading. `action_refresh()` discards the current tab from the set and reloads.

**Discovery race fix:** `TongsApp._on_discovery_complete()` clears `_loaded_tabs` on the current screen so tab data reloads with the discovered repos. Without this, tabs loaded before discovery finishes would show stale (empty) results.

**Worker pattern for data loading:**

```python
@work(exclusive=True, group="reviews")
async def load_reviews(self) -> None:
    self.loading_reviews = True
    table = self.query_one("#reviews-table", MRTable)
    table.loading = True
    try:
        hostnames = self.app.get_repo_hostnames()
        registry = self.app.forge_registry
        for hostname in hostnames:
            try:
                client = await registry.get_client(hostname)
                mrs = await client.list_my_reviews()
                for mr in mrs:
                    table.add_mr_row(mr, self.app.config.ascii_mode)
            except NotImplementedError:
                pass  # Forge not yet implemented (GitHub)
            except Exception as exc:
                self.notify(f"[dim]Reviews {hostname}:[/] {exc}", severity="warning")
    finally:
        self.loading_reviews = False
        table.loading = False
```

Key patterns: `exclusive=True` cancels previous loads; `NotImplementedError` is caught and silently skipped (safety net for any future forge backends not yet implemented); other exceptions surface as dim warnings; `table.loading` shows Textual's built-in loading indicator.

**"All Open" tab** uses `asyncio.Semaphore(max_parallel)` and `asyncio.gather()` to fetch MRs concurrently, with per-host failure tracking. In global mode, fetches from all discovered repos. In scoped mode, fetches only for the single scoped repo.

## RepoListScreen

`src/tongs/views/repo_list.py` shows discovered repos in a searchable, filterable `DataTable`.

**Layout:**
```
Header
Input#repo-search (placeholder: "type to filter...")
Static#repo-status (count + active forge filter)
DataTable#repo-table (columns: F, Repository, [Host])
Footer
```

**Columns:** Forge type label (F), Repository display name, and an optional Host column shown only when more than two distinct hostnames exist among discovered repos.

**Live search (`/`):** Focuses the `Input` widget. Text changes update the `search_text` reactive, which triggers `_apply_filters()`. Pressing Enter returns focus to the table. Matching is case-insensitive substring on `repo.display_name`.

**Forge filter (`f`):** Cycles `forge_filter` reactive through `None` (all) -> `GitHub` -> `GitLab` -> `None`. Current filter shown in the status bar.

**Compound row keys:** Each row is keyed by `"{hostname}:{display_name}"`, stored in `_repo_data` dict mapping keys to `Repo` objects.

**Forge icons:** `[blue]GL[/]` for GitLab, `[white]GH[/]` for GitHub, `[dim]--[/]` for unknown.

**Navigation:** Selecting a repo (`on_data_table_row_selected`) pushes `InboxScreen(repo=repo)` -- the scoped inbox for that repository.

Bindings: Esc/q = back, `/` = search, f = cycle forge filter, Ctrl+R = refresh.

## MRDetailScreen

`src/tongs/views/mr_detail.py` is the detail view for a single MR with tabbed interface.

Structure:

```
Header (sub_title = "!{number} {title}")
TabbedContent (initial="overview")
  TabPane "Overview"   -> VerticalScroll > MROverview (metadata) + Markdown (description)
  TabPane "Diff"       -> DiffPanel (split-pane diff viewer with inline discussions)
  TabPane "Commits"    -> VerticalScroll > Static (commit list)
  TabPane "Discussion" -> placeholder (discussions are shown inline in the Diff tab)
  TabPane "Pipeline"   -> placeholder (Phase 5)
CommentEditor (bottom-docked, hidden by default)
Footer
```

**Constructor** takes an `MRSummary`. Stores `mr_detail: MRDetail | None` (populated after API call), `_diff_loaded: bool`, and `_commits_loaded: bool` flags.

**Scrollable overview (Phase 3):** The Overview tab wraps `MROverview` and a `Markdown` widget inside a `VerticalScroll` container. `MROverview` renders metadata (title, author, branches, CI, etc.) as Rich markup. The MR description is rendered via the Textual `Markdown` widget (supports headings, links, code blocks, etc.) rather than plain text. `TongsApp` CSS sets `VerticalScroll { height: 1fr; }` to allow scrolling long descriptions.

**Lazy loading pattern:** Both the Diff and Commits tabs use the same lazy-load approach. `_on_tab_switch()` checks per-tab boolean flags (`_diff_loaded`, `_commits_loaded`); on first switch, sets the flag and calls the corresponding worker method. This avoids unnecessary API calls for tabs the user may never view.

**Commits tab (Phase 3):** `_load_commits()` worker calls `client.list_mr_commits()` and renders each commit as a Rich-formatted line in a `Static` widget: yellow short SHA, title, dimmed author. Multi-line commit messages are shown indented beneath the title line. The tab is wrapped in a `VerticalScroll` for long commit histories.

**Overview loading:** `_load_detail()` worker runs on mount, fetches `MRDetail` from the forge API, populates `MROverview` with metadata, and updates the `Markdown` widget with the MR description.

**Diff loading with parallel discussions fetch (Phase 4):** `_load_diff()` worker calls `_fetch_diff_and_discussions(client)`, which uses `asyncio.create_task()` to fire `get_mr_diff()` and `get_mr_discussions()` concurrently. Discussion fetch failures are caught and silently produce an empty list, so the diff still displays. The results are passed to `DiffPanel.set_files(files, discussions)` which distributes discussions to the file tree and per-line comment maps.

**Tab switching:** both number keys (1-5) and clicking tabs work. `action_focus_tab()` sets `TabbedContent.active` and calls `_on_tab_switch()`. `on_tabbed_content_tab_activated()` handles click-based tab switches.

**Refresh:** `action_refresh()` resets both `_diff_loaded = False` and `_commits_loaded = False`, then re-runs `_load_detail()`. Re-entering the Diff or Commits tab triggers a fresh fetch.

**Discussion handling (Phase 4):** `MRDetailScreen` handles three new messages from `DiffPanel`:
- `on_reply_requested(ReplyRequested)`: opens `CommentEditor.open_reply()` with the discussion ID, file, line, and author
- `on_reply_submitted(ReplySubmitted)`: posts the reply via `client.reply_to_discussion()` and reloads the diff tab
- `on_resolve_requested(ResolveRequested)`: calls `client.resolve_discussion()` with the toggled resolved state and reloads the diff tab

Both `_post_reply()` and `_resolve_thread()` reset `_diff_loaded = False` and call `_on_tab_switch("diff")` to refresh discussions inline after mutation.

Bindings: Esc/q = back, 1-5 = focus tab, c = general comment, A = approve, U = unapprove, M = merge, X = close, o = open in browser, Ctrl+Y = copy URL to clipboard, Ctrl+R = refresh. Most bindings have `show=False` to reduce footer clutter; all are discoverable via `Ctrl+P` command palette.

## MR Actions (MRDetailScreen)

`MRDetailScreen` supports four MR mutation actions: Approve (A), Unapprove (U), Merge (M), and Close (X). All use uppercase bindings, following the convention that uppercase keys indicate mutating operations.

**Double-press confirmation pattern:** Every action requires pressing the key twice. The first press sets `_pending_action` to the action name and shows a warning notification (e.g., "Approve !42? Press A again to confirm."). The second press checks that `_pending_action` matches, clears it, and executes the action via a `@work(exclusive=True, group="mr-action")` async method. This prevents accidental mutations without a modal dialog.

**State guards:** All four actions call `_check_open()` before proceeding. If the MR state is not `MRState.OPEN` (i.e., already merged or closed), the action is blocked with a "MR is no longer open" warning notification. This prevents impossible operations like merging an already-merged MR.

**Unapprove capability check:** `_do_unapprove()` checks `client.supports_unapprove` before calling the API. If the forge does not support it (GitHub), a "not supported on this forge" warning is shown and the operation is skipped.

**Auto-refresh after actions:** On success, each `_do_*()` method sets `_action_taken = True` and calls `_load_detail()` to re-fetch the MR from the API. This updates the overview metadata (approvals list, merge readiness, state) immediately after the action completes.

**Parent screen cache invalidation:** `action_go_back()` checks whether the parent screen (one level up in `app.screen_stack`) has a `_loaded_tabs` attribute. If so, it calls `_loaded_tabs.clear()` before popping. This forces the parent InboxScreen (global or scoped) to re-fetch tab data on return, ensuring the MR list reflects any state changes made in the detail view.

**Merge readiness indicator:** `_merge_readiness(mr)` in the overview computes a readiness status from MR metadata. It returns `[green]ready[/]` when no blockers exist, or `[yellow]blocked[/]` with a comma-separated list of reasons. Blockers checked: `is_draft`, `has_conflicts`, CI status (`FAILED` or `RUNNING`), and GitLab's `detailed_merge_status` (when not `"mergeable"` or `"can_be_merged"`).

## DiffPanel Widget

`src/tongs/widgets/diff_panel.py` is a split-pane diff viewer widget used inside `MRDetailScreen`.

Layout:

```
Horizontal
  DiffFileTree (width: 35, border-right)  |  Vertical (#diff-right-pane)
                                               Static (#diff-file-header)
                                               DiffContent
                                                 DiffOptionList (#diff-option-list)
                                                 Markdown (#diff-markdown-preview, hidden)
```

**DiffFileTree** extends `Tree`. `set_files()` populates the tree with file entries showing status icon (M/A/D/R color-coded), basename-only label, and +/- stats as child leaf nodes. When discussions are present, child nodes also show comment counts: unresolved comments in yellow (e.g., "2 comments"), resolved comments dimmed (e.g., "3 resolved"). File index stored as `data` on non-leaf nodes. Each file node is auto-expanded.

**DiffOptionList** extends `OptionList`. Provides per-line cursor navigation, multi-line selection, and inline discussion threading for diff content.

Internal state:
- `_line_map: dict[int, DiffLine]` -- maps option index to DiffLine (only selectable lines)
- `_line_types: dict[int, LineType]` -- maps option index to LineType (for background coloring)
- `_current_file: DiffFile | None` -- currently displayed file
- `_selection_anchor: int | None` -- starting option index of a visual selection (None when no selection)
- `_comment_map: dict[int, list[Discussion]]` -- maps option index to discussions anchored at that line
- `_expanded_threads: set[str]` -- set of discussion IDs currently expanded inline
- `_comment_indices: list[int]` -- sorted list of option indices that have discussions (for `]`/`[` jumping)

Line-level background coloring via `render_line()` override:
- Injects bgcolor into Textual `VisualStyle` BEFORE calling `_get_option_render()`. This is critical because `Strip.apply_style()` does not work here due to style priority rules; the VisualStyle must be set before the render call.
- Class-level VisualStyle constants: `_ADDITION_BG` (dark green), `_DELETION_BG` (dark red), `_SELECTION_BG` (blue).
- Selection range overrides addition/deletion colors. Highlighted line is not recolored (uses default highlight style).

Multi-line selection:
- Shift+J (`action_extend_down`) / Shift+K (`action_extend_up`): extends selection from `_selection_anchor`
- Regular j/k movement clears the selection anchor
- Ctrl+Click (`_on_click` override): extends selection to clicked line
- Escape clears selection via `action_clear_selection`, guarded by `check_action()` which returns False when no selection is active, allowing screen-level Escape to pass through

Comment/suggest actions:
- `c` (`action_comment`): posts `CommentRequested` with `CommentMode.COMMENT`, includes `context_lines` from selection if active
- `F3` (`action_suggest`): posts `CommentRequested` with `CommentMode.SUGGEST`, blocks on deletion lines with notification
- Both clear the selection anchor after posting

Discussion interactions (Phase 4):
- `]` (`action_next_comment`): jump to next line with a discussion, wrapping around. Uses `bisect.bisect_right` on `_comment_indices` for O(log n) lookup
- `[` (`action_prev_comment`): jump to previous line with a discussion, wrapping around. Uses `bisect.bisect_left`
- `d` (`action_toggle_discussion`): expand or collapse all discussion threads on the current line. Posts `_ThreadToggled` message to trigger re-render preserving cursor position
- `r` (`action_reply_discussion`): posts `ReplyRequested` with the target discussion (first unresolved, or first if all resolved). Bubbles up to `MRDetailScreen` which opens `CommentEditor.open_reply()`
- `R` (`action_resolve_discussion`): double-press confirmation pattern via `_pending_resolve`. Posts `ResolveRequested` with the toggled resolved state. Checks `disc.resolvable` before proceeding

**New messages (Phase 4):**
- `ReplyRequested(discussion_id, file, line, author)` -- posted by `DiffOptionList`, handled by `MRDetailScreen`
- `ResolveRequested(discussion_id, resolved)` -- posted by `DiffOptionList`, handled by `MRDetailScreen`
- `_ThreadToggled()` -- internal to `DiffContent`, triggers `_show_diff()` re-render while preserving highlighted line
- `ReplySubmitted(discussion_id, body)` -- posted by `CommentEditor`, handled by `MRDetailScreen`

**Discussion plumbing in DiffContent:**
- `_build_discussion_index(discussions)` -- indexes inline discussions by `(old_line, new_line)` tuple for O(1) lookup during rendering
- `_build_comment_lines(discussions)` -- builds `(old_line, new_line) -> all_resolved` map for gutter markers (yellow `*` for unresolved, dim `*` for resolved)
- `_match_discussions(dl, index)` -- matches a DiffLine to discussions by trying exact `(old, new)` match, then `(None, new)`, then `(old, None)` fallbacks
- `_render_thread_block(discussions)` -- renders expanded discussion threads as disabled `Option` lines with Rich Markdown body rendering via `rich.markdown.Markdown` + `Console.render_lines()`. Shows author, relative timestamp, resolution status, replies indented

**DiffContent** extends `Widget` (not VerticalScroll). Composes `DiffOptionList` + `Markdown` preview widget. `show_file()` populates the option list and passes per-file discussions for inline threading; `show_placeholder()` for loading/empty states; `toggle_markdown_preview()` switches between diff and rendered markdown for `.md` files. Handles `_ThreadToggled` to re-render the diff with expanded/collapsed threads while preserving cursor position.

**DiffRenderer** handles syntax highlighting and word-level diffs:
- `render_lines(hunk)` returns `list[tuple[DiffLine | None, Text]]` where `None` indicates a fold marker (non-selectable)
- Context folding: runs of context lines longer than `CONTEXT_LINES * 2` (default 3) are collapsed to top-N, fold marker, bottom-N
- Word-level diffs: consecutive deletion+addition blocks are paired; `difflib.SequenceMatcher` highlights changed words with bold+underline
- Foreground-only styling in render methods; backgrounds come from `DiffOptionList.render_line()`

**DiffPanel** extends `Widget`. Composes `DiffFileTree` + `DiffContent` in a `Horizontal` container. `set_files(files, discussions)` distributes discussions by file path into `_discussions_by_file`, populates the tree (with comment counts), and auto-selects the first file. `on_tree_node_selected()` switches the content pane when a file is clicked, passing per-file discussions to `DiffContent.show_file()`.

Bindings: n = next file, Shift+N = previous file (wraps around), m = toggle markdown preview.

**CommentMode enum and CommentRequested message:**
- `CommentMode` enum: `COMMENT` (default), `SUGGEST` (suggestion mode)
- `CommentRequested` message fields: `file: DiffFile | None`, `line: DiffLine | None`, `mode: CommentMode`, `context_lines: list[DiffLine] | None`
- `context_lines` carries the multi-line selection for both comments and suggestions

## CommentEditor Widget

`src/tongs/widgets/comment_editor.py` is a bottom-docked comment editor for both general MR comments and inline diff comments.

**Bottom-dock pattern:** The widget uses `dock: bottom` CSS with `display: none` by default. Opening it sets `display = True`, which pushes content above upward while keeping it scrollable. Closing (submit or cancel) sets `display = False`. `max-height: 40%` prevents the editor from consuming the entire screen.

**Three modes:**
- `open_general()` -- general MR comment. Header shows "Add comment".
- `open_inline(file, line)` -- inline comment on a specific diff line. Header shows the file path and line number. Computes a `DiffPosition` from the diff line via `position_from_diff_line()`.
- `open_reply(discussion_id, file, line, author)` -- reply to an existing discussion thread. Header shows "Reply to @author on file:line". Stores `_discussion_id` for routing. On submit, posts `ReplySubmitted(discussion_id, body)` instead of `CommentSubmitted`.

**Message flow (decoupled communication):**

```
DiffOptionList           CommentEditor            MRDetailScreen
  |                           |                        |
  |-- CommentRequested ------>|                        |
  |   (file, line, mode,      |-- (bubbles up) ------->|
  |    context_lines)         |                        |-- on_comment_requested()
  |                           |                        |     opens editor (inline or general)
  |                           |                        |     if SUGGEST mode: opens suggestion flow
  |                           |                        |
  |                    (user types, submits)            |
  |                           |                        |
  |                           |-- CommentSubmitted --->|-- _post_inline_comment()
  |                           |   (inline mode)        |     client.create_inline_comment()
  |                           |                        |
  |                           |-- GeneralComment   --->|-- _post_general_comment()
  |                           |   Submitted            |     client.add_comment()
  |                           |                        |
  |-- ReplyRequested -------->|                        |
  |   (discussion_id,         |-- (bubbles up) ------->|-- on_reply_requested()
  |    file, line, author)    |                        |     opens editor (reply mode)
  |                           |                        |
  |                    (user types, submits)            |
  |                           |                        |
  |                           |-- ReplySubmitted ----->|-- _post_reply()
  |                           |   (discussion_id,      |     client.reply_to_discussion()
  |                           |    body)               |     reloads diff tab
  |                           |                        |
  |-- ResolveRequested ------>|                        |
  |   (discussion_id,         |-- (bubbles up) ------->|-- on_resolve_requested()
  |    resolved)              |                        |     _resolve_thread()
  |                           |                        |     client.resolve_discussion()
  |                           |                        |     reloads diff tab
```

`DiffOptionList` posts `CommentRequested` (with `file`, `line`, `mode`, and optional `context_lines` from multi-line selection). `MRDetailScreen.on_comment_requested()` receives it and opens the editor in the appropriate mode. For `CommentMode.SUGGEST`, the suggestion flow uses helpers from `views/suggestion.py` to build the template and format the forge-specific suggestion block. The editor posts `CommentSubmitted` (with `body` + `DiffPosition`) or `GeneralCommentSubmitted` (with `body`). `MRDetailScreen` handles both and posts via the forge client in a `@work(exclusive=True, group="mr-comment")` method.

The `c` key on `MRDetailScreen` opens the editor in general mode directly. The `c` key on `DiffOptionList` posts `CommentRequested` which bubbles up to the screen. The `F3` key on `DiffOptionList` posts `CommentRequested` with `CommentMode.SUGGEST`.

**External editor (F2):** Creates a temp file (`.md` suffix, `0o600` perms), writes current TextArea content, launches `$VISUAL` / `$EDITOR` / nvim / vim / vi / nano via `subprocess.run()` inside `app.suspend()`, then reads back the result. Temp file is cleaned up in a `finally` block. Not supported on Windows.

**Unsaved-changes guard:** `action_cancel()` checks whether the TextArea has non-empty content. First Esc sets `_cancel_pending = True` and shows a warning notification. Second Esc discards.

Bindings: Ctrl+S / Ctrl+J = submit (priority bindings), Esc = cancel (with guard), F2 = external editor.

## Keybinding Conventions

- Lowercase = view/navigate. Uppercase = mutate (with confirmation)
- `Ctrl+R` = refresh everywhere
- `Ctrl+Y` = copy URL to clipboard (MRDetailScreen)
- `Ctrl+P` = command palette (context-aware, searches all available actions)
- `?` = contextual help
- `j/k` = navigate in lists/trees
- `q` / `Esc` = back/quit
- `o` = open in browser
- `c` = add comment (MRDetailScreen: general, DiffOptionList: inline via CommentRequested)
- `F3` = suggest edit (DiffOptionList: suggestion via CommentRequested with SUGGEST mode)
- `Shift+J/K` = extend multi-line selection (DiffOptionList)
- `Ctrl+Click` = extend selection to clicked line (DiffOptionList)
- `Ctrl+S` / `Ctrl+J` = submit comment (CommentEditor, priority bindings)
- `F2` = open external editor (CommentEditor)
- `m` = toggle markdown preview (DiffPanel, for .md files)
- `A/U/M/X` = approve/unapprove/merge/close (MRDetailScreen, double-press to confirm)
- `/` = search (RepoListScreen: live filter)
- `f` = cycle forge filter (RepoListScreen: None -> GH -> GL -> None)
- `]` = jump to next comment (DiffOptionList, wraps around)
- `[` = jump to previous comment (DiffOptionList, wraps around)
- `d` = toggle discussion thread expand/collapse (DiffOptionList)
- `r` = reply to discussion (DiffOptionList, opens CommentEditor in reply mode)
- `R` = resolve/unresolve discussion (DiffOptionList, double-press to confirm)

Current bindings are defined as `BINDINGS` lists on each Screen class. Format: `Binding(key, action_name, description, show=True/False)`.

**Footer cleanup (Phase 4):** Most MRDetailScreen bindings use `show=False` to keep the footer minimal. Only essential bindings (Back, Comment, Approve, Merge, Refresh) are visible by default. All actions are discoverable via `Ctrl+P` command palette. DiffOptionList bindings for comment navigation (`]`, `[`) and thread interaction (`d`, `r`, `R`) use `show=True` with `key_display` overrides for the bracket keys.

## How to Add a New Screen

1. Create `src/tongs/views/your_screen.py` with a class extending `Screen`.
2. Define `BINDINGS` list for keybindings.
3. Implement `compose()` returning widgets (Header, Footer, content).
4. Add data-loading methods as `@work` async methods with `exclusive=True` and named groups.
5. Register in `TongsApp.SCREENS` dict in `src/tongs/app.py`.
6. Add navigation binding or action in the source screen to push your screen.

Pattern for screen navigation:
- Use `self.app.push_screen("screen_name")` to navigate forward
- Use `self.app.pop_screen()` to go back
- Set reactive state on `self.app` before pushing (e.g., `self.app.current_repo = repo`)

## ASCII Mode

All visual elements have ASCII fallbacks:
- CI status: `[OK]`, `[!!]`, `[..]`, `[--]`, `[??]` instead of Unicode circles/arrows
- Forge icons: `GL`, `GH` text instead of symbols
- Controlled by `self.app.config.ascii_mode`
- Checked at render time, not in data models

## Navigation Flow

```
InboxScreen (global, default)
  |-- select MR row --> MRDetailScreen(mr_summary)
  |-- R key ----------> RepoListScreen
                           |-- select repo --> InboxScreen(repo=repo)  [scoped]
                                                 |-- select MR row --> MRDetailScreen(mr_summary)
                                                 |-- R key ----------> pop back to RepoListScreen
```

All forward navigation uses `app.push_screen()`. Back navigation uses `app.pop_screen()` (Esc/q). State is passed via constructor arguments (MRSummary, Repo), not reactive app-level attributes. The scoped InboxScreen reuses the same class as the global one, differentiated by the `repo` constructor parameter.

## Suggestion Flow

`src/tongs/views/suggestion.py` contains pure helper functions for building and parsing suggestion comments. All functions are forge-agnostic or accept `forge_type` as a parameter, contain no I/O or Textual dependencies, and are trivially testable.

**Functions:**
- `build_suggestion_template(original_code)` -- builds editor template with comment area above a separator (`SUGGESTION_SEPARATOR`) and original code below
- `parse_suggestion_template(edited)` -- splits editor output into `(comment_text, suggested_code)` at the separator; strips instruction lines
- `compute_backtick_fence(code)` -- returns the minimum backtick fence (at least 3) that avoids conflicts with backtick runs in the code
- `format_suggestion_block(suggested_code, n_original, forge_type, comment_text)` -- formats the full comment body with the fence block:
  - GitLab: `` ```suggestion:-0+N `` (range encoded in fence syntax)
  - GitHub: `` ```suggestion `` (range via API params `start_line`/`start_side`)
- `extract_new_side_lines(lines)` -- filters to context + addition lines (no deletions)
- `resolve_suggestion_position(new_side_lines, forge_type)` -- returns `(anchor_line, start_line, start_side)`:
  - GitLab: anchor is always the first line; start_line/start_side are None (range in fence)
  - GitHub single-line: anchor is the first line; start_line/start_side are None
  - GitHub multi-line: anchor is the LAST line (GitHub's `line` param); start_line is the first line's `new_lineno`; start_side is `"RIGHT"`

## Planned Views (Phase 5+)

- `PipelineListScreen` / `PipelineDetailScreen` / `JobLogScreen`
- Pipeline tab content for MRDetailScreen
