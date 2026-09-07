# Electron shell prototype

This directory is the disposable Electron half of the desktop shell comparison.
It implements the `prototype-1` contract and does not change Tongs production
packaging. The milestone target is Fedora 44 KDE on x86_64 using native Wayland.
Other Linux distributions, desktops, display systems, architectures, and
operating systems are deferred by the current prototype scope.

The shell launches the selected Python interpreter as an NDJSON sidecar. That
process discovers installed `tongs.plugins` entry points and owns the loopback
asset server. The main Electron process correlates concurrent requests by
integer ID and enforces 10 second startup and request timeouts. EOF and process
exit reject pending work. Normal application shutdown closes stdin, waits three
seconds, then uses TERM and finally KILL only if the sidecar does not stop.

The renderer receives only `window.tongs.invoke()` through a sandboxed preload:

- `contextIsolation: true`, `nodeIntegration: false`, `sandbox: true`, and
  `webviewTag: false`
- a fixed RPC method allowlist and a 64 KiB JSON parameter limit
- sender, main-frame, and exact loopback-origin checks on every IPC request
- all popups, permissions, webviews, and navigation away from the asset root
  denied
- a response Content Security Policy that permits scripts and connections only
  from the sidecar origin

The independently installed Python plugin is trusted code, as specified by the
comparison contract. The browser boundary prevents its frontend module from
acquiring Node or arbitrary IPC access. This is useful isolation, but it is not
a security sandbox for the Python plugin.

## Development and adapter checks

Node 22.12 or newer is required to install the pinned Electron 44.2.0 build.
Node and npm are build tools only. They are absent from the packaged runtime's
consumer prerequisites.

```bash
cd spikes/desktop/electron
npm ci
TONGS_DESKTOP_PYTHON=/path/to/python-with-tongs-and-plugin npm test
```

`npm test` uses Node's built-in test runner. It covers the URL, frame, origin,
method and input controls, concurrent real-sidecar requests, request timeout,
sidecar crash, and clean EOF shutdown. The real-sidecar test binds a local port
and needs permission to use loopback. `fixture/` is deliberately labeled as
synthetic and exists only to test the adapter before or independently of the
common frontend.

Run the common frontend in a real window and execute the shared interaction
probe with:

```bash
npm run smoke -- \
  --frontend ../frontend/dist \
  --python /path/to/python-with-tongs-and-plugin \
  --ozone-platform wayland \
  --ui-probe ../tests/common_ui_probe.js \
  --report evidence/common-ui-smoke.json \
  --screenshot evidence/common-ui-native-window.png
```

The helper always passes `--ozone-platform=wayland` by default and records the
actual Chromium switch. It currently also passes `--disable-gpu`. On the tested
host surface, Electron reported that Wayland was incompatible with Vulkan and
closed the renderer with either its default settings or `--disable-vulkan`.
Software rendering allowed a native Wayland window while the Chromium sandbox
remained enabled. This graphics workaround and its performance impact need
revalidation outside the prototype environment.

The optional probe is evaluated inside the native renderer after the direct
bridge checks and has a 12 second timeout. Its result is written under
`ui_probe` before the screenshot. The shared probe exercises the real UI,
including large-diff scrolling, split diff, keyboard focus, theme switching,
plugin module mount, navigation-driven unmount and remount, backend invocation,
and bundled help. The frontend unit test separately proves that the cleanup
callback returned by a module is called.

`scripts/measure-process-tree.py` samples the native process tree on Linux and
records both summed RSS and proportional set size. RSS double-counts shared
pages across Chromium processes, so PSS is the better comparison value:

```bash
python3 scripts/measure-process-tree.py --output evidence/memory.json -- \
  node scripts/native-smoke.mjs \
  --frontend ../frontend/dist \
  --python /path/to/python-with-tongs-and-plugin \
  --ui-probe ../tests/common_ui_probe.js
```

## Local distribution and wheel proof

The distribution helper follows Electron's documented manual Linux layout. It
copies the pinned runtime and puts the shell, compiled frontend, and Python
backend below `resources/app`. The resulting directory starts without npm,
Node, a virtual environment, a source checkout, or any first-launch download:

```bash
npm run build:distribution -- --frontend ../frontend/dist
TONGS_DESKTOP_PYTHON=/usr/bin/python3 \
  dist/tongs-electron-linux-x64/tongs-electron \
  --ozone-platform=wayland --disable-gpu
```

The experimental Python wheel embeds that complete distribution and uses the
invoking interpreter as the sidecar interpreter. This is the key property for a
future `pip install "tongs[desktop]"`: plugin entry points and Tongs come from
the same installed Python environment while the consumer needs no external
Node runtime.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install setuptools wheel
npm run build:wheel -- --frontend ../frontend/dist --python .venv/bin/python

python3 -m venv /tmp/tongs-electron-install
/tmp/tongs-electron-install/bin/python -m pip install \
  /path/to/tongs-wheel \
  /path/to/reference-plugin-wheel \
  dist/tongs_electron_prototype-0.0.1-py3-none-linux_x86_64.whl
cd /tmp
/tmp/tongs-electron-install/bin/tongs-electron-prototype \
  --ozone-platform=wayland --disable-gpu
