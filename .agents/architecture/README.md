# Architecture

## Layering

The application has four layers. Each depends only on the layer below it.

```
views/widgets  (Textual screens, UI logic)
    |
plugins        (TongsPlugin ABC, PluginRegistry, entry point discovery)
    |
state          (reactive AppState, MRFilter, ReviewDraft)
    |
forges         (ForgeClient ABC, models, registry, HTTP transport)
    |
cache          (CacheStore, async SQLite, TTL + LRU)
    |
scanner        (filesystem discovery, remote parsing, Repo dataclass)
```

The TUI layer never imports a concrete forge client. Everything goes through `ForgeRegistry` and the `ForgeClient` ABC. Views work against shared models from `forges/models.py`. The plugin layer sits alongside the TUI and can contribute commands and screens, but accesses forges only through `app.forge_registry`.

## Data Flow: API to TUI

1. `scanner/discovery.py:discover_repos()` walks the filesystem, finds `.git` dirs, reads remotes via `git remote -v` subprocess, returns `list[Repo]`.
2. `TongsApp._discover_repos()` runs discovery in a background thread (`@work(thread=True)`) to avoid blocking the UI.
3. `TongsApp.get_repo_hostnames()` extracts unique hostnames from discovered repos (not hardcoded).
4. `InboxScreen.load_reviews()` iterates hostnames, calls `ForgeRegistry.get_client(hostname)` which lazily creates an authenticated `ForgeClient`.
5. `ForgeRegistry.get_client()` resolves auth via `auth.py:resolve_token()` (CLI -> .netrc -> keyring cascade), creates an `httpx.AsyncClient` via `http.py:create_client()`, wraps it in a forge-specific client (`GitLabClient` or `GitHubClient`, selected by `ForgeType`), then wraps the client in `CachedForgeClient` if a cache is configured.
6. The forge client makes API calls using `http.py:request()`, which maps HTTP errors to `ForgeError` subclasses.
7. Forge client methods return shared dataclasses (`MRSummary`, `MRDetail`, `Commit`, etc.) to the view layer.
8. Views populate Textual widgets (DataTable, Tree, Static) with the returned data.

## Data Flow: MR Detail and Diff Rendering

1. User selects an MR row in `InboxScreen` (global or scoped). The `MRSummary` is passed to `MRDetailScreen(mr_summary)` via `push_screen()`.
2. On mount, `_load_detail()` worker fetches `MRDetail` from the forge API via `ForgeRegistry.get_client()` and `client.get_mr()`.
3. `MROverview` renders metadata (title, author, branches, CI, approvals, reviewers, labels, description).
4. When the user switches to the Diff tab (lazy, not on mount), `_load_diff()` calls `client.get_mr_diff()` which returns the forge's changes list.
5. `_changes_to_diff_text()` converts the changes list to unified diff text (prepends `---`/`+++` headers when missing).
6. `diff/parser.py:parse_diff()` parses the text into `list[DiffFile]` (shared model layer).
7. `DiffPanel.set_files()` populates `DiffFileTree` and renders the first file in `DiffContent`.
8. For inline commenting, `diff/position.py:position_from_diff_line()` creates a `DiffPosition`, then `to_forge_position()` converts it to the forge-specific API format.

This path keeps the view layer working against shared models (`DiffFile`, `DiffPosition`) without knowing which forge produced the data.

## Data Flow: Inline Suggestions (Phase 4)

1. User selects one or more lines in `DiffOptionList` (Shift+J/K or Ctrl+Click) and presses F3.
2. `DiffOptionList.action_suggest()` posts `CommentRequested` with `CommentMode.SUGGEST` and `context_lines` from the selection.
3. `MRDetailScreen.on_comment_requested()` receives the message and invokes the suggestion flow using pure helpers from `views/suggestion.py`.
4. `extract_new_side_lines()` filters out deletion lines from the selection.
5. `build_suggestion_template()` creates the editor template with original code below a separator.
6. The user edits the code in the CommentEditor (or external editor via F2).
7. `parse_suggestion_template()` splits the edited text into comment and suggested code.
8. `format_suggestion_block()` wraps the code in a forge-specific suggestion fence (GitLab: `suggestion:-0+N`, GitHub: `suggestion`).
9. `resolve_suggestion_position()` determines the anchor line and optional `start_line`/`start_side` for multi-line ranges.
10. `client.create_inline_comment()` posts the comment with the suggestion block.

