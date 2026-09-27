---
title: Desktop lifecycle
description: "Reference for the tongs desktop commands: options, exit codes, release selection, recovery states, status output, file layout and RPM coexistence."
lead: Every command, state and file the per-user desktop installer uses. For the steps to install, start with Install the desktop app.
---

:::note[Beta]
The desktop app is a beta. This page describes the per-user installer that
ships with the tongs core and the Fedora RPMs attached to each GitHub Release.
:::

For the task steps, see [Install the desktop app](/desktop/installation/). For
messages and fixes, see [Troubleshooting](/desktop/troubleshooting/).

## Commands

The long form is `tongs desktop`. The alias `tongs --install-desktop` is the
same as `tongs desktop install` and takes no other arguments.

```console
tongs desktop
tongs desktop install [--version VERSION] [--allow-downgrade]
tongs desktop update
tongs desktop repair [--redownload]
tongs desktop status [--json]
tongs desktop uninstall
```

| Command | What it does |
| --- | --- |
| `tongs desktop` | Launch the active per-user desktop app. |
| `tongs desktop install` | Download and verify a release, then activate it for this user. |
| `tongs desktop update` | Download and verify the release that matches the running core, then activate it. |
| `tongs desktop repair` | Recover local activation state, repair the menu entry and rebind to the invoking environment. |
| `tongs desktop status` | Report the per-user installation without changing it. |
| `tongs desktop uninstall` | Remove the per-user activation, its payloads and its own menu entry. |

| Option | Command | Meaning |
| --- | --- | --- |
| `--version VERSION` | `install` | Install one exact release, written without the `v` prefix, such as `1.0.2`. |
| `--allow-downgrade` | `install` | Accept a release older than the newest one accepted before. Valid only with `--version`. |
| `--redownload` | `repair` | Download a verified replacement when local recovery fails. |
| `--json` | `status` | Print the status as one JSON object. |

No plain `tongs` start and no background task runs any of these commands. A
launch request never turns into an install.

### Exit codes

| Code | When |
| --- | --- |
| `0` | The command succeeded. `status` also returns 0 when nothing is installed. |
| `1` | `status` only: an installation exists but cannot launch. |
| `2` | The installer refused or failed. It prints one message that says why. |

`status --json` keeps the same exit codes.

## Release selection and verification

The installer reads releases only from the `andre-motta/tongs` repository on
GitHub, and only immutable, published, stable `vX.Y.Z` releases. The desktop
assets share the core's tag: the release that publishes `tongs` to PyPI also
carries the desktop archive built from the same commit.

- `install` with no `--version`, `update`, and `repair --redownload` all select
  the release whose version equals the running core. If there is none, for
  example on a development build or in the minutes before the release assets
  are published, the command stops and names both versions. It never falls
  back to the newest release.
- `install --version X.Y.Z` selects that exact release.

Before activation the installer verifies the release manifest, the archive
digest, the platform target, the compatible core version range, the RPC and
desktop plugin API majors, and the GitHub-managed Sigstore attestation for the
release workflow and tag. An unsupported platform is rejected before the
archive is downloaded. [Security and signing](/reference/security/) describes
the verification in full.

The installer records the newest release it has accepted. It refuses an older
release unless `--version` and `--allow-downgrade` are both given. Uninstall
keeps this record, so the guard still applies after a reinstall.

## Environment binding

The installer binds the desktop app to the exact console script and Python
interpreter that ran the command, and the menu entry runs that console script.
The environment must be persistent:

| Environment | Accepted |
| --- | --- |
| Virtual environment | Yes |
| `pipx` | Yes |
| User site (`pip install --user`) | Yes |
| System installation | Yes |
| `uvx` | No: the environment is removed when the command exits. |

The console script must name its interpreter by absolute path. Three shapes are
accepted: a plain absolute shebang, the `-E` flag `pipx` appends to it, and the
fixed `/bin/sh` trampoline pip writes when the path contains a space.
`#!/usr/bin/env python3` and relative paths are rejected.

`install`, `update` and `repair --redownload` run two guards before anything is
downloaded:

- If an earlier cleanup is still pending, the command stops and asks you to
  run repair.
- If the active installation is bound to a different persistent environment,
  the command stops. Run `tongs desktop repair` from the environment you want
  to keep; repair rebinds it explicitly.

## Activation and launch

Activation extracts the archive into private staging, checks its declared
files and compatibility, and publishes it as one immutable version directory.
A journal records each step, so an interrupted activation can be completed by
repair. The previous version stays recorded until the new one is published,
and obsolete payloads are removed afterwards. Activation never changes an RPM
or a system directory.

Each launch rechecks the bound console script and interpreter, the payload
digest and file set, the core version range, and the RPC and desktop plugin API
majors. The launcher passes the absolute interpreter path, the core version and
a safe working directory to the app. It never falls back to `PATH` or the
current directory. On Linux with `WAYLAND_DISPLAY` set, it adds
`--ozone-platform=x11` so the app runs through XWayland.

## Repair

`tongs desktop repair` works locally, in this order:

1. Validate the invoking persistent environment.
2. Complete an interrupted activation from its journal, if there is one.
3. Remove the payloads recorded for cleanup.
4. Validate the active payload.
5. Repair the owned menu entry.
6. Rebind the installation to the invoking console script and interpreter.

It never moves to another environment silently, and it never downloads on its
own. With `--redownload`, a failed local repair is followed by the message
`Local recovery failed; downloading a verified replacement.` and a verified
download of the release that matches the running core. Without it, the failure
is reported and the recovery state stays in place for another attempt.

