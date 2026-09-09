# tongs

Terminal-first multi-forge MR/CI management with a Textual UI and an optional
production Electron desktop UI. Both frontends use the same Python application
services, forge clients, repository discovery, cache, and durable review drafts.
Python 3.12+ is required for the core; building the desktop shell requires
Node.js 22.12+.

## Agent Workflow

For substantial initiatives, use the installed `agent-sdlc` skill and read the
[project SDLC profile](docs/SDLC.md), a repository-only document that is excluded
from the published site. Roles are named by function, not by any vendor's
model codename: an **orchestrator** owns architecture, scheduling, and
integration; a **senior contributor** and a separate **senior reviewer** handle
senior implementation and independent review; a **bounded contributor** handles
well-specified assignments under senior review. Select actual runtime models and
record the model and effort setting actually used. Keep small fixes proportional.

For the desktop initiative, the orchestrator owns `feat/desktop-app`. Agents use
isolated `feat/desktop-<issue>-<slug>` branches and PRs into that branch, with
independent senior review and orchestrator integration. Every change is
issue-tracked with explicit dependencies. Only the final feature PR goes to `main`
for CTO review. Hardware GPU acceleration is a mandatory production and release
gate; see the profile for evidence requirements.

This file is the shared repository guide for coding agents. Codex and Claude Code both read `AGENTS.md` when working in this repository. Read `README.md` for product context and the relevant subsystem guides below before changing code. The `.agents/*/README.md` files are reference documentation to read explicitly.

- Run commands from the current checkout or worktree root. Use its local `.venv`, including when a subsystem guide shows a machine-specific path.
- Inspect `git status` before editing and preserve unrelated user changes.
- When asked to work on another branch and then return, use a git worktree to keep the current checkout undisturbed.
- Shell activation does not persist between tool calls. Activate the environment in each shell invocation that runs Python tools, or use `.venv/bin/python`, `.venv/bin/pytest`, and `.venv/bin/ruff` directly.
- For code changes, run the relevant tests during development and the full suite plus lint and format checks before handing off. For documentation-only changes, check the diff, links, and command examples. Report checks that could not run and why.

## Tech Stack

