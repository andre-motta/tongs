# Production desktop design and implementation baseline

Issue: #22, under #17. Status: production baseline authorized; independent
engineering review and verified feature integration required before dispatch.
Author/architecture owner: the orchestrator. Verified foundation: feature commit
`42c8bfce1db1a38eb872872d1b324e02aadc3ea6` and CI run 34147002731.
This document replaces the disposable `prototype-1` contract for production work;
it does not turn the spike into a supported SDK by declaration.

**Correction, 2026-09-08:** the verified-foundation commit above is the historical
starting point recorded when this baseline was authorized, not the current state.
`feat/desktop-app` has advanced hundreds of commits past it, and S1, S2, S4, S6,
S10, S15a, S16, and S17 from the dependency table below are implemented in the
current tree. Read the tree and Git log directly for the current integration state
rather than relying on this foundation commit.

CTO decisions, 2026-09-07: the default installer is per-user with the invoking
Tongs environment; RPM installation is a separate optional process documented in
the repository README. Quick comments remain immediate, with explicit Start review
mode for persistent drafts. Download verification uses GitHub-managed artifact
signing and the official repository/build identity, without personal release-key
maintenance. These answers supersede the earlier TUF proposal.
The [issue decision record](https://github.com/andre-motta/tongs/issues/22#issuecomment-5574209930)
retains the public-safe summary.
Together with the CTO's instruction to continue implementation until the feature
is ready for its main PR, these decisions authorize this production baseline.
The orchestrator owns its technical contracts and decomposition. New material
product or architecture changes return to the CTO; routine implementation details
and review corrections proceed under the existing feature-branch authority. Final
main merge and publication retain their separate gate.

## Delivery outcome

The main PR delivers a working optional Electron desktop application using the
same Python forge, discovery, cache and review logic as the terminal. Real GitHub
and GitLab review/CI data replaces generated fixture content. Terminal launch
remains the default and never starts or downloads Electron. Existing terminal-only
plugins remain valid without any desktop implementation.

Initial native support is Fedora 44 KDE x86_64, using the verified XWayland
configuration. Native Wayland, other operating systems/architectures, Jira, and
Fleet's own module implementation are outside this delivery. The host SDK must
unblock Fleet without depending on it. Prepare OS/architecture dispatch for later
installers, but reject unsupported targets explicitly.

Required feature scope includes real inbox/repository discovery, review detail,
unified/split diffs, inline/general comments and discussions, review verdicts,
supported MR actions, commits, pipeline/job/log navigation and existing supported
retry/cancel actions. Persistent review drafts and split diffs work in both
interfaces, reusing #16 and #3. Capabilities drive availability; unsupported forge
actions show an explanation instead of a control that silently does nothing.

## Shared Python application boundary

Add `src/tongs/services/`. A single async application session owns configuration,
repository discovery, `ForgeRegistry`, response cache and resource shutdown.
Services import no Textual widgets, Electron or frontend libraries. Existing
forge implementations and shared dataclasses remain the adapters to remote APIs.

Use immutable resource identities (`RepositoryRef`: configured forge host and
canonical project path; `ReviewRef`: repository plus review number) and a
`ReviewRevision` carrying head SHA and forge-specific base/start references.
Python identities are semantic data, not authorization tokens. The desktop bridge
maintains a session-local registry of opaque, unpredictable repository/review
handles issued from successful discovery or configured-forge inbox results.
Each privileged call resolves a handle and verifies its resource kind and current
session; caller-supplied hostname/project/number tuples cannot create authority.
Plugin location bindings use the same registry. Handles expire on reconnection;
the backend restores durable draft identities and reissues authorized handles.
The current desktop workspace defaults to repositories discovered on this machine
under the configured scan root, normally `~/git`. Its repository navigation and
default All reviews view stay within that discovered set, including after a
discovery refresh. An empty workspace explains local cloning/configuration and
offers discovery refresh; it does not offer online repository opening.
The current unreleased repository projection includes the discovered forge
hostname for local Host sorting. During protocol-major-1 development the desktop
also accepts the earlier exact three-field repository projection with no hostname;
it treats that metadata as unknown and keeps it last instead of deriving a host
from another display field. Both shapes reject unknown keys.

Keep the existing backend capability to accept configured-forge inbox results
and explicitly open another validated configured-host project. This capability
does not imply frontend exposure: the CTO deferred all remote-opening forms,
menus and commands from the current desktop release. A future explicit temporary
repository workflow is tracked in
[RFE #70](https://github.com/andre-motta/tongs/issues/70), outside this initiative's
delivery requirements. Define its UX and lifetime when that RFE is planned.

No renderer value chooses an API origin or arbitrary file. The Python service
itself admits only configured hosts and registered repositories; trusted
in-process plugins remain trusted code, not a separate adversarial security
principal. Preserve this backend boundary while keeping the default frontend
scoped to local discovery.

Service groups are repositories/inbox, review/detail/discussion, diff, CI,
drafts/submission, and plugin host. Reads return typed models and explicit partial
results per host. Mutations return receipts/outcomes, invalidate affected cache
entries and emit narrow resource-change events. Credentials and forge tokens stay
in Python and never appear in DTOs, logs, plugin frontend state or renderer storage.

Extract the existing forge-change-to-diff conversion out of the view layer.
Migrate TUI callers to services in a bounded follow-up, preserving existing
bindings, legacy plugin context properties and graceful partial-host failure.
Inject transports/registries and storage paths for tests. Do not introduce a
second forge client implementation inside desktop code.

The initial Python contract uses these names and invariants. S1 owns their
definitions and documents any additive implementation detail before dependents
start. Existing forge models can be composed into snapshots; do not duplicate
every field merely to create a service layer.

| Contract | Required shape and behavior |
| --- | --- |
| `RepositoryRef` | Frozen `hostname: str`, `project_path: str`; no caller-selected API base or local path |
| `ReviewRef` | Frozen `repository: RepositoryRef`, `number: int`; positive review number |
| `ReviewRevision` | Frozen `head_sha: str`, `base_sha: str`, optional `start_sha: str`; missing required revision metadata makes anchored writes unavailable |
| `ReviewSnapshot` | Review identity, existing `MRDetail`, revision and explicit forge capabilities |
| `ServiceError` | Stable code, safe message, retryability and typed safe details; no raw exception/HTTP response serialization |
| `ApplicationSession` | Async context manager; injectable registry/cache/config/storage; one owner closes its resources and emits bounded invalidation events |
| Read services | Async repository discovery, `list_reviews(query)`, `get_review(ref)`, `get_discussions(ref)`, `get_commits(ref)` and pipeline/job/log reads; per-host failures preserve successful results |
| `ServiceEvent` | Sequence, resource identity, kind and revision when known; a refresh hint, never a credential-bearing adapter payload |

S1 may expose revision-bound raw forge diff changes for the next integration,
but owns no conversion/alignment implementation. S2 owns the pure conversion
and projection. S6 composes the two into the paged `DiffSnapshot` desktop read;
S7 adopts the same conversion in the TUI. A snapshot captures both diff contents
and revision: when a forge lacks an immutable diff-read endpoint, compare head
before/after reading and fail/retry the read if it changed. Never label a moving
diff with an unrelated captured SHA.

## Diff and draft contracts

Pure shared diff alignment produces rows with independent old/new cells and
original `DiffLine`/file positions. Pair adjacent deletion/addition runs within a
hunk, retain unmatched cells, and never align across hunk boundaries. Binary,
renamed, deleted, empty, no-newline and truncated files retain explicit states.
Selecting an empty cell cannot manufacture a comment anchor. Desktop virtualizes
rows; TUI uses its own renderer and falls back to unified at narrow widths.

Store drafts in a separate application-data SQLite database, never the evictable
HTTP cache. Use restrictive new-file permissions, transactional migrations and
optimistic revision checks across processes. A draft has a stable UUID, review
identity, captured head revision, monotonically increasing edit revision, body,
verdict and ordered stable-ID comments. Comments distinguish general, inline and
reply forms and retain original anchor/head or discussion identity.

Every edit/discard/submit names the expected draft revision. A conflicting edit
returns the current revision and preserves both the stored content and the
caller's unsaved text for explicit resolution. Head changes mark anchors stale;
do not silently retarget comments onto new code. Reopening TUI or desktop recovers
the same draft. Starting a draft is explicit; existing immediate-comment flows
remain available and clearly distinguished from draft mode.

Submission is a durable state machine: editable, submitting, partially submitted,
outcome unknown, or submitted. Lock the attempted snapshot transactionally before
network I/O and record each confirmed remote receipt. Bind inline submission to
the captured forge revision. GitHub uses native batch reviews; GitLab records
sequential outcomes and sends the requested verdict only after comment delivery
is confirmed. Unknown network outcomes require reconciliation before any retry;
never replay the whole batch blindly. A known sent comment is not sent again.
If the API cannot establish a unique remote outcome, present the ambiguity for
explicit user reconciliation and preserve the draft. Do not promise exactly-once
delivery where the forge offers no idempotency primitive.

`DraftSnapshot` contains `id: UUID`, `review: ReviewRef`,
`revision: ReviewRevision`, `version: int`, `body: str`, an optional supported
review decision, an ordered tuple of `DraftComment`, and `DraftState`.
`DraftComment` carries a UUID and a discriminated general/inline/reply payload.
An inline anchor retains old/new paths and line numbers, selected side, optional
range start/side and context fingerprint; a reply retains its remote thread ID.
`save_draft` and `discard_draft` require the expected version; submission requires
both draft ID and expected version. Draft storage owns these definitions in S3,
using S1 identities and revisions. Submission receipts record attempt ID, frozen
draft version, completed step IDs/remote IDs and completed/partial/unknown outcome.
Recovery after a process dies during an in-flight write starts as outcome unknown,
not editable. An explicit user reconciliation records its decision durably.

## Additive production plugin SDK, version 1

Keep the `tongs.plugins` entry point group, config enablement and all existing
`TongsPlugin` TUI hooks unchanged. Add the companion `tongs.desktop_plugins`
entry-point group for explicit desktop providers. A dual-surface distribution
declares both groups with the same canonical entry-point name. Desktop lists
terminal-only providers through entry-point metadata without importing or
constructing them. No terminal hook is invoked by desktop, and TUI discovery
never loads the desktop group. Disabled providers are not imported. Reject
duplicate IDs, mismatched manifest IDs and incompatible API majors with an
isolated diagnostic instead of crashing the application.

A desktop contribution declares API major, stable plugin-local module/command
IDs, title, packaged resource roots, entry module and bundled help resource.
Compiled ESM/CSS/help assets ship in the Python distribution. End users need no
Node build. Validate resolved resource containment, symlinks, extensions and size
limits; never serve arbitrary package or filesystem paths. Plugin identifiers
scope navigation, commands, assets, invocation and event delivery.

The provider implements `manifest()`, `async start(context)`,
`async call(method, params, call_context)`, and `async stop()`. The manifest
contains typed frozen declarations and the allowlisted plugin method names.
Separate async desktop lifecycle receives a surface-independent context with a
narrow service facade, plugin-scoped config, notifications, cancellation and event
publication. It does not receive `ForgeRegistry`, credentials or the Textual app.
An invocation receives the plugin-local method, JSON params and request context.
Commands and navigation declare data and allowlisted targets, not executable
renderer strings. A module exports `mount(container, api)` and returns cleanup.
Unmount unsubscribes events and cancels owned pending work; shutdown awaits bounded
plugin cleanup. Show incompatible, failed, disabled and terminal-only states.

The JS facade exposes plugin-scoped `invoke`, event subscription, navigation,
notifications and bundled help. Fleet can use this for session list/detail,
typed focus-target metadata, asynchronous updates and partial/unavailable states.
The host does not implement Fleet-specific session or focus operations.

Installed Python and UI plugins are trusted code. Scoping prevents accidental
cross-plugin calls; it is not a sandbox against a malicious installed extension.
Document this boundary plainly. Plugin failure must not hide the core inbox or
break shutdown. Ship a separately installable production example plus legacy
terminal-only and incompatible fixtures.

S4 owns plugin declarations and a small host-facade protocol in
`plugins/desktop*`, with no dependency on concrete service classes. Core location
data is JSON-safe hostname/project/review identity, validated when the host binds
it to S1 resources in S6. The initial facade provides only declared read calls,
notification/event publication and plugin-owned invocation; core forge writes
must pass the normal application command/confirmation flow. This ownership makes
S1 and S4 independently implementable without competing edits to service models.
S4 does not add a second core model hierarchy or silently expose raw adapters.

## Desktop bridge and content boundary

Production Python code lives in `src/tongs/desktop/`; Electron and React sources
live under top-level `desktop/`. Preserve spikes as clearly labelled historical
evidence until the production acceptance package supersedes them.

Use protocol major 1 over child stdin/stdout with request IDs, bounded JSON
frames, structured redacted errors, events and cancellation. Stdout contains only
protocol frames; diagnostics go to stderr. The sidecar runs with the launching
Tongs interpreter using `sys.executable -E -P -m tongs.desktop.sidecar` and a safe
working directory, so installed plugins resolve in that exact environment without
trusting PYTHONPATH or the caller's working directory. Preserve that interpreter's
normal user-site policy; do not accidentally remove user-installed plugins with
an implicit isolated-mode switch.
Handshake validates core version, protocol major and supported capabilities before
loading UI. Unknown methods, parameters, resource references or protocol versions
fail closed. Bound outstanding work and queued events; coalesce replaceable
resource-change events. A gap/overflow prompts refetch instead of silent stale UI.
Initial limits: 256 KiB request, 8 MiB response, 64 KiB event, 64 pending requests
and 256 queued events. Larger diff/log results use bounded pages with snapshot
and revision identities; an expired snapshot returns an explicit refetch error.
The app owns mutation operation IDs and persistence, not the transport request ID.

Reads may be cancelled; cancellation of a mutation after dispatch does not mean
the remote action was rolled back. Surface unknown outcomes explicitly. EOF,
crash and shutdown reject pending calls and clean up child processes. Renderer
reload reconnects through a fresh handshake and recovers durable state.

The renderer has context isolation and sandboxing, no Node globals, no raw network
client and only operation-specific typed preload methods. Validate the calling frame and
origin on every privileged request. Deny navigation, popups, webviews and arbitrary
external URLs. User-requested external links pass a narrow HTTPS validator.
Render forge Markdown with raw HTML disabled and safe links; no remote scripts,
images or embedded active content. Text/code/logs render as text, not HTML.

Keep static assets separate from mutation RPC. Register a standard, secure
`tongs://app/` scheme on the exact Electron session, with no CSP bypass or service
worker privilege. Serve only bundled frontend and privately staged validated
plugin resources, with strict origin, path, MIME, cache and CSP behavior. Python
reports the allowed asset bundles during the handshake; the renderer cannot
choose arbitrary source paths. Replace the prototype loopback asset server;
there is no HTTP RPC or production listening TCP socket. The shell/security implementation is tested
against forged frames, navigation, traversal and malformed/oversized messages.

## Installation, packaging and release boundary

`tongs --install-desktop` explicitly installs the per-user archive. `tongs desktop` launches
an installed compatible desktop; ordinary startup performs no download. Status,
update, repair and uninstall are explicit operations. CLI parsing preserves
existing terminal flags and environment behavior.

The release manifest identifies schema, desktop/core/protocol compatibility,
release version and target OS/architecture/distribution/ABI, package kind,
artifact name/ID, byte count and SHA-256, extraction limits and ownership.
Normalize only an explicit architecture alias table before exact matching. Only
the fixed official repository is a production source. Missing, duplicate or
unsupported targets fail before download.

An earlier S0 artifact-contract slice supplies the versioned JSON schema, archive
layout, producer/consumer validation helpers and deterministic reference fixture.
The signed external `desktop-manifest-v1.json` describes the downloadable artifacts.
The initial user archive is `tar.gz`, with no enclosing version-name directory.
Inside it, `desktop-install.json` records matching version, platform,
core compatibility, RPC/plugin API major and fixed relative launcher path.
`runtime/tongs-desktop` is the executable; `runtime/resources/app.asar` contains
the application. The complete pinned Electron runtime and its licenses are retained
under `runtime/`. Directories use mode 0755; data uses 0644 and explicitly declared
executables 0755. Reject absolute/parent paths, duplicate or normalization-colliding
names, links, device/FIFO entries and special permission bits. Both installer tests
and the eventual archive producer consume the same schema/fixture and validation
helper. S16 must produce and reinstall a real archive through that contract, not
replace it with another layout after consumer tests pass.

Use the maintained Python `sigstore` verifier for a GitHub artifact attestation of
the release manifest. Publish the verification bundle beside the manifest as an
immutable release asset. Verify the Sigstore signature, certificate and transparency
evidence against GitHub's OIDC issuer and the exact authorized Tongs repository,
release workflow, tag and source commit. Validate the verified in-toto statement's
type and subject SHA-256 against the downloaded manifest, then verify every chosen
artifact's manifest-bound length/hash. A valid DSSE signature alone does not validate
the statement semantics. Bound and strictly parse manifests/statements; reject
duplicate keys, wrong subjects, wrong build identities and unsupported schemas.
Use public library policy APIs, not handwritten cryptography or a runtime `gh`
dependency. Pin supported dependency versions and exercise the actual emitted
certificate/predicate shape before accepting release workflow integration.
The initial verifier uses the supported `sigstore>=4.5,<5` API series with a locked
tested release in CI. The fixed builder is
`andre-motta/tongs/.github/workflows/release-desktop.yml`; release tags have the
form `desktop-v<semver>`, separate from the existing `v*` PyPI trigger. Require the
GitHub Actions issuer `https://token.actions.githubusercontent.com`, certificate
workflow identity ending in that exact tag ref, repository/ref/source-SHA policies
and the authorized `push` trigger. Resolve the tag's commit independently through
the fixed GitHub repository; do not trust a manifest's self-declared commit.
The attestation covers both manifest and artifact subjects with exact names/digests.
Enumerate matching stable desktop releases rather than treating GitHub's general
latest-release pointer as the desktop channel. Require immutable production release
metadata and reject an equal version with a conflicting digest or source commit.

The trust bootstrap is the installed Tongs distribution, its Sigstore verifier
and public-good trust configuration, plus the fixed GitHub build policy. Sigstore
operates its trust-root updates; Tongs does not maintain its own TUF repository or
private signing keys. Verification dependencies ship with Tongs, with no verifier
network activity during normal terminal launch. Electron remains a separate download.
Persist the highest accepted desktop release version and refuse implicit downgrade;
an explicit requested older version still must pass full provenance/integrity checks.
GitHub release discovery provides availability and newest-version selection. This
does not prove global freshness to a first-time client or protect against a fully
compromised authorized repository/workflow. Document that limit rather than claiming
TUF-equivalent freeze/rollback protection.

Release CI produces the manifest and artifact/SBOM attestations with GitHub-managed
OIDC signing. Repository/release settings and actual publication remain separately
CTO gated. Unpublished candidate validation exercises the real packaged application
and the verifier with retained authentic verification fixtures plus negative
fixtures; it must clearly distinguish fixture trust from a production release.
No test root or verification bypass is reachable from the production CLI.
Before the first production release, verify the real release builder's emitted
attestation and the complete install path. The main PR includes this explicit
pre-publication check, rather than an unconfigured personal trust-root placeholder.
The initial RPM is covered by the same GitHub provenance verification. Do not claim
an independent OpenPGP RPM signature or require personal RPM-signing keys; later
COPR/repository distribution can define its own package signing requirements.

Per-user archives install into versioned application-data directories. Bound
downloads/extraction, reject escaping paths and unsafe links, stage completely,
lock concurrent installation, and atomically activate a verified version. Keep
the previous working version after interruption or failure. Never overwrite
RPM-owned files. Explicit status identifies selected installation and ownership.

Desktop-menu registration binds an absolute persistent Tongs console launcher
and Python interpreter/environment, including desktop-entry Exec escaping.
Validate that environment at launch, negotiate compatibility again and show
repair instructions if it disappears or becomes incompatible. Never silently
substitute another Python from PATH. Transient uvx environments cannot silently
become permanent menu registrations. The RPM provides system Python Tongs and
desktop packages with compatible versions and a system launcher. Its menu uses
the system environment; a pipx/venv invocation uses its own plugin environment.
Status exposes both; explicit installation selection is required to switch
ownership. RPM installation is a separate optional README process using the
release artifact and DNF. The Python installer does not download or install the
RPM, elevate privileges or modify `/usr`.

Produce a Fedora RPM and a per-user archive as releasable build artifacts, with
desktop entry/icon, licenses, runtime inventory and reproducible prepared inputs.
Build/install/upgrade/uninstall RPM tests run in disposable hosted Fedora Podman
environments. COPR publication and official Fedora inclusion are later actions.
Do not install RPMs or alter SELinux/system packages on the CTO's host for tests.
Native GPU proof uses the installed per-user candidate. Verify that the RPM has
the identical Electron/application payload and launch policy, and exercise its
package-manager lifecycle on hosted Fedora. If its payload or launch policy
differs, exercise that extracted RPM payload natively in an isolated directory
too. Report that as an unpacked-payload test, never as a native RPM installation;
the final evidence identifies which package behavior was tested on hosted CI.
Actual GitHub releases, tags, PyPI publication and COPR enablement remain outside
this branch's authority. The final main PR can include tested release workflows
and unpublished candidate artifacts without publishing a production release.

## Acceptance before the main PR

Required checks remain the stable aggregate plus expanded production tests. Cover
actual services using mocked forge transports, serialization and process failure,
untrusted content, legacy/desktop plugins, draft conflicts and uncertain remote
submission, artifact integrity and installer recovery. Extend hosted Fedora RPM
and archive installation tests. Negative tests must prove the failure reaches
the gate, rather than accepting any unrelated setup failure.

Native evidence uses the built production artifact and real renderer: discover
repositories, load real public/authenticated read-only forge diffs, navigate
discussions/CI, exercise installed plugin UI/help, and recover drafts across TUI
and desktop. Run mutation scenarios against a clearly labelled controlled forge
test server, through the actual application. Live comments/approvals/CI actions
require authorization for a concrete disposable test target; no production data
is mutated merely to obtain proof. Real read-only views and mocked mutations are
reported separately.

Repeat the hardware GPU gate on the final installed application with physical
device and process evidence, post-workflow acceptance, repeated launches and
negative controls. Retain screenshots/recordings, redacted outputs, exact source
and artifact hashes, commands, environments, review findings and recovery notes.
UI controls must represent implemented behavior; no fixture branding or generated
review data appears in production mode. Keyboard/focus/resize/light-dark and
terminal regression checks are required, alongside explicit accessibility limits.

After all acceptance criteria pass, the orchestrator opens `feat/desktop-app`
against `main` with the complete evidence package. Main merge and release remain
CTO decisions.

## Dependency-driven implementation slices

These are planning IDs pending reviewed design integration and native GitHub issue creation.
Each issue will include exact files, accepted interfaces, base SHA, checks and Git
authority before dispatch. Reuse #3/#16 rather than duplicating their outcomes.

| ID | Scope and ownership | Dependencies | Role |
| --- | --- | --- | --- |
| S0 | Release manifest/archive layout schema, shared validation and deterministic reference artifact | approved design | Sol high |
| S1 | Shared session, resource/revision models and read services; `services/`, necessary forge metadata | approved design | Sol high |
| S2 | Pure diff conversion/alignment and position edge cases; `diff/` | approved design | Luna xhigh, Sol review |
| S3 | Durable draft store, revisions and migrations; `state/drafts*` | S1 | Sol high |
| S4 | Companion desktop entry-point declarations/context/lifecycle; `plugins/desktop*` | approved design | Sol high |
| S5a | Revision-bound forge mutations, receipts, ranges, thread identity and cache invalidation | S1 | Sol high |
| S5b | Durable safe draft submission/reconciliation | S3, S5a | Sol high |
| S5c | CI mutation services: typed pipeline/job identity, capabilities, receipts/unknown outcomes, invalidation and events; services/tests only | S1 | Sol high |
| S6 | Production sidecar RPC, events/cancellation and validated asset service | S1, S2, S4 | Sol high |
| S7 | TUI service migration preserving existing workflows | S1, S2, S5a, S5c | Sol high |
| S8 | TUI split diffs, #3 | S2, S7 | Sol high |
| S9 | TUI persistent draft/review workflow, #16 | S3, S5b, S8 | Sol high |
| S10 | Production Electron shell/preload/lifecycle and protocol security | S6 | Sol high |
| S11 | Real-data React workspace, repository/inbox/detail/diff reads | S1, S2, S6 | Sol high |
| S12 | Desktop discussions, comments, drafts and review mutations | S5b, S10, S11 | Sol high |
| S13 | Desktop pipeline/job/log actions | S5c, S10, S11 | Sol high |
| S14 | Production plugin UI/help/commands, example and compatibility harness | S4, S10, S11 | Sol high + bounded Luna work |
| S15a | GitHub/Sigstore provenance, platform/compatibility and verified download/extraction | S0 | Sol high |
| S15b | Atomic activation, same-interpreter launcher/menu/status/ownership | S15a, S6, S10 | Sol high |
| S16 | Release archive build, licenses and prepared sources | S0, S10, S11, S14, S15b | Sol high |
| S17 | Fedora RPM build and hosted install/upgrade/recovery checks | S16 | Sol high |
| S18 | Production CI and adversarial integration acceptance | S9, S12, S13, S14, S15b, S17 | Sol high |
| S19 | User/plugin documentation and migration guide | stable implemented interfaces, final S18 | Luna xhigh, Sol review |
| S20 | Final native/GPU/TUI acceptance and evidence-backed main PR | all required slices | Astra + independent Sol |

S1/S2/S4 can start independently; S0 queues within available capacity and then
unblocks S15a. Reserve
review capacity as authors finish, and do not keep idle completed handoffs waiting
for CTO prompts. Shared-file changes are serialized. Routine corrections proceed
autonomously; material contract/scope changes return to the design gate.

## Design references

Primary references informing implementation, to be checked against pinned tool
versions during the corresponding work:

- [Electron security](https://www.electronjs.org/docs/latest/tutorial/security)
  and [custom protocols](https://www.electronjs.org/docs/latest/api/protocol/).
- [Sigstore verifier](https://sigstore.github.io/sigstore-python/api/verify/verifier/)
  and [policy APIs](https://sigstore.github.io/sigstore-python/api/verify/policy/).
- [GitHub attestation action](https://github.com/actions/attest).
- [Python interpreter isolation flags](https://docs.python.org/3/using/cmdline.html).
- [GitHub artifact attestations](https://docs.github.com/en/actions/concepts/security/artifact-attestations)
  and [immutable releases](https://docs.github.com/en/code-security/concepts/supply-chain-security/immutable-releases).
- [XDG paths](https://specifications.freedesktop.org/basedir/)
  and [desktop launcher escaping](https://specifications.freedesktop.org/desktop-entry/latest/exec-variables.html).
- [RPM signing](https://rpm.org/docs/6.1.x/man/rpmsign.1).
