# Common desktop frontend

This directory contains the shared React/TypeScript/Vite screen used by the
desktop shell comparison. It builds into `dist/`, which is the only directory
the Python asset server needs at runtime. Node and npm are build-time tools;
the eventual pip/RPM package must ship the generated assets and must not depend
on a user having Node, npm, a source checkout, or a virtual environment.

## Development

From this directory:

```bash
npm ci
npm run test
npm run build
```

The app uses a small browser fixture bridge only when opened explicitly with
`?demo=1` (for example, `http://127.0.0.1:5173/?demo=1`). Without that flag, a
missing bridge stays visibly in a waiting state until a shell injects
`window.tongs.invoke(method, params)` and announces it with `tongs-ready`; the
UI checks both paths. The fixture banner is intentional: this prototype never
calls a forge.

To exercise the backend asset server after building, run the shell-specific
launcher from `spikes/desktop`. The server serves `dist/` and declared plugin
assets on loopback. Plugin modules are discovered from `list_plugins` and
loaded with a runtime `import(entry_url)`, so core does not import or name any
installed plugin package. The prototype mounts the first declared module for a
selected plugin; navigating between multiple modules is later scope.

## What the checks cover

`npm run test` is browser-independent unit coverage for the fixture bridge and
the diff window calculation. The latter asserts that a 20,000-line fixture
keeps a full scroll canvas while rendering a small bounded slice. Native shell
startup, plugin asset loading, backend round trips, and screenshots belong to
the webview and Electron evidence runs.
