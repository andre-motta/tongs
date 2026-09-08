# Contributing to tongs

Thanks for considering a contribution. Check
[GitHub issues](https://github.com/andre-motta/tongs/issues) for current work
and discuss a new direction in an issue before starting a large change.

## Development setup

Tongs requires Python 3.12 or newer. Run commands from the checkout root and
keep the virtual environment in that checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,mcp]" ruff
```

`uv venv --python 3.12` and `uv pip install` are valid equivalents. Activate the
environment again in each new shell. Dependency installation can require
network access; tests use mocked forge interactions.

Building the production desktop shell additionally requires Node.js 22.12 or
newer. Its locked dependency installation is:

```bash
npm ci --prefix desktop
```

## Code style

- **Formatter and linter:** Run `ruff check src/ tests/` and
  `ruff format --check src/ tests/` before submitting Python changes.
- **Comments:** Explain non-obvious reasons; do not restate the code.
- **Imports:** Use module-level imports unless a function-level import is needed,
  such as to avoid a circular dependency.
- **Type hints:** Type all parameters and return values. Put
  `from __future__ import annotations` at the top of every module.
- **Data models:** Use frozen dataclasses for immutable data and regular
  dataclasses for mutable state.
- **Commits:** Use a title, a blank line, a one-line description body, and
  `git commit -s`. When adding a Codex co-author, use
  `Co-Authored-By: Codex <model> <noreply@openai.com>` with the actual model
  name and no context-window annotation. Do not use em dashes in prose or
  commit messages.

## How the review process works

Substantial initiatives follow the [project SDLC profile](docs/SDLC.md). It
defines model roles, dependent work items, isolated worktrees, independent
review, local integration, and CTO design and upstream gates. Small fixes use
the relevant checks without requiring an initiative or agent team.

Desktop work uses issue-linked `feat/<work-item>` branches in isolated
worktrees. Open PRs against `feat/desktop-app` with dependencies, exact tested
commits, checks, and functional evidence. Independent Sol review precedes Astra
integration. Astra owns the feature branch and resolves integration conflicts.
The complete feature is presented as one evidence-backed PR into `main` for CTO
review.

Every change is reviewed for architecture, security, UX, and QE. Native
hardware-accelerated Electron on the supported Fedora host is a separate
production and release gate. A headless test result does not substitute for
that evidence.

## Running tests

Run Python tools through the checkout-local environment:

```bash
source .venv/bin/activate
pytest tests/ --ignore=tests/test_mcp -v
pytest tests/test_mcp -v --junitxml=/tmp/tongs-mcp.junit.xml
python tests/ci/verify_desktop_ci.py mcp-report \
  --path /tmp/tongs-mcp.junit.xml
ruff check src/ tests/
ruff format --check src/ tests/
```

The MCP extra is required so MCP tests execute rather than skip on import. Use a
focused path during development, then run the checks appropriate to the changed
source.

Install, build, type-check, and test the production Electron/React shell with:

```bash
npm ci --prefix desktop
npm run build --prefix desktop
TONGS_TEST_PYTHON="$(command -v python)" npm test --prefix desktop
```

`npm run build --prefix desktop` prepares assets, runs TypeScript checking, and
creates build output. The test command repeats that build before the Electron
and renderer test suites. The historical comparison fixtures have separate checks:

```bash
PYTHONPATH=spikes/desktop pytest spikes/desktop/tests/test_backend.py -v
npm ci --prefix spikes/desktop/frontend
npm test --prefix spikes/desktop/frontend
npm run build --prefix spikes/desktop/frontend
pytest spikes/desktop/electron/test/test_launcher.py -v
npm ci --prefix spikes/desktop/electron
TONGS_DESKTOP_PYTHON="$(command -v python)" \
  npm test --prefix spikes/desktop/electron
