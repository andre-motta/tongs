# CI and releases

Reference for maintainers and for contributors who want to know what CI runs.
The everyday contributor workflow is in [CONTRIBUTING.md](../../CONTRIBUTING.md);
this guide covers the full check set, the hosted workflows, releases, and what
to do when a hosted check fails.

## Full local check set


Run Python tools through the checkout-local environment:

```bash
source .venv/bin/activate
pytest tests/ --ignore=tests/test_mcp -v
pytest tests/test_mcp -v --junitxml="/tmp/tongs-mcp-$$.junit.xml"
python tests/ci/verify_desktop_ci.py mcp-report \
  --path "/tmp/tongs-mcp-$$.junit.xml"
ruff check src/ tests/ packaging/ scripts/release-evidence/ examples/
ruff format --check src/ tests/ packaging/ scripts/release-evidence/ examples/
```

The MCP extra is required so MCP tests execute rather than skip on import. The
`$$` in the report path keeps concurrent worktrees from overwriting each other's
report; CI writes to `"$RUNNER_TEMP/reports/mcp.junit.xml"`, which is private to each job's runner, for the same
reason. Use a focused path during development, then run the checks appropriate to
the changed source.

Locally one `pytest tests/` run covers every Python suite. In CI each test file
has exactly one pre-merge home, which `tests/ci/test_lane_test_ownership.py`
derives from the workflows and enforces:

| Suites | Job |
|---|---|
| `tests/ci`, `tests/containers` | `Lint, format and CI harness tests` (`lint`) |
| `tests/test_*.py`, `tests/test_*/`, `tests/desktop` (Python), `tests/plugins`, `tests/services`, `tests/state`, the draft and process acceptance test | `Core and MCP` (`core`) |
| the other `tests/integration` suites and `tests/packaging` | the native payload job of `Desktop production evidence` (`desktop`) |
| `tests/desktop/electron`, `tests/desktop/renderer`, `examples/desktop-plugin/tests` | the desktop TAP job of `Desktop production evidence` (`desktop`) |

The Fedora 44 Podman probe reruns a small installed-wheel smoke subset on
purpose; every file in it also has one of the homes above.

## Desktop and other suites

Install, build, type-check, and test the production Electron/React shell with:

```bash
npm ci --prefix desktop
npm run build --prefix desktop
TONGS_TEST_PYTHON="$(command -v python)" npm test --prefix desktop
```