The suggestion helpers are pure functions in `views/suggestion.py` with no I/O or Textual dependencies. Forge-specific differences are handled by `forge_type` parameters, not by conditional logic in the TUI layer.

## Data Flow: Inline Discussion Threading (Phase 4)

1. When the user switches to the Diff tab, `_load_diff()` calls `_fetch_diff_and_discussions(client)`.
2. `_fetch_diff_and_discussions()` fires `get_mr_diff()` and `get_mr_discussions()` as parallel `asyncio.Task`s. Discussion fetch failures are caught silently (diff still loads).
3. `DiffPanel.set_files(files, discussions)` distributes discussions by file path into `_discussions_by_file` and passes per-file lists to `DiffFileTree` (for comment counts) and `DiffContent` (for inline threading).
4. `_build_discussion_index()` indexes inline discussions by `(old_line, new_line)` for O(1) lookup during rendering.
5. `_build_comment_lines()` creates the gutter marker map `(old, new) -> all_resolved`.
6. During `DiffContent._show_diff()`, `_match_discussions(dl, index)` attaches discussions to option indices in `_comment_map`. If discussions are expanded (`_expanded_threads` set), `_render_thread_block()` inserts disabled options below the code line with Rich Markdown rendering of comment bodies, author, timestamp, and replies.
7. User interactions (`d` to toggle, `r` to reply, `R` to resolve) post messages (`_ThreadToggled`, `ReplyRequested`, `ResolveRequested`) that bubble up to `MRDetailScreen`.
8. `MRDetailScreen` handles mutations via forge client (`reply_to_discussion`, `resolve_discussion`), then resets `_diff_loaded = False` and re-enters the diff tab to refresh.

The discussion data flows through the same shared models (`Discussion`, `InlineComment`) from `forges/models.py`, keeping the view layer forge-agnostic.

## Data Flow: Discussion Tab (Phase 4)

1. When the user switches to the Discussion tab, `_on_tab_switch("discussion")` triggers `_load_discussions()` (lazy, same pattern as diff).
2. `_load_discussions()` fetches discussions via `client.get_mr_discussions()`.
3. If `_cached_diff_files` is not yet populated (Diff tab not visited), it fetches and parses the diff to build the cache. If the Diff tab was already loaded, the cache is reused without a second API call.
4. `DiscussionPanel.set_discussions(discussions, diff_files)` receives both the discussion list and cached diff files.
5. `_render_cards()` sorts discussions (unresolved first, then by file path and line number), applies the current filter (all/unresolved/resolved), and builds `DiscussionCard` widgets inside a `VerticalScroll`.
6. For each inline discussion, `render_diff_snippet(file, target_line)` extracts context lines from the matching `DiffFile` and renders them with syntax highlighting via `DiffRenderer._render_line()`.
7. `_render_thread(disc)` renders each comment body as Rich Markdown with author and timestamp.
8. Cross-tab navigation: pressing Enter on an inline `DiscussionCard` posts `JumpToDiffDiscussion`. `MRDetailScreen` handles it by switching to the Diff tab and calling `DiffPanel.jump_to_discussion()`, which selects the file, expands the thread, and scrolls to the target line.

## Data Flow: Pipeline/CI Management (Phase 5)

