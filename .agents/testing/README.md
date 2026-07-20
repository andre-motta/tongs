# Testing

## Setup

```bash
source /home/alustosa/git/tongs/.venv/bin/activate
pip install -e ".[dev]"

# Run all tests
pytest

# Verbose
pytest -v

# Specific module
pytest tests/test_forges/test_gitlab.py

# Specific test class
pytest tests/test_forges/test_gitlab.py::TestGitLabClientAsync
```

Tests run without network access. All forge interactions are mocked. The full suite (573 tests) runs in under 1 second.

## Directory Layout

```
tests/
  conftest.py               # Shared fixtures (if any)
  test_commands.py           # TongsCommandProvider, plugin command integration
  test_config.py             # Config loading, defaults, TOML parsing
  test_errors.py             # Error hierarchy, credential redaction patterns
  test_state.py              # MRFilter, ReviewDraft
  test_scanner/
    test_remote.py           # Remote URL parsing, forge detection
    test_discovery.py        # Filesystem discovery with tmp_path repos
  test_forges/
    test_models.py           # Data model construction and properties
    test_auth.py             # Token resolution cascade
    test_http.py             # HTTP transport, error mapping
    test_gitlab.py           # GitLab client parsing and async methods
    test_github.py           # GitHub client: inline comment, multi-line params (3 tests)
    test_registry.py         # ForgeRegistry hostname mapping
  test_diff/
    test_parser.py           # Diff parser with inline strings + fixture files
    test_position.py         # DiffPosition creation and forge converters
  test_cache/
    test_store.py            # CacheStore: open, get/put, TTL, LRU eviction, JSON, invalidation
  test_plugins/
    test_plugin_system.py    # TongsPlugin ABC, PluginRegistry discovery, config filtering, lifecycle
  test_mcp/
    test_server.py           # MCP server tools: list_mrs, get_mr, get_mr_diff, post_comment, approve_mr, list_pipelines
  test_views/
    test_helpers.py          # View helper functions (CI icons, relative time)
    test_suggestion.py       # Suggestion helpers: template, fence, format, position (25 tests)
  test_widgets/
    test_diff_panel.py       # DiffOptionList, DiffContent, DiffRenderer, selection, comments, discussion threading, comment navigation
    test_discussion_list.py  # DiscussionPanel, DiscussionCard, render_diff_snippet, card sorting/filtering
    test_pipeline_panel.py   # PipelinePanel pure functions (_format_duration, _relative_time, _ci_icon_text/markup) and message classes
  fixtures/
    builder_mr_3113.diff     # Real MR diff from builder project
    fromager_pr_1258.diff    # Real PR diff from fromager project
```

## Mock Patterns

### httpx.MockTransport for Forge Tests

The primary mock pattern for forge client tests. Create a handler function that receives an `httpx.Request` and returns an `httpx.Response`:

```python
from tongs.forges.models import ForgeHost
from tongs.forges.gitlab import GitLabClient
from tongs.scanner.repo import ForgeType

_TEST_HOST = ForgeHost(
    hostname="gitlab.example.com",
    forge_type=ForgeType.GITLAB,
    api_base="https://gitlab.example.com/api/v4",
)

def _make_gitlab_client(handler) -> tuple[GitLabClient, httpx.AsyncClient]:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url=_TEST_HOST.api_base)
    return GitLabClient(_TEST_HOST, http), http
```

Usage in async tests:

```python
@pytest.mark.asyncio
async def test_list_mrs_returns_summaries(self):
    def handler(req: httpx.Request) -> httpx.Response:
        assert "/projects/acme%2Fwidgets/merge_requests" in str(req.url)
        return httpx.Response(200, json=[...])

    client, http = _make_gitlab_client(handler)
    async with http:
        summaries = await client.list_mrs("acme/widgets")
    assert len(summaries) == 2
```

The handler can validate request URLs, methods, params, and body. Always use `async with http:` to properly close the client.

### Subprocess Mock for Auth Tests

Auth tests mock `subprocess.run` since the real CLI tools may not be installed:

```python
from unittest.mock import patch

def test_cli_returns_token_on_success(self):
    result = subprocess.CompletedProcess(
        args=["glab", "auth", "token", "--hostname", "gitlab.com"],
        returncode=0,
        stdout="glpat-abc123\n",
        stderr="",
    )
    with patch("tongs.forges.auth.subprocess.run", return_value=result):
        token = _token_from_cli("gitlab.com", ForgeType.GITLAB)
    assert token == "glpat-abc123"
```

