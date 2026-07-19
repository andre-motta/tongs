# Forges

## ForgeClient ABC

Defined in `src/tongs/forges/base.py`. All forge backends implement this interface:

**MR operations:**
- `list_mrs(repo_path, state, per_page, page)` -- list MRs for a repo
- `list_my_reviews()` -- MRs where current user is reviewer (host-scoped)
- `list_my_mrs()` -- MRs authored by current user (host-scoped)
- `get_mr(repo_path, number)` -- full MR detail
- `get_mr_diff(repo_path, number)` -- raw diff data
- `list_mr_commits(repo_path, number)` -- commits in an MR/PR (returns `list[Commit]`)

**Comment operations:**
- `get_mr_discussions(repo_path, number)` -- threaded discussions
- `create_inline_comment(repo_path, number, file_path, line, side, body, start_line=None, start_side=None)` -- new inline comment; `start_line`/`start_side` enable multi-line ranges (GitHub uses REST payload params, GitLab encodes range in suggestion fence syntax)
- `reply_to_discussion(repo_path, number, discussion_id, body)` -- reply to thread
- `resolve_discussion(repo_path, number, discussion_id, resolved)` -- resolve/unresolve

**Review operations:**
- `submit_review(repo_path, number, verdict, body, inline_comments)` -- abstract on both forges
- `approve_mr(repo_path, number)`
- `unapprove_mr(repo_path, number)` -- non-abstract, default raises `NotImplementedError`; GitLab overrides with `/unapprove` endpoint, GitHub does not implement
- `merge_mr(repo_path, number, squash, delete_branch)`
- `close_mr(repo_path, number)` / `reopen_mr(repo_path, number)`

**Pipeline operations:**
- `list_pipelines(repo_path, per_page)`
- `list_mr_pipelines(repo_path, number, per_page)` -- list pipelines for a specific MR/PR. Default implementation falls back to `list_pipelines()`. GitLab overrides with `/merge_requests/{number}/pipelines`. GitHub overrides to fetch the PR's head branch then query `/actions/runs?branch={branch}`.
- `get_pipeline_jobs(repo_path, pipeline_id)`
- `get_job_log(repo_path, job_id)` / `stream_job_log(repo_path, job_id)`
- `retry_job(repo_path, job_id)` / `cancel_pipeline(repo_path, pipeline_id)`
- `retry_pipeline(repo_path, pipeline_id)` -- retry all failed jobs in a pipeline. Default raises `NotImplementedError`. GitLab overrides with `/pipelines/{id}/retry`. GitHub overrides with `/actions/runs/{id}/rerun-failed-jobs`.
- `cancel_job(repo_path, job_id)` -- cancel a single job. Default raises `NotImplementedError`. GitLab overrides with `/jobs/{id}/cancel`.

**Capability queries** (properties, override in subclasses):
- `supports_batched_review` -- GitHub batches comments into a review; GitLab does not
- `supports_thread_resolution` -- GitLab has first-class resolution; GitHub uses GraphQL
- `supports_draft_notes` -- GitLab-only feature
- `supports_unapprove` -- GitLab returns `True` (uses `/unapprove` endpoint); GitHub returns `False` (default). TUI checks this before calling `unapprove_mr()`
- `supports_job_cancel` -- GitLab returns `True`; base returns `False` (default). TUI checks this before showing job cancel actions.

## Data Models

Defined in `src/tongs/forges/models.py`. Union approach with Optional forge-specific fields:

- `ForgeHost(hostname, forge_type, api_base)` -- identifies a forge instance
- `User(username, display_name)`
- `MRSummary` -- lightweight, for list views (no diff/comment data)
- `MRDetail(MRSummary)` -- full MR with description, approvals, reviewers
- `Commit` -- forge-agnostic commit (sha, short_sha, title, message, author, created_at, web_url)
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

**list_my_reviews fix (Phase 3):** Previously used `reviewer_id=self` which relied on GitLab interpreting "self". Now fetches the actual username via `GET /user` and passes `reviewer_username={username}` with `scope=all`. This is more reliable across GitLab versions.

**Unapprove:** `unapprove_mr()` POSTs to `/projects/{project}/merge_requests/{number}/unapprove`. `supports_unapprove` returns `True`. This is a GitLab-only feature; the ABC default raises `NotImplementedError`.

**submit_review implementation:** GitLab has no batched review concept. `submit_review()` posts each inline comment individually via `create_inline_comment()`, then calls `approve_mr()` if verdict is APPROVED, then posts body as a general comment. This means redundant API calls (known deferred optimization).

