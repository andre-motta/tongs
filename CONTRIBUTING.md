# Contributing to tongs

Thanks for considering a contribution. tongs is early stage, so your work will directly shape the architecture.

## Development setup

```bash
# Clone the repo
git clone https://github.com/andre-motta/tongs.git
cd tongs

# Create a virtual environment (uv or plain venv)
uv venv
source .venv/bin/activate

# Install in editable mode with development and MCP dependencies
pip install -e ".[dev,mcp]"

# Run tests
pytest

# Run linter
ruff check src/ tests/
ruff format --check src/ tests/
```

## Code style

- **Formatter/linter:** ruff. Run `ruff check` and `ruff format` before submitting.
- **Comments:** only when the "why" is non-obvious. No comments that restate what the code does.
- **Imports:** module-level imports unless function-level is necessary (for example, to avoid circular dependencies or defer heavy imports).
- **Type hints:** use them everywhere. `from __future__ import annotations` at the top of every module.
- **Data models:** frozen dataclasses for immutable data, regular dataclasses for mutable state.

## How the review process works

Substantial initiatives follow the [project SDLC profile](docs/SDLC.md), using
the reusable Agent SDLC guide and skill. It defines model roles, dependent work
items, isolated worktrees, independent review, local integration, and CTO design
and upstream gates. Small fixes use the relevant checks without requiring an
epic or agent team.

Desktop work uses issue-linked `feat/<work-item>` branches in isolated worktrees.
Open PRs against `feat/desktop-app`; include dependencies, exact tested commits,
checks and functional evidence. Independent Sol review precedes Astra
integration. Astra owns the feature branch and resolves integration conflicts.
The complete feature is presented as one evidence-backed PR into `main` for CTO
review. Hardware-accelerated Electron on the supported Fedora host is a required
production/release gate. See the profile for the full project-specific rules.

Every feature goes through a gate review covering four areas:

1. **Architecture** -- does this fit the existing abstractions? Does it extend them cleanly?
2. **Security** -- no token storage, no credential leaks, proper permission checks on `.netrc`
3. **UX** -- does the TUI flow feel right? Are keybindings consistent? Does ASCII mode work?
4. **QE** -- are there tests? Do they cover edge cases? Do they run without network access?

Open a draft PR early if you want feedback on direction before investing in polish.

## Running tests

```bash
# All tests
pytest

# Specific test module
pytest tests/test_forges/test_gitlab.py

# With verbose output
pytest -v

# Tests run without network access -- all forge interactions are mocked
```

Pull requests to `main` and `feat/desktop-app` run the complete CI workflow for
every change. The stable required check is `Desktop pre-merge aggregate`. It
accepts a revision only when lint and formatting, Python 3.12 and 3.13 core and
MCP tests, desktop fixture tests, and the hosted Fedora 44 Podman probe all
succeed. A skipped, cancelled, missing, or failed required job makes the
aggregate fail. A new commit supersedes earlier results, so review evidence must
refer to the exact pull request head revision.

Run the equivalent local checks with:

```bash
ruff check src/ tests/
ruff format --check src/ tests/
pytest tests/ --ignore=tests/test_mcp -v
pytest tests/test_mcp -v --junitxml=/tmp/tongs-mcp.junit.xml
python tests/ci/verify_desktop_ci.py mcp-report \
  --path /tmp/tongs-mcp.junit.xml
PYTHONPATH=spikes/desktop pytest spikes/desktop/tests/test_backend.py -v
npm ci --prefix spikes/desktop/frontend
npm test --prefix spikes/desktop/frontend
npm run build --prefix spikes/desktop/frontend
npm ci --prefix spikes/desktop/electron
TONGS_DESKTOP_PYTHON="$(command -v python)" \
  npm test --prefix spikes/desktop/electron
```

Install the reference desktop plugin before the backend and Electron fixture
checks with `python -m pip install ./spikes/desktop/reference-plugin`. The MCP
extra is constrained below MCP 2 because tongs currently uses the MCP 1.x
`mcp.server.fastmcp` interface. CI verifies that MCP tests actually run and
rejects an import-error skip.

The Podman job runs only on a disposable GitHub-hosted runner. Its public harness
interface remains:

```bash
tests/containers/run-fedora-44.sh --output-dir <empty-directory-outside-checkout>
```

The current desktop gate excludes the historical WebView experiment because it
was not selected as the desktop shell. The Electron lane runs fixture checks; it
does not claim native Fedora installation, KDE integration, or GPU acceptance.
Those require the separate hardware-hosted desktop evidence gate.

## How to add a plugin

The plugin system is designed for extensibility. To create a new plugin:

1. Create a new package under `src/tongs/plugins/your_plugin/`
2. Implement a plugin class that integrates with the TUI
3. Register it in `pyproject.toml` as an optional dependency group
4. Add a `[plugins.your_plugin]` section to the config schema

See the fleet monitor plugin (`tongs[fleet]`) as the reference implementation.

## How to add a new forge backend

The forge abstraction makes this straightforward:

1. **Implement `ForgeClient`** -- create `src/tongs/forges/your_forge.py` implementing the abstract interface defined in `src/tongs/forges/base.py`. The interface covers MR listing, detail, diff, comments, reviews, and pipeline operations.

2. **Add detection** -- update `src/tongs/scanner/remote.py` to recognize the new forge's URL patterns and `src/tongs/forges/registry.py` to instantiate your client.

3. **Add auth** -- extend `src/tongs/forges/auth.py` with the token resolution cascade for your forge (CLI tool, `.netrc` fallback, helpful error message).

4. **Write tests** -- mirror the structure in `tests/test_forges/`. All API calls should be mocked. See `tests/test_forges/test_gitlab.py` for the pattern.

5. **Use shared models** -- return the data models from `src/tongs/forges/models.py`. The TUI works against these models, not forge-specific types. If a forge has unique concepts, add optional fields (like `review_decision` for GitHub).

The key constraint: the TUI layer never imports a concrete forge client. Everything goes through `ForgeRegistry` and the `ForgeClient` ABC.

## Project structure

```
src/tongs/
  scanner/        # Filesystem walking, remote parsing
  forges/         # ForgeClient ABC + implementations
  views/          # Textual screens
  widgets/        # Reusable TUI components
  plugins/        # Plugin system
  mcp/            # MCP server
  cache/          # SQLite response cache
  state/          # App state management
tests/
  test_scanner/   # Scanner tests
  test_forges/    # Forge client tests
  test_views/     # TUI screen tests
  test_diff/      # Diff processing tests
```

## What to work on

Check [GitHub issues](https://github.com/andre-motta/tongs/issues) for open items. High-impact areas:

- **GitHub client** -- the `ForgeClient` interface is defined, GitLab is implemented. GitHub is the biggest open item.
- **Diff viewer** -- syntax-highlighted diff rendering with word-level highlighting (Phase 2).
- **Inline comments** -- comment on specific diff lines, reply to threads (Phase 2).
- **Pipeline viewer** -- job logs with ANSI rendering, retry/cancel actions (Phase 4).
- **New plugins** -- the plugin system is ready for extensions.

If you want to tackle something not on the list, open an issue first so we can discuss the approach.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