1. When the user switches to the Pipeline tab in `MRDetailScreen`, `_on_tab_switch("pipeline")` triggers `_load_pipelines()` (lazy, same pattern as diff/commits/discussions).
2. `_load_pipelines()` calls `client.list_mr_pipelines(repo_path, number)` to get MR-scoped pipelines.
3. `PipelinePanel.set_pipelines(pipelines)` renders `PipelineCard` widgets in a `VerticalScroll`.
4. User drills in (Enter or click): posts `LoadJobsRequested`. `MRDetailScreen._load_pipeline_jobs()` fetches `client.get_pipeline_jobs()` and calls `panel.set_jobs(jobs, pipeline)`.
5. `set_jobs()` groups jobs by `stage` and renders `JobCard` widgets.
6. User drills into a job (Enter or click): posts `LoadJobLogRequested`. `MRDetailScreen._load_job_log()` fetches `client.get_job_log()` and calls `panel.set_job_log()`.
7. `set_job_log()` renders the log in a `RichLog` widget. Each line uses `Text.from_ansi()` to parse ANSI escape codes (CI color output), prepended with a dim line number.
8. Cancel/Retry: `PipelinePanel` posts `CancelPipelineRequested`, `RetryPipelineRequested`, `CancelJobRequested`, or `RetryJobRequested`. `MRDetailScreen` handles each with a `@work` method that calls the appropriate forge client method, then reloads the pipeline tab.

The pipeline data flows through shared models (`Pipeline`, `PipelineJob`) from `forges/models.py`. The drill-down state (`_view_level`, `_saved_pipeline_idx`, `_saved_job_idx`) is local to `PipelinePanel`.

## Data Flow: Plugin System (Phase 6)

1. `TongsApp.__init__()` creates a `PluginRegistry()` instance as `self.plugin_registry`.
2. `TongsApp.on_mount()` calls `plugin_registry.discover(config.plugin_config)`.
3. `PluginRegistry.discover()` reads `importlib.metadata.entry_points(group="tongs.plugins")`, filters by config (plugins can be disabled via `[plugins.NAME] enabled = false` in TOML), loads each entry point, validates it is a `TongsPlugin` subclass, and appends to the internal list. Failures are logged and skipped (graceful degradation).
4. After discovery, `on_mount()` calls `plugin_registry.on_app_ready(app)`, which creates a `PluginContext(app)` security facade and passes it to each plugin's `on_app_ready()` lifecycle hook. Plugins receive the restricted `PluginContext` instead of the raw `TongsApp` instance.
5. During command palette discovery, `TongsCommandProvider._global_commands()` calls `app.plugin_registry.get_all_commands()`, which aggregates `(display, help_text, callback)` tuples from all loaded plugins. These are merged into the command palette alongside built-in commands.
6. On app exit, `TongsApp.on_unmount()` calls `plugin_registry.on_app_shutdown(app)` before closing forge clients and cache.

The plugin system uses Python entry points (`tongs.plugins` group in `pyproject.toml`) for discovery. The MCPPlugin is the first-party reference implementation, registered as `mcp = "tongs.mcp.plugin:MCPPlugin"`.

## Data Flow: Cache (Phase 7)

1. `TongsApp.__init__()` creates a `CacheStore(max_size_mb=config.max_cache_size_mb)` as `self.cache`.
2. `TongsApp.on_mount()` calls `cache.open()`, which creates the SQLite database file with `0o600` permissions, enables WAL journal mode, and creates the `cache` table if needed.
3. `ForgeRegistry` receives the cache instance in its constructor and can use it for API response caching.
4. `CacheStore.get(key)` returns cached bytes if the key exists and has not expired (TTL check). On hit, it updates `created_at` to the current time (LRU touch).
5. `CacheStore.put(key, value, ttl)` inserts or replaces a cache entry. After each put, `_enforce_size_limit()` checks total size and evicts the oldest 25% of entries if the `max_size_bytes` limit is exceeded.
6. Keys with `_EXCLUDED_PREFIXES` (`"job_log:"`, `"stream_log:"`) are never cached (large, ephemeral data).
7. `CacheStore.invalidate_prefix(prefix)` supports bulk invalidation (e.g., all entries for a specific repo).
8. Convenience methods `get_json()`/`put_json()` handle JSON serialization.
9. `TongsApp.on_unmount()` calls `cache.close()` to close the SQLite connection.

Cache database location: `platformdirs.user_cache_dir("tongs") / "cache.db"`.

## Data Flow: MCP Server (Phase 7)

