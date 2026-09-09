# Desktop troubleshooting

!!! warning "Unreleased feature"

    The desktop installer is part of the unreleased desktop initiative. There is
    no public desktop artifact, production tag, finalized RPM package name, or
    release install to download yet. This page describes the implemented
    recovery behavior, not a supported product you can install today.

Start with `tongs desktop status`. It acquires the per-user command lock, ensures
the private roots exist, and reports the current state without changing
activation, the menu, or payload content. It never downloads, repairs, or removes
anything.

```console
tongs desktop status
tongs desktop status --json
```

## Recovery states

| `recovery` | What it means | What to run |
| --- | --- | --- |
| `healthy` | The active payload and owned menu entry are ready. | Nothing. |
| `uninstalled` | No per-user activation is active. | `tongs desktop install`, once a verified release exists. |
| `activation-pending` | A verified activation was interrupted partway. | `tongs desktop repair` |
| `menu-repair-required` | The owned menu entry is missing or was changed. | `tongs desktop repair` |
| `payload-repair-required` | The bound environment or the payload no longer validates. | `tongs desktop repair` from the intended persistent environment, or `tongs desktop repair --redownload` if local recovery fails. |
| `cleanup-required` | An old owned payload has not been removed. | Depends on the `detail` field; see below. |

`cleanup-required` covers two different situations. Read the `detail` field, or
the sentence the plain command prints, to tell them apart:

- **The per-user desktop installation can launch, but old payload cleanup is
  incomplete; run 'tongs desktop repair'.** An active payload is still present.
  Repair before the next install or update. Install and update both stop before
  any download while cleanup is pending, with `Desktop payload cleanup is
  incomplete; run repair before installing another release.`
- **Per-user desktop cleanup is incomplete; run 'tongs desktop uninstall'
  again.** No active target remains, so an interrupted uninstall left work
  behind. Run uninstall again.

Recovery is bounded to the identities recorded in the private journal and state.
Repair does not search arbitrary directories for payloads, and there is no
rollback or migration command.

## The installer refuses to run from this environment

The desktop installation is bound to one exact console script and interpreter,
because the installed menu entry has to keep working after the shell that
created it is gone.

**Desktop menu registration requires a persistent Python installation. Install
Tongs with 'pipx install tongs' or 'python -m pip install --user tongs', then
retry.**

The invoking environment was classified as transient. `uvx tongs` creates a
throwaway environment that disappears after the command, so it cannot be bound.
Persistent environments are a virtual environment, a pipx install, a per-user
site install, and a system install. Reinstall Tongs into one of those, then run
the desktop command again.

**This desktop installation is bound to another Python environment; run repair to
rebind it explicitly.**

An activation already exists and was created from a different persistent
environment. Install and update both stop before any download. Run
`tongs desktop repair` from the environment you intend to keep, which rebinds the
installation deliberately rather than silently.

## The application will not launch

`tongs desktop` with no activation exits with code 2 and prints
`No per-user desktop installation is active. Run 'tongs desktop install'.` A
launch request is never turned into an implicit install.

Otherwise the launcher revalidates the exact bound console script and
interpreter, the installed payload digest and file set, the compatible core
version, and the RPC and desktop-plugin API majors before starting anything. It
does not fall back to `PATH` or to the current working directory, so a message
naming the bound environment means that exact recorded path no longer validates:

| Message | Cause |
| --- | --- |
| `The bound Tongs Python environment changed or disappeared.` | The recorded console script or interpreter is gone or was replaced. |
| `The bound Tongs Python environment could not be validated.` | The environment could be found but did not answer the identity probe. |
| `The bound Tongs console script has an invalid interpreter.` | The console script's shebang does not name a usable interpreter. |
| `The bound Tongs core is incompatible with this desktop payload.` | The bound core version is outside the payload's declared compatibility interval. |
| `The installed desktop launcher is unsafe.` | The launcher inside the payload failed its validation. |

All of these are repair conditions. Run `tongs desktop repair` from the intended
persistent environment first; use `tongs desktop repair --redownload` only when
local recovery cannot succeed.

Two console-script failures are not repair conditions, because repair rebinds an
environment and cannot rewrite a console script that a packaging tool wrote:

**The bound Tongs console script must name one absolute Python interpreter path,
followed only by the '-E' flag pipx adds. Install Tongs with 'pipx install tongs'
or 'python -m pip install --user tongs', then retry.**

The shebang has to identify the interpreter without a `PATH` lookup. Three shapes
are accepted: the plain absolute shim pip and venv write (`#!/usr/bin/python3`),
the fixed shell trampoline described below, and the pipx shape, which appends
`-E` to that absolute path when it exposes an app. A `#!/usr/bin/env python3`
shebang, a relative interpreter path, and any other interpreter flag stay
rejected, because each of them can resolve to a different Python than the one the
menu entry was bound to. Only a space or a tab separates the interpreter from
that flag, matching what the kernel treats as a separator; any other whitespace
belongs to the path the kernel would execute, so it is rejected as well.

**The bound Tongs console script's shell trampoline must exec one absolute Python
interpreter path and no arguments. Install Tongs with 'pipx install tongs' or
'python -m pip install --user tongs', then retry.**

pip and venv write a fixed `/bin/sh` trampoline instead of a plain shebang when
the environment path contains a space, and Tongs reads the interpreter out of
that one exact form. This message means the second line is not that form: the
`exec` preamble or trailing arguments differ, more than one value is quoted, or
the quoted interpreter is not an absolute path. pipx never rewrites a trampoline,
so the `-E` flag plays no part here. Reinstall into a supported layout instead of
editing the console script by hand.

## Graphics and session

The native acceptance policy covers Fedora 44 KDE on x86_64 using XWayland.
Native Wayland, other desktop environments, other distributions, and other
architectures are outside that policy. The release metadata check rejects an
unsupported artifact target before download, so an unsupported platform fails at
install rather than at launch.

On a Wayland session the desktop shell runs through XWayland. `tongs desktop`
adds `--ozone-platform=x11` to the launch itself when it runs on Linux with
`WAYLAND_DISPLAY` set, which is the same condition the shell checks, so the menu
entry and the command need no switch typed by hand. The shell still refuses to
start when that switch is missing:

```text
Fedora KDE Wayland sessions must launch the desktop through XWayland
```

Seeing that message means something other than `tongs desktop` started the
shell.

Hardware GPU acceleration is a required and currently unmet production gate. A
headless, container, or software-rendered run is not evidence that the
accelerated path works, and this documentation makes no hardware acceleration
claim.

## An RPM is also installed

The per-user archive and an RPM are independent installation methods. When both
are present, `status` selects the per-user installation and adds:

```text
A separate RPM installation is also present; this command selected the per-user installation.
```

The per-user commands never install RPM packages, elevate privileges, overwrite
`/usr`, or remove RPM-owned files. RPM detection on its own does not add that
line and does not make the RPM the active per-user target. See
[Coexisting with an RPM](installation.md#coexisting-with-an-rpm).

## Editor does not open from the desktop

The desktop **Open log in editor** control starts the configured graphical editor
with a private, bounded log export. Known terminal-only editor commands are
rejected because they cannot attach to the desktop window. Configure a
wait-capable graphical editor, for example `code --wait` or `kate --block`. Tongs
reports that the editor process started; that is not confirmation that the editor
read the file. See the
[editor configuration reference](../reference/configuration.md).
