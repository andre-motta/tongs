# Testing

## Checkout-local setup

Run tests from the checkout or worktree root. Create `.venv` there so source,
dependencies, and reports remain bound to the revision under test:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,mcp]" ruff
```

Use `.venv/bin/python`, `.venv/bin/pytest`, and `.venv/bin/ruff` directly when a
shell does not keep activation. Do not borrow another worktree's environment.
Most unit and integration tests mock forge network access. Dependency installs
and deliberately hosted evidence workflows are separate network boundaries.

## Python checks

During development, run the smallest test module that exercises the changed
contract. Before handing off a code change, run the full core and MCP split plus
Ruff unless the work item's approved profile says otherwise:

```bash
.venv/bin/pytest tests/ --ignore=tests/test_mcp -v
.venv/bin/pytest tests/test_mcp -v \
  --junitxml="/tmp/tongs-mcp-$$.junit.xml"
.venv/bin/python tests/ci/verify_desktop_ci.py mcp-report \
  --path "/tmp/tongs-mcp-$$.junit.xml"
.venv/bin/ruff check src/ tests/ packaging/
.venv/bin/ruff format --check src/ tests/ packaging/
```

The explicit MCP report check proves the optional tests ran and did not pass by
import-error skip. Keep generated reports outside the checkout unless a fixture
specifically requires a repository path. Use a process-scoped filename such as
the shell's `$$` rather than a fixed name, since a fixed report path collides
between concurrent worktrees; `ci.yml` writes to
`"$RUNNER_TEMP/reports/mcp.junit.xml"`, inside each job's private runner temp directory, for the same reason.

Python test areas include:

- `tests/services/`: `ApplicationSession`, service identities, reads, events,
  mutations, review submissions, and workspace utilities
- `tests/state/drafts/`: durable draft transitions, ownership, corruption, and
  recovery
- `tests/test_forges/`: GitHub/GitLab parsing and requests through
  `httpx.MockTransport`
- `tests/test_views/` and `tests/test_widgets/`: Textual workers, messages,
  bindings, and rendering behavior
- `tests/desktop/protocol/`: framing, handles, paging, cancellation, review/CI,
  and utility operations
- `tests/desktop/installer/` and `tests/desktop/artifact_contract/`: safe
  extraction, signatures, manifests, activation, status, and lifecycle
- `tests/plugins/`: desktop provider declarations, discovery, resources, and
  lifecycle bounds
- `tests/integration/desktop/`: production reports, installed-core composition,
  candidate attestation, and process acceptance

## Forge and service test patterns

Use `httpx.MockTransport` to test HTTP requests without network access:

```python
async def handler(request: httpx.Request) -> httpx.Response:
    assert request.url.path == "/api/v4/projects/acme%2Fapp/merge_requests"
    return httpx.Response(200, json=[])

transport = httpx.MockTransport(handler)
client = httpx.AsyncClient(
    base_url="https://gitlab.example.com/api/v4",
    transport=transport,
)
```

Prefer real immutable dataclasses and small protocol fakes over broad mocks.
Inject `ApplicationSession` resources at their declared protocols, call
`start()` before reads, and always close the session. Assert `ServiceErrorCode`
or protocol error codes and safe public messages, not private exception text.

For mutations, cover the service-issued identity and revision checks as well as
the forge call. Test stale revisions, wrong parent handles, duplicate operation
IDs, partial submission progress, retry/reconciliation, and cleanup failures
when those branches are part of the changed behavior.

## Textual tests

Use Textual's `app.run_test()` and `Pilot`. Wait for workers or a specific
observable state rather than sleeping:

```python
async with app.run_test() as pilot:
    await pilot.pause()
    await app.workers.wait_for_complete()
    assert app.screen.query_one("#inbox").display
