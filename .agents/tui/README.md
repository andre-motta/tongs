# TUI

## Textual Patterns

tongs uses Textual 1.0+ with these patterns:

- **Screens** for distinct views (inbox, repo list, MR detail). Pushed/popped via `app.push_screen()`/`app.pop_screen()`.
- **Workers** (`@work` decorator) for async operations. Each data-loading method gets its own named worker group with `exclusive=True` to cancel previous loads on refresh.
- **Reactive attributes** on `TongsApp` for shared state that persists across screen push/pop.
- **DataTable** for list views with row cursor, zebra stripes.
- **Tree** widget for hierarchical views (repo list grouped by namespace).
- **TabbedContent** for multi-tab views (inbox tabs: My Reviews, My MRs, All Open).
- **ComposeResult** for declarative widget layout via `compose()`.

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

## AppState Design

`src/tongs/state/app_state.py` contains:

- `MRFilter(state, author, search)` -- filter criteria for MR lists. `state` defaults to `"open"`.
- `ReviewDraft(repo_path, mr_number, verdict, body, inline_comments)` -- accumulates inline comments before submission (GitHub's batched review model).

These are regular (mutable) dataclasses because they represent in-progress state that changes during a session.

## InboxScreen

`src/tongs/views/inbox.py` is the default screen. Structure:

```
Header
TabbedContent (initial="reviews")
  TabPane "My Reviews" -> MRTable#reviews-table
  TabPane "My MRs"     -> MRTable#my-mrs-table
  TabPane "All Open"   -> MRTable#all-open-table
Footer
```

**MRTable** extends `DataTable`:
- `setup_columns()` adds CI, #, Title, Author, Repo, Updated columns
- `add_mr_row(mr, ascii_mode)` adds a row from `MRSummary`, stores the MR data keyed by `"{hostname}:{repo_path}:{number}"`
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

**"All Open" tab** uses `asyncio.Semaphore(max_parallel)` and `asyncio.gather()` to fetch MRs for all discovered repos concurrently, with per-host failure tracking.

## RepoListScreen

`src/tongs/views/repo_list.py` shows repos grouped by namespace in a `Tree` widget.

- Groups repos by `repo.namespace` (from primary remote's repo_path)
- Shows forge icons: `[blue]GL[/]` for GitLab, `[white]GH[/]` for GitHub
- Adds hostname suffix for non-default hosts when multiple instances exist
- `repo` object stored as `data` on tree leaf nodes
- Selecting a repo (Enter / `on_tree_node_selected`) pushes `MRListScreen(repo)`

## MRListScreen

`src/tongs/views/mr_list.py` shows open MRs for a single repository. Reuses `MRTable` from `inbox.py`.

Structure:

```
Header
MRTable#repo-mr-table
Footer
```

- Constructor takes a `Repo` object; `sub_title` set to `repo.display_name`
- `_load_mrs()` worker fetches MRs via `client.list_mrs(repo_path)`, populates the shared `MRTable`
- Selecting a row pushes `MRDetailScreen(mr)` via `on_data_table_row_selected`
- Handles repos without forge remotes (notifies, does not crash)
- Bindings: Esc/q = back, o = open in browser, Ctrl+R = refresh

## MRDetailScreen

`src/tongs/views/mr_detail.py` is the detail view for a single MR with tabbed interface.

Structure:

```
Header (sub_title = "!{number} {title}")
TabbedContent (initial="overview")
  TabPane "Overview"   -> VerticalScroll > MROverview (metadata) + Markdown (description)
  TabPane "Diff"       -> DiffPanel (split-pane diff viewer)
  TabPane "Commits"    -> VerticalScroll > Static (commit list)
  TabPane "Discussion" -> placeholder (Phase 4)
  TabPane "Pipeline"   -> placeholder (Phase 5)
Footer
```

**Constructor** takes an `MRSummary`. Stores `mr_detail: MRDetail | None` (populated after API call), `_diff_loaded: bool`, and `_commits_loaded: bool` flags.

**Scrollable overview (Phase 3):** The Overview tab wraps `MROverview` and a `Markdown` widget inside a `VerticalScroll` container. `MROverview` renders metadata (title, author, branches, CI, etc.) as Rich markup. The MR description is rendered via the Textual `Markdown` widget (supports headings, links, code blocks, etc.) rather than plain text. `TongsApp` CSS sets `VerticalScroll { height: 1fr; }` to allow scrolling long descriptions.

**Lazy loading pattern:** Both the Diff and Commits tabs use the same lazy-load approach. `_on_tab_switch()` checks per-tab boolean flags (`_diff_loaded`, `_commits_loaded`); on first switch, sets the flag and calls the corresponding worker method. This avoids unnecessary API calls for tabs the user may never view.

**Commits tab (Phase 3):** `_load_commits()` worker calls `client.list_mr_commits()` and renders each commit as a Rich-formatted line in a `Static` widget: yellow short SHA, title, dimmed author. Multi-line commit messages are shown indented beneath the title line. The tab is wrapped in a `VerticalScroll` for long commit histories.

**Overview loading:** `_load_detail()` worker runs on mount, fetches `MRDetail` from the forge API, populates `MROverview` with metadata, and updates the `Markdown` widget with the MR description.

**Diff loading:** `_load_diff()` worker fetches changes via `client.get_mr_diff()`, converts the changes list to unified diff text via `_changes_to_diff_text()`, parses with `parse_diff()`, and passes files to `DiffPanel.set_files()`.

**Tab switching:** both number keys (1-5) and clicking tabs work. `action_focus_tab()` sets `TabbedContent.active` and calls `_on_tab_switch()`. `on_tabbed_content_tab_activated()` handles click-based tab switches.

**Refresh:** `action_refresh()` resets both `_diff_loaded = False` and `_commits_loaded = False`, then re-runs `_load_detail()`. Re-entering the Diff or Commits tab triggers a fresh fetch.

Bindings: Esc/q = back, 1-5 = focus tab, A = approve, U = unapprove, M = merge, X = close, o = open in browser, Ctrl+Y = copy URL to clipboard, Ctrl+R = refresh.

## MR Actions (MRDetailScreen)

`MRDetailScreen` supports four MR mutation actions: Approve (A), Unapprove (U), Merge (M), and Close (X). All use uppercase bindings, following the convention that uppercase keys indicate mutating operations.

**Double-press confirmation pattern:** Every action requires pressing the key twice. The first press sets `_pending_action` to the action name and shows a warning notification (e.g., "Approve !42? Press A again to confirm."). The second press checks that `_pending_action` matches, clears it, and executes the action via a `@work(exclusive=True, group="mr-action")` async method. This prevents accidental mutations without a modal dialog.

**State guards:** All four actions call `_check_open()` before proceeding. If the MR state is not `MRState.OPEN` (i.e., already merged or closed), the action is blocked with a "MR is no longer open" warning notification. This prevents impossible operations like merging an already-merged MR.

**Unapprove capability check:** `_do_unapprove()` checks `client.supports_unapprove` before calling the API. If the forge does not support it (GitHub), a "not supported on this forge" warning is shown and the operation is skipped.

**Auto-refresh after actions:** On success, each `_do_*()` method sets `_action_taken = True` and calls `_load_detail()` to re-fetch the MR from the API. This updates the overview metadata (approvals list, merge readiness, state) immediately after the action completes.

**Parent screen cache invalidation:** `action_go_back()` checks whether the parent screen (one level up in `app.screen_stack`) has a `_loaded_tabs` attribute. If so, it calls `_loaded_tabs.clear()` before popping. This forces the parent screen (InboxScreen or MRListScreen) to re-fetch tab data on return, ensuring the MR list reflects any state changes made in the detail view.

**Merge readiness indicator:** `_merge_readiness(mr)` in the overview computes a readiness status from MR metadata. It returns `[green]ready[/]` when no blockers exist, or `[yellow]blocked[/]` with a comma-separated list of reasons. Blockers checked: `is_draft`, `has_conflicts`, CI status (`FAILED` or `RUNNING`), and GitLab's `detailed_merge_status` (when not `"mergeable"` or `"can_be_merged"`).

## DiffPanel Widget

`src/tongs/widgets/diff_panel.py` is a split-pane diff viewer widget used inside `MRDetailScreen`.

Layout:

```
Horizontal
  DiffFileTree (width: 35, border-right)  |  DiffContent (width: 1fr)
```

**DiffFileTree** extends `Tree`. `set_files()` populates the tree with file entries showing status icon (M/A/D/R color-coded), path, and +/- stats. File index stored as `data` on leaf nodes.

**DiffContent** extends `VerticalScroll`. `show_file()` renders a single file's diff: file header with stats, then hunk headers and diff lines. `show_placeholder()` for loading/empty states.

**DiffPanel** extends `Widget`. Composes `DiffFileTree` + `DiffContent` in a `Horizontal` container. `set_files()` populates the tree and auto-selects the first file. `on_tree_node_selected()` switches the content pane when a file is clicked.

**Diff line rendering** (`_render_diff_line`): additions get green-on-dark-green, deletions get red-on-dark-red, context lines are plain, no-newline markers are dimmed. Gutter shows old and new line numbers (4-char wide each).

Bindings: n = next file, Shift+N = previous file (wraps around).

## Keybinding Conventions

- Lowercase = view/navigate. Uppercase = mutate (with confirmation)
- `Ctrl+R` = refresh everywhere
- `Ctrl+Y` = copy URL to clipboard (MRDetailScreen)
- `Ctrl+P` = command palette (planned)
- `?` = contextual help
- `j/k` = navigate in lists/trees
- `q` / `Esc` = back/quit
- `o` = open in browser
- `A/U/M/X` = approve/unapprove/merge/close (MRDetailScreen, double-press to confirm)
- `/` = search (planned)

Current bindings are defined as `BINDINGS` lists on each Screen class. Format: `Binding(key, action_name, description, show=True/False)`.

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
InboxScreen (default)
  |-- select MR row --> MRDetailScreen(mr_summary)
  |-- R key ----------> RepoListScreen
                           |-- select repo --> MRListScreen(repo)
                                                 |-- select MR row --> MRDetailScreen(mr_summary)
```

All forward navigation uses `app.push_screen()`. Back navigation uses `app.pop_screen()` (Esc/q). State is passed via constructor arguments (MRSummary, Repo), not reactive app-level attributes.

## Planned Views (Phase 4+)

- `CommentEditor` -- inline TextArea + optional external editor
- `PipelineListScreen` / `PipelineDetailScreen` / `JobLogScreen`
- Discussion tab content for MRDetailScreen (Phase 4)
- Pipeline tab content for MRDetailScreen (Phase 5)