- **Frontends:** Textual and Rich (terminal), Electron and React (desktop)
- **Services:** shared `ApplicationSession`, typed read and mutation services, and durable review drafts
- **HTTP:** httpx (async), no CLI subprocess per operation
- **Config:** TOML via tomllib, platformdirs for cross-platform paths
- **Build:** hatchling + hatch-vcs, `pip install -e ".[dev]"` for development
- **Distribution:** `pipx install tongs`, `uvx tongs`, `uv tool install tongs`, or the unreleased Fedora RPMs `python3-tongs`, `python3-tongs+mcp` and `tongs-desktop`
- **Entry points:** `tongs` (TUI), `tongs-mcp` (MCP server), `tongs desktop` and the `tongs --install-desktop` alias (per-user desktop lifecycle), and the RPM-owned `/usr/libexec/tongs-desktop` launcher
- **Plugins:** independent `tongs.plugins` and `tongs.desktop_plugins` entry-point groups
- **Docs:** MkDocs Material site at [www.tongs.tools](https://www.tongs.tools). `docs/SDLC.md`, `docs/site-plan.md` and `docs/work/` are excluded from the build and stay repository-only; everything else under `docs/` is published.

## Critical Rules

```bash
# First-time setup from the checkout root, using Python 3.12+
# Create the environment only if .venv is missing
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]" ruff

# For MCP development, also install the optional server dependency
python -m pip install -e ".[dev,mcp]"

# In later shell invocations, activate the existing environment first
source .venv/bin/activate

# Run tests (no network access needed; forge interactions are mocked)
pytest

# Lint and check formatting
ruff check src/ tests/
ruff format --check src/ tests/

# Apply formatting to changed Python files when needed
# ruff format path/to/changed_file.py

# Production desktop dependency install, build/type check, and tests
npm ci --prefix desktop
npm run build --prefix desktop
TONGS_TEST_PYTHON="$(command -v python)" npm test --prefix desktop
```

If using `uv`, create the environment with `uv venv --python 3.12`, activate it, and use `uv pip install` in place of `python -m pip install`. Dependency installation may need network access; the mocked tests do not. Ruff is installed explicitly because it is not currently included in the `dev` extra. The `mcp` extra is needed to run MCP tests instead of skipping them.

The desktop test command builds and type-checks the production shell before
running its Electron and renderer tests. Node checks need whole-process memory,
swap, task, and time limits; native Electron needs the same OS-level guard but
does not honor every Node heap option. Use the procedure in the
[testing guide](.agents/testing/README.md). A successful headless test run is
separate from native Fedora, GPU, installer, and release evidence.

- `from __future__ import annotations` at the top of every module
- Module-level imports unless function-level is necessary to avoid circular deps
- Frozen dataclasses for immutable data, regular dataclasses for mutable state
- Type hints on all parameters and return values
- No em-dashes in text or commits
- Use `-s` flag on `git commit` for sign-off (DCO)

## Git Commits

- For approved SDLC initiatives, contributors may create coherent signed-off local commits in their assigned worktrees without per-commit approval. For desktop work, assigned contributor branch pushes and PRs into `feat/desktop-app` are authorized; the orchestrator alone integrates. The final PR into `main` requires CTO acceptance before merge. Other initiatives retain their existing upstream gates. For other work, preserve the existing requirement to approve the full commit message before committing.
- Include a one-line description body after the title, separated by a blank line, before any trailers.
- Use `git commit -s` to add the sign-off automatically; do not write `Signed-off-by` manually.
- When adding a co-author trailer, use the address of the vendor that produced the commit: `Co-Authored-By: Codex <model> <noreply@openai.com>` or `Co-Authored-By: Claude <model> <noreply@anthropic.com>`. Use the actual model name and no context-window annotation. Do not claim co-authorship by a vendor that did not produce the commit.

## Module Map

```
src/tongs/
  __main__.py              # CLI entry, argument routing, and desktop subcommand dispatch
  app.py, tui_services.py  # Textual app and adapter over shared services
  commands.py              # Textual command palette provider
  config.py, errors.py, helpers.py  # TOML config, error/redaction types, shared helpers
  services/                # Session-owned reads, mutations, drafts, CI, and utilities
  scanner/                 # Local repository discovery and remote parsing
  forges/                  # ForgeClient ABC, GitHub/GitLab clients, auth, and HTTP
  cache/                   # SQLite response cache and CachedForgeClient
  diff/, state/            # Diff models/conversion and terminal state/drafts
  views/, widgets/         # Textual screens and reusable widgets
  desktop/                 # Sidecar, bounded protocol, installer, artifacts, and assets
  plugins/                 # Terminal registry and independent desktop provider SDK
  mcp/                     # Separate FastMCP stdio server and first-party plugin

desktop/src/
  main/                    # Electron lifecycle, sidecar, IPC, security, and utilities
  preload/                 # Allowlisted context-isolated renderer bridge
  renderer/                # React application, features, navigation, and presentation
  shared/                  # Typed bridge, review, CI, and utility contracts

tests/
  desktop/                 # Python protocol/installer plus Electron, renderer, and native fixtures
  integration/desktop/     # Packaging, CI, artifact, and evidence integration checks
  plugins/                 # Desktop provider contract, lifecycle, discovery, and resources
  packaging/desktop/, packaging/rpm/  # Archive producer and Fedora RPM source checks
  containers/, ci/         # Fedora 44 harness interface and CI evidence verifiers
  fixtures/                # Shared recorded payloads used across suites

packaging/
  desktop/archive/         # Reproducible per-user archive producer and contract
  rpm/desktop/             # python-tongs and tongs-desktop SRPM sources and harness
  rpm/python-dependencies/ # Companion Python RPM specs and manifest
scripts/                   # Repository maintenance and evidence helpers
```

## Subsystem Guides

| Topic | Path | When to read |
|---|---|---|
| Architecture | [.agents/architecture/](.agents/architecture/README.md) | Understanding data flow, layering, error handling |
| Forges | [.agents/forges/](.agents/forges/README.md) | Working on forge clients, adding a new backend |
| TUI | [.agents/tui/](.agents/tui/README.md) | Working on screens, widgets, keybindings |
| Diff | [.agents/diff/](.agents/diff/README.md) | Working on diff parsing, rendering, position mapping |
| Testing | [.agents/testing/](.agents/testing/README.md) | Writing tests, understanding mock patterns |
| Security | [.agents/security/](.agents/security/README.md) | Auth, credentials, subprocess safety |
| Plugins | [.agents/plugins/](.agents/plugins/README.md) | Plugin system, MCP server, extending tongs |
| Cache | [.agents/cache/](.agents/cache/README.md) | SQLite cache store, TTL, LRU eviction |

## Development Status

The terminal application, shared services, GitHub and GitLab backends, cache,
durable review drafts, MCP server, production Electron shell, bounded sidecar
protocol, desktop provider host, installer, and artifact contracts are
implemented in this tree. `.github/workflows/ci.yml` currently requires Ruff,
Python 3.12 and 3.13 core/MCP tests, desktop fixture and production shell tests,
and the Fedora 44 Podman probe through `Desktop pre-merge aggregate`.

`ci.yml` is not the only workflow that runs on a `feat/desktop-app` pull request.
Four more also trigger on that base:

| Workflow | Trigger |
|---|---|
| `desktop-rpm.yml` (Desktop Fedora RPM) | every PR into `feat/desktop-app`, with no path filter |
| `desktop-archive.yml` (Reproducible desktop archive) | PRs into `feat/desktop-app` matching its path filter |
| `desktop-python-rpms.yml` (Desktop Python companion RPMs) | PRs into `feat/desktop-app` matching its path filter |
| `release-desktop.yml` (Unpublished desktop candidate attestation) | PRs into `feat/desktop-app` matching its path filter |

`docs.yml` deploys the site and runs `mkdocs build --strict`, but only on a push
to `main`. No pull-request check builds the documentation, so run the strict
build locally before submitting a `docs/` or `mkdocs.yml` change.

That pre-merge aggregate is not the final production desktop release gate.
Native Fedora/GPU proof, packaging and installer proof, candidate attestation,
and final release assembly remain separate evidence and authority boundaries.
Use issue dependencies and the current [SDLC profile](docs/SDLC.md), rather
than phase labels or fixed test counts, to assess readiness.

## Gate Process

Every feature goes through four review areas before merge:

1. **Architecture** -- fits existing abstractions, extends cleanly
2. **Security** -- no token leaks, proper credential handling
3. **UX** -- TUI flow, keybinding consistency, ASCII mode support
4. **QE** -- test coverage, edge cases, runs without network

Verdicts: APPROVED WITH NOTES (address notes, proceed) or NEEDS CHANGES (fix and re-review).