```

Patch or inject `TUIServiceAdapter` methods at the service boundary. A view test
should not create a concrete forge client unless the integration under test is
the adapter itself. Exercise messages and public actions instead of directly
mutating widget internals where possible.

## Production desktop checks

The production package is top-level `desktop/`; `spikes/desktop/` contains
historical comparison fixtures. Install and run the production suite with the
checkout's Python interpreter:

```bash
npm ci --prefix desktop
npm run build --prefix desktop
TONGS_TEST_PYTHON="$(command -v python)" npm test --prefix desktop
```

The build script prepares assets, runs TypeScript checking, and creates build
output. The test script repeats that build before Electron main and React
renderer tests. Focused Node tests can run against built output with
Node's test runner, for example:

```bash
node --test --test-concurrency=1 tests/desktop/renderer/diff.test.mjs
```

Rebuild first whenever TypeScript source changed. Do not use a stale `dist/`
from another revision as source evidence.

The reference desktop provider is an independently installable package. Install
it into the same Python environment as Tongs, then run its Python and prebuilt
ESM tests as documented in `examples/desktop-plugin/README.md`.

## Bounded local Node procedure

Node and jsdom can allocate outside the V8 heap. Bound the complete process tree
with an operating-system cgroup or equivalent runner. The standard local limits
for this repository are:

- memory maximum: 1 GiB
- swap maximum: 0
- task maximum: 64
- V8 old-space limit: `NODE_OPTIONS=--max-old-space-size=512`
- Node test concurrency: `--test-concurrency=1`
- an external deadline appropriate to the focused or full command

Before starting Node, fail closed unless the running process is inside the
intended guard and its effective memory, swap, and task limits match. Preserve
the command's exit status, timeout/OOM result, and peak resident memory when the
work item requires evidence. Do not treat a wrapper exit of zero as success if
the authoritative test or proof report is absent or failed.

On a Linux host with a user systemd manager and cgroup v2, this checkout-relative
example creates a unique service, checks the effective limits from inside it, and
returns the guarded command's status. `systemd-run` expands `${...}` in the
command line before `/bin/sh` receives it, so shell parameter expansions in the
inline script must escape the leading dollar as `$$`.

```bash
unit="tongs-node-${PPID}-${RANDOM}-$(date +%s)"
set +e
systemd-run --user --unit="$unit" --wait --collect \
  --property=MemoryMax=1073741824 \
  --property=MemorySwapMax=0 \
  --property=TasksMax=64 \
  --property=StandardOutput=journal \
  --property=StandardError=journal \
  --setenv=NODE_OPTIONS=--max-old-space-size=512 \
  --working-directory="$(pwd)" \
  /usr/bin/timeout --signal=TERM --kill-after=15s 20m \
  /bin/sh -eu -c '
    cgroup=$(awk -F: '"'"'$1 == "0" { print $3 }'"'"' /proc/self/cgroup)
    root="/sys/fs/cgroup$${cgroup}"
    test "$(cat "$root/memory.max")" = 1073741824
    test "$(cat "$root/memory.swap.max")" = 0
    test "$(cat "$root/pids.max")" = 64
    test "$NODE_OPTIONS" = --max-old-space-size=512
    npm run build --prefix desktop
    exec node --test --test-concurrency=1 \
      tests/desktop/renderer/diff.test.mjs
  '
