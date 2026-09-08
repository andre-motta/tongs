# Tongs example dashboard plugin

This directory is a separately installable, deterministic example of the Tongs
desktop plugin SDK. It registers the same `example_dashboard` identity in both
`tongs.desktop_plugins` and the legacy `tongs.plugins` entry-point groups.

The desktop provider declares one `dashboard` module and navigation target, an
`open-dashboard` command, the `refresh` method, the `refreshed` event, and the
`summary` focus target. Its packaged `dashboard.mjs`, `dashboard.css`, and
`help.md` resources are resolved by the production S4 resource validator.

The Python provider returns fixed review data and publishes `refreshed` through
`DesktopPluginContext`. It never calls a forge. The terminal entry point imports
only `TongsPlugin`, so legacy TUI discovery can load it without importing the
desktop provider or its assets.

## Install

From a Tongs checkout containing the desktop SDK, install Tongs and this example
into the same Python environment. Run these commands from the repository root:

```bash
python -m pip install -e .
python -m pip install ./examples/desktop-plugin
```

The example is distributed in this repository; these instructions do not require
a separately published PyPI package.

No Node build is needed. The ESM module is prebuilt and shipped as package data.

## Verification

From this directory, run the focused tests with the checkout's Python
environment:

```bash
python -m pytest -q
node --test tests/test_dashboard_module.mjs
```

The module test uses a small mocked DOM and a frozen scoped host API. It checks
mount, event subscription and rendering, invocation and notification, focus
binding, navigation, abort handling, and cleanup. Native Electron host
integration is exercised by the desktop application acceptance workflow.
