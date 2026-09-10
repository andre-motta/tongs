# Optional desktop interface: planning record

Status: **Electron selected; feature-branch PR workflow approved**.
Hardware GPU acceleration is a mandatory production/release gate and remains
unmet. Detailed production contracts and final main acceptance remain gated.
Workflow baseline: Agent SDLC 0.1.0, source commit
`4e851d1b8a903aa8bebceea078860a21152ee8e8`; see the project profile.

## Agreed outcomes

- Terminal use remains first class, without GUI dependencies for ordinary TUI
  use. The original desktop-extra proposal is being replaced in the design by
  an explicit `tongs --install-desktop` GitHub Releases installer, with automatic
  platform selection. Final release/installer contracts remain at the design gate.
- Use Electron with the shared web UI. The CTO selected it after the common
  native comparison. Hardware GPU acceleration is required for production and
  release; the software-rendered prototype is not sufficient.
- Compare the same React/TypeScript screen and fixtures for clean pip installation,
  Linux compatibility, startup, memory, diff responsiveness, and test automation.
  Users should receive built frontend assets rather than need Node/npm locally.
- Initial supported target: the existing Fedora 44 KDE x86_64 system. The CTO
  narrowed validation to this host; other distributions, desktop environments,
  native Xorg, ARM and other operating systems are deferred expansion. Record the
  actual native Wayland or XWayland backend. Broader platform checks are not
  required for this milestone. Do not set up other OSes/desktops or test VMs on
  the user's PC.
- Target current core review/CI parity plus side-by-side diffs and draft reviews.
  New review capabilities must also ship in the TUI.
- Drafts persist locally across restarts and can be resumed in either interface,
  with protection against simultaneous edits overwriting each other.
- Jira is a stretch goal, not a release prerequisite.
- Desktop plugin support is a first-release requirement. Existing Tongs plugins
  can explicitly opt in to desktop modules; terminal-only plugins continue to
  work unchanged in the TUI and do not need to implement desktop support.

## Existing upstream work