`npm run build --prefix desktop` prepares assets, runs TypeScript checking, and
creates build output. The test command repeats that build before the Electron
and renderer test suites. Run it under the
[bounded local Node procedure](../testing/README.md#bounded-local-node-procedure).
The installable production provider example has focused Python and prebuilt ESM
checks:

```bash
python -m pip install ./examples/desktop-plugin
python -m pytest -q examples/desktop-plugin/tests/test_provider.py
node --test examples/desktop-plugin/tests/test_dashboard_module.mjs
```

See the example [README](../../examples/desktop-plugin/README.md) for its declared
modules, resources, and same-environment installation contract.

The Fedora RPM sources have source-level checks that invoke no DNF, Podman, RPM,
Cargo, or network operation:

```bash
.venv/bin/pytest tests/packaging/rpm/desktop tests/test_plugins/test_plugin_system.py -q
.venv/bin/ruff check packaging/rpm/desktop tests/packaging/rpm/desktop src/tongs/mcp/plugin.py
.venv/bin/pytest tests/packaging/rpm/python-dependencies -v
```

Documentation changes build the Astro + Starlight site in `site/`, which reads
the Markdown in `docs/` through a committed symlink. `starlight-links-validator`
makes the build strict: a broken page link or anchor fails it. Run both commands
under the [bounded local Node procedure](../testing/README.md#bounded-local-node-procedure);
the build needs about 0.5 GiB:

```bash
npm ci --prefix site
npm run build --prefix site
```

The output lands in `site/dist`, including `CNAME`. `docs/SDLC.md`,
`docs/site-plan.md` and `docs/work/` are left out by the content collection glob
in `site/src/content.config.ts`, so they are never built, indexed or listed in
the sitemap, and nothing published may link into them.

## Hosted workflows

Pull requests to `main` and every push to `main` run `.github/workflows/ci.yml`.
Its lanes are selected by the paths a change touches. The first job, `Plan CI
lanes`, runs `tests/ci/ci_plan.py compute` on the checked-out synthetic merge
commit, classifies the paths it changes relative to its first parent, and
publishes one output per lane; each lane job runs only when its lane is
selected. `tests/ci/ci_plan.py` is the single source of that policy: its
`RULES` table maps path patterns to lanes, and matching rules add their lanes
together. Read the rules there, or ask the program what a branch would run:

```bash
python tests/ci/ci_plan.py explain --base origin/main
```

In outline, documentation selects `docs`, plus the lane of any test that reads
the document; terminal modules and core-only suites select `lint` and `core`;
the shared sidecar modules select `lint`, `core` and `desktop`; the renderer,
shared and shell stylesheet source under `desktop/src` adds `archive`; the
Electron main and preload source, the shell page and the other archive and RPM
inputs select `packaging`; and the files the Fedora probe reads (the example
plugin and its smoke subset) add `fedora_podman`. The CI infrastructure, the
build configuration and any path no rule matches select the full graph.
`tests/ci/test_ci_plan_drift.py` derives what each job reads from its source
and fails when a rule no longer covers it.

| Lane | Job |
|---|---|
| `docs` | `ci.yml` `Docs build`: the Astro + Starlight site build (`npm run build --prefix site`), the CNAME and repository-only record checks, and the pinned Markdown linter |
| `lint` | `ci.yml` `Lint, format and CI harness tests`: Ruff over `src/`, `tests/`, `packaging/`, `scripts/release-evidence/` and `examples/`, then the `tests/ci` and `tests/containers` suites, whose report must show no skip |
| `core` | `ci.yml` `Core and MCP`: the core suites listed above, on Python 3.12 and 3.13 on every plan |
| `fedora_podman` | `ci.yml` `Fedora 44 Podman` probe |
| `desktop` | `Desktop production evidence`: source identity, the production shell and renderer TAP, installed core, and the native payload job with the integration and packaging contract suites |
| `archive` | the archive, archive evidence and archive SBOM jobs of `Desktop production evidence` (implies `desktop`) |
| `packaging` | the RPM lifecycle job of `Desktop production evidence` (implies `archive`) |

A change under `desktop/src/renderer`, `desktop/src/shared` or to a stylesheet
in `desktop/src/main/shell` runs the archive and SBOM jobs but not the RPM
lifecycle or the Podman probe. Electron main and preload source and
`desktop/src/main/shell/index.html` also run the RPM lifecycle, because only
its launch of the packaged app catches a broken main process, preload bridge,
CSP meta or script tag. Any other path under `desktop/src` runs the full
graph. The desktop manifests, lock, build script, TypeScript configuration
and assets are build configuration and run everything. Core always runs Python
3.12 and 3.13, and the aggregate requires both receipts whenever core is
selected.

The plan fails closed. Any doubt selects the full graph and records why: an
event other than `pull_request`, a checkout that is not the expected two-parent
merge of the pull request head, a missing or zero SHA, any git error, an empty
diff, a path that matches no rule, or any unexpected exception. Changes to the
rules themselves live under `tests/ci/**`, and every workflow and build
configuration path also selects the full graph, so a rule change always runs
everything. Every push to `main` runs the full graph.

To force the full graph on a pull request, add the `ci:full` label. `ci.yml`
reruns on any label event, not only `ci:full`, and each new run of a pull
request cancels its in-flight run; pushes to `main` never cancel each other.
Removing `ci:full` does not start a run, so the reduced plan takes effect on the
next push.

Preview the plan for a local branch before pushing:

```bash
python tests/ci/ci_plan.py explain --base origin/main
python tests/ci/ci_plan.py explain --base origin/main --label ci:full
```

It diffs `HEAD` against its merge base with `--base` and prints the changed
paths, the lanes, the `ci.yml` and `Desktop production evidence` jobs that
would run, the core interpreters, and the reasons.

The single required check is the always-run `CI aggregate` (job
`desktop-pr-gate`). It recomputes the plan itself, unions it with the planning
job's plan (or uses the full graph when that job did not succeed or its plan is
unreadable), and then requires every selected lane to succeed and every
deselected lane to report exactly `skipped`. A failed, cancelled or missing
selected lane fails the aggregate, and so does a deselected lane that ran. A new
commit supersedes earlier results.

One more workflow runs on pull requests:

| Workflow | Trigger |
|---|---|
| `release-desktop.yml` (Unpublished desktop candidate attestation) | PRs into `main` matching its path filter; pushes to its named branches; pushes of a stable `vX.Y.Z` tag; and a manual `workflow_dispatch` dry run |

`desktop-rpm.yml` (Desktop Fedora RPM), `desktop-python-rpms.yml` (Desktop Python
companion RPMs) and `desktop-archive.yml` (Reproducible desktop archive) are manual
only (`workflow_dispatch`); the production gate's `rpm-lifecycle` and `archive` jobs
prove the same source rebuild, companion closure, lifecycle and byte-identical
rebuild against the receipt-bound fresh archive on every run that selects the
`packaging` lane, and the `archive` jobs alone run whenever the `archive` lane
is selected.

`docs.yml` runs that build on Node 22 and deploys `site/dist` to GitHub Pages,
but only on a push to `main` or a manual `workflow_dispatch`. It also checks
that `site/dist/CNAME` names `www.tongs.tools` and that no repository-only
record reached the output. On pull requests the `docs` lane of `ci.yml` runs
the identical build and checks, without the upload, then lints the Markdown,
so a documentation-only change runs that lane alone.
`tests/ci/test_production_workflow_contract.py` keeps the lane's commands and
Node setup identical to the deploy's.

## Releases

Two workflows run on a stable `vX.Y.Z` tag. `publish.yml` builds the core
and publishes it to PyPI. `release-desktop.yml` builds the beta desktop archive for
that version, attests it with GitHub-managed Sigstore, verifies the attestation
with the installer's own code path, rebuilds the Fedora RPMs from the signed
archive, and publishes everything to the GitHub Release of the same tag,
created as a draft and published only after every asset is confirmed. Its
`release-publish` job is the only job in the repository holding
`contents: write`. The two workflows do not wait for each other, so a core
version can be on PyPI before its desktop release exists; the installer reports
that state and asks for a retry.

A `workflow_dispatch` of `release-desktop.yml` from `main` with `dry_run` set
runs the build, signing, verification and RPM rebuild and publishes nothing.
It is the rehearsal to run before pushing a tag:
`gh workflow run release-desktop.yml --ref main -f dry_run=true`. The publish
job, including the draft-release lookup, only runs on the tag itself. `tests/ci/test_release_publication_workflow.py`
pins the trigger, permission and step-order contract.

`docs/releases/<tag>.md` is both a site page and the GitHub Release body.
`release-desktop.yml` drops its leading Starlight front matter block into
`$RUNNER_TEMP` and passes that copy to `verify --notes` and
`gh release create --notes-file`. Write the prose so it reads on GitHub too:
absolute `https://www.tongs.tools/...` links, inline code such as
`` `Ctrl+G` `` instead of `++key++` markup.

## Pinned actions and tools

Every GitHub Action referenced from a workflow under `.github/workflows` is
pinned to a full commit SHA with a trailing `# vX.Y.Z` comment, never a mutable
tag; `tests/ci/test_production_workflow_contract.py` enforces this for every
job- and step-level reference in every workflow, with no exceptions. The
documentation toolchain is pinned by `site/package-lock.json`, which `npm ci`
installs exactly, and every direct dependency in `site/package.json`, Astro and
Starlight included, is an exact version rather than a range; a test in the same
module enforces both. Other tools a workflow installs ad hoc, such as
`ruff` and the `build` the installed-core job uses, are not yet pinned and
are tracked by #162.

Jobs that hold a signing or write token install nothing that resolves fresh.
In `release-desktop.yml`, `candidate-attestation` (`id-token: write`) and
`release-publish` (`contents: write`) run
`pip install --require-hashes --only-binary=:all: -r requirements/release.lock`
and then `pip install --no-deps --no-build-isolation -e .`. The lock pins, by
hash and as wheels only, the core runtime dependencies, Sigstore and its
closure, `jsonschema` for the SPDX generator, and the `hatchling`, `hatch-vcs`
and `editables` build backend. Its direct inputs are in
`requirements/release.in`; regenerate it with the `uv pip compile` command on
its second line, moving `--exclude-newer` forward, and check the result in a
clean Python 3.12 venv with those two installs. `GH_TOKEN` is set only on the
publish steps that call `gh`. The `build` job of `publish.yml` holds no token,
but the PyPI publish job uploads its wheel and sdist as they are, so it installs
the same lock (which also pins `build`) and runs
`python -m build --no-isolation`. `tests/ci/test_release_lock.py` enforces all of
this for every job in every workflow that holds a write or OIDC token and for
that build job. `tests/ci/test_workflow_hygiene.py` shares its install scanner
and allows a follow-up `--no-deps` install without hashes only for local wheels
and local source trees built with `--no-build-isolation`.

Updates to these pins, the release lock included, will arrive as Dependabot
pull requests once #162 (v1.1.0) lands; until then, bump them by hand.

## Re-running a hosted check

**Download the evidence you need before you re-run anything.** Every gate
artifact is named with the commit and the run id but not the run attempt, and
a lane that re-runs overwrites its own gate artifact. Diagnostic artifacts,
such as the raw TAP reports and the RPM producer output, keep the attempt in
their names. Once a re-run has started, the evidence that would have explained
the failure may already be gone.

**A partial re-run can turn the aggregate green.** "Re-run failed jobs" re-runs
the failed jobs and the jobs that depend on them, including `CI aggregate`.
Lanes that passed earlier keep the gate artifact of the attempt that produced
it, and a lane that re-runs replaces its own, so the aggregate finds one
artifact per selected lane whichever attempt produced it. Each receipt still
records the attempt that produced it; the aggregate accepts any attempt of the
same run up to its own and rejects a later one. The archive transfer artifact
keeps the attempt in its name, and the archive job publishes that attempt so
an archive lifecycle or SBOM job re-run alone checks the transfer against the
attempt that built it.

A lane that is still failing still fails the aggregate. Every `needs` result is
the job's latest attempt, and the aggregate checks those results before it
downloads any evidence and again in the final verifier. A gate artifact left
over from an earlier attempt of a failing lane therefore never counts.

Gate artifacts are kept for 14 days. A partial re-run after that fails with
"Artifact not found" for a lane that passed earlier; re-run all jobs or push a
new commit instead.

**Never retry a flaky gate blindly.** A test that passes on the second attempt
is a defect in the test, and this repository fixes it rather than rolling the
dice: see #210, #215 and #218, each of which was a real ordering bug in an
assertion that sampled state before it had settled. File the flake, fix the
test, and say in the pull request which one it was. A re-run used to get past a
red check without an explanation is not acceptable evidence.

## Fedora harness and release boundaries

The Fedora harness interface is:

```bash
tests/containers/run-fedora-44.sh --output-dir <empty-directory-outside-checkout>
```

The production desktop release assembly gate is still separate from the
`CI aggregate`. Do not describe a fixture, Podman, or headless Node run as
native Fedora, GPU, installer, signing, RPM, or release acceptance.

## Local Node limits

For local Node work, bound the whole process tree, not only the V8 heap. Run
the command inside a `systemd-run --user` unit with a 1 GiB memory limit, zero
swap, a 64-task limit, `NODE_OPTIONS=--max-old-space-size=512`,
`node --test --test-concurrency=1`, and an external deadline. Assert those
effective limits from inside the guard before starting Node, and fail on a
timeout or an out-of-memory termination rather than reporting the wrapper's exit
status. See the [testing guide](../testing/README.md) for the exact
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
