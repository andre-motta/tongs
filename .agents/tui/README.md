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

**Lazy loading:** tabs are loaded on first focus, not on mount. `_loaded_tabs` set tracks which tabs have been loaded. `action_refresh()` discards the current tab from the set and reloads.

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

Key patterns: `exclusive=True` cancels previous loads; `NotImplementedError` is caught and silently skipped (for unimplemented forges like GitHub); other exceptions surface as dim warnings; `table.loading` shows Textual's built-in loading indicator.

**"All Open" tab** uses `asyncio.Semaphore(max_parallel)` and `asyncio.gather()` to fetch MRs for all discovered repos concurrently, with per-host failure tracking.

## RepoListScreen

`src/tongs/views/repo_list.py` shows repos grouped by namespace in a `Tree` widget.

- Groups repos by `repo.namespace` (from primary remote's repo_path)
- Shows forge icons: `[blue]GL[/]` for GitLab, `[white]GH[/]` for GitHub
- Adds hostname suffix for non-default hosts when multiple instances exist
- `repo` object stored as `data` on tree leaf nodes

## Keybinding Conventions

- Lowercase = view/navigate. Uppercase = mutate (with confirmation)
- `Ctrl+R` = refresh everywhere
- `Ctrl+P` = command palette (planned)
- `?` = contextual help
- `j/k` = navigate in lists/trees
- `q` / `Esc` = back/quit
- `o` = open in browser
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

## Planned Views (Phase 2+)

- `MRDetailScreen` -- tabbed: Overview, Diff, Discussion, Pipeline
- `DiffView` -- file tree + diff content split pane
- `CommentEditor` -- inline TextArea + optional external editor
- `PipelineListScreen` / `PipelineDetailScreen` / `JobLogScreen`