1. The MCP server runs as a separate process via `tongs-mcp` entry point (not inside the TUI).
2. On first tool call, `_get_registry()` lazily creates a `ForgeRegistry` from `load_config()`.
3. Each tool receives a `repo_path` in `hostname/owner/repo` format. `_parse_host_repo()` validates the format via regex (`_REPO_PATH_RE`) and splits into `(hostname, owner/repo)`.
4. Tools call `registry.get_client(hostname)` to get an authenticated `ForgeClient`, then invoke the corresponding forge method.
5. Six tools are exposed: `list_mrs`, `get_mr`, `get_mr_diff`, `post_comment`, `approve_mr`, `list_pipelines`.
6. Destructive actions (merge, close, reopen, cancel) are intentionally excluded as a security boundary.
7. The server uses `mcp.server.fastmcp.FastMCP` and runs via `mcp.run()` (stdio transport).

## Data Flow: CachedForgeClient (Phase 8)

`CachedForgeClient` in `src/tongs/cache/cached_client.py` wraps any `ForgeClient` with transparent SQLite caching. `ForgeRegistry.get_client()` wraps every forge client in `CachedForgeClient` when a cache is configured.

1. Read methods (`list_mrs`, `get_mr_diff`) check the cache first via `CacheStore.get_json()`. On hit, the cached JSON is deserialized and returned without making an API call.
2. On cache miss, the inner client's method is called, and the result is stored via `CacheStore.put_json()` with the appropriate TTL (`mr_list_ttl` for MR lists, `diff_ttl` for diffs).
3. Mutation methods (`approve_mr`, `unapprove_mr`, `merge_mr`, `close_mr`, `reopen_mr`) bypass the cache and delegate directly to the inner client. After the mutation completes, related cache entries are invalidated via `CacheStore.invalidate_prefix()`.
4. All other methods are forwarded to the inner client via `__getattr__` (transparent proxy).
5. Cache keys use the format `{hostname}:{repo_path}:{operation}[:params]`.

The wrapper preserves all capability properties (`supports_batched_review`, etc.) from the inner client via attribute forwarding.

## Diff Caching Between Tabs

`_cached_diff_files` on `MRDetailScreen` provides a shared cache for the parsed `list[DiffFile]` between the Diff and Discussion tabs. Whichever tab loads first populates it; the other reuses it. `action_refresh()` clears the cache. This avoids redundant diff API calls when reviewing discussions and also enables `render_diff_snippet()` to show code context in discussion cards.

## GitLab System Note Filtering

`GitLabClient.get_mr_discussions()` skips discussions whose root note has `system=True`. System notes are auto-generated by GitLab for events like label changes, assignee updates, and milestone changes. These are not user-authored discussions and would clutter both the Diff tab gutter markers and the Discussion tab cards.

## Data Flow: Commit Listing (Phase 3)

1. When the user switches to the Commits tab in `MRDetailScreen`, `_on_tab_switch("commits")` triggers `_load_commits()` (lazy, same pattern as diff loading).
2. `_load_commits()` calls `client.list_mr_commits(repo_path, number)` on the forge client.
3. Both `GitLabClient` and `GitHubClient` return `list[Commit]` using the shared `Commit` dataclass (forge-agnostic).
4. The view renders each commit as a Rich-formatted line in a `Static` widget (sha, title, author, optional body).

The `Commit` model is intentionally minimal (sha, short_sha, title, message, author, created_at, web_url) and lives in `forges/models.py` alongside the other shared models.

## Registry: GitHub Wiring (Phase 3)

`ForgeRegistry.get_client()` now instantiates `GitHubClient` for `ForgeType.GITHUB` hosts (previously raised `NotImplementedError`). The `InboxScreen` worker pattern that caught `NotImplementedError` for unimplemented forges is no longer needed for GitHub, though the catch remains as a safety net for any future forge backends.

## HTTP-First Architecture

All forge operations use `httpx.AsyncClient`, not subprocess calls to `glab`/`gh` per operation. Auth tokens are extracted from CLI credential stores once at startup via subprocess, then all subsequent operations use HTTP with Bearer auth.

