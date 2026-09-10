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
  `git commit -s`. Do not write `Signed-off-by` by hand; `-s` adds it. When a
  model produced the commit, add a co-author trailer naming the vendor that
  actually produced it. Every agent working on this project runs on an
  Anthropic model, so the trailer is
  `Co-Authored-By: Claude <model> <noreply@anthropic.com>` with the actual
  model name and no context-window annotation. Do not claim co-authorship by a
  vendor that did not produce the commit. Do not use em dashes in prose or
  commit messages.

## How the review process works

Substantial initiatives follow the [project SDLC profile](docs/SDLC.md), a
repository-only document that is excluded from the published documentation site.
It defines the roles, dependent work items, isolated worktrees, independent
review, local integration, and CTO design and upstream gates. Roles are named by
function: an orchestrator owns architecture, scheduling, and integration; a
senior contributor and a separate senior reviewer handle senior implementation
and independent review; a bounded contributor handles well-specified assignments
under senior review. Small fixes use the relevant checks without requiring an
initiative or agent team.

The models actually selected for those roles on the desktop initiative are
Claude Fable 5.1 as orchestrator, Claude Opus 5 at high effort for senior
implementation and for the separate independent review, and Claude Sonnet 5 at
xhigh effort for bounded work under that senior review. Record the model and
effort setting actually used in the handoff. The role names above are the
contract; the model assignment is a current choice and can change.

Desktop work uses issue-linked `feat/desktop-<issue>-<slug>` branches in isolated
worktrees. Open PRs against `feat/desktop-app` with dependencies, exact tested
commits, checks, and functional evidence. Independent senior review precedes
integration. The orchestrator owns the feature branch and resolves integration
conflicts. The complete feature is presented as one evidence-backed PR into
`main` for CTO review.

Every change is reviewed for architecture, security, UX, and QE. Native
hardware-accelerated Electron on the supported Fedora host is a separate
production and release gate. A headless test result does not substitute for
that evidence.

## Running tests

Run Python tools through the checkout-local environment:

```bash
source .venv/bin/activate
pytest tests/ --ignore=tests/test_mcp -v
pytest tests/test_mcp -v --junitxml="/tmp/tongs-mcp-$$.junit.xml"
python tests/ci/verify_desktop_ci.py mcp-report \
  --path "/tmp/tongs-mcp-$$.junit.xml"
ruff check src/ tests/
ruff format --check src/ tests/
```

The MCP extra is required so MCP tests execute rather than skip on import. The
`$$` in the report path keeps concurrent worktrees from overwriting each other's
report; CI uses `"$RUNNER_TEMP/mcp-<python-version>.junit.xml"` for the same
reason. Use a focused path during development, then run the checks appropriate to
the changed source.

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

The Fedora RPM sources have source-level checks that invoke no DNF, Podman, RPM,
Cargo, or network operation:

```bash
.venv/bin/pytest tests/packaging/rpm/desktop tests/test_plugins/test_plugin_system.py -q
.venv/bin/ruff check packaging/rpm/desktop tests/packaging/rpm/desktop src/tongs/mcp/plugin.py
.venv/bin/pytest tests/packaging/rpm/python-dependencies -v
```

Documentation changes build the site strictly:

```bash
mkdocs build --strict
```

Pull requests to `main` and `feat/desktop-app` run `.github/workflows/ci.yml`.
Its required `Desktop pre-merge aggregate` accepts an exact revision only when
Ruff, Python 3.12 and 3.13 core/MCP tests, desktop fixture and production shell
tests, and the Fedora 44 Podman probe succeed. A skipped, cancelled, missing, or
failed required job fails the aggregate. A new commit supersedes earlier
results.

`ci.yml` is not the only workflow a `feat/desktop-app` pull request triggers.
One more runs on that base:

| Workflow | Trigger |
|---|---|
| `release-desktop.yml` (Unpublished desktop candidate attestation) | PRs into `feat/desktop-app` matching its path filter; pushes to either of its two named branches, `feat/desktop-app` and `feat/desktop-120-candidate-attestation`; pushes of a stable `vX.Y.Z` tag; and a manual `workflow_dispatch` dry run |

`desktop-rpm.yml` (Desktop Fedora RPM), `desktop-python-rpms.yml` (Desktop Python
companion RPMs) and `desktop-archive.yml` (Reproducible desktop archive) are manual
only (`workflow_dispatch`); the production gate's `rpm-lifecycle` and `archive` jobs
prove the same source rebuild, companion closure, lifecycle and byte-identical
rebuild against the receipt-bound fresh archive on every pull request.

`docs.yml` runs `mkdocs build --strict` and deploys the site to GitHub Pages,
but only on a push to `main` or a manual `workflow_dispatch`. No pull-request
check builds the documentation, which is why the strict build belongs in your
local run.