**Thread resolution:** `supports_thread_resolution` returns `True`. The `resolve_discussion()` method PUTs `{"resolved": true/false}` to the discussion endpoint.

**MR-scoped pipelines (Phase 5):** `list_mr_pipelines()` GETs `/projects/{project}/merge_requests/{number}/pipelines` to return only pipelines associated with the specific MR. `retry_pipeline()` POSTs to `/pipelines/{id}/retry`. `cancel_job()` POSTs to `/jobs/{id}/cancel`. `supports_job_cancel` returns `True`.

## GitHub Client

`src/tongs/forges/github.py:GitHubClient(ForgeClient)` implements all ABC methods for the GitHub REST API.

**GitHub-specific patterns:**

- **Repo path splitting:** `_split_repo_path(repo_path)` splits `"owner/repo"` into a tuple. Called on every API method to construct `/repos/{owner}/{repo}/...` paths.
- **Search API for inbox:** `list_my_reviews()` and `list_my_mrs()` use `_search_prs()`, which queries `/search/issues` with `review-requested:@me` or `author:@me`. Search results are issue-shaped (missing PR fields like `mergeable_state`), so each result is re-fetched via `/repos/{owner}/{repo}/pulls/{number}` to get full PR data.
- **SSRF prevention:** `_repo_path_from_api_url(repo_url)` validates that the `repository_url` from search results starts with the client's `base_url + "/repos/"` before extracting the repo path. Results that do not match are silently skipped, preventing crafted API responses from redirecting requests to arbitrary hosts.
- **CI status:** `ci_status` is always `CIStatus.UNKNOWN` in PR list/detail responses. GitHub does not include check-run status in the PR payload; it requires a separate `GET /repos/{owner}/{repo}/commits/{ref}/check-runs` call which is not implemented yet.
- **State mapping:** GitHub uses `"open"/"closed"`, plus the `merged` flag on the PR object for merged state. `_parse_mr_state()` handles the translation.
- **CI status mapping:** `_parse_ci_status(status, conclusion)` maps GitHub Actions two-field status (status + conclusion) to `CIStatus`. Example: `status="completed"` + `conclusion="success"` becomes `CIStatus.SUCCESS`.

**submit_review implementation:** GitHub natively supports batched reviews. `submit_review()` posts to `/repos/{owner}/{repo}/pulls/{number}/reviews` with event (`APPROVE`, `REQUEST_CHANGES`, `COMMENT`), body, and optional inline comments in a single API call. `supports_batched_review` returns `True`.

**Thread resolution:** `resolve_discussion()` is a documented no-op. GitHub REST API does not support per-thread resolution (it requires GraphQL). `supports_thread_resolution` is not overridden (defaults to `False`). Callers should check this property before attempting resolution.

**Multi-line comment support:** `create_inline_comment()` accepts optional `start_line` and `start_side` parameters. When provided, these are included in the REST payload to create multi-line review comments. GitLab does not use these parameters; its multi-line range is encoded in the suggestion fence syntax instead.

**Branch deletion safety:** `merge_mr()` with `delete_branch=True` verifies that `head.repo.full_name` matches the target `repo_path` before deleting the branch. This prevents accidental deletion of branches on fork repositories in cross-fork PRs.

**Commit listing:** `list_mr_commits()` paginates `/repos/{owner}/{repo}/pulls/{number}/commits` and returns `list[Commit]`. Author is parsed from the top-level `author` (GitHub user), not `commit.author` (git author name).

**PR-scoped workflow runs (Phase 5):** `list_mr_pipelines()` first fetches the PR to get the head branch ref, then GETs `/repos/{owner}/{repo}/actions/runs?branch={branch}`. `retry_pipeline()` POSTs to `/actions/runs/{id}/rerun-failed-jobs`. `cancel_job` and `supports_job_cancel` are not overridden (GitHub Actions jobs are canceled at the run level via `cancel_pipeline`).

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
| ForgeClient ABC | Complete (includes list_mr_commits, list_mr_pipelines, retry_pipeline, cancel_job) |
| Shared data models | Complete (includes Commit, Pipeline, PipelineJob) |
| Auth cascade (CLI + .netrc) | Complete |
| HTTP transport + error mapping | Complete |
| ForgeRegistry | Complete (GitHub + GitLab wired) |
| GitLabClient | Complete (all ABC methods) |
| GitHubClient | Complete (all ABC methods; CI status from PR list is UNKNOWN) |
| GitHub CI check-runs | Not implemented (requires separate API call per PR) |
| Keyring auth | Planned (future) |
| Rate limit auto-retry | Error raised, UI retry planned |
