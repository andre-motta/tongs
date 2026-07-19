# Architecture

## Layering

The application has four layers. Each depends only on the layer below it.

```
views/widgets  (Textual screens, UI logic)
    |
state          (reactive AppState, MRFilter, ReviewDraft)
    |
forges         (ForgeClient ABC, models, registry, HTTP transport)
    |
scanner        (filesystem discovery, remote parsing, Repo dataclass)
```

The TUI layer never imports a concrete forge client. Everything goes through `ForgeRegistry` and the `ForgeClient` ABC. Views work against shared models from `forges/models.py`.

## Data Flow: API to TUI

1. `scanner/discovery.py:discover_repos()` walks the filesystem, finds `.git` dirs, reads remotes via `git remote -v` subprocess, returns `list[Repo]`.
2. `TongsApp._discover_repos()` runs discovery in a background thread (`@work(thread=True)`) to avoid blocking the UI.
3. `TongsApp.get_repo_hostnames()` extracts unique hostnames from discovered repos (not hardcoded).
4. `InboxScreen.load_reviews()` iterates hostnames, calls `ForgeRegistry.get_client(hostname)` which lazily creates an authenticated `ForgeClient`.
5. `ForgeRegistry.get_client()` resolves auth via `auth.py:resolve_token()`, creates an `httpx.AsyncClient` via `http.py:create_client()`, wraps it in a forge-specific client (`GitLabClient` or `GitHubClient`, selected by `ForgeType`).
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
- `request(client, method, path, **kwargs)` -- makes request, maps errors, returns parsed JSON
- `paginate(client, path, per_page, max_pages)` -- collects paginated GET results
- `map_http_error(response)` -- maps HTTP status codes to `ForgeError` subclasses

## Error Hierarchy

Defined in `src/tongs/errors.py`:

```
ForgeError (base)
  AuthError           -- 401, missing credentials -> "Run glab/gh auth login"
  RateLimitError      -- 429, has retry_after attribute -> auto-retry with countdown
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