Recovery only touches identities recorded in the private state and journal. It
does not search other directories for payloads, and there is no rollback or
migration command.

## Recovery states

`status --json` reports one of these values in `recovery`.

| State | Meaning | Next step |
| --- | --- | --- |
| `healthy` | The active payload and the owned menu entry are ready. | None. |
| `uninstalled` | No per-user activation is active. | `tongs desktop install` |
| `activation-pending` | A verified activation was interrupted. | `tongs desktop repair` |
| `menu-repair-required` | The owned menu entry is missing or was changed. | `tongs desktop repair` |
| `payload-repair-required` | The bound environment or the payload no longer validates. | `tongs desktop repair` from the intended environment; `repair --redownload` if local recovery fails. |
| `cleanup-required` | An old owned payload was not removed. | Depends on `detail`; see below. |

`cleanup-required` covers two situations, and `detail` tells them apart.

With an active payload still present, the app can launch and the detail reads:

```text
The per-user desktop installation can launch, but old payload cleanup is incomplete; run 'tongs desktop repair'.
```

With no active payload, an interrupted uninstall left work behind:

```text
Per-user desktop cleanup is incomplete; run 'tongs desktop uninstall' again.
```

## Status output

`tongs desktop status` prints the `detail` sentence. With no active
installation, it is one of three:

| Situation | Output |
| --- | --- |
| An interrupted activation can be recovered | `A verified activation is recoverable with 'tongs desktop repair'.` |
| An interrupted uninstall left cleanup | `Per-user desktop cleanup is incomplete; run 'tongs desktop uninstall' again.` |
| Nothing is installed | `No per-user desktop installation is active.` |

`tongs desktop status --json` prints one object with these fields:

| Field | Content |
| --- | --- |
| `installed` | `true` when a per-user activation is active. |
| `version` | The active desktop version, or `null`. |
| `active_target` | Absolute path of the active payload, or `null`. |
| `previous_target` | Absolute path of the previous payload, or `null`. |
| `ownership` | Ownership of the active payload, or `null`. |
| `environment` | The bound environment kind (`venv`, `pipx`, `user-site` or `system`), or `null`. |
| `recovery` | One of the [recovery states](#recovery-states). |
| `menu_registered` | `true` when the owned menu entry matches its recorded digest. |
| `launch_ready` | `true` when the bound launch validates. |
| `rpm_detected` | `true` when an RPM launcher or menu entry is present. |
| `coexistence` | `true` when an active per-user installation and an RPM are both present. |
| `detail` | The same sentence the plain command prints. |

Status takes the per-user command lock and creates its private directories if
they are missing. It does not change the activation, the menu entry or any
payload, and it never downloads, repairs or removes anything.

## Per-user files

The installer uses `XDG_DATA_HOME` when it is an absolute path, and
`~/.local/share` otherwise. Under that directory:

| Path | Purpose |
| --- | --- |
| `tongs/desktop/staging/` | Downloads and extraction in progress. |
| `tongs/desktop/versions/` | Installed, immutable desktop payloads. |
| `tongs/desktop/installation-v1.json` | Active, previous, recovery and cleanup state. |
| `tongs/desktop/accepted-release-v1.json` | The newest accepted release, used by the downgrade guard. |
| `tongs/desktop/activation-journal-v1.json` | The record used to complete an interrupted activation. |
| `tongs/desktop/.command.lock` | The per-user command lock. |
| `applications/tongs.desktop` | The menu entry owned by the per-user activation. |

The private root, staging and versions directories must be owned by you with
restrictive permissions. The installer writes the menu entry only when it is
absent or still matches the digest it recorded. If another installation owns
or changed the entry, the command stops instead of overwriting it. Uninstall
removes only the exact menu content it recorded.

## Coexisting with an RPM

The per-user archive and the RPMs are separate installation methods. The
per-user commands never install RPM packages, elevate privileges, write to
`/usr` or remove RPM-owned files. Both can be installed at once:
`tongs desktop` starts the per-user app, and `tongs-desktop` starts the RPM
app.

When a per-user activation is active and an RPM is detected, `status` adds:

```text
A separate RPM installation is also present; this command selected the per-user installation.
```

Detection looks for `/usr/bin/tongs-desktop` and
`/usr/share/applications/tongs.desktop`. An RPM on its own, with no per-user
activation, does not add the line and never becomes the per-user target.

Each release attaches three Fedora 44 packages to its GitHub Release, with the
source-built companion packages they require. Install them together with
`dnf install ./*.rpm`. They are not GPG signed, and there is no COPR
repository.

| Package | Owns |
| --- | --- |
| `python3-tongs` | `/usr/bin/tongs`, the Python modules and their metadata. |
| `python3-tongs+mcp` | `/usr/bin/tongs-mcp` only, which keeps the MCP dependency out of the base package. |
| `tongs-desktop` | The Electron runtime under `/usr/libexec/tongs-desktop`, the `/usr/bin/tongs-desktop` launcher, the desktop entry, icon, AppStream metadata, man page, and license and inventory copies. |

`tongs-desktop` requires the exact `python3-tongs` build it was made with,
plus `xorg-x11-server-Xwayland`, and its launcher always starts the app
through XWayland with the system `/usr/bin/python3`. The packages have no
install scriptlets and do not read or change home directories. The packaging
sources are in `packaging/rpm/` in the repository.

## Supported platform

The archive and the RPMs target Linux on Fedora 44, x86_64, with the GNU ABI.
The beta is tested on Fedora 44 KDE through XWayland. Other distributions,
desktop environments, operating systems, architectures and native Wayland are
outside that support. Hardware GPU acceleration has not been verified yet.
