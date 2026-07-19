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
5. `ForgeRegistry.get_client()` resolves auth via `auth.py:resolve_token()`, creates an `httpx.AsyncClient` via `http.py:create_client()`, wraps it in a forge-specific client (e.g., `GitLabClient`).
6. The forge client makes API calls using `http.py:request()`, which maps HTTP errors to `ForgeError` subclasses.
7. Forge client methods return shared dataclasses (`MRSummary`, `MRDetail`, etc.) to the view layer.
8. Views populate Textual widgets (DataTable, Tree) with the returned data.

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
