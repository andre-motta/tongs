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

3. **Error with instructions**
   - Raises `AuthError` with forge-specific setup command
   - Never silently fails or uses empty tokens

Planned future step: system keyring via `keyring` package (optional dependency).

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

## resolve_discussion Documentation Requirement (Phase 3)

`GitHubClient.resolve_discussion()` is a documented no-op because the GitHub REST API does not support per-thread resolution (this would require GraphQL). The `supports_thread_resolution` property returns `False` (the default). Callers must check this property before calling `resolve_discussion()`, and the UI must not show resolution controls for GitHub PRs. The method has an explicit docstring explaining the limitation rather than silently doing nothing.

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
- Never include tokens in cache (SQLite, planned)
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

For external editor integration (CommentEditor F2 binding):
- `tempfile.mkstemp(mode=0o600)` for comment editing temp files
- Cleanup in `finally` block
- Pass `--cmd "set noswapfile noundofile nobackup"` to neovim to prevent swap/undo file creation
- Platform gate: `app.suspend()` not available on Windows

## MCP Server Security (Planned)

- High-level tools only: `list_mrs`, `get_mr`, `get_mr_diff`, `post_comment`, `approve_mr`, `list_pipelines`
- No raw API exposure
- No auth primitives in MCP I/O
- Separate entry point from TUI

## Plugin Security (Planned)

- Declarative plugin registration only
- All plugin lifecycle calls wrapped in `try/except`
- Disable plugin on unhandled exception
- Plugins access forge data via `self.app.forge_registry` and `self.app.cache`, not raw HTTP clients