```

Local proof used non-editable wheels built from foundation commit `a33d8d3`:
Tongs `0.4.2.dev1+ga33d8d3eb`, reference plugin `0.0.1`, and Electron prototype
`0.0.1`. All three were installed under the disposable interpreter's
`lib64/python3.14/site-packages`; the launch ran from `/tmp`. The installed
launcher found `mcp`, `sample-desktop`, and `sample-terminal`, invoked the
desktop plugin, loaded its help, fetched 20,000 diff lines, and shut down cleanly.
This proves installed resource and entry-point lookup without an editable
checkout. The inputs were local build artifacts, not PyPI releases.

The final common-frontend wheel is 132,000,538 bytes (125.9 MiB), with a 291
MiB installed package. It exceeds PyPI's [default 100.0 MB per-file limit](https://docs.pypi.org/project-management/storage-limits/).
PyPI says increases are sometimes possible, but no account-specific increase
has been requested or verified. A 150 MB project limit would fit this prototype,
but PyPI's [upload-limit request guidelines](https://github.com/pypi/support#guidelines-for-upload-limit-requests)
say requests caused by bundling another programming-language runtime are
generally denied. Electron embeds Node, so an exception is materially uncertain
and must not be treated as a release plan. A platform runtime wheel therefore
has a viable local pip installation path but is not currently publishable under
the default limit. It remains useful installation evidence rather than the
proposed primary release channel.

The next design gate will consider a `tongs --install-desktop` flow that selects
an OS, architecture, and package-format entry from a signed/checksummed GitHub
Release manifest and explicitly downloads the matching artifact. This removes a
PyPI limit increase as an architecture blocker. It still needs installer design,
transactional verification and rollback, and clear user consent. No installer
is implemented by this spike.

## Fedora RPM and COPR feasibility

The RPM design should keep the terminal package independent:

- `python3-tongs` contains the core and TUI dependencies.
- `tongs-desktop` is x86_64-specific, requires `python3-tongs` and the needed
  GTK, NSS, audio, X11/Wayland and graphics libraries, and installs the runtime
  under `/usr/libexec/tongs-desktop`.
- `/usr/bin/tongs-desktop` launches `/usr/bin/python3` as the sidecar, which lets
  normal system Python entry-point discovery find RPM-installed plugins.
- Compiled core assets belong in the libexec application tree. Plugin assets
  stay in each plugin's Python package and are found through
  `importlib.resources`. Neither path may refer to a checkout or venv.
- The RPM installs a desktop entry, scalable and raster icons, AppStream
  metadata, license files, and man page under standard `/usr/share` locations.
  The prototype copies the existing website/favicon artwork into `assets/icon.png`
  and sets it on the native window; package builds include it without a web fetch.
- Release automation publishes the built RPM as a GitHub Release asset and may
  also submit it through COPR. Runtime startup never contacts GitHub. Updates
  remain explicit package/installer operations rather than an Electron
  self-update channel.

An official Fedora 44 and updates repository query on 2026-09-07 returned no
package named `electron` that this shell could reuse. The prototype therefore
bundles Electron 44.2.0, including Chromium 152 and Node 24.20.0. The copied
runtime is about 296 MB before RPM compression. Electron itself is MIT licensed;
the distribution already carries `LICENSE` and `LICENSES.chromium.html`, but an
RPM needs a complete license review, SPDX metadata, source correspondence, and
an SBOM for all bundled components. COPR's allowed-license rules still apply.
Official Fedora inclusion is likely harder because bundled libraries and
prebuilt upstream binaries need Fedora packaging review. No acceptance by COPR
or Fedora has been established.

A reproducible offline SRPM needs prepared, checksummed sources for Tongs, the
reference contract/plugin used by tests, the frontend lockfile and npm source
tarballs, Electron's npm package and transitive packages, and the matching
Electron Linux runtime archive plus checksums. The build must use those `Source`
entries or an offline npm cache, build the frontend, assemble `resources/app`,
and fail on every network request. Building Electron and Chromium from source
would add a much larger source, toolchain, patch, and build-capacity obligation;
it may be necessary for official Fedora policy if no reusable runtime emerges.

Tongs maintainers would own runtime security updates. Electron supports only
its latest three stable majors, so a desktop release needs a monitored Electron,
Chromium, Node, and codec update cadence with a rebuilt wheel/RPM and regression
test on each update. The RPM should update only through DNF/COPR; Electron
self-update must remain absent. This keeps ownership with the package manager
and avoids an untracked runtime download on first launch.

## Observed Fedora 44 result

The native common-UI run used KDE Wayland on x86_64 with Electron 44.2.0,
Chromium 152.0.7977.76, embedded Node 24.20.0, and Python 3.14.7. Startup to the
completed direct bridge proof was 868.6 ms for the final installed wheel.
Fetching the 20,000-line fixture over NDJSON took 61.4 ms. The native UI probe
took 286 ms; its large-diff render step took 40.2 ms, kept 27 rows mounted in a
540,000 pixel virtual canvas, and
reached the last row. The probe also observed keyboard focus, light and dark
theme behavior, split diff, plugin mount and navigation-driven remount, backend
response, and visible bundled help.

A common-frontend process-tree sample rooted at the Node smoke wrapper and
including Electron helpers plus the Python sidecar peaked at 10 processes,
1,034,528 KiB summed RSS, and 531,053 KiB PSS across six samples. Summed RSS
double-counts shared Chromium pages; use the 518.6 MiB PSS observation for shell
comparison. This is a short startup-and-probe peak, not an idle steady-state
measurement.

The completed screenshot and JSON files live in the ignored `evidence/`
directory so local machine paths and transient metrics are not committed.
Fedora 44 KDE x86_64 native Wayland is the only validated environment for this
milestone.

References: [Electron manual application packaging](https://www.electronjs.org/docs/latest/tutorial/application-distribution),
[Electron release support policy](https://www.electronjs.org/docs/latest/tutorial/electron-timelines),
[Electron security guidance](https://www.electronjs.org/docs/latest/tutorial/security),
and [PyPI storage limits](https://docs.pypi.org/project-management/storage-limits/).
