# Security

## Auth Model

Token resolution cascade defined in `src/tongs/forges/auth.py:resolve_token()`:

1. **CLI credential store** (preferred)
   - GitLab: `glab auth token --hostname {host}`
   - GitHub: `gh auth token` (default host) or `gh auth token --hostname {host}` (enterprise)
   - Single subprocess call per host at startup, not per operation
   - `shell=False` always (list of args, never string)

2. **~/.netrc fallback**
   - Standard `netrc` stdlib parser
   - POSIX: enforces `0o600` permissions, raises `AuthError` if group/other readable
   - Windows: reads `_netrc`, no permission enforcement (warning only)

3. **System keyring** (optional fallback)
   - Uses the `keyring` package (optional dependency, imported with try/except)
   - Reads from `keyring.get_password("tongs", hostname)`
   - Silently returns None if keyring is not installed or lookup fails
   - Users store tokens via `keyring set tongs <hostname>`

4. **Error with instructions**
   - Raises `AuthError` with forge-specific setup command
   - Never silently fails or uses empty tokens

## Token Lifecycle

- Tokens are loaded lazily per-host on first API call via `ForgeRegistry.get_client()`
- Stored only in the `httpx.AsyncClient` session headers (`Authorization: Bearer {token}`)
- Never written to cache, config files, logs, or TUI display
- Never passed as CLI arguments (visible in process listing)
- Never stored in environment variables
- Clients are closed on app unmount via `ForgeRegistry.close_all()`

## Credential Redaction

`src/tongs/errors.py:redact_credentials(text)` removes token patterns from any text before display or logging.

The regex covers 13+ token formats:

```python
_TOKEN_PATTERNS = re.compile(
    r"("
    r"glpat-|gldt-|glcbt-\d*_?|glptt-|glft-|glsoat-|glimt-|gloas-"
    r"|ghp_|gho_|ghs_|ghu_|github_pat_"
    r"|Bearer\s+"
    r"|PRIVATE-TOKEN:\s*"
    r")[A-Za-z0-9_.\-]+"
)
```

This catches:
- GitLab: personal access tokens (`glpat-`), deploy tokens (`gldt-`), CI/CD build tokens (`glcbt-`), pipeline trigger tokens (`glptt-`), feed tokens (`glft-`), service account tokens (`glsoat-`), incoming mail tokens (`glimt-`), OAuth tokens (`gloas-`)
- GitHub: personal access tokens (`ghp_`), OAuth tokens (`gho_`), server tokens (`ghs_`), user tokens (`ghu_`), fine-grained PATs (`github_pat_`)
- Generic: Bearer tokens, PRIVATE-TOKEN headers

Redaction is applied in:
- `http.py:_safe_body()` on error response bodies
- `http.py:request()` on timeout/transport error messages
- `gitlab.py:get_job_log()` on error messages

**Rule:** Every code path that surfaces an error message to the user or writes to logs must pass through `redact_credentials()`. When adding new error paths, always wrap the message.

## Subprocess Safety

- **Never use `shell=True`**. All subprocess calls use a list of arguments.
- All subprocess calls are in two places:
  - `forges/auth.py:_token_from_cli()` -- runs `glab`/`gh` auth commands
  - `scanner/discovery.py:_read_remotes()` -- runs `git remote -v`
- Both use `capture_output=True`, `text=True`, `timeout=5`
- Both catch `FileNotFoundError` (command not found) and `TimeoutExpired`
- Environment is not modified; subprocess inherits the standard environment

## SSRF Prevention (Phase 3)

`GitHubClient._repo_path_from_api_url(api_url)` validates that API URLs from search results are scoped to the client's own `base_url` before constructing follow-up requests. The method checks that `api_url` starts with `self._http.base_url + "/repos/"` and returns an empty string (skipping the result) if it does not match. This prevents a crafted search response from redirecting the client to an arbitrary host.

