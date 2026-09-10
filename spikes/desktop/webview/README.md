# Tongs Python webview prototype

This subtree evaluates a Python-hosted desktop shell for issue #20. It is a
disposable comparison prototype, not a supported desktop extra or plugin API.
The launcher uses pywebview 6.2 with the Qt backend, QtPy, PySide6, and Qt
WebEngine. All review data is fixture data.

The fixed Python backend and the selected prebuilt frontend are copied into the
wheel at build time. At runtime the launcher resolves those files with
`importlib.resources`, starts the fixed loopback-only asset server, creates one
native window, exposes only `pywebview.api.invoke`, installs the promised
`window.tongs.invoke` adapter after every page load, and owns server shutdown.
The common frontend only consumes that adapter. Node, npm, a source checkout,
and a first-launch download are not runtime requirements.

The RPM input `packaging/io.github.andre_motta.tongs.png` is an unchanged copy
of `docs/assets/icon.png`, the existing Tongs website logo and favicon. The
wheel's common frontend contains that same image once at
`assets/frontend/assets/tongs-icon.png`; the launcher also uses it as the native
window icon instead of duplicating a 2048-pixel image in the wheel.

## Local development and checks

Use a checkout-local environment. On Fedora, `--system-site-packages` allows the
environment to use the distribution's PySide6 and Qt WebEngine bindings instead
of downloading duplicate Qt wheels.

```bash
cd spikes/desktop/webview
python3.14 -m venv --system-site-packages .venv
.venv/bin/python -m pip install "pywebview>=6.2,<7" "QtPy>=2.4,<3" \
  "build>=1.2,<2" "pytest>=8,<9" "ruff>=0.12,<1"
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

The test fixture at `tests/fixtures/minimal_frontend` is visibly labelled as a
minimal shell fixture. It only proves host packaging, bridge startup, and
shutdown while the common React build is unavailable. It must not be used as
the final UI comparison evidence.

## Build and installed-wheel exercise

Build from an explicit frontend input. For the final comparison, that input is
the common `frontend/dist` directory built by issue #19.

```bash
cd spikes/desktop/webview
.venv/bin/python build_wheel.py \
  --frontend ../frontend/dist \
  --output dist
python3.14 -m venv --system-site-packages /tmp/tongs-webview-installed
/tmp/tongs-webview-installed/bin/python -m pip install \
  dist/tongs_desktop_webview_prototype-0.0.1-py3-none-any.whl
```

For a pip-only environment that has no system Qt binding, install the wheel with
its `pyside6` extra. This downloads the PySide6 wheel stack at install time and
is substantially larger than the prototype wheel itself.

```bash
/tmp/tongs-webview-installed/bin/python -m pip install \
  "dist/tongs_desktop_webview_prototype-0.0.1-py3-none-any.whl[pyside6]"
```

The independently built `tongs-desktop-sample` wheel must be installed into the
same Python environment to exercise plugin module discovery, its Python `echo`
call, and bundled help. The host wheel does not copy or hardcode that plugin.

Launch the installed command without `--frontend` to prove it uses wheel data:

```bash
/tmp/tongs-webview-installed/bin/tongs-desktop-webview-prototype
```

The launcher explicitly chooses Qt and PySide6, disables downloads and `file:`
access, uses private browser storage, and leaves the Chromium sandbox enabled.
It refuses `QTWEBENGINE_DISABLE_SANDBOX=1` and an explicit `--no-sandbox` token
in `QTWEBENGINE_CHROMIUM_FLAGS`. Debug mode is off unless `--debug` is supplied.

## Native Fedora evidence

The milestone target is the user's existing Fedora 44 KDE x86_64 system. The
native smoke calls health, lists installed plugins, invokes the sample plugin,
reads its bundled help, then closes the window and loopback server. The JSON
records `QGuiApplication.platformName()` as `qt_platform`; `wayland` is native
Wayland while `xcb` is XWayland and must be labelled that way.

For an automatic native-window capture and `/proc` process-tree RSS observation:

```bash
cd spikes/desktop/webview
.venv/bin/python capture_smoke.py \
  --executable /tmp/tongs-webview-installed/bin/tongs-desktop-webview-prototype \
  --ui-probe ../tests/common_ui_probe.js \
  --screenshot evidence/native-window.png \
  --evidence evidence/native-smoke.json
