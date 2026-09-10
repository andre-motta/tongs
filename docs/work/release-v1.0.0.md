# v1.0.0 release preparation handoff

Prepared for issue #54 against the v1.0.0 candidate head
`f899063cbe0d84da9719dc0ad637d1f3dd6af24d` on `feat/desktop-app`.

**This document prepares a release. It does not perform or authorize one.** No
tag, PyPI upload, GitHub Release, RPM or COPR publication, workflow change or
availability announcement follows from merging it. Every value below was read
from the candidate head; nothing here changes a version number. The one value
the audit found that would be wrong at tag time needs a compatibility decision
and is raised as decision 1 rather than edited in this patch.

Its predecessor, the read-only audit written at integration commit `a92ca22`,
is retained locally at `.worktrees/desktop-v1-release-preparation.md`. Where the
two disagree, this document is the later reading.

## 1. Version numbering

### 1.1 Core Python distribution

The core version is derived entirely from Git tags. There is no static version
anywhere and no generated version file.

- `pyproject.toml:1-3` builds with `hatchling` and `hatch-vcs`.
- `pyproject.toml:5-7` declares `dynamic = ["version"]`.
- `pyproject.toml:69-70` is the whole version stanza: `[tool.hatch.version]`,
  `source = "vcs"`. There is no `version_file`, no `raw-options`, and no
  `fallback-version`, so the scheme is the default `guess-next-dev` plus
  `node-and-date`.

Runtime code never reads a `__version__` attribute; it reads installed
distribution metadata through `importlib.metadata`:
`src/tongs/desktop/protocol/server.py:1614-1618`,
`src/tongs/desktop/installer/launcher.py:43-47,120`,
`src/tongs/desktop/installer/commands.py:246`, and
`src/tongs/plugins/desktop_registry.py:157-158`.

At the candidate head, `git describe --tags` reports `v0.4.1-516-gf899063` and
the installed distribution reports `0.4.2.dev516+gf899063cb`. The newest tag in
the repository is `v0.4.1`.

**The tag the CTO must push for core 1.0.0 is `v1.0.0`, on the exact release
commit.** With a clean tree at distance zero, hatch-vcs produces exactly
`1.0.0`; no source edit is needed or wanted. `publish.yml` already checks out
with `fetch-depth: 0`, which is what makes that derivation correct in CI.

### 1.2 Desktop application

**The desktop shell has no version of its own to report.** Searches across
`desktop/src` and `desktop/scripts` for `app.getVersion`, a `package.json`
import, or any embedded version constant return nothing. The only version the
shell knows is the core version handed to it on the command line
(`desktop/src/main/launch.ts:6,81`) and forwarded in the handshake
(`desktop/src/main/sidecar.ts:219`). The one hardcoded string nearby,
`client: "tongs-electron-44.2.0"` at `sidecar.ts:221`, is the Electron version.

`desktop/package.json:3` holds `"version": "0.1.0"`. The package is
`private: true`, is never published to npm, and is never read by `desktop/src`.
The CI gate reads the file for Electron and dependency pinning
(`tests/ci/desktop_production_expectations.py:56`), not for a version number.
Setting it to `1.0.0` changes nothing at runtime; it is a cosmetic decision
about what is visible inside `app.asar`.

**What actually carries a desktop version is the archive payload.** The producer
takes `release_version` as an input and derives the archive filename, the
internal `desktop-install.json`, and the external `desktop-manifest-v1.json`
from it. To make the desktop payload say 1.0.0, these values must change
together:

| Surface | Current value |
|---|---|
| `packaging/desktop/archive/run_hosted.sh:30` | `release_version=0.5.0` |
| `.github/workflows/release-desktop.yml:331` | attested subject `tongs-desktop-0.5.0-fedora44-x86_64.tar.gz` |
| `packaging/rpm/desktop/manifest.json:20,35` | accepted archive filename and `release_version` |
| `packaging/rpm/desktop/manifest.json:21-22,24-32` | the accepted archive's byte count and SHA-256, and eight evidence digests |
| `packaging/rpm/desktop/install_and_verify.sh:581` | asserts the RPM `%{VERSION}` equals `0.5.0` |
| `tests/ci/desktop_production_expectations.py:70` | `DESKTOP_RELEASE_VERSION = "0.5.0"`, the caller-owned gate constant |
| `packaging/desktop/archive/README.md:6,43,45` | prose and examples, already labelled unpublished |
| `packaging/rpm/desktop/README.md:51` | prose describing the lifecycle as a `0.4.9` to `0.5.0` upgrade |

**The RPM manifest digests cannot be hand-edited.** Changing `release_version`
renames and rebuilds the archive, so its byte count, its SHA-256 and every one of
the eight evidence digests change with it, and
`packaging/rpm/desktop/package_contract.py:188-193` verifies each of them against
the real files: `_verify_file` on the archive, then a SHA-256 comparison per
evidence entry, raising `accepted evidence mismatch` on the first difference.
Those values have to be regenerated from a fresh hosted archive run in the same
change. A reader who edits only the two version strings gets a digest mismatch,
which is a far harder failure to read than a version mismatch.

Mirrored test constants also move: `tests/ci/test_desktop_production_expectations.py`,
`tests/integration/desktop/candidate_attestation.py`,
`tests/integration/desktop/test_candidate_attestation.py`,
`tests/integration/desktop/test_archive_evidence.py`,
`tests/packaging/desktop/archive/test_producer.py`,
`tests/packaging/desktop/sbom/test_builder.py`, and
`tests/packaging/rpm/desktop/test_contract.py`.

## 2. Compatibility ranges and `api_major`

There are three separate compatibility mechanisms. Only one is sensitive to the
number 1.0.0, and conflating them is the main hazard in this area.

### 2.1 Plugin API major, not version-sensitive