status=$?
journalctl --user -u "$unit.service" --no-pager --output=cat
set -e
exit "$status"
```

Change only the deadline and test file list for the intended check. The
documentation site build is the one exception to the task limit: run
`npm ci --prefix site` and `npm run build --prefix site` with
`--property=TasksMax=256` and assert `pids.max` is 256. Node starts a worker
thread per CPU for both `npm` and `astro`, so on a host with many cores the
build aborts at startup with SIGABRT under 64 tasks. This is a thread-creation
failure, not an out-of-memory kill. Keep the 1 GiB memory maximum and zero
swap; the build peaks near 0.9 GiB. The desktop build has a similar limit:
esbuild starts a thread per CPU, so on such a host add
`--setenv=GOMAXPROCS=4` to the guard for `npm run build --prefix desktop` and
`npm test --prefix desktop`, or they abort with `newosproc` before any test
runs. A stricter
parent cgroup can make an effective value lower; in that case, adapt the
fail-closed assertions to prove the effective limit is no greater than the
stated maximum. If a user systemd manager is unavailable, use an equivalent
container or cgroup-v2 wrapper that performs the same in-guard assertions and
preserves the child exit status.

DOM failure formatting can recursively inspect large jsdom objects and exhaust
memory outside V8. Assert primitive values such as text, attributes, counts,
serialized payloads, and numeric rectangle coordinates. For layout proofs,
compare each visible control rectangle with its viewport and owning panel and
assert sibling panels do not overlap. Avoid passing complete DOM nodes or cyclic
objects to deep-equality assertions.

## Native Electron evidence

Native tests under `tests/desktop/native/` launch real Electron and therefore
need an exclusive, coordinated display/GPU window. Do not run them concurrently
with another native proof and do not change host display, power, sandbox, SELinux,
or GPU settings to make them pass.

Electron filters unsupported `NODE_OPTIONS`, including the V8 old-space option
used by the Node test runner. For native packaged Electron, the 1 GiB complete-
process cgroup, zero swap, task limit, and external deadline are authoritative;
do not claim a 512 MiB renderer/main-process heap limit. See Electron's
[`NODE_OPTIONS` environment-variable contract](https://www.electronjs.org/docs/latest/api/environment-variables#node_options).

A valid native result binds all of the following:

- exact source commit and tree, plus the source paths actually imported or built
- the guarded launcher exit and the authoritative JSON report result
- expected sidecar, Electron, and fixture process cleanup
- screenshots or geometry records required by the acceptance flow
- observed hardware acceleration/GPU evidence when the gate calls for it

Synthetic sidecars and fixture data prove deterministic UI behavior. They do
not prove live forge access or a production installer. Headless Electron and
Podman prove different contracts from native Fedora/KDE/GPU execution.

## Hosted pre-merge checks

`.github/workflows/ci.yml` selects lanes by the paths a pull request changes,
using the rules in `tests/ci/ci_plan.py`:

<!-- ci-lanes:begin -->
| Rule | Paths | Lanes |
| --- | --- | --- |
| docs | `docs/**`, `site/**`, `.agents/**`, `*.md`, `.github/ISSUE_TEMPLATE/**`, `.github/PULL_REQUEST_TEMPLATE.md`, `.github/PULL_REQUEST_TEMPLATE/**`, `.github/FUNDING.yml`, `.github/linters/**` | docs |
| readme | `README.md` | docs, core |
| core-read-docs | `.agents/ci/README.md`, `.agents/testing/README.md`, `.github/linters/**` | docs, core |
| tui | `src/tongs/views/**`, `src/tongs/widgets/**`, `src/tongs/mcp/**`, `src/tongs/app.py`, `src/tongs/commands.py`, `src/tongs/helpers.py`, `src/tongs/__main__.py` | lint, core |
| core-tests | `tests/test_*.py`, `tests/test_cache/**`, `tests/test_diff/**`, `tests/test_forges/**`, `tests/test_mcp/**`, `tests/test_plugins/**`, `tests/test_scanner/**`, `tests/test_views/**`, `tests/test_widgets/**` | lint, core |
| sidecar | `src/tongs/cache/**`, `src/tongs/config.py`, `src/tongs/desktop/**`, `src/tongs/diff/**`, `src/tongs/errors.py`, `src/tongs/forges/**`, `src/tongs/plugins/**`, `src/tongs/scanner/**`, `src/tongs/services/**`, `src/tongs/state/**`, `src/tongs/tui_services.py`, `tests/__init__.py`, `tests/desktop/**`, `tests/fixtures/**`, `tests/integration/**`, `tests/plugins/**`, `tests/services/**`, `tests/state/**`, `examples/desktop-plugin/**` | lint, core, desktop_fixtures, desktop |
| packaging | `LICENSE`, `scripts/build_desktop_archive.py`, `scripts/build_desktop_sbom.py`, `src/tongs/__init__.py`, `src/tongs/desktop/artifact_contract/**`, `src/tongs/desktop/installer/**`, `tests/integration/desktop/archive_evidence.py`, `tests/integration/desktop/candidate_attestation.py`, `tests/integration/desktop/rpm_payload_contract.py`, `tests/integration/desktop/sbom_evidence.py`, `tests/desktop/installer/fixtures/**`, `tests/packaging/**` | lint, core, desktop_fixtures, desktop, packaging |
| spikes | `spikes/**` | desktop_fixtures |
| ci-infrastructure | `.github/workflows/**`, `.github/scripts/**`, `tests/ci/**`, `tests/containers/**` | full graph |
| build-configuration | `pyproject.toml`, `requirements/**`, `packaging/**`, `desktop/**`, `.gitignore` | full graph |
| (unmatched) | any other path | full graph |

Matching rules add their lanes together, and a push to `main`, the `ci:full` label or any doubt about the diff selects the full graph.
<!-- ci-lanes:end -->

The lanes run Ruff lint and format (`lint`), core and MCP tests on Python 3.12
and 3.13 (`core`), the docs build and Markdown lint (`docs`), desktop fixture
checks and the production Electron/renderer suite (`desktop_fixtures`), the
Fedora 44 Podman probe (`fedora_podman`), the desktop production evidence
(`desktop`), and the archive, SBOM and RPM lifecycle jobs (`packaging`). Any
doubt about the diff, a push to `main`, a rule change under `tests/ci/**`, or
the `ci:full` label selects the full graph. Preview a local branch with
`python tests/ci/ci_plan.py explain --base origin/main`; the
[CI guide](../ci/README.md#hosted-workflows) has the full policy.

The always-run `CI aggregate` is the single required check. It recomputes the
plan and fails when a selected lane is failed, cancelled, skipped or missing, or
when a deselected lane did not report `skipped`. Bind review evidence to the
actual checkout commit and tree for the current PR run, and to the lanes that
run actually selected: a reduced plan proves only its lanes. The final
production desktop release assembly is a separate workflow and evidence
boundary; do not infer native GPU, installer, signing, RPM, or release
acceptance from the `CI aggregate` alone.

## Test quality

Test observable contracts and meaningful failure modes. Avoid tests that merely
repeat a constant, duplicate implementation logic, or assert private call order
without a user-visible or security reason. For file authorities, include
symlink, non-regular-file, replacement/race, size, hash, and cleanup behavior as
relevant. For async lifecycles, exercise cancellation, timeout, early completion,
late completion, and exactly-once cleanup where the contract depends on them.