Two workflows run on a stable `vX.Y.Z` tag. `publish.yml` builds the core
and publishes it to PyPI. `release-desktop.yml` builds the desktop archive for
that version, attests it with GitHub-managed Sigstore, verifies the attestation
with the installer's own code path, rebuilds the Fedora RPMs from the signed
archive, and publishes everything to the GitHub Release of the same tag,
created as a draft and published only after every asset is confirmed. Its
`release-publish` job is the only job in the repository holding
`contents: write`. The two workflows do not wait for each other, so a core
version can be on PyPI before its desktop release exists; the installer reports
that state and asks for a retry.

A `workflow_dispatch` of `release-desktop.yml` with `dry_run` set runs the
build, signing, verification and RPM rebuild on a branch and publishes nothing.
It is the rehearsal to run before pushing a tag. `tests/ci/test_release_publication_workflow.py`
pins the trigger, permission and step-order contract.

Every GitHub Action referenced from a workflow under `.github/workflows` is
pinned to a full commit SHA with a trailing `# vX.Y.Z` comment, never a mutable
tag; `tests/ci/test_production_workflow_contract.py` enforces this for every
job- and step-level reference in every workflow, with no exceptions. The
documentation toolchain (`mkdocs`, `mkdocs-material`) is pinned to an exact
version in `pyproject.toml`'s `dev` extra, and a test asserts `docs.yml`'s
install step matches it; other tools a workflow installs ad hoc, such as
`build` and `ruff`, are not yet pinned and are tracked by #162. Updates to
these pins will arrive as Dependabot pull requests once #162 (v1.1.0) lands;
until then, bump them by hand.

### Re-running a hosted check

**Download the evidence you need before you re-run anything.** Every gate
artifact is named with the run id and the run attempt, and re-running all jobs
starts a new attempt and drops the artifacts the previous attempt produced.
Once a re-run has started, the evidence that would have explained the failure
may already be gone.

**A partial re-run fails closed, by design.** Re-running only the failed jobs
does not re-run the jobs that passed, so those jobs never upload artifacts under
the new attempt number, and `Desktop pre-merge aggregate` cannot download the
complete receipt set it requires. That is intended: the aggregate asserts that
one attempt produced every receipt for one revision. To get a green aggregate,
re-run all jobs or push a new commit.

**Never retry a flaky gate blindly.** A test that passes on the second attempt
is a defect in the test, and this repository fixes it rather than rolling the
dice: see #210, #215 and #218, each of which was a real ordering bug in an
assertion that sampled state before it had settled. File the flake, fix the
test, and say in the pull request which one it was. A re-run used to get past a
red check without an explanation is not acceptable evidence.

The Fedora harness interface is:

```bash
tests/containers/run-fedora-44.sh --output-dir <empty-directory-outside-checkout>
```

The production desktop release assembly gate is still separate from this
pre-merge aggregate. Do not describe a fixture, Podman, or headless Node run as
native Fedora, GPU, installer, signing, RPM, or release acceptance.

For local Node work, bound the whole process tree, not only the V8 heap. Run
the command inside a `systemd-run --user` unit with a 1 GiB memory limit, zero
swap, a 64-task limit, `NODE_OPTIONS=--max-old-space-size=512`,
`node --test --test-concurrency=1`, and an external deadline. Assert those
effective limits from inside the guard before starting Node, and fail on a
timeout or an out-of-memory termination rather than reporting the wrapper's exit
status. See the [testing guide](.agents/testing/README.md) for the exact
`systemd-run` invocation and the native evidence rules.

Two rules exist because breaking them has repeatedly exhausted a developer's
machine:

- **Never assert on a DOM node.** Deep-equality and failure formatting walk
  jsdom objects recursively and allocate outside V8, so a single failing
  assertion can pass 1 GiB before the runner reports anything. Assert primitive
  values: text, attributes, counts, serialized payloads, and numeric rectangle
  coordinates.
- **Never run a scratch negative control against reverted source.** Reviewers
  verifying that a test would have caught a bug must reason from the diff and
  the test, not rebuild the shell in a throwaway worktree with the fix removed.
  Those runs are unbounded by construction, they duplicate a suite that already
  ran in CI, and they are the usual cause of an out-of-memory kill.

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
  packaging/desktop/, packaging/rpm/ # Archive producer and Fedora RPM source checks
  containers/, ci/  # Fedora 44 harness interface and CI evidence verifiers
  fixtures/         # Shared recorded payloads used across suites
packaging/
  desktop/archive/  # Reproducible per-user archive producer and contract
  rpm/desktop/      # python-tongs and tongs-desktop SRPM sources and harness
  rpm/python-dependencies/ # Companion Python RPM specs and manifest
scripts/            # Repository maintenance and evidence helpers
```

Read [AGENTS.md](AGENTS.md) and the relevant `.agents/*/README.md` subsystem
guide before changing a subsystem.

## License

By contributing, you agree that your contributions will be licensed under the
MIT License.
