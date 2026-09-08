# Fedora 44 desktop RPMs

This directory defines two source RPMs. `python-tongs` builds the independently
installable `python3-tongs` core and the optional `python3-tongs+mcp` command
subpackage. `tongs-desktop` installs the accepted issue 51 Electron payload and
requires the exact core EVR used by its launcher.

The source preparation step derives the unpublished development version from the
exact checked-out commit. By default it requires the desktop producer contract
to name that same commit, and embeds both identities into the desktop SRPM. It
copies the accepted issue 51 archive only after
checking its size, SHA-256, source identity, platform, compatibility interval,
and every declared payload file. The desktop SRPM repeats that validation before
extracting the payload. No wheel, virtual environment, npm build, or network
operation is part of either SRPM rebuild.

`python3-tongs` owns `/usr/bin/tongs`, the Python modules, and their metadata.
`python3-tongs+mcp` owns only `/usr/bin/tongs-mcp`; its explicit dependency on
`python3dist(mcp[cli])` keeps MCP out of the base closure. `tongs-desktop` owns
the complete runtime under `/usr/libexec/tongs-desktop`, its system launcher,
desktop entry, icon, AppStream metadata, man page, and conventional license and
inventory copies. The RPMs contain no scriptlets and do not inspect or modify
home directories.

The repository manifest names the historical `825a421` archive only as the
reviewed initial issue 52 fixture. The hosted issue 52 proof opts into that
fixture explicitly. A combined or release candidate supplies its own immutable
producer manifest to `prepare_sources.py` and uses the default exact-source
pairing. A differing payload source requires separate reviewed byte-equivalence
evidence and the explicit fixture switch; semver compatibility alone is not an
acceptable production pairing.

The desktop license field is an honest aggregate expression for a GitHub Release
artifact containing the upstream Electron distribution. It does not claim that
this package is eligible for Fedora or COPR publication review.

The end-to-end harness is deliberately restricted to a disposable GitHub-hosted
runner because it performs normal Fedora DNF and Podman operations. It rebuilds
the seven accepted issue 85 companion SRPMs, creates and independently rebuilds
both issue 52 SRPMs, then proves clean install, a dependency-negative failed
upgrade, upgrade, reinstall, optional MCP installation, file and metadata
integrity, and uninstall preservation of user and unrelated sentinels.

Local source-level checks do not invoke Podman or RPM tooling:

```bash
.venv/bin/pytest tests/packaging/rpm/desktop tests/test_plugins/test_plugin_system.py -q
.venv/bin/ruff check packaging/rpm/desktop tests/packaging/rpm/desktop src/tongs/mcp/plugin.py
```