`DESKTOP_PLUGIN_API_MAJOR = 1` at `src/tongs/plugins/desktop.py:21`. A plugin's
own `manifest.compatibility.api_major` is checked for equality against it in
`src/tongs/plugins/desktop_registry.py:589-601`, which marks a mismatch
`INCOMPATIBLE` with `INCOMPATIBLE_API`. The value relayed at
`src/tongs/desktop/protocol/server.py:1501-1504` is the third-party plugin's
declared major, not a host version.

**Correction to a common description of this path: the consumer is not
`desktop/src/main/sidecar.ts`.** That file contains no `api_major` at all. The
TypeScript consumers are `desktop/src/main/security.ts:237-241`, which validates
the shape only, and `desktop/src/renderer/features/plugins/types.ts:190-193`,
which refuses a started plugin whose `api_major !== 1`. That `1` is a magic
literal in the renderer rather than a shared constant, which is a small
follow-up worth filing.

This mechanism is unaffected by a 1.0.0 core.

### 2.2 Sidecar handshake, version-sensitive but self-consistent

`desktop/src/main/sidecar.ts:471-494` rejects a handshake whose `protocol_major`
is not `PROTOCOL_MAJOR` or whose `core_version` is not the exact string the
shell was launched with. The server applies the mirror check at
`src/tongs/desktop/protocol/server.py:1280,1285-1290`, with
`PROTOCOL_MAJOR = 1` at `src/tongs/desktop/protocol/messages.py:14`.

Both sides derive the string from the same installed distribution, because the
launcher probes the bound interpreter and passes the result on argv
(`src/tongs/desktop/installer/launcher.py:180,209-210`). Exact equality of
`1.0.0` with `1.0.0` holds. This mechanism is also unaffected.

### 2.3 Core version range, the one value that breaks

The payload declares a half-open core interval and three consumers enforce it:

- `src/tongs/desktop/installer/launcher.py:187-203`, on every launch;
- `src/tongs/desktop/installer/metadata.py:613-637`, on install and verify; and
- `packaging/rpm/desktop/package_contract.py:165-170`, in the RPM binding.

The declared values today are `core_minimum = 0.4.2-dev.183` and
`core_maximum_exclusive = 0.5.0`, at
`packaging/desktop/archive/run_hosted.sh:87-88`,
`packaging/desktop/archive/build_in_container.sh:34-35`, and
`packaging/rpm/desktop/manifest.json:37-38`.

`Version("1.0.0") < Version("0.5.0")` is false, so a 1.0.0 core falls outside
the interval. Concretely, with `v1.0.0` tagged:

1. `packaging/rpm/desktop/package_contract.py:169-170` raises
   `RuntimeError("core version is outside the desktop compatibility interval")`,
   failing the `desktop-rpm-lifecycle` job in `desktop-production.yml` and
   therefore the `Desktop pre-merge aggregate` in `ci.yml`. This fires on the
   next branch push, not on the tag, and stays broken afterwards because the
   next commit derives `1.0.1.dev1+g...`, which is also above `0.5.0`.
2. `launcher.py:196-203` refuses to launch an installed payload against a 1.0.0
   core with "The bound Tongs core is incompatible with this desktop payload."
3. `metadata.py:627-637` refuses installation with "This desktop release is
   incompatible with the installed core."

`core_minimum` stays valid at its current value, because `1.0.0` is above
`0.4.2.dev183`. The upper bound has to move, in the three source locations above
plus the mirrored test constants, in one change.

### The lower bound has the mirror-image problem, before the tag exists

The RPM binding does not read the core version from installed metadata. It
derives it from the checkout: `packaging/rpm/desktop/package_contract.py:55-70`
runs `git describe --tags --long --match 'v[0-9]*'` and, at any distance above
zero, produces `<major>.<minor>.<patch+1>.dev<distance>+g<sha>`. The job checks
out with `fetch-depth: 0` (`desktop-production.yml:690,709`), so the derivation
is live on every branch push. In this worktree the description is
`v0.4.1-516-gf899063`, giving `0.4.2.dev516+gf899063cb`.

That means **raising `core_minimum` to `1.0.0` while the newest tag is still
`v0.4.1` fails the same check from below.** `Version("0.4.2.dev516")` is less
than `Version("1.0.0")`, so `minimum <= core_version` is false and
`package_contract.py:169-170` raises on every push and pull request into
`feat/desktop-app` from the moment the change lands until `v1.0.0` is pushed. The
final PR into `main` cannot be green in that window. The same rule that makes the
upper bound wrong after the tag makes an eagerly raised lower bound wrong before
it.

Two sequences avoid the window. Whichever the release-preparation change adopts,
this document should name it and drop the other:

- **Option A, a lower bound that admits the pre-tag development version.** Move
  only `core_maximum_exclusive` now and leave `core_minimum` where it is, or set
  it to another value the pre-tag `0.4.2.devN` still satisfies. The manifest's
  `-dev.` spelling is understood: `package_contract.py:166` rewrites it to
  `.devN` before comparing, which is why `0.4.2-dev.183` admits `0.4.2.dev516`
  today. The gate stays green throughout and no tag-time edit is needed, at the
  cost of a payload that formally accepts a development core it was never tested
  against.
- **Option B, the lower bound moves in the tag-time commit.** Move
  `core_maximum_exclusive` now, and raise `core_minimum` to `1.0.0` only in the
  commit that `v1.0.0` will be created on. The aggregate cannot be green on that
  exact commit before the tag exists, so the green run that authorises the tag
  has to be the `main` run at the same tree with the old lower bound, and the
  bound change has to be the last thing in. That is a tighter compatibility
  statement bought with a deliberate, documented gap in gate coverage.

The trigger that settles it is the release-preparation change itself. Read the
values it lands and rewrite this subsection to describe only that sequence.

Emptiness of the interval is already enforced at build time
(`src/tongs/desktop/artifact_contract/manifests.py:280-286`), and the API majors
are bounded to 1 through 255 at `manifests.py:290-293`, so a malformed range
fails the producer rather than shipping.

