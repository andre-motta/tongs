# Forges

## ForgeClient ABC

Defined in `src/tongs/forges/base.py`. All forge backends implement this interface:

**MR operations:**
- `list_mrs(repo_path, state, per_page, page)` -- list MRs for a repo
- `list_my_reviews()` -- MRs where current user is reviewer (host-scoped)
- `list_my_mrs()` -- MRs authored by current user (host-scoped)
- `get_mr(repo_path, number)` -- full MR detail
- `get_mr_diff(repo_path, number)` -- raw diff data

**Comment operations:**
- `get_mr_discussions(repo_path, number)` -- threaded discussions
- `create_inline_comment(repo_path, number, file_path, line, side, body)` -- new inline comment
- `reply_to_discussion(repo_path, number, discussion_id, body)` -- reply to thread
- `resolve_discussion(repo_path, number, discussion_id, resolved)` -- resolve/unresolve

**Review operations:**
- `submit_review(repo_path, number, verdict, body, inline_comments)` -- abstract on both forges
- `approve_mr(repo_path, number)`
- `merge_mr(repo_path, number, squash, delete_branch)`
- `close_mr(repo_path, number)` / `reopen_mr(repo_path, number)`

**Pipeline operations:**
- `list_pipelines(repo_path, per_page)`
- `get_pipeline_jobs(repo_path, pipeline_id)`
- `get_job_log(repo_path, job_id)` / `stream_job_log(repo_path, job_id)`
- `retry_job(repo_path, job_id)` / `cancel_pipeline(repo_path, pipeline_id)`

**Capability queries** (properties, override in subclasses):
- `supports_batched_review` -- GitHub batches comments into a review; GitLab does not
- `supports_thread_resolution` -- GitLab has first-class resolution; GitHub uses GraphQL
- `supports_draft_notes` -- GitLab-only feature

## Data Models

Defined in `src/tongs/forges/models.py`. Union approach with Optional forge-specific fields:

- `ForgeHost(hostname, forge_type, api_base)` -- identifies a forge instance
- `User(username, display_name)`
- `MRSummary` -- lightweight, for list views (no diff/comment data)
- `MRDetail(MRSummary)` -- full MR with description, approvals, reviewers
- `InlineComment` -- anchored to diff position, has `replies` tuple
- `Discussion` -- comment thread, may or may not be inline
- `Pipeline` / `PipelineJob` -- CI data

Enums: `MRState`, `CIStatus`, `ReviewDecision`, `FileStatus` (in diff models).

Forge-specific optional fields pattern:

```python
@dataclass(frozen=True)
class MRDetail(MRSummary):
    # GitLab-specific (None on GitHub)
    detailed_merge_status: str | None = None
    draft_notes_count: int | None = None
    # GitHub-specific (None on GitLab)
    status_check_rollup: str | None = None
```

## ForgeRegistry

`src/tongs/forges/registry.py` maps hostnames to authenticated `ForgeClient` instances.

- Clients are created lazily on first `get_client(hostname)` call and cached
- `get_host(hostname)` resolves `ForgeHost` with correct API base URL
- `_detect_type(hostname)` uses explicit host sets + substring heuristic
- `active_hostnames()` returns all known hosts (defaults + configured)
- `close_all()` closes all cached httpx clients (called from `TongsApp.on_unmount`)

API base URL patterns:
- GitLab: `https://{hostname}/api/v4`
- GitHub: `https://api.github.com` (github.com) or `https://{hostname}/api/v3` (enterprise)

## Auth Cascade

`src/tongs/forges/auth.py:resolve_token(hostname, forge_type)`:

1. **CLI credential store** -- `glab auth token --hostname {host}` or `gh auth token [--hostname {host}]`. Single subprocess call per host at startup.
2. **~/.netrc** -- reads with `netrc` stdlib. Enforces 0o600 permissions on POSIX, raises `AuthError` otherwise.
3. **Error** -- raises `AuthError` with setup instructions specific to the forge type.

Key details:
- `gh auth token` omits `--hostname` for `github.com` (default), adds it for enterprise
- `glab auth token` always includes `--hostname`
- Token is held in the `httpx.AsyncClient` session, never stored in cache or logs
- Subprocess calls use `timeout=5`, catch `FileNotFoundError` (CLI not installed) and `TimeoutExpired`

## GitLab Client

`src/tongs/forges/gitlab.py:GitLabClient(ForgeClient)` implements all ABC methods for GitLab API v4.

**GitLab-specific patterns:**

- **URL encoding:** `_encode_project(repo_path)` encodes `/` as `%2F` for API paths. Called on every API method.
- **State mapping:** GitLab uses `"opened"` where the ABC uses `"open"`. `_parse_mr_state()` handles the translation.
- **CI status mapping:** `_parse_ci_status()` maps `"created"`, `"manual"`, `"waiting_for_resource"`, `"preparing"`, `"scheduled"` all to `CIStatus.PENDING`.
- **Diff refs for inline comments:** `create_inline_comment()` fetches the MR first to get `diff_refs` (base_sha, start_sha, head_sha), then posts with position object.
- **approved_by unwrapping:** GitLab wraps approved users as `{"user": {...}}`. `_parse_mr_detail()` calls `a.get("user", a)` to unwrap.
- **Repo path extraction:** `_extract_repo_path()` gets repo path from MR data returned by global endpoints (used by `list_my_reviews`/`list_my_mrs`). Tries `references.full` first, falls back to parsing `web_url`.

**submit_review implementation:** GitLab has no batched review concept. `submit_review()` posts each inline comment individually via `create_inline_comment()`, then calls `approve_mr()` if verdict is APPROVED, then posts body as a general comment. This means redundant API calls (known deferred optimization).

**Thread resolution:** `supports_thread_resolution` returns `True`. The `resolve_discussion()` method PUTs `{"resolved": true/false}` to the discussion endpoint.

## Adding a New Forge Backend

1. **Create `src/tongs/forges/your_forge.py`** implementing `ForgeClient`. All abstract methods must be implemented. Return shared models from `forges/models.py`.

2. **Add hostname detection** in `src/tongs/scanner/remote.py`:
   - Add to `GITHUB_HOSTS`/`GITLAB_HOSTS` or create a new set
   - Update `_detect_forge_type()` with the new forge's hostname patterns
   - Add `ForgeType.YOUR_FORGE` to `src/tongs/scanner/repo.py`

3. **Add auth resolution** in `src/tongs/forges/auth.py`:
   - Add a CLI command for the forge's credential store
   - .netrc fallback works automatically (hostname-based lookup)
   - Error message should include forge-specific setup instructions

4. **Register in `ForgeRegistry`** (`src/tongs/forges/registry.py`):
   - Add API base URL logic in a `_your_forge_api_base()` function
   - Add instantiation branch in `get_client()`
   - Update `_detect_type()` with the new forge's default hostname

5. **Write tests** mirroring `tests/test_forges/test_gitlab.py`:
   - Use `httpx.MockTransport` for all API calls
   - Test parsing methods with realistic API response dicts
   - Test async methods with handler functions that validate request URLs/params

6. **Add forge-specific optional fields** to models in `forges/models.py` if needed (with `None` defaults).

## What's Implemented vs Planned

| Component | Status |
|---|---|
| ForgeClient ABC | Complete |
| Shared data models | Complete |
| Auth cascade (CLI + .netrc) | Complete |
| HTTP transport + error mapping | Complete |
| ForgeRegistry | Complete |
| GitLabClient | Complete (all ABC methods) |
| GitHubClient | Planned (Phase 3) |
| Keyring auth | Planned (future) |
| Rate limit auto-retry | Error raised, UI retry planned |
