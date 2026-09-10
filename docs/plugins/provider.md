# Desktop plugin providers

The desktop plugin SDK lets an installed Python distribution add a module,
navigation item, command, packaged help, and a narrow Python provider to the
Tongs desktop application. The current public API major is **1**.

Desktop support is opt-in. A terminal plugin registered under `tongs.plugins`
continues to work without a desktop provider. A desktop provider uses the
separate `tongs.desktop_plugins` entry-point group, and Tongs discovers the two
groups independently.

Desktop providers and their ESM and CSS resources are trusted installed code.
Manifest allowlists, plugin-scoped APIs, resource containment, and CSS isolation
reduce accidental cross-plugin access. They do not sandbox a malicious Python
or UI extension. Install a plugin only when you trust its publisher and code.

## Start from the reference package

The repository contains a separately installable example in
`examples/desktop-plugin`. It is source in the Tongs checkout, not a package
published separately on PyPI. The example has both terminal and desktop entry
points, a production provider, and prebuilt ESM, CSS, and Markdown resources.

Install Tongs and the example into the same isolated Python environment from the
repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m pip install ./examples/desktop-plugin
```

The interpreter running Tongs discovers installed entry points. Installing a
provider into another virtual environment, pipx environment, or Python
installation will not make it visible to that Tongs installation.

Ordinary plugin installation does not require Tongs development dependencies.
To run the example's tests in a clean environment, install the optional Tongs
test dependencies from the repository root first:

```bash
python -m pip install -e ".[dev]"
python -m pip install ./examples/desktop-plugin
python -m pytest -q examples/desktop-plugin/tests
node --test examples/desktop-plugin/tests/test_dashboard_module.mjs
```

The Python test exercises its manifest, lifecycle, call, event, and terminal
import boundary. The Node test uses a mocked DOM and host API to exercise module
mounting and cleanup. It is not native Electron integration evidence.

## Register each surface explicitly

A distribution that supports both interfaces declares the same canonical name
in both groups. Each value is an independently imported class:

```toml
[project.entry-points."tongs.desktop_plugins"]
example_dashboard = "tongs_example_dashboard.desktop:ExampleDashboardProvider"

[project.entry-points."tongs.plugins"]
example_dashboard = "tongs_example_dashboard.terminal:ExampleDashboardTerminalPlugin"
```

Use only `tongs.plugins` for a terminal-only distribution. Use only
`tongs.desktop_plugins` for a desktop-only distribution. Desktop discovery lists
terminal-only entry-point names as `terminal_only` without importing or
constructing their classes. Terminal discovery never loads the desktop group.

Plugins are enabled by default. The shared canonical name selects the plugin's
configuration:

```toml
[plugins.example_dashboard]
enabled = false
```

Both registries check `enabled` before importing that surface. For a desktop
provider, all remaining values in the table are validated as bounded JSON,
frozen, and passed as `context.config`; the `enabled` key is removed. See the
[terminal plugin guide](../guides/plugins.md) for the existing TUI API.

## Implement the Python provider

The desktop entry point must construct an object matching
`DesktopPluginProvider` from `tongs.plugins.desktop`:

```python
from __future__ import annotations

from tongs.plugins.desktop import (
    DESKTOP_PLUGIN_API_MAJOR,
    DesktopAsset,
    DesktopAssetBundle,
    DesktopAssetKind,
    DesktopCallContext,
    DesktopCompatibility,
    DesktopMethod,
    DesktopModule,
    DesktopPluginContext,
    DesktopPluginManifest,
    FrozenJsonObject,
    JsonValue,
)


class DashboardProvider:
    def __init__(self) -> None:
        self._context: DesktopPluginContext | None = None

    def manifest(self) -> DesktopPluginManifest:
        return DesktopPluginManifest(
            plugin_id="dashboard",
            title="Dashboard",
            version="1.0.0",
            compatibility=DesktopCompatibility(
                api_major=DESKTOP_PLUGIN_API_MAJOR,
            ),
            modules=(
                DesktopModule(
                    id="dashboard",
                    title="Dashboard",
                    bundle_id="ui",
                    entry_asset_id="module",
                ),
            ),
            asset_bundles=(
                DesktopAssetBundle(
                    id="ui",
                    package="tongs_dashboard",
                    root="assets",
                    assets=(
                        DesktopAsset(
                            id="module",
                            path="dashboard.mjs",
                            kind=DesktopAssetKind.MODULE,
                        ),
                    ),
                ),
            ),
            methods=(DesktopMethod(id="refresh"),),
        )

    async def start(self, context: DesktopPluginContext) -> None:
        self._context = context

    async def call(
        self,
        method: str,
        params: FrozenJsonObject,
        call_context: DesktopCallContext,
    ) -> JsonValue:
        if method != "refresh":
            raise ValueError("Unknown method")
        if call_context.cancellation.cancelled:
            return {"cancelled": True}
        return {"status": "ready"}

    async def stop(self) -> None:
        self._context = None