Benefits:
- One subprocess per host at startup instead of one per operation
- Proper async I/O for concurrent requests
- Structured JSON responses without CLI output parsing
- Rate limit headers visible for auto-retry
- Testable via `httpx.MockTransport`

The HTTP layer lives in `src/tongs/forges/http.py`:

- `create_client(base_url, token, timeout)` -- returns configured `httpx.AsyncClient` with Bearer auth header
- `request(client, method, path, **kwargs)` -- makes request, maps errors, returns parsed JSON. On 429 (rate limit), waits `retry_after` seconds (from Retry-After header, defaults to 5s) and retries once via `_retried` flag
- `paginate(client, path, per_page, max_pages)` -- collects paginated GET results
- `map_http_error(response)` -- maps HTTP status codes to `ForgeError` subclasses

## Error Hierarchy

Defined in `src/tongs/errors.py`:

```
ForgeError (base)
  AuthError           -- 401, missing credentials -> "Run glab/gh auth login"
  RateLimitError      -- 429, has retry_after attribute -> auto-retry once in http.py:request()
  NetworkError        -- timeout, transport errors -> switch to offline mode
  NotFoundError       -- 404 -> "MR not found (may have been deleted)"
  ConflictError       -- 409 -> "Cannot merge: resolve conflicts first"
  ForgePermissionError -- 403 -> "Insufficient permissions"
  ConfigError         -- malformed config, invalid scan root
```

Each forge client maps HTTP status codes to these via `http.py:map_http_error()`. The UI has a single error handler pattern per type: views catch errors in `@work` methods and surface them as Textual notifications with appropriate severity.

## Scanner Design

`src/tongs/scanner/` handles local repo discovery:

- `discovery.py:discover_repos(root, max_depth)` -- recursive walk, finds `.git` dirs, skips symlinks and nested repos, reads remotes, picks primary remote (origin > upstream > first).
- `remote.py:parse_remote_url(name, url)` -- parses SSH (`ssh://`), SCP (`git@host:path`), and HTTPS URLs. Detects forge type from hostname (known hosts + `"gitlab"/"github"` substring heuristic). Strips credentials from URLs.
- `repo.py` -- `Repo(path, remotes, primary_remote)` and `Remote(name, url, hostname, repo_path, forge_type)` dataclasses. `ForgeType` enum (GITHUB, GITLAB).

Key design decisions:
- Symlinks are skipped to avoid infinite loops
- Nested repos (a `.git` inside another repo) are detected and skipped
- `_strip_userinfo()` removes embedded credentials from URLs before storing
- Forge detection uses explicit host lists plus substring fallback for self-hosted instances

## Config

`src/tongs/config.py` loads from `platformdirs.user_config_dir("tongs")/config.toml`. Returns `Config` dataclass with sensible defaults if file doesn't exist. Extra forge hosts are configured via `[hosts.*]` TOML sections.

Key properties:
- `Config.scan_root_path` -- expands `~` in scan_root
- `Config.extra_gitlab_hosts` / `Config.extra_github_hosts` -- frozensets derived from host configs
- Config is injected into `TongsApp.__init__()` (testable, no global state)

## State Management

Reactive state lives on `TongsApp` (Textual-idiomatic):

```python
class TongsApp(App):
    current_repo: reactive[Repo | None] = reactive(None)
    current_mr_number: reactive[int | None] = reactive(None)
    mr_filter: reactive[MRFilter] = reactive(MRFilter)
    pending_review: reactive[ReviewDraft | None] = reactive(None)
    offline: reactive[bool] = reactive(False)
```

`MRFilter` and `ReviewDraft` are defined in `src/tongs/state/app_state.py`. State persists across screen push/pop. `watch_*` methods react to changes.

Non-reactive instance attributes on `TongsApp`:
- `config: Config` -- loaded from TOML
- `cache: CacheStore` -- async SQLite cache for API responses
- `forge_registry: ForgeRegistry` -- authenticated forge clients (receives cache)
- `plugin_registry: PluginRegistry` -- discovered plugins (lifecycle managed)
- `repos: list[Repo]` -- populated by background discovery worker
