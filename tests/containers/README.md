# Fedora 44 Podman validation harness

This harness validates the current checkout in a headless Fedora 44 x86_64
container. Run it on a disposable Linux host with rootless Podman. The supported
probe runs on GitHub's `ubuntu-24.04` x86_64 hosted runner. Do not run it on a
developer workstation that must remain unchanged.

From the repository root, use one command and place results outside the checkout:

```bash
tests/containers/run-fedora-44.sh --output-dir /tmp/tongs-fedora-44
```

The output directory must be empty so evidence from separate revisions cannot be
mixed accidentally.

The script pulls the Fedora image, builds a harness image, mounts the checkout
read-only, and runs the container without network access. The container root is
read-only, all capabilities are dropped, and `no-new-privileges` is enabled. No
credentials are mounted. Build and test scratch data stays in a temporary
filesystem, while reports and wheels go to the requested output directory.

The probe builds and installs fresh core and `examples/desktop-plugin` wheels,
checks `tongs --help`, and runs the installed-wheel smoke subset named by
`SMOKE_TESTS` in `probe.py` (deselecting tests marked `needs_git`). The subset
covers what the wheel must carry: entry points and the MCP dependency gate,
desktop plugin discovery and resources, packaged schemas and desktop assets,
the installed sidecar, the MCP server, config, and launcher resolution. The
step fails if a listed path is missing or contributes no test. The hosted core
lane runs the whole suite from the checkout. The probe then checks installed
plugin discovery from both directions: the TUI registry must load the example's
terminal command, and the desktop registry must report the example provider as
discovered and the core `mcp` entry point as terminal-only.
Test and runtime dependencies are provisioned in the harness image, then exposed
to the clean wheel-install environment without downloading during container use.
The image installs exactly Textual 4.0.0, the oldest release the core declares,
so the core suite runs on that floor while other lanes use the newest release.

`summary.json` records every command, duration, and exit status. JUnit XML files,
stdout and stderr, Fedora and Python versions, installed dependency versions,
runner details, Podman information, and base and built image inspection JSON are
retained beside it. The image inspection records immutable image IDs and repo
digests. Any required step failure makes the command return nonzero.

Failure propagation has an explicit test mode:

```bash
tests/containers/run-fedora-44.sh \
  --output-dir /tmp/tongs-fedora-44-failure \
  --inject-failure
```

That mode skips the build, install and smoke steps, runs one clearly labelled
failing pytest assertion, writes `deliberate-failure.junit.xml`, and must
return nonzero. The success run already proves the installed product, so this
run proves only that a failing step propagates through the container and
`run-fedora-44.sh`. The probe workflow accepts the nonzero result only when
`summary.json` and the JUnit report prove that `deliberate-failure` was the
only step and only the named assertion failed. Missing, malformed, skipped, or
unexpected results fail the workflow. It does not use `continue-on-error`.

Remove the output directories and the local image after inspection:

```bash
rm -rf /tmp/tongs-fedora-44 /tmp/tongs-fedora-44-failure
podman image rm localhost/tongs-fedora44-probe:issue-27
```

This container proves Fedora 44 userspace, clean wheel installation, installed
resources and entry points, and discovery of the example desktop plugin. It
does not prove a Fedora KDE session, the Fedora host kernel, physical GPU
acceleration, native Wayland or XWayland behavior, a running desktop plugin, or
installation of a production RPM. Native ARM64 and QEMU execution are outside
this harness.