```

The four required operations are:

| Operation | Contract |
| --- | --- |
| `manifest()` | Returns one frozen `DesktopPluginManifest`. Discovery validates it before the provider starts. |
| `start(context)` | Receives the plugin-scoped `DesktopPluginContext` once after successful discovery. |
| `call(method, params, call_context)` | Handles a manifest-declared local method and returns JSON data. `params` is recursively frozen. |
| `stop()` | Releases provider resources. It runs during sidecar shutdown, including after a start or call failure when the provider was constructed. |

Do not import the desktop provider from a terminal-only module. Keeping the
entry-point modules independent preserves terminal startup when optional desktop
dependencies or resources are unavailable.

## Declare the manifest

All declaration dataclasses are frozen. IDs are plugin-local except for
`plugin_id`, which must exactly match the `tongs.desktop_plugins` entry-point
name. Plugin and local IDs are at most 80 characters, start with a lowercase
letter, and then contain lowercase letters, digits, or `.`, `_`, and `-`
separators followed by a lowercase letter or digit. Titles and help text are at
most 500 characters. Versions use PEP 440 syntax and are at most 100 characters.

| Field | Purpose and validation |
| --- | --- |
| `plugin_id`, `title`, `version` | Stable provider identity and display metadata. |
| `compatibility` | Manifest validation requires a positive integer `api_major`; discovery then rejects a manifest whose `api_major` does not exactly match the host's supported value. Optional `minimum_host_version` is a PEP 440 lower bound for Tongs. |
| `modules` | At least one UI module. Each names one bundle, one module entry asset, and optional stylesheet assets from that same bundle. |
| `asset_bundles` | Package resource roots and their declared assets and size limits. |
| `navigation` | Navigation IDs and titles bound to declared module IDs. |
| `commands` | Command IDs, titles, help text, and a declared navigation target. Commands are data declarations, not renderer code. |
| `methods` | Local method IDs that the host may pass to `call()`. Calls to undeclared methods are rejected before provider code runs. |
| `events` | Event IDs the provider may publish. Other event IDs are rejected. |
| `focus_targets` | Focus IDs and titles bound to declared modules. Other targets are rejected. |
| `help_asset_id` | Optional asset ID whose kind is `help`. |
| `reads` | Host read operations the provider may request through its context. Duplicate or undeclared reads are rejected. |

The available read declarations are:

| `DesktopReadKind` | Value |
| --- | --- |
| `REPOSITORIES` | `repositories` |
| `REVIEWS` | `reviews` |
| `REVIEW` | `review` |
| `DISCUSSIONS` | `discussions` |
| `COMMITS` | `commits` |
| `PIPELINES` | `pipelines` |
| `JOBS` | `jobs` |
| `LOG` | `log` |

These are read-only application service calls. The provider SDK does not expose
`ForgeRegistry`, credentials, raw HTTP clients, the cache, or the Textual app.
Forge writes continue through the application's normal command and confirmation
flows.

## Package modules, styles, and help

Declare resources by importable Python package, resource root, and normalized
relative path. Tongs resolves them with `importlib.resources`, validates them at
discovery, and later exposes only plugin-scoped opaque asset handles. It never
accepts an arbitrary package or filesystem path from the renderer.

Package names use dotted Python identifiers and are at most 200 characters. A
bundle root can be `.` or a normalized relative path. Asset paths are normalized
relative paths and cannot contain empty, `.`, or `..` components, backslashes,
NUL bytes, or an absolute prefix.

Supported resource kinds and extensions are:

| Kind | Extensions | Media type |
| --- | --- | --- |
| `module` | `.js`, `.mjs` | `text/javascript; charset=utf-8` |
| `stylesheet` | `.css` | `text/css; charset=utf-8` |
| `help` | `.md` | `text/markdown; charset=utf-8` |

The default bundle limits are 1 MiB per file and 8 MiB total. A provider may
declare lower limits or raise them up to the host caps of 8 MiB per file and
32 MiB total. The total limit must be at least the per-file limit. Tongs rejects
missing resources, symlinks in filesystem-backed resource paths, resources that
escape the package root, duplicate normalized paths, unsupported extensions,
and files that exceed the effective limits. Asset IDs are unique across the
whole manifest.

Ship compiled ESM, CSS, and help in the Python wheel or source distribution.
End users do not need Node.js or a frontend build. For setuptools, the reference
package uses:

```toml
[tool.setuptools.package-data]
tongs_example_dashboard = ["assets/*.mjs", "assets/*.css", "assets/*.md"]
```

The module entry asset exports `mount(container, api)` and returns a cleanup
function. Cleanup must remove DOM listeners, unsubscribe from plugin events,
unbind focus targets, and stop touching detached DOM. Treat `api.signal` abort
as the end of owned pending work. The reference `dashboard.mjs` demonstrates
mount, invocation, event subscription, notification, navigation, focus binding,
abort handling, and idempotent cleanup.

## Use the scoped context

`DesktopPluginContext` contains immutable provider data and wrapper methods:

| Member | Behavior |
| --- | --- |
| `plugin_id` | The validated canonical provider ID. |
| `config` | The plugin's recursively frozen JSON configuration without `enabled`. |
| `host` | The plugin-bound `DesktopHostFacade`. Prefer the context wrappers, which enforce the manifest declarations before delegating to this same narrow facade. |
| `cancellation` | Provider-lifetime cancellation. It is signalled when startup times out or shutdown begins. |
| `read_kinds`, `method_ids`, `event_ids`, `focus_target_ids` | Frozen allowlists derived from the validated manifest by the host. |
| `current_location()` | Returns a host-validated, frozen JSON location snapshot or `None`. |
| `read(kind, params, cancellation)` | Runs a declared read with separately cancellable, frozen parameters. |
| `notify(message, severity)` | Publishes a notification with `information`, `warning`, or `error` severity. |
| `publish_event(event_id, payload)` | Publishes a declared plugin event with a frozen JSON payload. |
| `focus(target_id, metadata)` | Requests a declared focus target with frozen JSON metadata. |
| `invoke(method, params, call_context)` | Routes a declared plugin method through the bound host. Providers normally implement host invocations in `call()`. |

Queued events with the same plugin and event ID are replaceable refresh hints,
so consumers may observe the latest payload rather than every intermediate
payload. Focus requests are likewise replaceable per plugin. Notifications are
queued as individual events. Consumers should refetch durable data after a
refresh event instead of treating event delivery as a transaction log.

Context data, call parameters, return values, event payloads, and focus metadata
must be JSON values. Tongs freezes objects into read-only mappings and arrays
into tuples. Each value is limited to 4,096 total items, nesting depth 16, and
256 KiB UTF-8 per string or object key. Object keys must be strings and floating
point values must be finite.

`DesktopCallContext` carries a host-generated `invocation_id`, a per-call
`cancellation` signal, and the current `DesktopLocation` if one exists. Observe
the signal during long operations and pass cancellation to any declared reads.
Cancelling a task or reaching a timeout signals the corresponding cancellation
object and cancels the provider task. Providers are trusted and expected to
cooperate, but the host stops waiting after its deadline.

## Lifecycle, states, and failures

Discovery imports and constructs enabled desktop entry points, validates the
manifest, checks API and minimum host compatibility, and resolves all declared
resources. One provider's failure does not stop discovery, startup, calls, or
cleanup for other providers.

The observable states are `discovered`, `started`, `disabled`, `terminal_only`,
`incompatible`, `failed`, and `stopped`. Duplicate IDs, entry-point and manifest
ID mismatch, invalid manifests or resources, incompatible API or host versions,
and import, construction, start, call, stop, or cleanup failures become scoped
plugin diagnostics. Unavailable providers and undeclared methods, reads, events,
assets, navigation, commands, or focus targets also fail within that provider's
boundary.

The serialized error codes are `duplicate_id`, `entry_point_mismatch`,
`invalid_manifest`, `incompatible_api`, `incompatible_host`, `import_failed`,
`construction_failed`, `start_failed`, `call_failed`, `call_timeout`,
`stop_failed`, `cleanup_timeout`, `unavailable`, `undeclared_method`,
`undeclared_read`, `undeclared_target`, and `invalid_data`. Messages are safe,
scoped diagnostics rather than raw third-party exceptions.

Default host deadlines are 10 seconds for `start()`, 30 seconds for `call()`,
and 2 seconds for shutdown cleanup, including `stop()`. Calls require a unique
active invocation ID. A timed-out call returns a retryable `call_timeout`
diagnostic, signals cancellation, and retains its invocation ID until provider
code actually exits. During shutdown, Tongs signals lifecycle and call
cancellation, cancels pending work, awaits bounded cleanup, and rejects new
calls. An exception, invalid JSON return, or timeout produces a typed call error
while the provider remains available for later calls. Provider code that
suppresses task cancellation is detached after the call deadline and remains
tracked for cleanup. Start, stop, or cleanup failure marks the provider
`failed`. The core desktop application and other plugins continue operating or
shutting down.