```

Install `./spikes/desktop/reference-plugin` in the same Python environment before
running fixture tests that discover it. The installable production provider example has focused Python and prebuilt ESM
checks:

```bash
python -m pip install ./examples/desktop-plugin
python -m pytest -q examples/desktop-plugin/tests/test_provider.py
node --test examples/desktop-plugin/tests/test_dashboard_module.mjs
```

See the example [README](examples/desktop-plugin/README.md) for its declared
modules, resources, and same-environment installation contract.

Pull requests to `main` and `feat/desktop-app` run `.github/workflows/ci.yml`.
Its required `Desktop pre-merge aggregate` accepts an exact revision only when
Ruff, Python 3.12 and 3.13 core/MCP tests, desktop fixture and production shell
tests, and the Fedora 44 Podman probe succeed. A skipped, cancelled, missing, or
failed required job fails the aggregate. A new commit supersedes earlier
results.

The Fedora harness interface is:

```bash
tests/containers/run-fedora-44.sh --output-dir <empty-directory-outside-checkout>
```

The production desktop release assembly gate is still separate from this
pre-merge aggregate. Do not describe a fixture, Podman, or headless Node run as
native Fedora, GPU, installer, signing, RPM, or release acceptance.

For local Node work, bound the whole process tree, not only the V8 heap. Use a
1 GiB memory limit, zero swap, a 64-task limit, `NODE_OPTIONS=--max-old-space-size=512`,
one test worker, and an external deadline. Verify the guard before starting
Node and fail on timeout or out-of-memory termination. See the
[testing guide](.agents/testing/README.md) for the complete procedure and native
evidence rules.

## How to add a plugin

Tongs has two independent trusted-code entry-point groups:

- `tongs.plugins` for terminal `TongsPlugin` implementations
- `tongs.desktop_plugins` for production `DesktopPluginProvider`
  implementations and their packaged ESM, CSS, and help resources

A plugin must be installed in the same Python environment as Tongs. The
registries honor `[plugins.<name>].enabled`, but installed Python, ESM, and CSS
plugins are trusted code rather than a sandboxed extension format. Use the
[terminal plugin guide](docs/guides/plugins.md), the
[desktop provider guide](docs/plugins/provider.md), and the installable
[desktop example](examples/desktop-plugin/README.md) as the current contracts.

## How to add a new forge backend

1. Implement `ForgeClient` in `src/tongs/forges/your_forge.py`, returning shared
   models from `src/tongs/forges/models.py`.
2. Update remote detection in `src/tongs/scanner/remote.py` and client creation
   in `src/tongs/forges/registry.py`.
3. Extend the credential cascade in `src/tongs/forges/auth.py` with a CLI,
   `.netrc`, or optional keyring source and a useful `AuthError`.
4. Add mocked transport and service compatibility tests under
   `tests/test_forges/` and `tests/services/` as appropriate.

UI code must not import a concrete forge client. `ApplicationSession` in
`src/tongs/services/session.py` owns authenticated resources and exposes typed
reads and mutations. The terminal consumes it through `TUIServiceAdapter`; the
desktop consumes it through the bounded sidecar protocol.

## Project structure

```text
src/tongs/
  services/        # Shared application session, reads, mutations, drafts, CI
  forges/          # ForgeClient ABC, GitHub/GitLab clients, auth, and HTTP
  scanner/         # Local repository discovery and remote parsing
  cache/           # SQLite response cache and cached forge wrapper
  diff/, state/    # Diff conversion/models and review draft state
  views/, widgets/ # Textual screens and reusable terminal widgets
  desktop/         # Sidecar protocol, installer, artifacts, and assets
  plugins/         # Terminal registry and desktop provider SDK
  mcp/             # Separate FastMCP server
desktop/src/
  main/             # Electron lifecycle, IPC, sidecar, and local utilities
  preload/          # Allowlisted context-isolated bridge
  renderer/         # React application and feature modules
  shared/           # Typed cross-process contracts
tests/
  desktop/          # Protocol, installer, Electron, renderer, and native fixtures
  integration/desktop/ # Packaging, CI, artifact, and evidence integration
  plugins/          # Desktop provider contract and lifecycle
```

Read [AGENTS.md](AGENTS.md) and the relevant `.agents/*/README.md` subsystem
guide before changing a subsystem.

## License

By contributing, you agree that your contributions will be licensed under the
MIT License.
