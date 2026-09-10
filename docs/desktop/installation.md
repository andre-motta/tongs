# Desktop installation

!!! warning "Unreleased feature"

    The desktop installer is part of the unreleased desktop initiative. There
    is no public desktop artifact, production tag, finalized RPM package name,
    or release install to download yet. The commands and paths on this page
    describe the implemented contract that the first production release will
    use after packaging and native acceptance are complete.

Tongs remains terminal-first. Normal `tongs` startup scans local repositories
and opens the TUI. It does not download, activate, update, or start the
desktop application. Desktop lifecycle actions are always explicit.

## Requirements and support boundary

Use Python 3.12 or newer and install Tongs into an environment that will stay
available for desktop launches. The installer records the exact console script
and Python interpreter that invoke it, then launches the desktop with that
same interpreter. A virtual environment, `pipx`, a user-site installation,
and a system installation are persistent environment kinds supported by the
implemented checks.

Transient `uvx` executions are rejected because their interpreter and package
location may disappear after the command exits. A console script whose
shebang does not name the invoking interpreter is rejected as well. For a
persistent user installation, use one of these setup patterns before running
the lifecycle commands:

```console
python -m pip install --user tongs
pipx install tongs
```

Those commands describe persistent installation choices. They do not create a
public desktop release, and this page does not claim that the unreleased
desktop package is currently available from PyPI.

The release contract currently matches Linux, Fedora 44, x86_64, and the GNU
ABI for the per-user archive. The initial native acceptance policy covers
Fedora 44 KDE on x86_64 using XWayland. Other distributions, desktop
environments, operating systems, architectures, and native Wayland remain
outside that native support policy. The release metadata check rejects an
unsupported artifact target before download.

## Lifecycle commands

The long form is `tongs desktop`. The top-level alias
`tongs --install-desktop` is exactly the same as
`tongs desktop install` with no options.

| Command | Options | Operation |
| --- | --- | --- |
| `tongs desktop` | none | Launch the active compatible per-user desktop. |
| `tongs desktop install` | `--version X.Y.Z`, `--allow-downgrade` | Download and verify a selected release, then activate it for this user. |
| `tongs desktop update` | none | Download and verify the newest stable release, then activate it. |
| `tongs desktop repair` | `--redownload` | Recover local activation, repair the menu, and rebind to the invoking environment. Download only when local recovery fails and `--redownload` is explicit. |
| `tongs desktop status` | `--json` | Inspect the per-user installation without changing it. |
| `tongs desktop uninstall` | none | Remove the per-user activation, owned payloads, and owned menu entry. |

The parser accepts a release version such as `1.2.3`, without the
`desktop-v` tag prefix. `--allow-downgrade` is valid only with an explicit
`--version`; it never changes the default newest-release selection. Install
and update reject a release older than the accepted release unless this
explicit combination is used.

Lifecycle success returns exit code 0. Installer failures print a safe message
and return exit code 2. `status` returns 0 when no per-user installation is
present or when the installed payload is launch-ready. It returns 1 when an
installation exists but is not launch-ready. The `--json` form keeps the same
exit behavior.

The current source-bound parser exposes the following options:

```console
tongs desktop install [--version VERSION] [--allow-downgrade]
tongs desktop update
tongs desktop repair [--redownload]
tongs desktop status [--json]
tongs desktop uninstall
```

For the future production path, the installer first finds an immutable stable
`desktop-vX.Y.Z` release in the fixed Tongs repository. It then verifies the
release manifest, archive digest, compatibility fields, and GitHub-managed
Sigstore provenance before activation. No normal terminal start or background
task performs this work.

## Installing and launching

When a verified release is available, choose an explicit install command:

```console
tongs desktop install
tongs desktop install --version 1.2.3
tongs desktop install --version 1.2.3 --allow-downgrade
```

The first command selects the newest verified stable release. The second
selects one exact release version. The third permits that explicit version to
be older than the locally accepted release. All three require a persistent,
exactly bound invoking environment before remote staging begins.