A fourth, unrelated check: a plugin may declare `minimum_host_version`, enforced
at `src/tongs/plugins/desktop_registry.py:603-618`. It is a lower bound, so a
higher core only ever helps.

## 3. Tag triggers and what each workflow does

*Superseded by section 9: `release-desktop.yml` now fires on the same
`vX.Y.Z` tag as `publish.yml` and publishes the desktop assets to that tag's
GitHub Release. The audit below is kept as the state it described.*

**Exactly one workflow in the repository fires on a tag, and it is not a desktop
workflow.** A repository-wide search finds no workflow matching `desktop-v*`, no
`gh release` or `softprops/action-gh-release` step, and no COPR step anywhere
under `.github/`.

| Workflow | Trigger | Publishes |
|---|---|---|
| `publish.yml` | push of a tag matching `v*` (`:3-6`) | **PyPI**, through `pypa/gh-action-pypi-publish` in the `pypi` environment with Trusted Publishing (`:36-50`) |
| `ci.yml` | push and pull request on `main` and `feat/desktop-app` (`:3-8`) | nothing; artifacts only |
| `docs.yml` | push to `main`, plus `workflow_dispatch` (`:3-6`) | **GitHub Pages**, in the `github-pages` environment (`:34-39`) |
| `release-desktop.yml` | pull requests into `feat/desktop-app` on a path filter, and pushes to two named branches (`:1-18`) | nothing; named "Unpublished desktop candidate attestation"; artifacts only |
| `desktop-production.yml` | `workflow_call` from `ci.yml` and `workflow_dispatch` (`:20-42`) | nothing; artifacts only |
| `desktop-archive.yml` | `workflow_dispatch` only | nothing; artifacts only |
| `desktop-rpm.yml` | `workflow_dispatch` only | nothing; artifacts only |
| `desktop-python-rpms.yml` | `workflow_dispatch` only | nothing; artifacts only |
| `desktop-podman-probe.yml` | called by `ci.yml` | nothing |

Three consequences follow, and they are the substance of this handoff:

**A `v1.0.0` tag push runs only `publish.yml`.** Tags do not match a `branches:`
filter, so neither `ci.yml` nor `docs.yml` nor `release-desktop.yml` runs on the
tag. There is no test gate in front of the PyPI upload at tag time; the gate has
to be the exact commit's `main` CI run before the tag is created.

**`desktop-v1.0.0` currently triggers nothing.** No workflow listens for it.
Meanwhile `src/tongs/desktop/installer/metadata.py:66,73-75,128,301-304` accepts
only a GitHub Release whose tag matches `^desktop-v(\d+)\.(\d+)\.(\d+)$`, with
no prerelease and no leading-zero component, and enumerates matching releases
rather than trusting GitHub's latest pointer. **There is therefore no publisher
for the desktop at all today, and the installer's discovery path has nothing to
discover.** Building that production tag path is a real implementation task, not
a tagging step.

**SBOM and signing exist only inside the unpublished candidate workflow.**
`release-desktop.yml:290-315` generates an SPDX 2.3 SBOM through
`scripts/build_desktop_sbom.py` and schema-validates it;
`release-desktop.yml:316-334` makes two `actions/attest` calls, one carrying the
SBOM and one carrying provenance over `desktop-manifest-v1.json` and the
hardcoded `tongs-desktop-0.5.0-fedora44-x86_64.tar.gz`. Signing is
GitHub-managed Sigstore; there is no cosign and no GPG anywhere. The privileged
job is gated at `release-desktop.yml:198-206` on the exact repository, owner and
repository IDs, a `push` event, and one of the two named branches
`feat/desktop-app` and `feat/desktop-120-candidate-attestation`; its permission
block at `release-desktop.yml:209-212` grants `contents: read`,
`id-token: write` and `attestations: write`, with no `environment:`. `publish.yml`
performs no SBOM generation and no artifact attestation at all.

## 4. Order of operations

This is the recommended order. Each step is the CTO's to authorize; steps 2
through 5 are implementation work that does not exist yet.

1. **Merge the final PR into `main`.** That is the CTO's gate. The merge also
   deploys the documentation site through `docs.yml`, so the merged prose must
   still describe the desktop as unreleased at that moment.
2. **Land the release-policy source changes** decided in section 5, as reviewed
   commits on `feat/desktop-app` before the final merge, or as a follow-up PR
   after it. At minimum this is the core compatibility range; realistically it
   is also the archive `release_version`, the classifier and support policy, and
   the production desktop tag path. **Watch the compatibility interval's lower
   bound here.** Raising `core_minimum` to `1.0.0` on a branch where the newest
   tag is still `v0.4.1` makes `package_contract.py:169-170` raise from below on
   every push, so the aggregate is red until the tag exists. Follow section 2.3:
   either keep a lower bound the pre-tag `0.4.2.devN` satisfies, or move the
   lower bound only in the commit `v1.0.0` will be created on and accept that the
   gate cannot be green on that exact commit.
3. **Wait for a green `main` CI run at the exact release commit.** Record every
   required job and the aggregate. Nothing runs on the tag, so this is the only
   gate the tag will ever have.
4. **Push `v1.0.0` at that commit.** This fires `publish.yml`, which builds and
   uploads to PyPI through the `pypi` environment. **This step is
   irreversible**: PyPI does not allow a filename to be replaced, and a moved or
   re-pushed tag re-runs the upload and is rejected as a duplicate, because
   `skip-existing` is not configured. Verify wheel and sdist filenames, their
   `METADATA` version, a clean install, both entry points, and
   `importlib.metadata.version("tongs")` before going further.