This is particularly important for the `_search_prs()` method, which processes `repository_url` values from GitHub's `/search/issues` endpoint. Without validation, a malicious or manipulated response could cause the client to send authenticated requests (with the user's Bearer token) to an attacker-controlled URL.

**Rule:** Any method that constructs API URLs from data returned by a previous API call must validate the URL against the client's known `base_url` before making the request.

## Cross-Fork Branch Deletion Safety (Phase 3)

`GitHubClient.merge_mr()` includes a safety check before deleting the head branch after merge. It verifies that `head.repo.full_name` matches the target `repo_path`. This prevents accidental branch deletion on fork repositories when merging cross-fork PRs, where the head branch belongs to a different user's fork.

```python
# Only delete the branch if it belongs to the target repo, not a fork
if branch and head_repo == repo_path:
    await request(self._http, "DELETE", f"/repos/{owner}/{repo}/git/refs/heads/{branch}")
```

## GitHub Thread Resolution via GraphQL (Phase 7)

`GitHubClient.resolve_discussion()` now uses GraphQL mutations (`resolveReviewThread`/`unresolveReviewThread`) to resolve and unresolve review threads. The `_find_thread_node_id()` method queries `pullRequest.reviewThreads` to map a comment's `databaseId` to the thread's GraphQL node ID. `supports_thread_resolution` returns `True`.

The `_graphql()` helper sends POST requests to the GraphQL endpoint with the user's Bearer token (same `httpx.AsyncClient`). Error handling follows the same patterns as REST: timeouts raise `NetworkError`, transport errors raise `NetworkError`, HTTP 4xx/5xx raise mapped `ForgeError` subclasses, and GraphQL-level errors (from the response `errors` key) raise `ForgeError` with redacted messages.

## Self-Approval Error Handling

Both forge clients catch self-approval errors from the API and re-raise them with clear, user-facing messages:

- **GitLab:** `approve_mr()` catches `AuthError` (HTTP 401/403) and raises `ForgeError("Cannot approve: you may not have permission or this project does not allow self-approval")`. GitLab returns a permission error when the project has "Prevent MR approval by author" enabled, or when the user lacks approval rights.
- **GitHub:** `approve_mr()` catches `ForgeError` where the message contains `"422"` (GitHub returns 422 Unprocessable Entity for self-reviews) and raises `ForgeError("GitHub does not allow self-approving PRs")`. All other errors are re-raised as-is.

These clear messages surface in the TUI via the MRDetailScreen action error notifications (e.g., "Approve failed: GitHub does not allow self-approving PRs"). Without this handling, users would see raw API error payloads.

## Hostname Allowlist

Only make API calls to hostnames that are either:
- Default known hosts (`github.com`, `gitlab.com`)
- Configured in `[hosts.*]` TOML sections
- Discovered from local git remotes (verified by the scanner)

`ForgeRegistry._detect_type()` returns `None` for unknown hostnames, which prevents client creation.

## What Never to Do with Tokens

- Never store tokens in config files (`config.toml`)
- Never pass tokens as CLI arguments (visible in `ps` output)
- Never set tokens in environment variables from within the application
- Never include tokens in cache (CacheStore enforces this; only API response bodies are cached)
- Never include tokens in audit logs
- Never log raw HTTP request/response headers (contain `Authorization: Bearer`)
- Never display tokens in the TUI (error messages, notifications, diff content)
- Never commit tokens to git (`.netrc` should be in `.gitignore`)

## URL Credential Stripping

`src/tongs/scanner/remote.py:_strip_userinfo()` removes embedded credentials from remote URLs:

```python
# Input:  https://user:token@gitlab.com/org/repo.git
# Output: https://gitlab.com/org/repo.git
```

This runs on every parsed remote URL before storing it in the `Remote` dataclass. The URL stored in `Remote.url` is always credential-free.

## Temp File Security