Activation extracts a complete per-user archive into private staging, checks
its declared files and compatibility, and publishes one immutable version
target. The previous target remains recorded while the new target is being
published. A journaled, recoverable sequence records the menu and installation
state transitions, then drains obsolete owned payloads after publication. It
never changes a system RPM or a system application directory.

After activation, launch the installed application with either form:

```console
tongs desktop
# or the per-user menu entry, when your desktop environment indexes it
```

If no per-user activation is present, `tongs desktop` exits with code 2 and
prints `No per-user desktop installation is active. Run 'tongs desktop
install'.` It never turns a launch request into an implicit install.

The launcher rechecks the exact bound console and interpreter, the installed
payload digest and file set, the compatible Tongs core version, and the RPC and
desktop-plugin API majors. It passes the absolute interpreter, exact core
version, and a safe payload working directory to the desktop launcher. It does
not fall back to `PATH` or the current working directory.

## Updating

Updating is explicit:

```console
tongs desktop update
```

The command selects the newest verified stable release. It uses the same
persistent environment and journaled, recoverable activation sequence as
install.

Two guards run before any remote staging, and they apply to `install`,
`update`, and `repair --redownload` alike, not to `update` alone. If the
current installation has pending obsolete-payload cleanup, the command stops
and asks you to repair first. If the active payload is bound to another
persistent Python environment, the command stops before any download and asks
you to run repair from the intended environment to rebind it explicitly.

## Repair and recovery

Repair is local-first:

```console
tongs desktop repair
```

It validates the invoking persistent environment, completes a durable
activation journal when one exists, drains recorded cleanup targets, validates
the active payload, repairs the owned menu entry, and explicitly rebinds the
installation to the invoking console and interpreter. It does not silently
migrate to a different Python environment and it does not automatically
redownload an archive.

If the local payload or journal cannot be recovered, opt into a verified
replacement download:

```console
tongs desktop repair --redownload
```

The command reports `Local recovery failed; downloading a verified replacement.`
before the explicit fallback download. A failed local repair without this flag
returns an error and leaves the durable recovery state for another attempt.

The implementation records recovery states in the installation document. The
user-facing actions are:

| State | Meaning and next action |
| --- | --- |
| `healthy` | The active payload and owned menu are ready. |
| `uninstalled` | No per-user activation is active. Install when a verified release is available. |
| `activation-pending` | A verified activation was interrupted. Run `tongs desktop repair`. |
| `menu-repair-required` | The owned menu entry is missing or changed. Run `tongs desktop repair`. |
| `payload-repair-required` | The bound environment or payload no longer validates. Run repair from the intended persistent environment, or use explicit `--redownload` if local recovery fails. |
| `cleanup-required` | An old owned payload remains to be removed. The next action depends on whether an active payload is still present; see below. |

`cleanup-required` reports two different situations, and `status` distinguishes
them in its `detail` field. When an active payload is still present, the detail
reads:

```text
The per-user desktop installation can launch, but old payload cleanup is incomplete; run 'tongs desktop repair'.
```

When no active target remains, an interrupted uninstall left work behind and the
detail reads:

```text
Per-user desktop cleanup is incomplete; run 'tongs desktop uninstall' again.
```

Recovery is bounded to identities recorded in the private journal and state.
Repair does not search arbitrary directories for payloads, and it does not
invent a rollback or migration command.

## Status and JSON output

Use status before changing anything:

```console
tongs desktop status
tongs desktop status --json
```

With no active target, the human-readable result is one of three sentences.
When a verified activation journal is present:

```text
A verified activation is recoverable with 'tongs desktop repair'.
```

When cleanup is still outstanding:

```text
Per-user desktop cleanup is incomplete; run 'tongs desktop uninstall' again.
```

Otherwise:

```text
No per-user desktop installation is active.
```

