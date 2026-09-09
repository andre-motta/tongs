# Desktop first-wave assignment drafts

This document preserves the original assignment drafts. For current work status,
use [issue #17](https://github.com/andre-motta/tongs/issues/17) and its children.
The [project SDLC profile](../SDLC.md) supersedes the historical instructions below
to keep every completed issue open until main delivery. Verified scoped work items
may now close under the orchestrator's tracker gate; final product acceptance
remains separate.

**Correction, 2026-09-08:** the blocked, unassigned status described in the next
paragraph was accurate when these drafts were written. It is now historical: the
production design was approved and integrated, and S0, S1, S2, S4, S6, S10, S15a,
S16, and S17 are implemented in the current tree
(`src/tongs/desktop/artifact_contract/`, `src/tongs/services/`, `src/tongs/diff/`,
`src/tongs/plugins/desktop*.py`, `src/tongs/desktop/protocol/`, `desktop/`,
`src/tongs/desktop/installer/`, the release archive under
`packaging/desktop/archive/`, and the Fedora RPM packaging under
`packaging/rpm/`). None of the items below remains unassigned or blocked. For
current status and issue identifiers, use
[issue #17](https://github.com/andre-motta/tongs/issues/17) and its children rather
than the drafting-time snapshot preserved below.

These issue-ready drafts remain planned. None is ready or assigned. Every item is
blocked until the production desktop design passes independent review and Astra
integrates its design PR into `feat/desktop-app`, records the exact merge SHA, and
verifies the required branch checks. Issue identifiers, worktrees, bases, and
readiness evidence must be filled from verified tracker and Git state at dispatch.
The CTO has separately recorded three product decisions: `--install-desktop`
installs the per-user archive while RPM installation remains a README-documented
optional process; quick comments remain immediate and persistent drafts begin only
through explicit Start review; and release verification uses GitHub-managed signing
with the exact official build identity instead of personal TUF keys. These decisions
are the authorized production baseline but do not constitute independent approval
of the complete design. After that review and verified integration, S1, S2, and S4
are the first runnable items, S0 queues behind available author/reviewer capacity,
and S15a remains blocked until S0 is verified and integrated.

## S1: Shared application session and read services

Parent: production desktop initiative. State: planned.
Issue: pending creation after design review. Owner/model: Sol high.
Independent reviewer: a separate senior reviewer at high effort. Integration gate:
the orchestrator.

### Outcome and ownership

Deliver a UI-independent async application session and typed read-service boundary
that owns configuration, repository discovery, forge clients, cache access, events,
and resource shutdown once. Both terminal and desktop follow-up work can consume the
same validated repository and review identities without exposing credentials, API
origins, arbitrary local paths, Textual objects, Electron types, or raw clients.

Owned files are `src/tongs/services/**`, `tests/services/**`, and only the minimal
read-side additions agreed in the approved design under `src/tongs/forges/` and
their focused tests. Coordinate any shared `src/tongs/forges/models.py`,
`src/tongs/forges/base.py`, `src/tongs/forges/github.py`,
`src/tongs/forges/gitlab.py`, or `src/tongs/forges/registry.py` edit with the
orchestrator; no other first-wave item edits those files concurrently.

Once the gate records their final shapes, implement and preserve the contracts for
`RepositoryRef`, `ReviewRef`, `ReviewRevision`, `ReviewSnapshot`, `ServiceError`,
`ServiceEvent`, and `ApplicationSession`. Read services cover repository discovery,
review queries and detail, discussions, commits, and pipeline/job/log reads.
Multi-host queries return successful results alongside explicit per-host failures.
Events are bounded refresh hints with sequence and resource identity. A
revision-bound raw forge-diff read may be exposed for later composition.

Exclude diff conversion, parsing, alignment, and split-row models owned by S2;
draft storage or submission; forge mutations; desktop plugin declarations; desktop
RPC; frontend code; and TUI view/widget migration. The recorded product behavior is
immediate quick comments with explicit Start review, but S1 does not implement that
UI or draft state. Do not add a second forge client or move TUI plugin hooks.

### Dependencies and readiness

- Prerequisite: independent approval of the complete production design, followed by
  the orchestrator's verified integration of its design PR into `feat/desktop-app`.
- Stable interface approval: pending. The names and invariants in the design
  proposal remain candidate interfaces until independent review is recorded.
- Verified prerequisite merge SHA: pending.
- Blocker: independent design approval and verified design integration are absent.
  The orchestrator records both before assignment.
- Readiness evidence: pending exact integration head and required checks.

### Assignment and Git authority

Branch: `feat/desktop-<issue>-shared-services`.
Base: latest verified `feat/desktop-app` SHA after design integration.

The contributor may edit only the owned scope, create coherent commits with
`git commit -s`, push this assigned feature branch, and open or update its PR with
base `feat/desktop-app`. Each commit has a title, a blank line, a one-line body, and
`Co-Authored-By: Codex <actual model> <noreply@openai.com>`. The contributor does
not edit shared tracker state, integrate the PR, push directly to
`feat/desktop-app` or `main`, merge any PR, create tags or releases, publish
artifacts, or expand the approved contract. The orchestrator alone integrates and
maintains shared issue and dependency tracking.

### Acceptance and checks

- `ApplicationSession` is an async context manager with injected registry, cache,
  configuration, discovery, and storage paths. It closes each owned resource once
  after success, partial initialization failure, task cancellation, or normal exit.
- The session issues semantic resource references only through successful discovery,
  configured-forge inbox results, or a validated explicit host operation. Every
  public read rejects a handcrafted but well-formed same-host `RepositoryRef` that
  the current session did not issue, as well as nonpositive review numbers,
  caller-selected API bases, and arbitrary local filesystem paths.
- `RepositoryRef` and `ReviewRef` are semantic values, not bearer credentials or
  authorization tokens. S1 neither exposes them directly to a renderer nor invents
  opaque handles; S6 owns unpredictable session-local handles, kind/session checks,
  expiration on reconnect, and binding those handles back to S1 resources.
- Review snapshots bind existing forge models to a captured revision and explicit
  capabilities without copying all forge fields into a competing model hierarchy.
- Revision-bound diff reads compare the review head before and after a moving forge
  read. A changed or incomplete revision fails with a stable safe error. S1 returns
  raw changes and performs no conversion.
- Service errors contain stable codes, safe messages, retryability, and typed safe
  details. Token text, HTTP bodies, and raw exceptions never enter DTOs or events.
- Tests use injected fakes and mocked forge traffic. Cover partial host failure,
  cancellation and shutdown, duplicate/unknown resources, the well-formed
  same-host unissued-repository case, missing revision data, event bounds, cache
  behavior, and credential redaction.
- Run focused service/forge tests, the full Python suite, Ruff lint, Ruff format
  check, and the required PR checks. No native desktop or GPU proof applies to this
  headless service item.

### Handoff and progress

PR, tested head, evidence, independent findings, integration SHA, and resume notes:
pending. Keep the eventual issue open until the accepted feature reaches main and
use `Refs` language in the intermediate PR.

## S2: Pure forge-diff conversion and split alignment

Parent: production desktop initiative. State: planned.
Issue: pending creation after design review. Owner/model: Luna xhigh.
Independent reviewer: a senior reviewer at high effort. Integration gate: the
orchestrator.

### Outcome and ownership

Deliver pure, deterministic conversion from the existing GitHub/GitLab change
payload shape into `DiffFile` values, plus a renderer-independent split projection.
The projection pairs adjacent deletion/addition runs within one hunk and retains
unmatched cells and original line identities so later TUI and desktop renderers can
virtualize or display the same data without manufacturing comment positions.

Owned files are new `src/tongs/diff/conversion.py`,
`src/tongs/diff/alignment.py`, focused additive models in
`src/tongs/diff/models.py`, and `tests/diff/**` files dedicated to these contracts.
The stable callable boundary is:

```python
def convert_forge_changes(
    changes: Sequence[Mapping[str, object]],
) -> tuple[DiffFile, ...]: ...

def align_hunk(hunk: DiffHunk) -> tuple[SplitDiffRow, ...]: ...
```

`SplitDiffRow` has independent optional old/new cells that retain the originating
`DiffLine`; empty cells have no anchor. Conversion accepts only the documented
GitHub `filename`/`previous_filename`/`patch` and GitLab
`old_path`/`new_path`/`diff` fields plus explicit status/truncation metadata.

Exclude services and revision capture owned by S1; forge clients; TUI views and
widgets; draft/comment submission; desktop DTO paging; React virtualization; and
plugin code. Do not move code into `services/`. S6 composes revision-bound raw data
with this conversion later, and S7 replaces the current view-local conversion only
after both dependencies integrate.

### Dependencies and readiness

- Prerequisite: independent approval of the complete production design, followed by
  the orchestrator's verified integration of its design PR into `feat/desktop-app`.
- Stable interface approval: pending. The proposed alignment pairs only inside a
  hunk and never treats an empty cell as a comment target.
- Verified prerequisite merge SHA: pending.
- Blocker: independent design approval and verified design integration are absent.
- Readiness evidence: pending exact integration head and required checks.

### Assignment and Git authority

Branch: `feat/desktop-<issue>-diff-alignment`.
Base: latest verified `feat/desktop-app` SHA after design integration.

The contributor may edit only the owned scope, create coherent commits with
`git commit -s`, push this assigned feature branch, and open or update its PR with
base `feat/desktop-app`. Each commit has a title, a blank line, a one-line body, and
`Co-Authored-By: Codex <actual model> <noreply@openai.com>`. The contributor does
not edit shared tracker state, integrate the PR, push directly to
`feat/desktop-app` or `main`, merge any PR, create tags or releases, publish
artifacts, or expand the approved contract. The orchestrator alone integrates and
maintains shared issue and dependency tracking.

### Acceptance and checks

- Conversion preserves order, paths, status, counts, language, hunks, line numbers,
  no-newline markers, and explicit binary/truncated/empty state for both forge
  payload shapes. Missing patches do not disappear when metadata describes a file.
- Renames, additions, deletions, mode-only changes, binary files, empty diffs,
  malformed optional fields, truncated patches, and paths containing spaces have
  focused fixtures.
- Alignment never crosses hunk boundaries. Context rows pair the same source line;
  adjacent deletion/addition runs pair by order and keep excess lines as empty-side
  rows. Added-only and deleted-only hunks remain addressable only on the real side.
- Every nonempty split cell round-trips to its original `DiffLine` and valid
  old/new position. Selecting an empty cell yields no position.
- Functions have no UI, filesystem, network, cache, or mutable global dependency.
  Identical inputs produce equal immutable outputs.
- Run focused parser/conversion/alignment/position tests, the full Python suite,
  Ruff lint, Ruff format check, and required PR checks. No native UI or GPU proof
  applies; later renderer items own visual and viewport evidence.

### Handoff and progress

PR, tested head, evidence, independent findings, integration SHA, and resume notes:
pending. Keep the eventual issue open until the accepted feature reaches main and
use `Refs` language in the intermediate PR.

## S4: Additive desktop plugin SDK declarations and lifecycle

Parent: production desktop initiative. State: planned.
Issue: pending creation after design review. Owner/model: Sol high.
Independent reviewer: a separate senior reviewer at high effort. Integration gate:
the orchestrator.

### Outcome and ownership

Deliver a companion `tongs.desktop_plugins` discovery and declaration SDK for
explicit desktop providers while leaving the existing `tongs.plugins` group,
`TongsPlugin`, `PluginContext`, registry behavior, commands, screens, and terminal
lifecycle unchanged. Terminal-only and disabled providers are not imported or
constructed by desktop discovery, and no terminal lifecycle hook runs in desktop.

Owned files are new `src/tongs/plugins/desktop*.py`, focused
`tests/plugins/test_desktop*.py`, and separately packaged plugin fixture projects
under a new test-only fixture directory agreed with the orchestrator. Any
entry-point metadata fixture change is confined to test packages. Production
packaging or shared `pyproject.toml` edits require an exclusive
orchestrator-coordinated window.

Stable SDK types are frozen manifest, module, asset bundle, navigation, command,
method, focus-target, error, and compatibility declarations. A provider implements:

```python
class DesktopPluginProvider(Protocol):
    def manifest(self) -> DesktopPluginManifest: ...
    async def start(self, context: DesktopPluginContext) -> None: ...
    async def call(
        self,
        method: str,
        params: JsonObject,
        context: DesktopCallContext,
    ) -> JsonValue: ...
    async def stop(self) -> None: ...
```

S4 also owns a small independent `DesktopHostFacade` protocol containing declared
read calls, plugin-scoped invocation, notification, event publication,
cancellation, JSON-safe current location, and typed focus-target metadata. It does
not import or duplicate S1 service classes. S6 later validates location data,
binds the facade to S1 services, and supplies IPC/event transport.

Exclude concrete application-service binding; forge writes; sidecar/RPC and asset
serving; Electron/React; Fleet-specific types, routes, commands, or methods; and
changes to legacy TUI hooks. Compiled assets/help are declarations and validated
package-resource resolution in this item, not a frontend build system.

### Dependencies and readiness

- Prerequisite: independent approval of the complete production design, followed by
  the orchestrator's verified integration of its design PR into `feat/desktop-app`.
- Stable interface approval: pending. The companion entry-point group and SDK major
  remain candidate interfaces until independent review is recorded.
- Verified prerequisite merge SHA: pending.
- Blocker: independent design approval and verified design integration are absent.
- Readiness evidence: pending exact integration head and required checks.

### Assignment and Git authority

Branch: `feat/desktop-<issue>-plugin-sdk`.
Base: latest verified `feat/desktop-app` SHA after design integration.

The contributor may edit only the owned scope, create coherent commits with
`git commit -s`, push this assigned feature branch, and open or update its PR with
base `feat/desktop-app`. Each commit has a title, a blank line, a one-line body, and
`Co-Authored-By: Codex <actual model> <noreply@openai.com>`. The contributor does
not edit shared tracker state, integrate the PR, push directly to
`feat/desktop-app` or `main`, merge any PR, create tags or releases, publish
artifacts, or expand the approved contract. The orchestrator alone integrates and
maintains shared issue and dependency tracking.

### Acceptance and checks

- Discovery reads `tongs.desktop_plugins` independently. Tests prove a legacy
  terminal-only provider is listed from metadata without import, construction, or
  any hook call; a disabled desktop provider is also never imported.
- A dual-surface fixture can declare both groups without TUI discovery loading the
  desktop entry point. Duplicate names/IDs, manifest mismatch, incompatible major,
  import failure, start/call/stop failure, and cleanup timeout remain isolated and
  do not hide other providers or the core host.
- Plugin, module, navigation, command, method, asset, invocation, event, and focus
  IDs are stable and namespace-scoped. Only manifest-allowlisted methods and targets
  resolve; unknown or cross-plugin calls fail with safe structured errors.
- Assets, entry ESM, CSS, and bundled help resolve through declared package
  resources. Reject traversal, escaping symlinks, disallowed extensions, duplicate
  resources, and configured size-limit violations. Do not serve or fetch them.
- Desktop lifecycle receives only the independent facade, plugin-scoped config,
  cancellation, safe notification, and event publication. It receives no Textual
  app, forge registry, cache object, token, or raw client.
- An async fixture proves event publication, cancellation observation, bounded stop,
  and cleanup. Document that installed Python/UI plugins are trusted code and this
  scoping is not a malicious-extension sandbox.
- Run focused plugin tests, the full Python suite, Ruff lint, Ruff format check, and
  required PR checks. No Electron/native/GPU evidence applies until S6/S10/S14 bind
  and render the SDK.

### Handoff and progress

PR, tested head, evidence, independent findings, integration SHA, and resume notes:
pending. Keep the eventual issue open until the accepted feature reaches main and
use `Refs` language in the intermediate PR.

## S0: Release manifest and archive contract

Parent: production desktop initiative. State: planned and queued.
Issue: pending creation after design review. Owner/model: Sol high.
Independent reviewer: a separate senior reviewer at high effort. Integration gate:
the orchestrator.

### Outcome and ownership

Deliver the versioned release-manifest and installed-archive schemas, one shared
strict validator used by later producers and consumers, and a deterministic
`tar.gz` reference archive with recorded hashes. This fixes the byte-level handoff
between the future release builder and installer before either implements its own
layout.

Owned files are a new `src/tongs/desktop/artifact_contract/**` package, its packaged
JSON schemas, `tests/desktop/artifact_contract/**`, and clearly synthetic reference
fixtures under that test tree. Any package-data or shared `pyproject.toml` change
requires an exclusive orchestrator-coordinated window. S0 does not edit S15a's
installer modules or S16's future producer modules.

The external `desktop-manifest-v1.json` schema carries schema version,
desktop/core/protocol compatibility, release version, source commit, target
OS/architecture/distribution/ABI, package kind, artifact name/ID, byte count,
SHA-256, extraction limits, and ownership. The archive's `desktop-install.json`
repeats matching version/platform/core compatibility and RPC/plugin API majors plus
the fixed relative launcher path. The initial container is `tar.gz` with no
enclosing release/version directory: `desktop-install.json` and `runtime/` are at
the archive root. The layout contains `runtime/tongs-desktop`,
`runtime/resources/app.asar`, the complete pinned Electron runtime, and
runtime/license inventory. Directories are mode `0755`, ordinary data is `0644`,
and only declared executables are `0755`.

The stable callable boundary uses immutable parsed models and pure validation:

```python
def parse_release_manifest(document: bytes) -> DesktopReleaseManifest: ...
def parse_install_manifest(document: bytes) -> DesktopInstallManifest: ...
def validate_archive_layout(
    entries: Sequence[ArchiveEntry],
    install: DesktopInstallManifest,
) -> ValidatedArchiveLayout: ...
```

The reference builder belongs to tests/tooling and must produce byte-identical
`tar.gz` output for the same fixture inputs; it is not a release publisher.

Exclude release discovery/download, Sigstore or attestation verification, network
access, extraction writes, activation/rollback, CLI behavior, launcher/menu/status,
RPM installation, real Electron builds, release workflows, signing, publication,
and production artifacts. No personal keys or TUF roots are created or maintained.

### Dependencies and readiness

- Prerequisite: independent approval of the complete production design, followed by
  the orchestrator's verified integration of its design PR into `feat/desktop-app`.
- Recorded CTO decisions: the CLI default is the per-user archive; RPM installation
  is a separate optional README process; GitHub-managed signing with an exact
  official build identity replaces personal TUF keys.
- Verified prerequisite merge SHA: pending.
- Blocker: independent design approval and verified design integration are absent.
  S0 then remains queued behind available implementation and independent-review
  capacity; it has no code dependency on S1, S2, or S4.
- Readiness evidence: pending exact integration head, reference-fixture inputs, and
  required checks.

### Assignment and Git authority

Branch: `feat/desktop-<issue>-artifact-contract`.
Base: latest verified `feat/desktop-app` SHA after design integration.

The contributor may edit only the owned scope, create coherent commits with
`git commit -s`, push this assigned feature branch, and open or update its PR with
base `feat/desktop-app`. Each commit has a title, a blank line, a one-line body, and
`Co-Authored-By: Codex <actual model> <noreply@openai.com>`. The contributor does
not edit shared tracker state, integrate the PR, push directly to
`feat/desktop-app` or `main`, merge any PR, create tags or releases, publish
artifacts or metadata, or expand the approved contract. The orchestrator alone
integrates and maintains shared issue and dependency tracking.

### Acceptance and checks

- Both schemas are versioned, bounded, strictly parsed, reject duplicate keys and
  unknown required-enumeration values, and produce immutable typed models. Invalid
  UTF-8, non-finite numbers, wrong types, excessive nesting/count/size, malformed
  semantic versions, and invalid digests fail with stable safe errors.
- Release entries preserve exact artifact names and distinguish per-user archive
  from RPM. Missing/duplicate targets, duplicate or normalization-colliding names,
  unsupported schema versions, incompatible ranges, and conflicting same-version
  descriptors are rejected without selecting or downloading an artifact.
- Layout validation rejects absolute and parent paths, separator/Unicode/case
  normalization collisions, links, devices/FIFOs, unsupported entry types, special
  permission bits, undeclared executables, incorrect modes, missing/duplicate
  required files, an enclosing version-name directory, and declared size/count-limit
  violations. It performs no writes.
- Cross-document validation rejects a release descriptor, archive name/hash/length,
  or inner install manifest that disagrees on version, platform, package kind,
  compatibility, launcher, or layout limits.
- The synthetic reference fixture contains no production executable or credentials,
  is reproducible byte for byte as `tar.gz`, has committed expected hashes, places
  the install manifest and `runtime/` at archive root, passes the shared validator,
  and fails after deterministic corruption or forbidden-entry injection.
- Tests prove S15a can consume the same schemas/validator without copying rules and
  that S16 can later import the producer-facing contract. No test claims that the
  fixture has GitHub provenance or is a releasable desktop.
- Run focused artifact-contract tests, the full Python suite, Ruff lint, Ruff format
  check, package-data checks, and required PR checks. No native application, GPU,
  network, signing, installation, or release evidence applies.

### Handoff and progress

PR, tested head, schema/fixture hashes, evidence, independent findings, integration
SHA, and resume notes: pending. Keep the eventual issue open until the accepted
feature reaches main and use `Refs` language in the intermediate PR.

## S15a: Verified installer metadata, download, and safe extraction

Parent: production desktop initiative. State: planned and queued.
Issue: pending creation after design review. Owner/model: Sol high.
Independent reviewer: a separate senior reviewer at high effort. Integration gate:
the orchestrator.

### Outcome and ownership

Deliver GitHub release discovery, Sigstore verification against the exact official
build identity, platform/compatibility selection, bounded download, and safe staging
extraction for the per-user archive selected by `tongs --install-desktop`. Verify
GitHub-managed attestations for the external manifest and selected archive, then
enforce S0's manifest-bound length/hash and archive-layout rules. Unsupported or
ambiguous targets fail before artifact download. This item creates no installed
desktop and changes no active version, launcher, menu, RPM-owned path, or system
state. It exposes no RPM selection or installation path through the Python CLI;
RPM remains a separate optional README-documented DNF process.

Owned files are new `src/tongs/desktop/installer/models.py`,
`src/tongs/desktop/installer/metadata.py`,
`src/tongs/desktop/installer/download.py`,
`src/tongs/desktop/installer/extract.py`, focused `tests/desktop/installer/**`, and
retained authentic GitHub/Sigstore-shaped verification fixtures plus explicit
negative variants. Consume S0's schemas, typed models, shared validator, and
deterministic archive fixture without copying them. The approved Sigstore dependency
uses the maintained `sigstore>=4.5,<5` API series with one exact release locked in
CI. `pyproject.toml` and lock/config edits require an exclusive
orchestrator-coordinated window because those files are shared.

Stable outputs are a validated exact target selection and a `VerifiedStagedArtifact`
containing schema/version compatibility, normalized supported platform identity,
artifact name, byte count, digest, package kind, verified source commit/build
identity, and private staging path. Callers inject the fixed GitHub transport,
Sigstore verifier, accepted-version store, platform probe, clock, limits, and
staging root for tests. No unsigned, alternate-repository, personal-key, TUF, or
no-verify path exists.

Exclude atomic activation, launcher/menu/status/repair/uninstall, RPM download or
installation, release archive construction, workflow authoring, SBOM production,
repository/release configuration, tag/release creation, publication, and host
mutation evidence. Do not create personal keys, a Tongs TUF repository/root, custom
cryptography, or a runtime `gh` dependency. Tests consume retained verification
fixtures and clearly distinguish them from a published production release.

### Dependencies and readiness

- Prerequisites: independent approval of the complete production design, the
  orchestrator's verified integration of that design into `feat/desktop-app`, and
  S0's reviewed, verified integration on that branch.
- Recorded CTO decisions: `--install-desktop` selects only the per-user archive;
  RPM is a separate optional README process; verification uses GitHub-managed
  signing and the exact official build identity, with no personal TUF keys.
- Verified prerequisite merge SHAs: design and S0 pending.
- Blocker: independent design review, design integration, and S0 integration are
  absent. S15a remains queued until the orchestrator verifies all three and
  allocates author and independent-review capacity.
- Readiness evidence: pending exact integration heads, S0 schema/fixture hashes,
  Sigstore dependency lock, authentic fixture provenance, and required checks.

### Assignment and Git authority

Branch: `feat/desktop-<issue>-verified-installer`.
Base: latest verified `feat/desktop-app` SHA after design integration.

The contributor may edit only the owned scope, create coherent commits with
`git commit -s`, push this assigned feature branch, and open or update its PR with
base `feat/desktop-app`. Each commit has a title, a blank line, a one-line body, and
`Co-Authored-By: Codex <actual model> <noreply@openai.com>`. The contributor does
not edit shared tracker state, integrate the PR, push directly to
`feat/desktop-app` or `main`, merge any PR, create tags or releases, publish
artifacts or metadata, handle production keys, or expand the approved contract.
The orchestrator alone integrates and maintains shared issue and dependency tracking.

### Acceptance and checks

- Enumerate matching stable `desktop-v<semver>` releases from the fixed
  `andre-motta/tongs` repository. Do not use GitHub's general latest-release pointer
  or accept existing `v*` tags, which belong to the separate PyPI workflow.
- Verify the Sigstore signature, certificate, transparency evidence, in-toto
  statement type, and exact named subjects/digests for both manifest and selected
  archive. A valid DSSE signature without matching statement semantics fails.
- Require issuer `https://token.actions.githubusercontent.com`, repository
  `andre-motta/tongs`, builder
  `andre-motta/tongs/.github/workflows/release-desktop.yml`, the exact
  `desktop-v<semver>` tag ref, its independently resolved source commit, and the
  authorized `push` trigger. Wrong repository, workflow, event, ref, source SHA,
  subject, name, digest, certificate, or transparency evidence fails closed.
- Strictly parse bounded verification bundles and the S0 manifest. Reject duplicate
  keys, malformed/unsupported schemas, missing or duplicate platform targets, and
  incompatible core/protocol/plugin API ranges. Only an explicit architecture alias
  table normalizes before exact OS/architecture/distribution/ABI matching.
- `--install-desktop` selects only the matching per-user archive. An RPM-only,
  duplicate, wrong-kind, unsupported, or ambiguous result fails before artifact
  download. The shared verifier may validate RPM provenance for later packaging
  checks, but this CLI path never downloads, extracts, or installs it.
- Require immutable production release metadata. Persist the highest accepted
  desktop release and refuse implicit downgrade or equal-version conflicting digest
  or source commit. An explicit older version still requires full provenance and
  integrity verification. Document that GitHub discovery does not guarantee global
  freshness to a first-time client or survive compromise of the authorized
  repository/workflow; do not claim TUF-equivalent freeze protection.
- Enforce S0's manifest-bound artifact name, length, SHA-256, package kind, platform,
  compatibility, extraction limits, and inner `desktop-install.json` agreement.
- Download tests cover truncation, oversized response, digest mismatch, interrupted
  transfer, destination collision, retry cleanup, redirect/source policy, timeout,
  cancellation, and concurrent staging without exposing partial output as verified.
- Extraction rejects absolute paths, `..` traversal, path-prefix tricks, symlinks,
  hard links, device/special files, duplicate/conflicting entries, excessive member
  count, per-file or total expanded-size excess, and writes outside the private
  staging directory. Failure cleans only the owned staging area and preserves any
  previously active installation.
- Positive tests use the retained authentic attestation shape and S0 reference
  archive to produce identical validated metadata and extracted bytes. Negative
  tests assert the intended identity, statement, integrity, selection, download, or
  extraction failure rather than accepting any unrelated setup error.
- Run focused installer tests, the full Python suite, Ruff lint, Ruff format check,
  dependency/security checks required by the eventual PR, and required CI. No host
  installation, native Electron, RPM, signing ceremony, release, or GPU proof
  applies to this slice.

### Handoff and progress

PR, tested head, fixture/artifact hashes, evidence, independent findings,
integration SHA, and resume notes: pending. Keep the eventual issue open until the
accepted feature reaches main and use `Refs` language in the intermediate PR.
