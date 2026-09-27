---
title: Install the desktop app
description: "Install, launch, update and remove the tongs desktop app, from the per-user archive or the Fedora RPMs."
eyebrow: install
lead: The desktop app is an optional window over the same repositories, reviews and drafts as the terminal. You install it with one explicit command.
---

:::note[Beta]
The desktop app is a beta. Each GitHub Release from v1.0.0 on carries it as a
per-user archive with its attestation, and as unsigned Fedora RPMs.
:::

tongs stays terminal-first. Plain `tongs` scans your repositories and opens the
terminal app. It never downloads, installs, updates or starts the desktop app.
Every desktop action on this page is a command you run yourself.

There are two ways to install it:

- **The per-user archive.** `tongs --install-desktop` downloads, verifies and
  activates it in your home directory. No root access is needed.
- **The Fedora RPMs.** Download them from the GitHub Release and install them
  with `dnf`.

The two methods are independent and can be installed side by side. The command
reference, recovery states and file layout are on the
[Desktop lifecycle](/reference/desktop-lifecycle/) page.

## Before you start

The desktop app is built for Linux on Fedora 44, x86_64. It is tested on
Fedora 44 KDE running through XWayland. The installer checks your platform
before it downloads anything and stops on an unsupported one.

Install tongs into an environment that will still exist when you launch the
desktop app later. Either of these works:

```console
pipx install tongs
python -m pip install --user tongs
```

A virtual environment or a system installation also works. `uvx tongs` does
not: its environment disappears when the command exits, so the installer
refuses it.

## Install the per-user archive

Install the core, then the desktop app:

```console
pipx install tongs
tongs --install-desktop
```

`tongs --install-desktop` is the same as `tongs desktop install`. It picks the
release whose version matches the tongs core you are running, because the
desktop archive is built from that same commit. It then checks the release
manifest, the archive digest, the platform and version compatibility, and the
GitHub-managed Sigstore attestation, and only then activates the app for your
user.

The desktop assets are built after a release is tagged. If you install within
minutes of a new version reaching PyPI, the installer can find no matching
release yet. It says so and names both versions; retry once the release is
published.

## Launch

Run the desktop app from the terminal, or open **Tongs** from your desktop
menu:

```console
tongs desktop
```

The menu entry runs the same command through the environment you installed
from. If nothing is installed, `tongs desktop` stops with
`No per-user desktop installation is active. Run 'tongs desktop install'.`
It never installs anything on its own.

## Update

Upgrade the core first, then the desktop app:

```console
pipx upgrade tongs
tongs desktop update
```

`update` installs the desktop release that matches the core you now run. The
launcher only starts a desktop payload that is compatible with the installed
core, so run `update` after every core upgrade.

To install one exact release instead, name its version without the `v`
prefix:

```console
tongs desktop install --version 1.0.1
```

tongs remembers the newest release you have installed and refuses an older
one. To go back on purpose, add `--allow-downgrade` to an explicit
`--version`:

```console
tongs desktop install --version 1.0.0 --allow-downgrade
```

## Check and repair

`status` reports the installation without changing it:

```console
tongs desktop status
```

If it reports a problem, run repair from the environment you installed from:

```console
tongs desktop repair
```

Repair works locally and never downloads on its own. If local recovery fails,
ask for a verified replacement explicitly:

```console
tongs desktop repair --redownload
```

[Troubleshooting](/desktop/troubleshooting/) covers each message, and the
[recovery states](/reference/desktop-lifecycle/#recovery-states) are listed in
the reference.

## Uninstall

```console
tongs desktop uninstall
```

This removes the per-user activation, the payloads it installed and its own
menu entry. It does not touch an RPM installation or the tongs core. Remove the
core with the tool you installed it with, for example `pipx uninstall tongs`.

## Install the Fedora RPMs

Each release attaches three Fedora 44 packages to its GitHub Release, together
with the source-built companion packages they depend on. Download all of them
with the checksum file, check them, and install them together:

```console
gh release download v1.0.1 --repo andre-motta/tongs --pattern '*.rpm' --pattern SHA256SUMS
sha256sum --check --ignore-missing SHA256SUMS
sudo dnf install ./*.rpm
```

You can also download the files from the release page in a browser. The RPMs
are not GPG signed, and there is no COPR or other package repository, so
`dnf` does not update them for you. Install the next release's RPMs the same
way.

The RPM installs a system launcher. Start it from the menu or with:

```console
tongs-desktop
```

The packages and the files they own are listed under
[Coexisting with an RPM](/reference/desktop-lifecycle/#coexisting-with-an-rpm).

## Known issues

The defects that ship in this beta are listed on
[Known issues](/releases/known-issues/). Read it before you rely on the desktop
app for a review you cannot redo.