5. **The desktop release is created by the same tag push**, not by a second
   tag. `release-desktop.yml` builds, attests, verifies and publishes the
   desktop assets to the GitHub Release `v1.0.0` (section 9). There is no
   `desktop-v1.0.0`. The release must be stable, non-prerelease, immutable, and
   carry at least `desktop-manifest-v1.json`,
   `desktop-manifest-v1.sigstore.json`, and
   `tongs-desktop-1.0.0-fedora44-x86_64.tar.gz`, each with a unique name,
   positive size, `sha256:` digest, `uploaded` state and the expected asset URL
   (`src/tongs/desktop/installer/metadata.py`); the publish job confirms all of
   that before and after publication. The RPM and SBOM assets join that set.
6. **Verify the public consumer path** from a fresh core 1.0.0 installation:
   `tongs desktop install --version 1.0.0`, status, launch, update selection,
   repair, downgrade guard, uninstall and ownership.
7. **Announce availability last.** Update README, the installation page, the
   support policy and the release notes only after step 6 passes, then verify
   the Pages deployment.

Core first is still what happens, by construction: `publish.yml` and
`release-desktop.yml` start from the same tag push, the PyPI upload takes
minutes and the desktop release well over an hour, and the installer names the
missing release and asks for a retry in between. One tag resolves to one commit.

### Stop conditions

- Stop before the tag if the built metadata or the PyPI workflow have not been
  reviewed. The upload cannot be recalled.
- Stop if the core version falls outside the payload's declared interval. The
  upper bound is 2.0.0 since PR #238, and the publish job replays the
  installer's compatibility check with the tag's own version before creating
  the release.
- Stop if the desktop production tag path is absent from the exact tagged
  source: `release-desktop.yml` must carry the `release-rpm` and
  `release-publish` jobs at the tagged commit, and
  `docs/releases/v1.0.0.md` must exist there.
- Stop if immutable releases are not enabled for the repository. The installer
  refuses a mutable release, and the publish job fails after publication when
  the flag is missing.
- Stop if the dry run (`workflow_dispatch` of `release-desktop.yml` with
  `dry_run`) has not passed on the release commit or its parent.
- Stop if a signing job would receive OIDC, attestation, release or contents
  write authority in the same job that builds the archive.
- Stop after immutability rather than mutating a published release; prepare a
  separately reviewed corrective version instead.

### Renewed release evidence

