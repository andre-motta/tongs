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
- `cache: CacheStore` -- async SQLite cache for API responses
- `forge_registry: ForgeRegistry` -- manages authenticated forge clients (receives cache)
- `plugin_registry: PluginRegistry` -- discovered plugins with lifecycle management
- `repos: list[Repo]` -- populated by background discovery worker
- Reactive state: `current_repo`, `current_mr_number`, `mr_filter`, `pending_review`, `offline`

Lifecycle:
1. `__init__()` loads config, creates `CacheStore`, `ForgeRegistry` (with cache), and `PluginRegistry`
2. `on_mount()` opens cache, discovers plugins via `plugin_registry.discover(config.plugin_config)`, fires `on_app_ready()` hooks, pushes InboxScreen, starts `_discover_repos()` worker
3. `_discover_repos()` runs in a background thread (`@work(thread=True)`) to avoid blocking the UI
4. `_on_discovery_complete()` triggers the current screen's `action_refresh()` if it has one
5. `on_unmount()` fires `plugin_registry.on_app_shutdown(app)`, closes all forge clients via `forge_registry.close_all()`, closes cache

Registered screens: `"inbox"` -> `InboxScreen`, `"repo_list"` -> `RepoListScreen`.

## Command Palette

`src/tongs/commands.py:TongsCommandProvider(Provider)` implements a context-aware command palette, registered on `TongsApp` via `COMMANDS = {TongsCommandProvider}` and invoked with `Ctrl+P`.

**Context-aware command generation:** `_get_commands()` inspects `type(screen).__name__` to determine the current screen and combines global commands with screen-specific commands. Each command is a `(display, help_text, callback)` tuple.

**Global commands** (always available): Repos, Inbox, Help, plus any commands registered by plugins via `app.plugin_registry.get_all_commands()` (e.g., MCPPlugin adds "Start MCP Server").

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
  TabPane "Discussion" -> Static (status bar) + DiscussionPanel (card-based discussion view)
  TabPane "Pipeline"   -> Static (status bar) + PipelinePanel (three-level drill-down)
