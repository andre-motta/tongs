# Desktop comparison contract, prototype-1

This is a disposable feasibility spike, not a supported Tongs plugin API or the
production desktop extra. Both shells use exactly the same built React frontend,
fixture backend, and independently installed Python reference plugin. No real
forge credentials or mutations are used.

## Component ownership and dependencies

- Astra: `backend/`, `reference-plugin/`, `tests/`, this contract, integration and evidence.
- Luna xhigh: `frontend/` only, including locked React/TypeScript/Vite dependencies.
- Sol high: `webview/` only, Python-hosted pywebview/PySide6 launcher and smoke proof.
- Separate Sol high: `electron/` only, Electron shell, Python sidecar, packaging and smoke proof.
- Both shell agents may add their own subtree README, tests, and build helpers.
  Do not edit backend/frontend or root packaging. Return contract defects to Astra.
- Independent Sol review follows implementation. Shell and frontend work may run
  concurrently against this fixed, committed contract; integrated UI verification
  follows the frontend build. Use separate assigned branches/worktrees.

## Frontend contract

The React build lives in `frontend/dist`. Its only backend interface is
`window.tongs.invoke(method: string, params?: Record<string, unknown>): Promise<unknown>`.
Listen for `tongs-ready` and also check whether `window.tongs` already exists.
The webview adapter waits for `pywebviewready` then wraps `pywebview.api.invoke`.
Electron supplies `window.tongs` through contextBridge, with context isolation
enabled and Node integration disabled. Never require Node in the renderer.

RPC results:

- `health`: `{fixture: true, protocol: "prototype-1"}`.
- `list_reviews`: array of `{id, number, title, author, repo, forge, status}`.
- `get_diff`, `{id}`: `{path, lines: [{old_line, new_line, kind, text}]}`;
  kinds `context`, `addition`, `deletion`. Both normal and large fixture IDs exist.
- `list_plugins`: array of `{id, title, status, modules}`; statuses `ready`,
  `terminal_only`, `disabled`, `error`; modules contain `{id, title, entry_url}`.
- `plugin_invoke`, `{plugin, method, params}`: plugin-defined JSON result;
  sample plugin supports `echo`, `{text}`, returning `{message, calls}`.
- `plugin_help`, `{plugin, module}`: bundled documentation text as a string.

Use a dense review workspace with a repo/navigation sidebar, review inbox,
unified/side-by-side diff toggle, and Plugins navigation. Clearly label all data
as fixture data. Include keyboard-accessible controls, light/dark support, and
window resizing. Virtualize or otherwise bound DOM size for the large diff.

Plugin modules are standalone bundled ES modules dynamically imported from
`entry_url`. Each exports `mount(container: HTMLElement, api)` and returns a
cleanup function; `api.invoke(method, params)` routes to that plugin's
`plugin_invoke`. Core frontend code must not import the sample plugin package or
hardcode its ID. Provide a Help control using `plugin_help`.

## Python backend and asset server

From `spikes/desktop`, import `Backend` and `start_assets` from `backend`.
`backend = Backend()` discovers installed `tongs.plugins` entry points and
supports the RPC methods above synchronously. `start_assets(frontend_dir, backend)`
returns a running server object with `url` and `close()`. Assets are loopback-only
and confined to frontend or declared plugin roots. There is no HTTP mutation API.
`backend.invoke(method, params)` returns JSON-compatible data or raises ValueError.

For Electron, launch `python -m backend --frontend /absolute/path/to/frontend/dist`
with cwd `spikes/desktop` using the selected Python interpreter. A development
venv is convenient, but installed launchers must also support system Python. The
sidecar prints one readiness line `{event:"ready", url:"http://127.0.0.1:PORT/"}`.
Send newline JSON `{id, method, params}` to stdin and read
`{id, result}` or `{id, error:{message}}`. Handle timeout, EOF/crash, concurrent
requests, and clean shutdown. The sidecar owns the asset server.

## Packaging and evidence

Users must receive prebuilt frontend/plugin assets. Node/npm are build tools,
not an acceptable runtime prerequisite for an eventual pip-installed desktop app.
Each shell must document and exercise a local wheel/distribution installation
route, identifying unresolved PyPI or native library constraints honestly.
Do not publish packages or download runtimes on first user launch. No disabling
Chromium's sandbox as the supported launch configuration.

The CTO also requires eventual Fedora RPM distribution, targeting an upstream
RPM/COPR first, with official Fedora repository inclusion considered later.
Evaluate this alongside the pip route, without building a release RPM in this
spike. Describe the future spec/build inputs, runtime dependency split, installed
asset lookup, desktop entry/icon installation, plugin entry point discovery under
system Python, and upgrade ownership. Neither launcher may assume a source tree
or virtual environment after installation. Keep the terminal package independent
of desktop runtime dependencies. Prefer system-managed native libraries where
available and disclose bundled runtimes, licenses, size, and update obligations.
Build-time network downloads must be explicit and separable from runtime startup;
identify work needed for a future offline RPM build from prepared sources.

Deliver commands, versions, startup/process-tree memory observations, large-diff
behavior, plugin backend-call/help evidence, and screenshots from the actual
native window. Label browser-only checks separately. The CTO narrowed initial
support and acceptance to the existing Fedora 44 KDE x86_64 system. Record the
actual Qt/Ozone backend, distinguishing native Wayland from XWayland. Other
distributions, desktops, native Xorg, architectures and operating systems are
deferred expansion, not unmet gates for this milestone. Do not install another
OS or desktop, or set up test VMs on the user's PC. Record limitations rather than
equating the spike with a production release.