Actions artifacts expire after fourteen days, so the release handoff has to
retain, outside Actions: the exact release commit, tree and both tag objects;
the reviewed workflow blobs, action pins and permissions; core build tool
versions, the wheel and sdist with their metadata and hashes, and the public
PyPI hashes; a two-build archive comparison with both manifests; the SBOM and
its attestation; RPM NEVRA, payload parity and hosted lifecycle results; the
Sigstore bundle with its certificate claims and subjects; the GitHub Release API
record before and after publication including the `immutable` field; and the
clean public installation run. The #55 acceptance evidence is retained locally
and is described in
[the acceptance record](desktop-production.md#acceptance-record-for-the-v100-candidate).

## 5. Decisions the CTO must make

Each of these blocks a v1.0.0 tag or changes what the release means. The
recommendation is mine; none of them is decided by this document.

**1. The core compatibility range for the desktop payload, and when each bound
moves.**
Today `core_minimum = 0.4.2-dev.183` and `core_maximum_exclusive = 0.5.0`. The
upper bound excludes 1.0.0 and will break the RPM contract job, the installer,
and every launch once the tag exists. The lower bound has the mirror-image
problem before the tag exists, because the RPM binding derives the core version
from `git describe` on the checkout rather than from installed metadata, so it
sees `0.4.2.devN` until `v1.0.0` is pushed; section 2.3 works this through.

*Recommendation: set `core_maximum_exclusive` to `2.0.0` now, so desktop 1.0.0
supports the whole core 1.x series, across `run_hosted.sh:88`,
`build_in_container.sh:35`, `manifest.json:38` and the mirrored test constants in
one commit. Do not raise `core_minimum` to `1.0.0` in the same commit unless the
tag is being created on it: doing so turns `Desktop pre-merge aggregate` red on
every push until the tag exists, and the final PR into `main` cannot be green in
that window. Choose option A or option B from section 2.3 and record which. Prove
the boundaries either way by installing at the lower bound, just below it, just
below the upper bound, and at the excluded upper bound.* This is a compatibility
policy decision, which is why this documentation patch does not make it.

**2. The desktop archive `release_version`.**
Today `0.5.0`, explicitly labelled an unpublished candidate. *Recommendation:
`1.0.0`, so the archive, the RPM version, the manifest and the attested subject
filename all agree with the core release. Doing this also requires updating the
hardcoded subject at `release-desktop.yml:331`, the assertion at
`install_and_verify.sh:581`, and the prose at
`packaging/rpm/desktop/README.md:51`, which describes the lifecycle as a `0.4.9`
to `0.5.0` upgrade.*

*This is not a string edit.* `packaging/rpm/desktop/manifest.json:21-22,24-32`
pins the accepted archive's byte count, its SHA-256 and eight evidence digests,
and `packaging/rpm/desktop/package_contract.py:188-193` verifies every one of
them against the real files. Renaming the archive changes all of them, so they
must be regenerated from a fresh hosted archive run in the same change. Editing
only the version strings produces an `accepted evidence mismatch`, which reads
like a corrupted artifact rather than a version bump.

**3. Whether a desktop production release path is built before v1.0.0, or the
desktop ships later.**
No workflow publishes a GitHub Release, and the installer can only consume one.
*Recommendation: tag and publish core `v1.0.0` on its own first, keep the
desktop documented as unreleased, and treat the `desktop-v1.0.0` production path
as its own reviewed work item with its own gate. The alternative, holding core
1.0.0 until the desktop publisher exists, delays a release that is otherwise
ready.*

*Overruled after this audit: the CTO ruled the desktop release blocking for
v1.0.0. The production path was built (section 9) so that the `v1.0.0` tag
publishes both, and the installer's tag contract moved from `desktop-v` to the
core's own `v` tag.*

**4. The Python package classifier.**
`pyproject.toml:17` still says `Development Status :: 4 - Beta`.
*Recommendation: change it to `Development Status :: 5 - Production/Stable`
before the release commit, and verify the built wheel and sdist metadata. Do not
let the number 1.0.0 imply it.*

**5. The security support policy.**
`SECURITY.md:5` says tongs is "in early development (pre-1.0)".
*Recommendation: rewrite it to name the supported 1.x series, but only in the
same change that actually publishes 1.0.0, never while it is unavailable.*

**6. Where release notes live.**
There is no `CHANGELOG` file in the tree. *Recommendation: use the GitHub
Release body as the single source, and do not start a parallel changelog file
that will drift. The draft in section 6 is written to be pasted there.*

**7. Whether `desktop/package.json` moves to 1.0.0.**
It is inert: private, unpublished, unread by the shell. *Recommendation: set it
to `1.0.0` anyway, with `package-lock.json` regenerated so both root
occurrences agree, because the file ships inside `app.asar` and a visible
`0.1.0` invites the wrong conclusion. This is cosmetic and can be skipped.*

**8. Whether `publish.yml` is hardened before the tag.**
It accepts any tag beginning with `v` without validating stable SemVer, asserts
nothing about the built version, and configures no `skip-existing`.
*Recommendation: add a stable-SemVer tag check and an assertion that the built
wheel version equals the tag, as a small reviewed change before the tag. Leave
`skip-existing` off, so a duplicate upload fails loudly.*

**9. The documentation URL inconsistency.**
`mkdocs.yml:3` and `docs/CNAME` use `www.tongs.tools`; `pyproject.toml:58`
declares `Documentation = "https://tongs.tools"`. *Recommendation: make
`pyproject.toml` match the CNAME, since the published metadata is what users
follow from PyPI.*

**10. Whether the deferred acceptance groups gate the tag.**
Group D, the native GPU verifier policy run, and groups I through O were not
executed at this head and are tracked in #234 for v1.0.1. *Recommendation: hold
to the decision already taken, ship v1.0.0 on the defect triage and run them for
v1.0.1, because the desktop is not being published in this release anyway under
decision 3.*

## 6. Draft release notes for v1.0.0

Draft only. Numbers and claims are as of the candidate head; re-check them at
the release commit before publishing.

> ## tongs 1.0.0
>
> First stable release. tongs is a terminal-native code review inbox that spans
> GitHub and GitLab: one inbox, real diffs, inline comments, discussions,
> suggestions, durable review drafts, pipeline and CI drill-down, an SQLite
> cache, a plugin system and an MCP server.
>
> ### The terminal application
>
> Everything previously shipped is here, plus the two features that were on the
> development branch:
>
> - Split diff view, toggled with `v`, with `h` and `l` moving focus between the
>   old and new side.
> - Durable review drafts: `Ctrl+G` starts review mode, comments collect
>   locally, and the whole set submits as one review with a verdict. Drafts
>   survive a crash, and an interrupted submission is reconciled rather than
>   replayed.
>
> ### The desktop workspace, first release
>
> An optional Electron workspace over the same repositories, reviews and drafts.
> The terminal stays the default; plain `tongs` never downloads or starts it.
>
> The review workflow was redesigned before this release. Reviewing happens on
> the diff: hover a line for the gutter control, and the composer opens under
> that line. Two buttons keep the same shape throughout, **Start a review** or
> **Add to review** as the primary, **Add comment now** as the secondary, so an
> immediate comment and a pending one are never the same gesture. Pending
> comments render inline under their anchor with a **Pending** badge and can be
> edited or deleted in place. **Insert suggestion** pre-fills a suggestion block
> from the selected new-side lines. A **Your review** button carries the pending
> count and opens a drawer holding the pending comments, the summary, the
> verdict tiles, **Submit review** and **Discard review**, along with the
> submission progress and recovery. A draft that changed elsewhere presents both
> texts and a real choice instead of wedging.
>
> ### Fixed in this release
>
> Split diff navigation no longer crashes the terminal, log search opens
> correctly in the terminal pipeline view, the per-user installer accepts a real
> `pipx` installation, the installed menu entry launches, diff reads no longer
> fail on an empty field, every redesigned review surface has its own background
> and colour tokens on the dark theme, and a merge the forge refuses with HTTP
> 405 is reported as a known conflict instead of an unknown outcome.
>
> ### Known limitations
>
> See the [known limitations page](https://www.tongs.tools/desktop/known-limitations/).
> The notable ones: lifecycle actions are reachable only from the Discussions
> tab, a failed read's Retry does not restart a dead sidecar, a rotated token is
> not re-resolved without a restart, and large addition-heavy diffs render
> without syntax colouring.
>
> ### Availability
>
> `pipx install tongs`. The desktop workspace is documented but is not published
> as a downloadable artifact in this release.
>
> ### Next
>
> v1.0.1 carries the deferred acceptance runs and the discoverability fix for
> the lifecycle actions.

## 7. What this document did not do

No tracked version number was changed. No tag, branch, release, package, or
workflow run was created. No forge object was deleted. The compatibility range
that would be wrong at tag time is recorded as decision 1 and left in place,
because choosing its replacement is a compatibility policy decision and because
issue #54 excludes packaging producer changes from a documentation patch.

## 8. Decision record, added after the CTO ruled

Sections 1 through 7 above are the audit as merged in PR #236 and are left byte
for byte. This section is the later reading and supersedes them wherever they
disagree.

The CTO took every recommendation in section 5. Decisions 4, 5, 8 and 9 landed
whole on `chore/desktop-release-prep-v1`. Decision 1 landed in the half that can
land now. Decisions 2 and 7 were implemented, refused by a gate, reverted, and
moved to the desktop production release path; 8.3 says why and what re-pinning
them costs. Decisions 3, 6 and 10 are procedure and remain the CTO's to execute.

**Still true: this document prepares a release and does not perform one.** No
tag, PyPI upload, GitHub Release, RPM or COPR publication or availability
announcement follows from merging the implementing pull request.

### 8.1 What is done, and what moved

| # | Decision | Before | After |
|---|---|---|---|
| 1 | Core compatibility range | `core_minimum` 0.4.2-dev.183, `core_maximum_exclusive` 0.5.0 | `core_maximum_exclusive` **2.0.0**; `core_minimum` unchanged. **Option A** of 2.3, see 8.2 |
| 2 | Desktop archive `release_version` | 0.5.0 | **Unchanged.** Moved to the decision 3 path, see 8.3 |
| 4 | Python trove classifier | Development Status :: 4 - Beta | Development Status :: 5 - Production/Stable |
| 5 | Security support policy | "in early development (pre-1.0)" | "released from the 1.x series", latest 1.x only, no backports |
| 7 | Desktop shell package version | `desktop/package.json` 0.1.0 | **Unchanged.** Moved to the decision 3 path, see 8.3 |
| 8 | `publish.yml` hardening | tag filter `v*`, no version assertion | filter `v[0-9]+.[0-9]+.[0-9]+`, plus an anchored tag check and a wheel, METADATA and sdist version assertion before any upload |
| 9 | Documentation URL | `https://tongs.tools` | `https://www.tongs.tools`, the `docs/CNAME` host |

Decision 3 (desktop ships after core), decision 6 (release notes live in the
GitHub Release body) and decision 10 (deferred acceptance groups do not gate the
tag) were deliberately not implemented. They are procedure, and section 5 records
the ruling.

### 8.2 Decision 1 takes option A

Section 2.3 asked the release-preparation change to choose between option A, a
lower bound the pre-tag `0.4.2.devN` still satisfies, and option B, a lower bound
raised in the tag-time commit. **It takes option A, and option B should now be
read as rejected rather than open.**

`core_maximum_exclusive` is 2.0.0 in
`packaging/desktop/archive/run_hosted.sh`,
`packaging/desktop/archive/build_in_container.sh`,
`packaging/rpm/desktop/manifest.json` and every mirrored constant. A tagged core
1.0.0 is inside the interval, which removes the third stop condition.
`core_minimum` stays `0.4.2-dev.183`.

Option B was rejected on the evidence section 2.3 already assembles. A commit
carrying the raise is red on its own CI run, and that run is exactly the green
run section 4 step 3 requires before the tag is pushed, so the two requirements
cannot both be met at the same commit. Section 2.3 states this as "a deliberate,
documented gap in gate coverage"; measured against step 3 it is not a gap but a
deadlock, because the run that authorises the tag is the run that cannot pass.
Option A also cannot be repaired by admitting a pre-release of the minimum: under
this scheme `git describe` bumps the patch of the newest tag, so no reachable
pre-tag state produces a `1.0.0.devN`, and a `0.4.2.devN` is not a PEP 440
pre-release of `1.0.0` by any reading.

The cost option A names, a payload that formally accepts a development core it
was never tested against, now does arise, because section 9 publishes a 1.0.0
payload whose declared interval starts at `0.4.2-dev.183`. It is contained
rather than removed: `tongs desktop install` without `--version` selects only
the release whose version equals the running core, so a development core cannot
take the 1.0.0 payload implicitly; it can only be asked for with an explicit
`--version 1.0.0`, and the interval check then admits it. Raising the lower
bound to 1.0.0 after the tag, step 4 of 8.4, closes that path for v1.0.1.

`tests/ci/desktop_production_expectations.py` now carries `DESKTOP_CORE_MINIMUM`
and `DESKTOP_CORE_MAXIMUM_EXCLUSIVE` as caller-owned constants, and
`tests/ci/test_desktop_production_expectations.py` pins them against all three
producer sources and asserts that the interval admits a tagged `1.0.0`, admits
the pre-tag `0.4.2.devN`, and refuses `2.0.0`. The raise is therefore a
deliberate edit that fails a test until it is made everywhere. Make it after the
tag, in the decision 3 work item, where the boundaries can be proved the way
section 5 asked, against a real payload rather than a fixture.

### 8.3 Decisions 2 and 7 move to the desktop production release path

Both were implemented on this branch and both were refused by a production gate,
for the same underlying reason: each edits a value that a reviewed, byte-verified
build input depends on, and neither can be corrected without a hosted run that
regenerates that input. Section 5 already warned about the first; the second was
not known.

**Decision 2, the archive `release_version`.** Section 5 anticipated the digest
problem and understated it in one respect: **the accepted archive's identity is
read by the gate, not merely recorded for the fixture path.**
`tests/integration/desktop/rpm_payload_contract.py:228` takes
`accepted_desktop.archive.filename` from the checked-in base manifest and
requires the producer's own `SHA256SUMS` to cover exactly that name plus the
seven other accepted files. Raising `release_version` renames the archive the
producer emits, so the reviewed set and the produced set stop matching, and the
`Materialize the exact-mode payload contract` step of `rpm-lifecycle` fails
before any digest is compared:

```
producer checksum list does not cover the reviewed accepted file set:
missing=['tongs-desktop-0.5.0-fedora44-x86_64.tar.gz'],
unexpected=['tongs-desktop-1.0.0-fedora44-x86_64.tar.gz']
```

Moving the accepted filename to match does not rescue it, it only moves the
failure later. `rpm_payload_contract.py` preserves `release_version` byte for
byte from the base manifest, and
`packaging/rpm/desktop/package_contract.py:204-206` then compares that preserved
value against the freshly built `desktop-manifest-v1.json`, raising
`accepted desktop release_version mismatch` whenever the manifest and the
producer disagree, with `package_contract.py:217` raising
`accepted desktop release mismatch` on the same disagreement against
`desktop-install.json`. Every one of these is satisfied only by a manifest whose
accepted block describes an archive that a hosted run actually produced. The
block in `packaging/rpm/desktop/manifest.json` is evidence of a reviewed 0.5.0
archive, so a 1.0.0 archive cannot satisfy it until a 1.0.0 archive has been
produced and accepted.

The coupling that made this a two-step failure rather than one is now pinned:
`tests/ci/test_desktop_production_expectations.py` asserts that the accepted
archive filename, the attested subject in `release-desktop.yml`, the RPM version
assertion in `install_and_verify.sh`, `run_hosted.sh`'s `release_version` and the
manifest's `release_version` all carry `DESKTOP_RELEASE_VERSION`. Raising the
release version in one place and not the others now fails a unit test instead of
a hosted job.

**Decision 7, `desktop/package.json`.** Section 5 called it cosmetic and
skippable, which it is not.
`scripts/build_desktop_archive.py:596-603` refuses to build unless the SHA-256 of
`desktop/package-lock.json` equals `expected_package_lock_sha256`, held in
`packaging/desktop/archive/contract.json`. That pin is what makes the archive
reproducible: the producer builds from an approved lock, never from whatever lock
is checked out. Editing the lockfile's two root `version` fields changes its
digest, so the producer refused the input with
`desktop archive build failed: desktop package lock is not the approved input`,
failing `Reproducible archive and unsigned transfer`. Bumping the shell version
therefore requires `expected_package_lock_sha256` to move in the same reviewed
commit, and a rebuild confirming the two clean source roots still produce
byte-identical output.

Section 9 settles decision 2 differently from a re-pin: the release version is
no longer a checked-in literal at all. `run_hosted.sh` takes it from
`TONGS_RELEASE_VERSION`, the trusted workflow derives that from the tag and
falls back to the 0.5.0 candidate version on a branch, and the RPM payload
contract is materialised per run with `--release-version`, replacing the
accepted archive name and `release_version` while copying the reviewed
compatibility and runtime policy from `manifest.json` unchanged. The accepted
block in `packaging/rpm/desktop/manifest.json` therefore stays the reviewed
0.5.0 candidate evidence that the branch gate needs, and no 1.0.0 archive has to
be produced and accepted before the tag. The re-pin procedure below is kept for
the case where the checked-in candidate identity itself is refreshed; it is not
on the v1.0.0 path.

Decision 7 stays deferred as described. `desktop/package.json` and
`desktop/package-lock.json` are byte identical to the base commit.

The consequence of deferring decision 7 is that `app.asar` in a desktop 1.0.0
payload would contain a `package.json` reading `0.1.0`. Nothing reads it: the
package is `private`, is never published to npm, and `desktop/src` never imports
it. The only version the shell knows is the core version passed on argv, as
section 1.2 records.

#### The re-pin procedure, only if the checked-in candidate identity is refreshed

Not required for v1.0.0 (see above). Run in one reviewed change when the
reviewed candidate identity in `manifest.json` is deliberately moved to a newer
archive. The order matters: the lock pin gates the build, and the accepted set
can only be written from a build that succeeded.

1. **Set the shell version.** `desktop/package.json` and both root `version`
   fields of `desktop/package-lock.json` to `1.0.0`. Do not touch a dependency
   entry.
2. **Re-pin the approved lock.** Put the new SHA-256 of
   `desktop/package-lock.json` into `expected_package_lock_sha256` in
   **`packaging/desktop/archive/contract.json`**. Without this the producer
   refuses to start. The check is `scripts/build_desktop_archive.py:596-603`;
   `tests/packaging/desktop/archive/test_producer.py` exercises the producer
   against the contract.
3. **Set the candidate release version.** The branch fallback in
   `packaging/desktop/archive/run_hosted.sh` and in the `Derive the release
   version from the ref` step of `.github/workflows/release-desktop.yml`, the
   prose at `packaging/desktop/archive/README.md` and
   `packaging/rpm/desktop/README.md`, and the caller-owned gate constant
   `DESKTOP_RELEASE_VERSION` in `tests/ci/desktop_production_expectations.py`.
   The attested subject and the RPM version assertion are derived at run time
   and need no edit. The mirrored test constants listed at the end of section
   1.2 move with them.
4. **Produce the accepted set.** The job that generates it is the `archive` job
   of `.github/workflows/desktop-production.yml`, which runs
   `packaging/desktop/archive/run_hosted.sh` twice from two clean source roots
   and requires the outputs to be byte identical. Its transfer artifact carries
   `tongs-desktop-1.0.0-fedora44-x86_64.tar.gz`, the eight evidence files and the
   producer's `SHA256SUMS`.
5. **Receive it.** The file that holds the accepted set is
   **`packaging/rpm/desktop/manifest.json`**, under `accepted_desktop`: the
   `archive` block's `filename`, `bytes` and `sha256`, and all eight `evidence`
   digests, plus `artifact_id`, `artifact_name`, `run_id` and `source_commit`
   from that run. The `filename` must be exactly the name the producer emitted,
   `tongs-desktop-1.0.0-fedora44-x86_64.tar.gz`, because
   `tests/integration/desktop/rpm_payload_contract.py:228` compares it against
   the producer's `SHA256SUMS`; that module computes every one of these values
   and is the reference for what each must be. The `reviewed_fixture` block is
   removed when the accepted commit is the commit the core is built from.
6. **Confirm what pins it.** `tests/packaging/rpm/desktop/test_contract.py`
   pins the accepted identity and must be updated to the new run in the same
   change. `tests/ci/test_desktop_production_expectations.py` pins
   `DESKTOP_RELEASE_VERSION` against `run_hosted.sh`'s fallback, the
   manifest's `release_version`, the accepted archive filename and the branch
   fallback in `.github/workflows/release-desktop.yml`, so step 3 is complete
   only when that test is green. Both must pass before the change is proposed.
7. **Verify end to end.** A full `desktop-production.yml` run, green through
   `rpm-lifecycle`, on the commit that carries all of the above.

### 8.4 The sequence left for the CTO

Steps 1 through 3 are the whole of what the v1.0.0 tag needs, and the tag now
publishes the desktop as well as the core; section 9 is the operating sequence
and the prerequisites. Step 4 follows the tag. Step 5 is done.

1. **Merge, in this order.** The implementing pull request for this section, then
   `feat/desktop-app` into `main`. Both are CTO gates. Merging into `main` also
   deploys the documentation site through `docs.yml`, so the merged prose must
   still describe the desktop as unreleased at that moment, which it does.
2. **Wait for a green `main` CI run at the exact release commit.** Record every
   required job and the aggregate, and retain them outside Actions. Nothing runs
   on the tag, so this run is the only gate the tag will ever have. This is the
   step that option A in 8.2 exists to keep passable.
3. **Push `v1.0.0` at that commit, and nothing else.** Only `publish.yml` fires.
   It now refuses any tag that is not exactly `v<major>.<minor>.<patch>`, and
   fails the build job before the artifact upload if the built wheel filename,
   the wheel `METADATA` version or the sdist filename disagrees with the tag.
   **This step is irreversible.** PyPI does not allow a filename to be replaced,
   and `skip-existing` is deliberately still off, so a moved or re-pushed tag is
   rejected as a duplicate rather than silently succeeding. Verify the wheel and
   sdist filenames, the `METADATA` version, a clean install, both entry points and
   `importlib.metadata.version("tongs")` before going further.
   The same push fires `release-desktop.yml`, which builds, attests and
   verifies the 1.0.0 desktop archive, rebuilds the RPMs and creates the
   GitHub Release `v1.0.0` with every asset, as section 9 describes. Do not
   create a release by hand: the publish job refuses to touch an existing
   release, draft or not, and a hand-made release carries no attestation the
   installer accepts.
4. **Raise `core_minimum` to 1.0.0** in a reviewed change after the tag exists,
   across `run_hosted.sh`, `build_in_container.sh`, `manifest.json` and
   `DESKTOP_CORE_MINIMUM`, for v1.0.1. Until then 8.2 explains what the wide
   lower bound admits and why it is contained.
5. **The desktop production tag path is built.** The `v1.0.0` tag carries both
   publishes; the installer's tag contract, `SECURITY.md`, decision 3 and the
   interval argument in 8.2 were revised for it, and the re-pin in 8.3 is not
   on the release path. Section 9 is the sequence.

Section 4's stop conditions, as revised there, hold: the unreviewed metadata or
workflow, the tagged source lacking the publication jobs or the notes file,
immutable releases disabled, a missing dry run, a signing job holding build
authority, and mutating a published release instead of preparing a corrective
version.
## 9. Desktop release publication from the version tag

Added by the change that made one `vX.Y.Z` tag publish the desktop. It is the
operating sequence; sections 3, 4, 5 and 8 carry short notes where it changed
them. The compatibility upper bound from section 8.2 (PR #238) is already on
the branch.

### What one tag now does

1. `publish.yml` builds the core and publishes it to PyPI, unchanged.
2. `release-desktop.yml` runs on the same tag push:
   - `candidate-archive` derives the release version from the tag, exports it
     to the producer as `TONGS_RELEASE_VERSION`, and builds
     `tongs-desktop-X.Y.Z-fedora44-x86_64.tar.gz` twice, byte-identical.
   - `candidate-attestation` attests the release manifest and the archive with
     `actions/attest`, then verifies the bundle with the installer's own
     `_verify_production_attestation` against the identity the installer
     derives from the tag. On a branch that policy must still reject.
   - `release-rpm` materialises the exact-mode payload contract for the tag's
     version (`rpm_payload_contract.py --release-version`) and runs the full
     RPM lifecycle from the signed archive.
   - `release-publish` assembles the assets under the names the installer
     expects (`desktop-manifest-v1.sigstore.json` for the bundle), verifies
     them again with the installer's policy, requires that no release exists,
     creates the release as a draft with every asset, confirms the draft
     carries exactly the assembled bytes, publishes it as the latest release,
     and confirms it is immutable and complete. Release notes come from
     `docs/releases/vX.Y.Z.md`, which must exist at the tagged commit.
3. `tongs --install-desktop` selects the release whose version equals the
   running core. The two workflows do not wait for each other, so the desktop
   release can appear minutes after the PyPI upload; the installer reports
   that state and asks for a retry.

### Prerequisites the CTO must confirm before the tag

- **Immutable releases are enabled for the repository** (Settings, General,
  Releases). The installer refuses a release whose `immutable` flag is not
  `true`, and the publish job fails after publication if the flag is missing.
  A mutable published release can be deleted and the tag's workflow run
  re-run once the setting is on; a release is never edited in place.
- **The compatibility upper bound admits core 1.0.0** (section 8.2, PR #238,
  merged). The publish job replays the installer's compatibility check with the
  tag's own version and refuses to create a release the installer would reject.
- **`docs/releases/v1.0.0.md` exists at the tagged commit.**
- **A dry run passed.** Dispatch `release-desktop.yml` on `main` (or the
  release branch) with `dry_run` set. It builds, signs, verifies and rebuilds
  the RPMs and publishes nothing.

### The release sequence

```console
git checkout main && git pull --ff-only
git tag -a v1.0.0 -m "tongs 1.0.0"
git push origin v1.0.0
```

Then watch both workflow runs for the tag. When `release-publish` succeeds:

```console
pip install tongs==1.0.0
tongs --install-desktop
```

### If the desktop run fails

Nothing was published unless the `release-publish` job reached its last step.
If the failure is in a job before it, fix the cause on the branch, and either
re-run the failed jobs for the same tag (the release does not exist yet, so
`require-absent` still passes) or release the fix as v1.0.1. If a draft was
created and the job failed before publishing, delete the draft by hand and
re-run; the job refuses to touch an existing release, draft or not. A
published release is immutable and stays; the next fix is v1.0.1.
