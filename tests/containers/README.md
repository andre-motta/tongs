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

The probe builds and installs fresh core and reference-plugin wheels, checks
`tongs --help`, runs the core suite and existing fixture backend tests, and
checks installed plugin discovery from both directions. The TUI registry must
retain the legacy terminal command, while the experimental desktop backend must
report the opt-in plugin as ready and the legacy plugin as terminal-only.
Test and runtime dependencies are provisioned in the harness image, then exposed
to the clean wheel-install environment without downloading during container use.

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

That mode performs the normal checks, adds one clearly labelled failing pytest
assertion, writes `deliberate-failure.junit.xml`, and must return nonzero. The
probe workflow treats that nonzero result as the expected assertion. It does not
use `continue-on-error`.

Remove the output directories and the local image after inspection:

```bash
rm -rf /tmp/tongs-fedora-44 /tmp/tongs-fedora-44-failure
podman image rm localhost/tongs-fedora44-probe:issue-27
```

This container proves Fedora 44 userspace, clean wheel installation, headless
core behavior, and compatibility with the existing experimental fixture. It
does not prove a Fedora KDE session, the Fedora host kernel, physical GPU
acceleration, native Wayland or XWayland behavior, a production desktop plugin
contract, or installation of a production RPM. Native ARM64 and QEMU execution
are outside this harness.
