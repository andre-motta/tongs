# Optional desktop interface: planning record

Status: **prototype milestone in progress**. The CTO authorized starting the agreed shell comparison; production architecture remains gated.
Workflow baseline: Agent SDLC 0.1.0, source commit
`4e851d1b8a903aa8bebceea078860a21152ee8e8`; see the project profile.

## Agreed outcomes

- Terminal use remains first class, without GUI dependencies for ordinary TUI
  use. The original desktop-extra proposal is being replaced in the design by
  an explicit `tongs --install-desktop` GitHub Releases installer, with automatic
  platform selection. Final release/installer contracts remain at the design gate.
- Use a web UI. Compare Electron against a Python-hosted webview before choosing
  the production shell; no shell winner has been approved.
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
| [#22 Comparison and architecture](https://github.com/andre-motta/tongs/issues/22) | Astra with Sol assessment | #19, #20, #21 | Assigned |

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