For external editor integration (CommentEditor F2 binding and PipelinePanel F2 job log viewer):
- `tempfile.mkstemp(mode=0o600)` for comment editing and job log temp files
- Cleanup in `finally` block
- Pass `--cmd "set noswapfile noundofile nobackup"` to neovim to prevent swap/undo file creation
- Platform gate: `app.suspend()` not available on Windows
- Job log temp files use `.log` suffix with `tongs-{job_name}-` prefix

## Cache Store Security (Phase 7)

`src/tongs/cache/store.py:CacheStore` uses aiosqlite for async SQLite caching.

**File permissions:**
- Cache directory created with `mode=0o700`
- Database file created with `os.open(..., os.O_CREAT | os.O_RDWR, 0o600)` before aiosqlite opens it
- Only the owning user can read/write the cache

**Data exclusions:** Keys with `_EXCLUDED_PREFIXES` (`"job_log:"`, `"stream_log:"`) are never cached. This prevents large, potentially sensitive CI log data from persisting on disk.

**No credential storage:** Tokens are never included in cache keys or values. Only forge API response bodies (MR metadata, diffs, pipeline data) are cached. Auth is handled entirely by the `httpx.AsyncClient` session headers.

**WAL mode:** `PRAGMA journal_mode=WAL` enables concurrent reads during writes, reducing lock contention. The WAL file (`cache.db-wal`) inherits the database file's permissions.

**LRU eviction:** When total cache size exceeds `max_size_bytes`, `_enforce_size_limit()` evicts the oldest 25% of entries by `created_at` timestamp. `get()` touches `created_at` on hit (LRU behavior).

## MCP Server Security

`src/tongs/mcp/server.py` exposes forge operations as MCP tools.

**Security boundaries:**
- High-level tools only: `list_mrs`, `get_mr`, `get_mr_diff`, `post_comment`, `approve_mr`, `list_pipelines`
- Destructive actions (merge, close, reopen, cancel) are intentionally excluded
- No raw API endpoint exposure
- No auth primitives in MCP tool inputs or outputs

**Input validation:** `_parse_host_repo()` validates `repo_path` format via regex (`_REPO_PATH_RE = r"^(?!.*\.\.)[\w.-]+(?:/[\w.-]+){2,}$"`). The `(?!.*\.\.)` negative lookahead prevents path traversal. Invalid paths raise `ValueError`.

**Separate process:** The MCP server runs as `tongs-mcp`, a separate entry point from the TUI. It creates its own `ForgeRegistry` from `load_config()`, inheriting the same auth cascade and hostname allowlist.

**No auth in tool I/O:** Tool inputs are `repo_path` and `number` (primitives). Tool outputs are plain dicts with MR metadata. Tokens never appear in MCP messages.

## Plugin Security

`src/tongs/plugins/` implements the plugin discovery and lifecycle system.

**Entry point discovery:** Plugins are discovered via `importlib.metadata.entry_points(group="tongs.plugins")`. Only installed Python packages can register plugins; there is no dynamic code loading from arbitrary paths.

**Type validation:** Each discovered entry point is loaded and checked with `isinstance(plugin, TongsPlugin)`. Non-conforming plugins are logged and skipped.

**Config filtering:** Plugins can be disabled via `[plugins.NAME] enabled = false` in `config.toml`. Disabled plugins are skipped during discovery (never loaded).

**Graceful failure:** All plugin lifecycle calls (`on_app_ready`, `on_app_shutdown`) and command/screen collection (`get_commands`, `get_screens`) are wrapped in individual `try/except` blocks with `exc_info=True` logging. A failing plugin does not crash the app or affect other plugins.

**Data access:** Plugins receive a `PluginContext` security facade in lifecycle hooks, not the raw `TongsApp` instance. `PluginContext` (defined in `src/tongs/plugins/context.py`) exposes only safe, read-only properties: `forge_registry`, `cache`, `config`, `repos`, plus `notify()`, `push_screen()`, `pop_screen()`, and `plugin_config()`. This prevents plugins from accessing internal app state, mutating reactive attributes, or interfering with the TUI event loop directly.