The JSON form exposes `installed`, `version`, `active_target`,
`previous_target`, `ownership`, `environment`, `recovery`, `menu_registered`,
`launch_ready`, `rpm_detected`, `coexistence`, and `detail`. `detail` carries
the same human-readable sentence the plain command prints, which is what
distinguishes the three no-target situations above from each other and the two
`cleanup-required` situations from each other. Paths are absolute
when a target exists. Status acquires the per-user command lock and ensures its private
roots exist, but it does not change activation, menu, or payload content. It
does not download, repair, register a menu entry, or remove a payload.

When an active per-user installation and a separately managed RPM installation
coexist, status adds:

```text
A separate RPM installation is also present; this command selected the per-user installation.
```

The RPM check looks for the conventional system launcher and menu paths. RPM
detection alone does not add the coexistence line, and it does not make the RPM
the active per-user target.

## Per-user files and ownership

The installer uses `XDG_DATA_HOME` when it is an absolute path. If the variable
is unset or relative, it falls back to `~/.local/share`. Under that data home,
the private installation layout is:

| Path | Purpose |
| --- | --- |
| `tongs/desktop/staging/` | Incomplete and verified staging work. |
| `tongs/desktop/versions/` | Complete immutable per-user payload targets. |
| `tongs/desktop/installation-v1.json` | Atomic active, previous, recovery, and cleanup state. |
| `tongs/desktop/accepted-release-v1.json` | Highest fully staged release identity and downgrade watermark. |
| `tongs/desktop/activation-journal-v1.json` | Durable candidate used to recover an interrupted activation. |
| `tongs/desktop/.command.lock` | Per-user lifecycle command lock. |
| `applications/tongs.desktop` | The menu entry owned by the per-user activation. |

The private root, staging directory, and versions directory require private
ownership and restrictive permissions. The menu entry is replaced only when
it is absent or its recorded digest matches this installation. If another
installation owns or changed the entry, the command stops instead of
overwriting it. Uninstall removes only the exact menu content recorded by the
per-user state.

## Coexisting with an RPM

The per-user archive and an RPM are separate installation methods. The per-user
commands do not install RPM packages, elevate privileges, overwrite `/usr`, or
remove RPM-owned files. An RPM can remain installed while the per-user
activation is selected for `tongs desktop` and reported by `status`.

The RPM path builds three packages:

| Package | Owns |
| --- | --- |
| `python3-tongs` | `/usr/bin/tongs`, the Python modules, and their metadata. |
| `python3-tongs+mcp` | `/usr/bin/tongs-mcp` only, which keeps the MCP dependency out of the base closure. |
| `tongs-desktop` | The complete Electron runtime under `/usr/libexec/tongs-desktop`, its system launcher, desktop entry, icon, AppStream metadata, man page, and license and inventory copies. |

`tongs-desktop` requires the exact `python3-tongs` build it was made against,
plus `xorg-x11-server-Xwayland`. The packages contain no scriptlets and do not
inspect or modify home directories.

!!! warning "Unreleased feature"

    No RPM is published. There is no package repository, no COPR repository, no
    signed package, and no Fedora review submission, so there is nothing to
    `dnf install` today. The package names above are the names the first
    published build would use. The packaging sources and the local rebuild
    harness are in `packaging/rpm/` in the repository. The desktop license field
    is an honest aggregate expression for an artifact containing the upstream
    Electron distribution; it is not a claim of Fedora or COPR publication
    eligibility.

## Current boundary

Hardware GPU acceleration, an installed production artifact, final packaging,
and the main-branch release decision are separate gates. Do not treat the
examples on this page as evidence that a public archive, production tag, or RPM
is already available.

The defects that are known and are shipping unfixed, and the acceptance
scenarios that were not executed at the release candidate, are listed on the
[known limitations](known-limitations.md) page. Read it before relying on the
desktop workspace for a review you cannot redo.

The shared desktop architecture and acceptance boundary are tracked in the
project's internal planning records rather than on this site. See
[Contributing](../contributing.md) for how desktop changes are proposed,
validated, and reviewed.
