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

Tests run without network access. All forge interactions are mocked. The full suite (217 tests) runs in under 0.5 seconds.

## Directory Layout

```
tests/
  conftest.py               # Shared fixtures (if any)
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
    test_registry.py         # ForgeRegistry hostname mapping
  test_diff/
    test_parser.py           # Diff parser with inline strings + fixture files
  test_views/
    test_helpers.py          # View helper functions (CI icons, relative time)
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