- [#3: Side-by-side diff view](https://github.com/andre-motta/tongs/issues/3)
- [#16: Review draft mode](https://github.com/andre-motta/tongs/issues/16)
- [#13: Jira plugin](https://github.com/andre-motta/tongs/issues/13), stretch only

The feature parent is [#17](https://github.com/andre-motta/tongs/issues/17).
Existing review issues remain related work; the production implementation graph
will be finalized after the shell decision.

## Architectural observations and open decisions

Forge clients, caching, scanning, and diff parsing already have non-Textual
boundaries. Application lifecycle and review orchestration still live in TUI
classes, so shared services need design before adding another interface.
Existing plugin screens, commands, context navigation, and lifecycle hooks are
coupled to Textual. Desktop support must be additive, with separate contributions
and lifecycle handling rather than invoking TUI hooks inside a web shell.

## Required plugin compatibility and proposed design

Confirmed by the CTO: desktop is opt-in, and terminal-only plugins retain their
existing behavior. The public API shape remains a design decision.

- Preserve existing `tongs.plugins` discovery and `[plugins.<name>]` enablement.
  Keep legacy plugins valid without new mandatory methods or metadata.
- Separate common backend services from TUI and desktop contributions. Desktop
  must skip unsupported terminal contributions without treating them as failures.
  TUI startup must not import optional desktop runtimes or require frontend tools.
- An opted-in plugin should declare desktop modules, navigation/commands,
  compatibility information, frontend assets, and documentation through a
  documented extension contract. Keep names and routes scoped to the plugin.
- Package compiled plugin frontend assets with its Python distribution. Users
  should install a plugin without rebuilding Tongs or installing Node/npm.
- Provide a versioned host API for plugin/backend interaction and a documented
  frontend integration contract. Installed Python plugins are trusted code; an
  API facade is not a security sandbox. Define browser-content and bridge
  boundaries during architecture review.
- Deliver plugin-author documentation, migration guidance, packaging instructions,
  a working reference plugin, and a way for users to open plugin help from desktop.

Both shell prototypes must exercise the same independently installed reference
plugin, including discovery, desktop module mounting, one backend interaction,
and access to its bundled documentation. This becomes part of the shell decision
alongside installation, compatibility, performance, and automation evidence.

Release acceptance must cover legacy terminal-only plugins, opt-in plugins in
both interfaces, a plain TUI installation with no desktop extra, disabled plugins,
incompatible/missing desktop assets, plugin errors, and clean lifecycle shutdown.
The exact frontend module format, API version rules, and error recovery behavior
will be specified before dependent implementation is dispatched.

## Other open architecture decisions

GitLab review submission currently posts comments sequentially. Shared draft
services need explicit partial-failure handling, submission progress, and
protection against duplicate posting. Commit changes can invalidate anchors.
The storage schema, bridge protocol, launch/packaging details, and final UX
remain to be designed after the shell comparison.

## Prototype work graph and authority

| Issue | Owner | Depends on | Current state |
| --- | --- | --- | --- |
| [#18 Shared harness and plugin](https://github.com/andre-motta/tongs/issues/18) | Astra, independent Sol review | None | Locally integrated |
| [#19 Common frontend](https://github.com/andre-motta/tongs/issues/19) | Luna xhigh, Sol review | #18 | Locally integrated |
| [#20 Python webview](https://github.com/andre-motta/tongs/issues/20) | Sol high, independent Sol review | #18 | Locally integrated |
| [#21 Electron](https://github.com/andre-motta/tongs/issues/21) | Sol high, independent Sol review | #18 | Locally integrated |
| [#22 Comparison and architecture](https://github.com/andre-motta/tongs/issues/22) | Astra with Sol assessment | #19, #20, #21 | Review |

The experimental contract and code live under `spikes/desktop/`, outside production
packaging. The shared fixture backend, frontend interface, plugin bundle format,
and ownership are defined in `spikes/desktop/CONTRACT.md`. They are prototype-only
contracts, not the public plugin SDK. The common backend has seven passing tests
using an independently installed reference plugin and loopback asset server.

The original checkout contains pending documentation edits and remains intact.
Prototype assignments use isolated worktrees under the temporary directory for
this milestone, with `codex/desktop-*` branches. Only Astra integrates. Each
assignment records its exact foundation commit before dispatch. Native GUI and
loopback tests need the applicable host permissions; fixture traffic uses no
forge credentials. Concurrency is limited to the actual available agent slots.

The CTO's request to start work authorizes this comparison milestone and its
tracking issues. Production shell selection, architecture, and implementation
remain subject to the next design gate after the evidence is reviewed. Tongs
upstream code publication retains its separate CTO gate. Locally complete issues
remain open until upstream delivery.

## Fedora RPM requirement

The CTO confirmed eventual distributable RPM/COPR support first, with official
Fedora repository inclusion considered later. Both shell evaluations must cover
system Python, packaged frontend/plugin assets, separate terminal and desktop
runtime dependencies, desktop integration files, and runtime update ownership.
No virtual environment, source checkout, Node/npm installation, or first-launch
runtime download may be required by the installed RPM. Identify source/build
inputs and work needed for an offline build. RPM release implementation is later
work; feasibility is part of the current architecture comparison.

COPR can build an SRPM and provide a repository for upstream projects outside the
standard Fedora repositories. Its license restrictions still apply. See the
[official COPR user documentation](https://docs.copr.fedorainfracloud.org/user_documentation.html).

## Foundation verification

Independent Sol high review: APPROVED WITH NOTES; all notes resolved in a second
review. Seven tests cover installed plugin compatibility, assets, sidecar protocol,
error recovery, and shutdown. New Python files pass Ruff lint and format checks.
No production source files changed. The unchanged baseline currently has 157 Ruff
findings and four files needing formatting; those checks remain unmet. The sandboxed test run stalled in a cache test; the same test and full suite
completed outside the sandbox. The initial unconstrained MCP 2.1.1 install caused
five misleading "mcp not installed" skips because FastMCP was removed. This is a
pre-existing optional dependency compatibility problem. Validation uses MCP v1 in
the temporary environment, without modifying production dependency declarations.

**Correction, 2026-09-08:** the 157-finding, four-file Ruff baseline above is
historical. `ruff check src/ tests/` and `ruff format --check src/ tests/` both
pass cleanly on the current tree (0 findings; 235 files already formatted),
verified directly with Ruff 0.16.6.

## Active assignment checkpoint

Foundation: `a33d8d3eb4274f072aaf2d50b0092ed4249a00d1`, independently reviewed by
Sol high, seven fixture/sidecar tests passed. #19 uses `codex/desktop-frontend`
(Luna xhigh, owns frontend only); #20 uses `codex/desktop-webview` (Sol high, owns
webview only); #21 uses `codex/desktop-electron` (separate Sol high, owns Electron
only). All depend on this exact foundation revision. Native combined verification
and independent reviews follow implementation. Root owns comparison and tracking.

Baseline rerun: 618 passed with Python 3.14.7, MCP 1.29.1, Textual 8.2.8,
pytest 9.1.1, pytest-asyncio 1.4.0, aiosqlite 0.22.1. Strict MkDocs build passed.
Ruff 0.16.6 found the baseline lint/format issues recorded above.

Frontend candidate `08c44f4ae92c7af45ef9da399f11a28eccdf94be` received Sol
NEEDS CHANGES: plugin load status must reflect errors, and keyboard hints must
match implemented navigation. Luna owns corrections before integration. The
first-module-only plugin UI is a documented comparison limitation. Eight frontend
checks and the build passed; combined native proof remains pending.

Frontend corrections at `2989b48` received independent Sol approval; integrated
locally through `efb3a51` after `2303a71`. All ten frontend tests and the production
build pass in the integration worktree. npm audit reports zero vulnerabilities.
The shared build is now the input for both installed native shell evaluations.

Branding: the CTO requested the existing website icon for desktop. Reuse
`docs/assets/icon.png`, already configured as the MkDocs logo/favicon, in built
frontend and native packages. Keep it local to the distribution.

The website-icon delta received independent Sol approval and is integrated at
`883e0ad`. Source and built PNG hashes match. The shared native probe was also
independently reviewed and now fails if keyboard focus cannot be acquired.
Native DOM unmount/remount and unit-tested cleanup-callback execution are distinct
pieces of evidence.

Distribution update: the CTO wants the eventual RPM attached to GitHub Releases
in addition to any COPR repository. The subsequent explicit installer proposal
supersedes the earlier desktop-extra distribution goal for planning. PyPI size
exceptions are possible but uncertain for an embedded Electron/Node runtime. Release publication and external support requests remain
outside this comparison milestone.

The CTO proposed an explicit `tongs --install-desktop` GitHub Releases download
as the replacement for the desktop extra. This is the proposed primary
distribution flow for the next architecture gate, independent of shell selection.
It must preserve same-interpreter plugin discovery, compatible versions, verified
artifacts, safe atomic installation/recovery, and RPM ownership. Ordinary TUI
startup and desktop launch must not silently download a runtime.

Installer selection must automatically match OS and CPU architecture, plus
distribution/ABI or package format when needed, using a versioned release manifest.
Only the current Fedora target is supported initially; unknown combinations fail
clearly before download. Future OS support adds tested manifest entries and
installer handlers.

Webview candidate `1d5474d` received independent Sol approval and was integrated
at `46328ff`. Root correction `708ab1b` aligns the minimum width to the common
frontend and kills/reaps timed-out capture sessions. A separate Sol re-review
approved this delta, with 14 tests and Ruff checks passing.

Electron candidate `1539bd3` and correction `82ab348` received separate Sol
approval and were integrated at `bfce520` and `2baa331`. Ten tests passed,
including the real sidecar. The correction aligns minimum width and excludes
generated Python bytecode from packaged assets. Final combined artifact builds
and native runs follow these exact integration commits.

## Combined comparison checkpoint

The exact application candidate is
`02ad69685f8511bcd7945a26650f3e2de2e6c908`. Both freshly built host wheels launched
outside the checkout with installed core and reference-plugin wheels. The shared
native probe passed on Wayland in both applications. All 659 automated tests
passed: 618 core, 7 backend, 10 frontend, 10 Electron, and 14 webview. New-code
lint/format and the strict documentation build passed. Baseline core lint/format
remains unmet and is not waived.

**Correction, 2026-09-08:** the 618/659 test counts above are the historical
prototype-comparison baseline, not the current count. The current tree collects
1754 tests under `tests/` excluding `tests/test_mcp` (all passing) and 1759 total
including `tests/test_mcp`, verified directly with `pytest --collect-only` and a
full `pytest` run. 28 `.mjs` test files exist under `tests/desktop/`.

The retained acceptance package is `spikes/desktop/proof/README.md`; the shell
comparison and proposed production design are in `spikes/desktop/DECISION.md`.
Evidence includes actual application captures, versioned input/output hashes,
installed-plugin interaction, limitations and recovery. These repository-root
paths are not public site links until the accepted branch is published.

Astra recommends Electron for the next production design because its separate
GUI/runtime and same-interpreter Python sidecar fit the proposed explicit download
flow. Graphics compatibility remains a mandatory follow-up on the supported host;
Qt remains a viable alternative with better observed default graphics and system
RPM dependency reuse. No shell or production work graph is approved by this
recommendation. Local main promotion remains pending because its checkout is dirty;
no Tongs code was pushed and all upstream issues remain open.

Final independent Sol assessment approved the comparison and acceptance record.
Its only clarity note, explicitly naming the passing terminal-only plugin hook
sentinels, is incorporated in the retained evidence. #18 through #21 are locally
integrated; #22 is in review pending CTO acceptance and production design choices.

## Approved project workflow and next work, 2026-09-07

The CTO selected Electron and made hardware GPU acceleration a mandatory production
and release gate. The existing --disable-gpu evidence remains a historical fixture
result. GPU investigation is tracked in [#25](https://github.com/andre-motta/tongs/issues/25).

The CTO authorized orchestrator-owned GitHub branch `feat/desktop-app`, bootstrapped
from reviewed comparison/evidence `58cf120` plus the independently reviewed workflow
update. Agents use issue-scoped `feat/<work-item>` branches in isolated worktrees,
may sign off and push their commits, and open PRs into `feat/desktop-app`.
Independent senior review remains required; the orchestrator alone gives the final
engineering disposition, resolves integration conflicts, verifies affected checks
and merges. After feature completion the orchestrator opens the complete
evidence-backed PR into `main` for Andre's review. Main merge, release/tag and
deployment are not pre-authorized.

This project override is [#23](https://github.com/andre-motta/tongs/issues/23) and
is defined in [the SDLC profile](../SDLC.md). It supersedes earlier local-only
publication and codex-branch checkpoints in this chronological record. The original
comparison branch remains retained, and the dirty main checkout is preserved.

Every change and gate decision must be issue-tracked. Work is split into small,
independently verifiable tasks with explicit native dependencies, owned files and
stable interfaces. The orchestrator dispatches only ready items whose prerequisite
merge commits exist in the feature branch and have been verified, respecting actual
concurrency and shared-file ownership. Use the project work-item and PR templates.

| Issue | Work | Prerequisites | State / next action |
| --- | --- | --- | --- |
| #23 | Project SDLC, branch bootstrap, PR templates and CI routing | Reviewed comparison 58cf120 | In review; verify then publish bootstrap |
| #24 | Passing feature-branch CI and MCP/lint baseline | #23 verified bootstrap | Planned; inspect and split identified fixes before dispatch |
| #25 | Actual hardware-accelerated Electron evidence | #23 verified bootstrap; #21 code already verified at 02ad696 | Planned; assign Sol investigation after bootstrap |

#24 and #25 can be investigated concurrently with disjoint ownership. Their PRs
cannot bypass required failing checks. Production contract/design work in #22
continues before its dependent implementation graph is approved and dispatched.
The final main PR must include exact tested commits/artifacts, CI, native GPU and
application proof, plugin/TUI compatibility, reviews/resolutions and recovery.

Independent Sol high review approved the #23 workflow, templates, CI branch
filters and GPU gate. Strict MkDocs, local links, YAML trigger inspection and
whitespace checks passed. GitHub issue #23 is the authoritative bootstrap
publication checkpoint; its remote commit and CI outcome are recorded there.

## Verified foundation and production design checkpoint

This checkpoint supersedes the earlier pending bootstrap, lint and prototype GPU
status above. The current verified feature head is
`42c8bfce1db1a38eb872872d1b324e02aadc3ea6`.
[CI run 34147002731](https://github.com/andre-motta/tongs/actions/runs/34147002731)
passed all six displayed checks, including the required aggregate. No branch
protection or ruleset has been configured; the orchestrator checks the required
statuses before integration. Main remains `c8ead224a92d33486c9d97aa27568b8136cb7cb0`.

**Correction, 2026-09-08:** the feature head above is the historical checkpoint
recorded at the time, not the current state. `feat/desktop-app` has advanced
hundreds of commits past it, and S1, S2, S4, S6, S10, S15a, S16, and S17 from the
[production design](desktop-production.md) are implemented in the current tree.
The `main` reference above is still accurate: `main` remains
`c8ead224a92d33486c9d97aa27568b8136cb7cb0`, verified directly.

| Issue / PR | Verified feature merge | Result |
| --- | --- | --- |
| #24 / #26 | `f710767` | Baseline Ruff and format repairs; core tests pass |
| #27 / #29 | `339df10f6658c643936d17328587e6e5755c48d0` | Hosted Fedora 44 rootless Podman harness, including strict expected-failure verification |
| #25 / #30 | `db6a9171caec1f6bda779abefd417fe88d2b4f5b` | Installed prototype GPU proof, physical RTX 5090, XWayland, repeated workflows and post-workflow failure detection |
| #28 / #31 | `42c8bfce1db1a38eb872872d1b324e02aadc3ea6` | Required CI aggregate, core/MCP, desktop fixtures and hosted Podman coverage |

Each received independent Sol review and Astra integration. The deliberate CI
failure PR #32 was closed without merge; its negative result remains evidence.
The fixture diff contains generated stress data, not real repository code.
Prototype graphics evidence is retained under `spikes/desktop/proof/gpu/` and
does not replace final production application acceptance. Native Wayland remains
unsupported; the accepted prototype used XWayland on KDE Wayland.

The CTO requested continuing until the feature is ready for its main PR. The
[production design](desktop-production.md) records the concrete service, plugin,
draft, desktop bridge and installer contracts, final evidence requirements and
dependency graph. [First-wave assignments](desktop-first-wave.md) make the initial
ownership and checks reviewable. The CTO authorized continued implementation and
selected per-user installation, a separate optional RPM README path, immediate
quick comments with explicit Start review, and GitHub-managed artifact provenance.
These choices establish the production baseline under #22. Independent engineering
review and verified feature integration precede dispatch. No production slice is
represented as implemented.

After design integration, the orchestrator publishes the issue graph, dispatches
ready work, consumes handoffs, directs independent review/corrections, verifies CI
and serializes integration. Reevaluate readiness after every integration. Do not leave
finished agent handoffs idle waiting for the CTO to point out ready PRs. Persist
current assignments, prerequisite merge SHAs, review findings and next actions.
Issues stay open until accepted main delivery. Final main merge and publication
retain the CTO gate.