```

The helper waits for a successful renderer-side bridge round trip, asks the
application's own Qt widget to capture itself, records steady and observed peak
aggregate RSS for the launcher and descendants, and waits for clean exit. The
capture cannot include another application or the rest of the desktop. Keep the
raw JSON and screenshot with integration evidence. `--ui-probe` reads an async JavaScript
expression after the page and bridge are ready, evaluates it in the native
renderer, stores its result under `ui_probe`, and requires its `ok` field to be
true for the combined smoke result. This lets the shared probe exercise actual
frontend selection, large-diff bounds, dynamic plugin mounting, backend calls,
and help before capture. Other distributions, desktop
environments, native Xorg sessions, and operating systems are deferred by the
current milestone scope and are not implied by this Fedora result.

The helper sets `PYTHONNOUSERSITE=1` for the launched application. This prevents
unrelated per-user Python packages and plugin entry points from entering the
installed-wheel proof. Its RSS number is the sum of resident memory reported by
`/proc` for the process tree, so shared pages can be counted more than once.

### Recorded result on 2026-09-07

The final local run used Python 3.14.7 on Fedora 44 KDE x86_64. The wheel
contained core `tongs` 0.4.2.dev1+ga33d8d3eb and the fixed backend from
foundation commit `a33d8d3eb4274f072aaf2d50b0092ed4249a00d1` (backend SHA-256
`5d63570de7fc556ec82f1817cd26e0cf10fe115ac53ab2d03a46262161e2a45a`).
The common frontend came from issue #19 candidate
`5cb91f4ae809edbe86aed37579fa1571efb35138`. The isolated environment installed
the local core, host, and `tongs-desktop-sample` 0.0.1 wheels. Their modules and
assets resolved under the environment's `site-packages`, outside every source
checkout. Runtime versions were pywebview 6.2.1, QtPy 2.4.3, and Fedora PySide6
6.11.2.

The native Qt platform was `wayland` and the renderer was `qtwebengine`. The
full renderer-side smoke completed in 1,248.8 ms and exited cleanly in 3,864.9
ms including a two-second evidence hold. The large fixture rendered 20,000 lines
in 40.6 ms with 28 diff rows in the DOM, reached the last row, and reset scroll
for the normal fixture. Side-by-side mode, keyboard focus, light and dark themes,
plugin mount, DOM removal, remount, backend call, and bundled help all passed.

The five-process steady and observed-peak aggregate RSS was 898,516 KiB: 531,620
KiB for the Python host, 169,720 KiB across three Qt WebEngine zygote processes,
and 197,176 KiB for the renderer. This aggregate RSS can double-count shared
pages. No `QTWEBENGINE_*`, `QT_QPA_PLATFORM`, or GPU override was set. The child
arguments contained neither `--no-sandbox` nor `--disable-gpu`; Qt enabled Vulkan
and disabled accelerated video decoding. One Qt zygote used its normal
`--no-zygote-sandbox` argument, which does not disable the renderer sandbox.

The host wheel was about 3.76 MB, the exact core wheel was 83,488 bytes, and
the independently installed sample plugin wheel was 3,310 bytes. The host wheel
includes the 4,250,698-byte project icon once and compresses it. Installed Fedora
RPM payload sizes were 57,480,479 bytes for PySide6, 290,845,397 bytes for Qt
WebEngine, and 470,515 bytes for Qt WebChannel. The host's Python package payload
was 4.4 MiB including installed bytecode, pywebview was 2.4 MiB, and QtPy was 1.3
MiB by `du`. The native self-capture was 1440 by 900 pixels and 122,829 bytes.
These startup and memory observations describe one synthetic run, not a benchmark.

## Fedora RPM and COPR feasibility

The proposed primary release flow for the next design gate is an explicit
`tongs --install-desktop` command that selects a signed GitHub Release artifact
by OS, architecture, and package format from a manifest. Installer and release
implementation are outside this spike. The standalone wheel exercise remains
useful evidence that the shell can consume installed backend/frontend/plugin
assets without a checkout or first-launch build.

An eventual RPM should preserve the existing terminal package as the base and
put desktop-only dependencies and files in a separate subpackage, for example
`tongs-desktop`. The desktop subpackage would require the core Tongs package,
pywebview, `python3-QtPy`, `python3-pyside6`, and the Qt WebEngine/WebChannel
runtime. Fedora 44 currently supplies QtPy 2.4.3, PySide6 6.11.2, and the native
Qt 6 WebEngine libraries. pywebview itself still needs a Fedora package or a
reviewed bundled Python source build in COPR.

The future source RPM needs these prepared inputs:

- a pinned Tongs source archive with the already-built frontend assets;
- pywebview source if Fedora still lacks it, with BSD license material and a
  Fedora-compatible Python build;
- the npm lockfile and a complete offline source strategy for every frontend
  dependency when assets are rebuilt in Koji/COPR, or a policy-approved source
  artifact whose provenance is verifiable;
- the desktop file, AppStream metadata, icon, and license inventory in this
  directory;
- `%pyproject_buildrequires`, `%pyproject_wheel`, `%pyproject_install`, desktop
  database/AppStream validation, and tests that launch against packaged assets.

The RPM must install Python and frontend files under system locations, the
desktop entry under `%{_datadir}/applications`, AppStream metadata under
`%{_metainfodir}`, and the icon under the hicolor theme. System Python entry
point discovery then sees separately installed `tongs.plugins` distributions,
including their own packaged frontend assets and help. It must not use a venv or
write application code into a user's home directory.

Fedora owns updates for PySide6 and Qt WebEngine in this model. Tongs owns its
compiled frontend and Python code. A COPR pywebview package owner would track
pywebview security and compatibility updates until it is accepted into Fedora.
The Qt WebEngine RPM is the dominant installed component and carries Chromium's
large transitive source and license surface; using Fedora's build avoids making
Tongs the distributor of a second bundled Chromium. The desktop metadata here
is only a reviewed input for later RPM work. No release RPM is produced by this
spike.

The prototype license inventory starts with MIT for Tongs, BSD-3-Clause for
pywebview, MIT and BSD terms for QtPy, and Fedora's `LGPL-3.0-only OR
GPL-3.0-only WITH Qt-GPL-exception-1.0` declaration for PySide6. The common
frontend lockfile includes MIT and Apache-2.0 among its dependency declarations,
and Qt WebEngine has a much broader Chromium-derived license inventory. A
production SRPM must generate and review the complete npm and Qt WebEngine
license inputs rather than treating this short list as sufficient.

## Prototype security and lifecycle limits

The Python plugin entry points are trusted installed code, not a sandbox. The
bridge is injected into the loaded page by pywebview, while the fixed asset
server confines file reads to the packaged frontend and declared plugin roots.
This prototype does not yet add a navigation allowlist, Content Security Policy,
origin authentication, per-plugin permissions, or production error redaction.
Those boundaries must be designed before a production desktop API. The server
uses unauthenticated ephemeral loopback HTTP and has no mutation endpoint.

Normal close, smoke close, and launcher failure all execute the asset server's
`close()` method. Abrupt process termination still relies on operating-system
socket cleanup. The prototype does not add background services or update itself.
