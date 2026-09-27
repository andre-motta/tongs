# tongs

Terminal-first multi-forge MR/CI management with a Textual UI and an optional
beta Electron desktop UI. Both frontends use the same Python application
services, forge clients, repository discovery, cache, and durable review drafts.
Python 3.12+ is required for the core; building the desktop shell or the
documentation site requires Node.js 22.12+.

## Agent Workflow

For substantial initiatives, use the installed `agent-sdlc` skill (0.2.3) and the
[project SDLC profile](docs/SDLC.md), a repository-only document excluded from
the published site. Small fixes skip it. Agents inherit the session model;
record the model actually used in the co-author trailer. A hardware GPU check
runs only when a change can affect GPU rendering; see the profile.

This file is the shared repository guide for coding agents; every agent that works in this repository reads it. Read `README.md` for product context and the relevant subsystem guides below before changing code. The `.agents/*/README.md` files are reference documentation to read explicitly.

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
- **Distribution:** `pipx install tongs`, `uvx tongs`, `uv tool install tongs`, or the unsigned beta Fedora RPMs `python3-tongs`, `python3-tongs+mcp` and `tongs-desktop` attached to each stable GitHub Release
- **Entry points:** `tongs` (TUI), `tongs-mcp` (MCP server), `tongs desktop` and the `tongs --install-desktop` alias (per-user desktop lifecycle), and the RPM-owned `/usr/libexec/tongs-desktop` launcher
- **Plugins:** independent `tongs.plugins` and `tongs.desktop_plugins` entry-point groups
- **Docs:** Astro and Starlight site in `site/`, published at [www.tongs.tools](https://www.tongs.tools). The Markdown stays in `docs/`, reached through the committed symlink `site/src/content/docs -> ../../../docs`. The content collection glob in `site/src/content.config.ts` excludes `docs/SDLC.md`, `docs/site-plan.md` and `docs/work/`, which stay repository-only; everything else under `docs/` is published. `starlight-links-validator` fails the build on a broken page link or anchor.

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

# Documentation site install and strict build (output in site/dist)
npm ci --prefix site
npm run build --prefix site
```

If using `uv`, create the environment with `uv venv --python 3.12`, activate it, and use `uv pip install` in place of `python -m pip install`. Dependency installation may need network access; the mocked tests do not. Ruff is installed explicitly because it is not currently included in the `dev` extra. The `mcp` extra is needed to run MCP tests instead of skipping them.

The desktop test command builds and type-checks the production shell before
running its Electron and renderer tests. Node checks need whole-process memory,
swap, task, and time limits; native Electron needs the same OS-level guard but
does not honor every Node heap option. Run them inside a `systemd-run --user`
unit with a 1 GiB memory maximum, zero swap, a 64-task limit,
`NODE_OPTIONS=--max-old-space-size=512`, `node --test --test-concurrency=1`, and
an external deadline, asserting those effective limits from inside the guard
before Node starts. Use the exact procedure in the
[testing guide](.agents/testing/README.md). Never assert on a DOM node, because
failure formatting walks jsdom recursively and allocates outside V8; assert
text, attributes, counts, serialized payloads, and numeric rectangles instead.
Never build a scratch worktree with the fix reverted to prove a test would have
caught a bug: reason from the diff, because those runs are unbounded and are the
usual cause of an out-of-memory kill. A successful headless test run is separate
from native Fedora, GPU, installer, and release evidence. The documentation
site's `npm ci` and `npm run build` run under the same guard with a 256-task
limit instead of 64, because Node starts a worker thread per CPU and the build
aborts at startup on hosts with many cores; it peaks near 0.9 GiB.

- `from __future__ import annotations` at the top of every module
- Module-level imports unless function-level is necessary to avoid circular deps
- Frozen dataclasses for immutable data, regular dataclasses for mutable state
- Type hints on all parameters and return values
- No em-dashes in text or commits
- Use `-s` flag on `git commit` for sign-off (DCO)

## Git Commits

- For approved SDLC initiatives, agents may create signed-off local commits in their assigned worktrees and integrate locally without per-commit approval; pushes, PRs, and merges follow the profile's upstream path. For other work, approve the full commit message before committing.
- Include a one-line description body after the title, separated by a blank line, before any trailers.
- Use `git commit -s` to add the sign-off automatically; do not write `Signed-off-by` manually.
- When a model produced the commit, add a co-author trailer naming the vendor that actually produced it. Every agent working on this project runs on an Anthropic model, so the trailer is `Co-Authored-By: Claude <model> <noreply@anthropic.com>`, with the actual model name and no context-window annotation. Do not claim co-authorship by a vendor that did not produce the commit.

## Module Map

```text
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

docs/                      # Site Markdown; SDLC.md, site-plan.md and work/ stay repository-only
site/                      # Astro + Starlight site and homepage; builds to site/dist with CNAME
  src/content/docs         # Committed symlink to ../../../docs

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
| CI and releases | [.agents/ci/](.agents/ci/README.md) | Hosted workflows, releases, re-running checks, pinned actions |

## Development Status

The terminal application, shared services, GitHub and GitLab backends, cache,
durable review drafts, MCP server, production Electron shell, bounded sidecar
protocol, desktop provider host, installer, and artifact contracts are
implemented in this tree. `.github/workflows/ci.yml` selects its lanes (docs,
lint, core, desktop fixtures, the Fedora 44 Podman probe, desktop production
evidence, and packaging) by the paths a pull request changes, using the rules
in `tests/ci/ci_plan.py`. Any doubt fails closed to the full graph, as do rule
changes under `tests/ci/**`, the `ci:full` label, and every push to `main`.
Preview a branch with `python tests/ci/ci_plan.py explain --base origin/main`.
The single required check is the always-run `CI aggregate`, which recomputes
the plan and requires every selected lane to succeed and every other lane to
skip. The [CI guide](.agents/ci/README.md#hosted-workflows) has the lane table
and the label and cancellation rules.

`release-desktop.yml` (Unpublished desktop candidate attestation) also runs on
pull requests into `main` that match its path filter, on stable `vX.Y.Z` tags,
and on a manual `workflow_dispatch` dry run from `main`.

`desktop-rpm.yml` (Desktop Fedora RPM), `desktop-python-rpms.yml` (Desktop Python
companion RPMs) and `desktop-archive.yml` (Reproducible desktop archive) are manual
only (`workflow_dispatch`); the production gate's `rpm-lifecycle` and `archive` jobs
prove the same source rebuild, companion closure, lifecycle and byte-identical
rebuild against the receipt-bound fresh archive whenever the `packaging` lane
is selected.

`docs.yml` builds `site/` with `npm ci --prefix site` and
`npm run build --prefix site` and deploys `site/dist` to GitHub Pages, but only
on a push to `main` or a manual `workflow_dispatch`. On pull requests the
`docs` lane of `ci.yml` runs the same build, without a deploy, and the pinned
Markdown linter. Run the build locally, under the Node memory guard, before
submitting a `docs/` or `site/` change.

`publish.yml` and `release-desktop.yml` run on stable `vX.Y.Z` tags: the first
publishes the core to PyPI, the second builds, attests and publishes the desktop
GitHub Release. Tags match no `branches:` filter, so a tag push runs neither
`ci.yml` nor `docs.yml`.

The `CI aggregate` is not the final production desktop release gate.
Native Fedora proof (and a GPU check when the profile calls for one), packaging
and installer proof, candidate attestation, and final release assembly remain
separate evidence and authority boundaries.
Use issue dependencies and the current [SDLC profile](docs/SDLC.md), rather
than phase labels or fixed test counts, to assess readiness.

## Gate Process

Every feature goes through four review areas before merge:

1. **Architecture**: fits existing abstractions, extends cleanly
2. **Security**: no token leaks, proper credential handling
3. **UX**: TUI flow, keybinding consistency, ASCII mode support
4. **QE**: test coverage, edge cases, runs without network

Verdicts: APPROVED WITH NOTES (address notes, proceed) or NEEDS CHANGES (fix and re-review).