Also test error cases: `FileNotFoundError` (CLI not installed), `TimeoutExpired`, non-zero exit code, empty stdout.

### tmp_path Git Repos for Scanner Tests

Scanner tests create temporary git repos using pytest's `tmp_path` fixture:

```python
def test_discovery(tmp_path):
    repo_dir = tmp_path / "org" / "myrepo"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()
    # Write git config or mock git remote -v output
    repos = discover_repos(tmp_path)
    assert len(repos) == 1
```

Scanner discovery tests also mock `subprocess.run` for `git remote -v` output since the tmp repos don't have real git configs.

### .netrc Tests

Auth `.netrc` tests use `tmp_path` to create temporary `.netrc` files and patch `Path.home()`:

```python
def test_reads_valid_netrc(self, tmp_path):
    netrc_file = tmp_path / ".netrc"
    netrc_file.write_text("machine gitlab.com\n  login __token__\n  password glpat-test123\n")
    netrc_file.chmod(0o600)
    with patch("tongs.forges.auth.Path.home", return_value=tmp_path):
        token = _token_from_netrc("gitlab.com")
    assert token == "glpat-test123"
```

POSIX permission tests use `@pytest.mark.skipif(sys.platform == "win32", ...)`.

## Widget Tests (DiffPanel)

`tests/test_widgets/test_diff_panel.py` tests the DiffOptionList, DiffContent, DiffRenderer, and related classes directly without running the Textual app. The tests construct model objects (DiffFile, DiffHunk, DiffLine) and call methods on the widgets.

Key patterns:

**Direct model construction for test data:**

```python
def _make_line(lt: LineType, content: str = "x", old: int | None = 1, new: int | None = 1) -> DiffLine:
    return DiffLine(old_lineno=old, new_lineno=new, content=content, line_type=lt)
```

**Testing render_line() behavior:**
Tests verify that `DiffOptionList.render_line()` injects the correct VisualStyle (addition/deletion/selection backgrounds) by checking the style applied before `_get_option_render()`.

**Selection range testing:**
Tests verify `_in_selection_range()` and `_get_selection_lines()` by setting `_selection_anchor` and `highlighted` directly, then asserting the expected lines are included.

**CommentRequested message assertions:**
Tests verify that `action_comment()` and `action_suggest()` post `CommentRequested` with the correct `CommentMode`, `context_lines`, and blocking behavior (e.g., suggest on deletion lines is blocked).

## Suggestion Helper Tests

`tests/test_views/test_suggestion.py` tests pure functions from `src/tongs/views/suggestion.py`. No Textual dependencies, no mocking required.

Covers: template building/parsing, backtick fence computation (including edge cases with inner triple-backtick code blocks), forge-specific suggestion block formatting (GitLab fence syntax vs GitHub plain fence), new-side line extraction, and position resolution for single-line and multi-line suggestions on both forges.

## GitHub Client Tests

`tests/test_forges/test_github.py` tests `GitHubClient.create_inline_comment()` via `httpx.MockTransport`. Covers single-line comments (no `start_line`/`start_side` in payload) and multi-line comments (`start_line`/`start_side` included in REST payload).

## Diff Parser Tests

Test files in `tests/test_diff/test_parser.py` use two approaches:

1. **Inline diff strings** for targeted edge cases (empty hunks, binary files, renames, no-newline markers)
2. **Fixture files** (`tests/fixtures/*.diff`) for real-world validation with complex multi-file diffs

Fixture files are real diffs captured from actual MRs and PRs.

## pytest Conventions

- Use `pytest.mark.asyncio` for all async test methods
- Use `pytest.raises(ErrorType, match=...)` for exception testing
- Group related tests in classes (`TestParseDatetime`, `TestTokenFromCli`, etc.)
- Helper functions for creating test data (`_mr_api_json(overrides)`, `_make_gitlab_client(handler)`)
- No global state; all fixtures are function-scoped or use `tmp_path`
- Tests must run without any network access

## Gate Review Process

Every feature module goes through four review areas:

1. **Architecture** -- does the code fit existing abstractions?
2. **Security** -- no credential leaks, proper error redaction?
3. **UX** -- keybinding consistency, ASCII mode, responsive UI?
4. **QE** -- test coverage, edge cases, no network dependencies?

Verdicts:
- **APPROVED WITH NOTES** -- address notes in subsequent work, no re-review needed
- **NEEDS CHANGES** -- fix issues, then re-submit for review

Run `ruff check src/ tests/` and `ruff format --check src/ tests/` before every commit. Fix issues before pushing.