CommentEditor (bottom-docked, hidden by default)
Footer
```

**Constructor** takes an `MRSummary`. Stores `mr_detail: MRDetail | None` (populated after API call), `_diff_loaded: bool`, `_commits_loaded: bool`, `_discussions_loaded: bool`, `_pipeline_loaded: bool` flags, and `_cached_diff_files: list | None` for sharing parsed diff files between the Diff and Discussion tabs.

**Scrollable overview (Phase 3):** The Overview tab wraps `MROverview` and a `Markdown` widget inside a `VerticalScroll` container. `MROverview` renders metadata (title, author, branches, CI, etc.) as Rich markup. The MR description is rendered via the Textual `Markdown` widget (supports headings, links, code blocks, etc.) rather than plain text. `TongsApp` CSS sets `VerticalScroll { height: 1fr; }` to allow scrolling long descriptions.

**Lazy loading pattern:** The Diff, Commits, Discussion, and Pipeline tabs all use the same lazy-load approach. `_on_tab_switch()` checks per-tab boolean flags (`_diff_loaded`, `_commits_loaded`, `_discussions_loaded`, `_pipeline_loaded`); on first switch, sets the flag and calls the corresponding worker method. This avoids unnecessary API calls for tabs the user may never view.

**Commits tab (Phase 3):** `_load_commits()` worker calls `client.list_mr_commits()` and renders each commit as a Rich-formatted line in a `Static` widget: yellow short SHA, title, dimmed author. Multi-line commit messages are shown indented beneath the title line. The tab is wrapped in a `VerticalScroll` for long commit histories.

**Overview loading:** `_load_detail()` worker runs on mount, fetches `MRDetail` from the forge API, populates `MROverview` with metadata, and updates the `Markdown` widget with the MR description.

**Diff loading with parallel discussions fetch (Phase 4):** `_load_diff()` worker calls `_fetch_diff_and_discussions(client)`, which uses `asyncio.create_task()` to fire `get_mr_diff()` and `get_mr_discussions()` concurrently. Discussion fetch failures are caught and silently produce an empty list, so the diff still displays. The results are passed to `DiffPanel.set_files(files, discussions)` which distributes discussions to the file tree and per-line comment maps.

**Tab switching:** both number keys (1-5) and clicking tabs work. `action_focus_tab()` sets `TabbedContent.active` and calls `_on_tab_switch()`. `on_tabbed_content_tab_activated()` handles click-based tab switches.

**Refresh:** `action_refresh()` resets `_diff_loaded = False`, `_commits_loaded = False`, `_discussions_loaded = False`, `_pipeline_loaded = False`, and `_cached_diff_files = None`, then re-runs `_load_detail()`. Re-entering the Diff, Commits, Discussion, or Pipeline tab triggers a fresh fetch.

**Discussion handling (Phase 4):** `MRDetailScreen` handles messages from both `DiffPanel` and `DiscussionPanel`:

From `DiffPanel`:
- `on_reply_requested(ReplyRequested)`: opens `CommentEditor.open_reply()` with the discussion ID, file, line, and author
- `on_reply_submitted(ReplySubmitted)`: posts the reply via `client.reply_to_discussion()` and reloads both diff and discussion tabs
- `on_resolve_requested(ResolveRequested)`: calls `client.resolve_discussion()` with the toggled resolved state and reloads both tabs

From `DiscussionPanel`:
- `on_jump_to_diff_discussion(JumpToDiffDiscussion)`: switches to the Diff tab and calls `DiffPanel.jump_to_discussion()` to navigate to the file, line, and expand the thread
- `on_discussion_reply_requested(DiscussionReplyRequested)`: opens `CommentEditor.open_reply()` for inline discussions (using dummy DiffFile/DiffLine objects) or `CommentEditor.open_reply_general()` for general discussions

Both `_post_reply()` and `_resolve_thread()` reset both `_diff_loaded = False` and `_discussions_loaded = False` to refresh both tabs after mutation.

**Pipeline handling (Phase 5):** `MRDetailScreen` handles messages from `PipelinePanel`:
- `on_load_jobs_requested(LoadJobsRequested)`: calls `_load_pipeline_jobs(pipeline)` worker which fetches jobs via `client.get_pipeline_jobs()` and calls `panel.set_jobs()`
- `on_load_job_log_requested(LoadJobLogRequested)`: calls `_load_job_log(job, pipeline)` worker which fetches log via `client.get_job_log()` and calls `panel.set_job_log()`
- `on_cancel_pipeline_requested(CancelPipelineRequested)`: calls `_do_cancel_pipeline()` then reloads the pipeline tab
- `on_retry_pipeline_requested(RetryPipelineRequested)`: calls `_do_retry_pipeline()` then reloads the pipeline tab
- `on_cancel_job_requested(CancelJobRequested)`: calls `_do_cancel_job()` via `client.cancel_job()`
- `on_retry_job_requested(RetryJobRequested)`: calls `_do_retry_job()` via `client.retry_job()`

All pipeline workers use `group="mr-pipelines"` with `exclusive=True`.

**Pipeline drill-in binding suppression:** `check_action()` returns `False` for MR-level actions (`add_comment`, `approve`, `unapprove`, `merge`, `close_mr`, `yank_url`) when `PipelinePanel._view_level > 0`, preventing accidental mutations while browsing jobs or logs.

**Pipeline Escape delegation:** `action_go_back()` checks if PipelinePanel is drilled in (`_view_level > 0`) and delegates to `panel.action_drill_out()` instead of popping the screen. This allows Escape to navigate back through pipeline levels before exiting the MR detail view.

**Diff caching:** `_cached_diff_files` on `MRDetailScreen` stores the parsed `list[DiffFile]` from the Diff tab loading. When the Discussion tab loads, it reuses this cache to build diff snippets on `DiscussionCard`s without re-fetching. If the Discussion tab is visited before the Diff tab, it fetches and parses the diff itself, populating the cache for later. `action_refresh()` clears the cache (`_cached_diff_files = None`).

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

**`jump_to_discussion(file_path, line, discussion_id)`** enables cross-tab navigation from the Discussion tab. It finds the file by matching `new_path` or `old_path`, switches to it via `_show_file()`, adds the `discussion_id` to `_expanded_threads` on the `DiffOptionList`, then re-renders the diff to show the expanded thread. It scrolls to the target line using `ol.highlighted` and `ol.scroll_to_highlight()`. Called by `MRDetailScreen.on_jump_to_diff_discussion()` after switching to the Diff tab.

Bindings: n = next file, Shift+N = previous file (wraps around), m = toggle markdown preview.

**CommentMode enum and CommentRequested message:**
- `CommentMode` enum: `COMMENT` (default), `SUGGEST` (suggestion mode)
- `CommentRequested` message fields: `file: DiffFile | None`, `line: DiffLine | None`, `mode: CommentMode`, `context_lines: list[DiffLine] | None`
- `context_lines` carries the multi-line selection for both comments and suggestions

## CommentEditor Widget

`src/tongs/widgets/comment_editor.py` is a bottom-docked comment editor for both general MR comments and inline diff comments.

**Bottom-dock pattern:** The widget uses `dock: bottom` CSS with `display: none` by default. Opening it sets `display = True`, which pushes content above upward while keeping it scrollable. Closing (submit or cancel) sets `display = False`. `max-height: 40%` prevents the editor from consuming the entire screen.

**Focus save/restore:** `_save_focus_and_open()` captures `self.app.focused` into `_previous_focus` before opening the editor. `_restore_focus()` refocuses the previously focused widget on close (submit or cancel). This ensures keyboard focus returns to the correct widget (DiffOptionList, DiscussionPanel, etc.) after editing.

**Four modes:**
- `open_general()` -- general MR comment. Header shows "Add comment".
- `open_inline(file, line)` -- inline comment on a specific diff line. Header shows the file path and line number. Computes a `DiffPosition` from the diff line via `position_from_diff_line()`.
- `open_reply(discussion_id, file, line, author)` -- reply to an existing inline discussion thread. Header shows "Reply to @author on file:line". Stores `_discussion_id` for routing. On submit, posts `ReplySubmitted(discussion_id, body)` instead of `CommentSubmitted`.
- `open_reply_general(discussion_id, author)` -- reply to a general (non-inline) discussion. Header shows "Reply to @author". No file or line context. Uses the same reply submission path.

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

## DiscussionPanel Widget

`src/tongs/widgets/discussion_list.py` is a card-based discussion panel for the Discussion tab. It replaces the earlier placeholder with a full interactive discussion view.

Layout:

```
DiscussionPanel (Widget, height: 1fr)
  VerticalScroll (#disc-scroll)
    DiscussionCard (Static, one per discussion)
    DiscussionCard ...
```

**DiscussionCard** extends `Static`. Each card renders a complete discussion thread as a single Rich `Text` block:
- **Header line:** resolution status marker (yellow `*` for unresolved, dim `[resolved]` for resolved), file path and line number (for inline) or `[general]` label, reply count, relative timestamp
- **Diff snippet:** rendered via `render_diff_snippet()` when the discussion is inline and a matching `DiffFile` is available. Shows 2 lines of context around the target line, with the target line marked with a yellow `>` prefix
- **Thread body:** full comment thread rendered via `_render_thread()` with Rich Markdown bodies, author names, timestamps, and indented replies
- **Action hints:** `[r] Reply`, `[R] Resolve/Unresolve`, `[enter] Jump to diff` (inline only)

CSS classes: `.focused` for the currently selected card (border highlight), `.resolved` for resolved discussions (dashed border).

**DiscussionPanel** extends `Widget`. Contains `VerticalScroll` with `DiscussionCard` children. Manages card focus, filtering, sorting, and keyboard navigation.

State:
- `_discussions: list[Discussion]` -- all discussions from the API
- `_diff_files: list[DiffFile]` -- cached diff files for rendering snippets
- `_filtered: list[Discussion]` -- discussions after applying the current filter
- `_filter: str` -- current filter mode: `"all"`, `"unresolved"`, `"resolved"`
- `_focused_index: reactive[int]` -- index of the currently focused card (triggers `watch__focused_index` for visual update)
- `_pending_resolve: str | None` -- double-press confirmation state for resolve

Sorting: `_sort_discussions()` orders by (1) unresolved first, (2) file path, (3) line number. General discussions sort last (file path set to `\xff`).

**`set_discussions(discussions, diff_files)`** -- called by `MRDetailScreen._load_discussions()`. Stores discussions and diff files, resets focus, calls `_render_cards()`.

**`render_diff_snippet(file, target_line, context=2)`** -- module-level helper that extracts diff lines around a target line number from a `DiffFile`. Uses `DiffRenderer._render_line()` for syntax-highlighted output. Returns `list[Text]` with the target line prefixed by `> ` in yellow.

**`_render_thread(disc)`** -- module-level helper that renders a full discussion thread using `rich.markdown.Markdown` and `rich.console.Console.render_lines()`. Root comment is shown bold, replies are indented with dim styling.

**Messages:**
- `JumpToDiffDiscussion(discussion_id, file_path, line)` -- posted on Enter key for inline discussions. Handled by `MRDetailScreen.on_jump_to_diff_discussion()` which switches to the Diff tab and calls `DiffPanel.jump_to_discussion()`.
- `DiscussionReplyRequested(discussion_id, file_path, line, author)` -- posted on `r` key. Handled by `MRDetailScreen.on_discussion_reply_requested()` which opens the `CommentEditor` in reply mode (inline or general).

**Bindings:** j/k = move cursor, Enter = jump to diff, r = reply, R = resolve (double-press), f = cycle filter (all/unresolved/resolved), `]`/`[` = next/prev unresolved (wraps around).

## PipelinePanel Widget

`src/tongs/widgets/pipeline_panel.py` is a three-level drill-down pipeline viewer used inside `MRDetailScreen`.

Layout:

```
PipelinePanel (Widget, can_focus=True, height: 1fr)
  VerticalScroll (#pipeline-list-scroll)    -- level 0: pipeline cards
  VerticalScroll (#job-list-scroll)         -- level 1: job cards grouped by stage
  Vertical (#job-log-container)             -- level 2: log viewer
    Static (#job-log-header)
    RichLog (#job-log-content, max_lines: 50000, wrap: False)
    Input (#log-search-input, dock: bottom, hidden by default)
```

Only one of the three containers is visible at a time, controlled by `_view_level` (0, 1, or 2).

**PipelineCard** extends `Static`. Renders pipeline ID, CI status icon (color-coded), relative timestamp, SHA, ref, source, and duration. CSS classes `.failed` (red border) and `.running` (yellow border) for visual distinction. Click posts `LoadJobsRequested`.

**JobCard** extends `Static`. Renders job name (bold if failed), CI status icon, duration, FAILED label, allow_failure flag. Click posts `LoadJobLogRequested`.

**Three-level drill-down:**
- Level 0 (pipelines): `set_pipelines()` renders `PipelineCard`s in `#pipeline-list-scroll`. Enter drills into jobs.
- Level 1 (jobs): `set_jobs()` renders `JobCard`s grouped by `stage` in `#job-list-scroll`. Enter drills into log.
- Level 2 (log): `set_job_log()` renders job log in a `RichLog` widget with line numbers and `Text.from_ansi()` for ANSI color parsing. Supports F2 editor and / search.

**Navigation state:** `_saved_pipeline_idx` and `_saved_job_idx` preserve cursor position when drilling out, so the user returns to the same pipeline/job they drilled into.

**Card focus management:** `_focused_index` reactive triggers `watch__focused_index()` which toggles the `.focused` CSS class on the old and new cards. `_render_gen` counter prevents stale ID collisions across re-renders.

**Cancel/Retry (double-press):** Same pattern as MR actions. `_pending_cancel`/`_pending_retry` track the pending item ID. First press shows confirmation notification, second press posts the message. State guards prevent canceling non-running or retrying non-failed items.

**Log search (/ key):** Shows the `#log-search-input` at the bottom. On submit, `_do_search()` strips ANSI from each line via `Text.from_ansi().plain` and finds case-insensitive matches. Scrolls `RichLog` to the first match and shows "Match 1/N: line M".

**Open in editor (F2):** Same pattern as CommentEditor F2. Creates a temp `.log` file, writes the raw log text, launches `$VISUAL`/`$EDITOR`/nvim/vim/vi/nano/less via `app.suspend()`, cleans up in `finally`.

**Open in browser (o):** Opens the focused pipeline or job `web_url` via `app.open_url()`.

**check_action guard:** `drill_out` action returns `False` when `_view_level == 0` (nothing to drill out of), allowing screen-level Escape to handle back navigation.

**Messages:**
- `CancelPipelineRequested(pipeline_id)` -- posted on C key at level 0 (double-press). Handled by `MRDetailScreen._do_cancel_pipeline()`.
- `RetryPipelineRequested(pipeline_id)` -- posted on R key at level 0 (double-press). Handled by `MRDetailScreen._do_retry_pipeline()`.
- `CancelJobRequested(job_id)` -- posted on C key at level 1 (double-press). Handled by `MRDetailScreen._do_cancel_job()`.
- `RetryJobRequested(job_id)` -- posted on R key at level 1 (double-press). Handled by `MRDetailScreen._do_retry_job()`.
- `LoadJobsRequested(pipeline)` -- posted on Enter at level 0 or PipelineCard click. Handled by `MRDetailScreen._load_pipeline_jobs()`.
- `LoadJobLogRequested(job, pipeline)` -- posted on Enter at level 1 or JobCard click. Handled by `MRDetailScreen._load_job_log()`.

Bindings: j/k = navigate cards, Enter = drill in, Escape = drill out, C = cancel (double-press), R = retry (double-press), o = open in browser, F2 = open log in editor (level 2 only), / = search log (level 2 only).

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

- `j/k` = navigate cards (DiscussionPanel)
- `Enter` = jump to diff from discussion card (DiscussionPanel, inline discussions only)
- `f` = cycle filter: all / unresolved / resolved (DiscussionPanel)

- `j/k` = navigate pipeline/job cards (PipelinePanel)
- `Enter` = drill into jobs (level 0) or log (level 1) (PipelinePanel)
- `Escape` = drill out one level (PipelinePanel, blocked at level 0)
- `C` = cancel pipeline (level 0) or job (level 1) (PipelinePanel, double-press)
- `R` = retry pipeline (level 0) or job (level 1) (PipelinePanel, double-press)
- `o` = open pipeline or job in browser (PipelinePanel)
- `F2` = open job log in external editor (PipelinePanel, level 2 only)
- `/` = search job log (PipelinePanel, level 2 only)

Current bindings are defined as `BINDINGS` lists on each Screen class. Format: `Binding(key, action_name, description, show=True/False)`.

**Footer cleanup (Phase 4):** Most MRDetailScreen bindings use `show=False` to keep the footer minimal. Only essential bindings (Back, Comment, Approve, Merge, Refresh) are visible by default. All actions are discoverable via `Ctrl+P` command palette. DiffOptionList bindings for comment navigation (`]`, `[`) and thread interaction (`d`, `r`, `R`) use `show=True` with `key_display` overrides for the bracket keys. DiscussionPanel bindings for reply, resolve, filter, and jump-to-diff use `show=True`.

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

## Plugin Integration (Phase 6)

Plugins can contribute commands and screens to the TUI:

- **Commands:** `TongsPlugin.get_commands()` returns `(display, help_text, callback)` tuples that are merged into the command palette's global commands section. Plugin commands appear alongside built-in commands in both `discover()` and `search()` modes.
- **Screens:** `TongsPlugin.get_screens()` returns `screen_name -> Screen class` mappings that could be registered on the app (currently collected but not auto-registered in `TongsApp.SCREENS`; plugins can register them in `on_app_ready()`).
- **Lifecycle:** `on_app_ready(app)` fires after mount (plugin can access `app.forge_registry`, `app.cache`, `app.repos`). `on_app_shutdown(app)` fires before exit.
- **Config filtering:** Plugins are enabled by default. Disable via `[plugins.NAME] enabled = false` in `config.toml`.
- **Graceful failure:** All plugin lifecycle calls and command/screen collection are wrapped in `try/except` with logging. A failing plugin does not crash the app.
