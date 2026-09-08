# Plugins

Tongs has two independent Python entry-point surfaces:

- `tongs.plugins` loads terminal `TongsPlugin` implementations.
- `tongs.desktop_plugins` loads production desktop
  `DesktopPluginProvider` implementations.

A dual-surface distribution uses the same canonical entry-point name in both
groups, but each registry imports only its own group. Desktop discovery lists
terminal-only names from entry-point metadata without importing their modules.
Terminal discovery never imports desktop providers. Both registries check
`[plugins.<name>].enabled` before importing the corresponding entry point.

Installed terminal, desktop Python, ESM, and CSS plugins are trusted code. The
contexts, declaration allowlists, resource validation, plugin IDs, and UI
scoping are supported isolation boundaries for accidental access. They do not
sandbox a hostile installed extension.

## Plugin System Architecture

The plugin system enables extending tongs with new commands, screens, and lifecycle hooks. It uses Python entry points for discovery and TOML config for filtering.

## TongsPlugin ABC

Defined in `src/tongs/plugins/base.py`. All plugins must subclass this:

```python
class TongsPlugin(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...        # Unique plugin identifier

    @property
    def version(self) -> str:         # Defaults to "0.0.0"
        return "0.0.0"

    async def on_app_ready(self, ctx: PluginContext) -> None: ...    # After TUI mount
    async def on_app_shutdown(self, ctx: PluginContext) -> None: ... # Before app exit

    def get_commands(self) -> list[tuple[str, str, object]]:  # (display, help, callback)
        return []

    def get_screens(self) -> dict[str, type]:  # screen_name -> Screen class
        return {}
```

Only `name` is abstract. All other methods have safe defaults (empty lists/dicts, no-op hooks).

## PluginRegistry

Defined in `src/tongs/plugins/registry.py`. Manages plugin discovery and lifecycle.

**Discovery:** `discover(plugin_config)` reads `importlib.metadata.entry_points(group="tongs.plugins")`. For each entry point:
1. Checks `plugin_config[ep.name].get("enabled", True)`. Disabled plugins are skipped (logged at INFO).
2. Calls `ep.load()` to import the class, then instantiates it.
3. Validates `isinstance(plugin, TongsPlugin)`. Non-conforming plugins are skipped (logged at WARNING).
4. On any exception during load/instantiate, logs the error with traceback and continues.

**Lifecycle hooks:** `on_app_ready(app)` and `on_app_shutdown(app)` create the
supported `PluginContext(app)` interface and pass it to each plugin's hook. Each
call is wrapped in its own `try/except` so a failing plugin does not affect
others.

**Command/screen aggregation:** `get_all_commands()` and `get_all_screens()` collect results from all plugins. Each plugin's call is wrapped in `try/except`. Commands are merged into the command palette; screens are collected as a dict.

## Entry Point Registration

Plugins register via the `tongs.plugins` entry point group in `pyproject.toml`:

```toml
[project.entry-points."tongs.plugins"]
mcp = "tongs.mcp.plugin:MCPPlugin"
```

This makes the plugin discoverable to any tongs installation that has the package installed.

## Config Filtering

Users control plugins via `[plugins.*]` sections in `config.toml`:

```toml
[plugins.mcp]
enabled = false
```

The `plugin_config` dict is loaded in `config.py:load_config()` from the `[plugins]` TOML section and passed to `PluginRegistry.discover()`.

## MCPPlugin (First-Party Reference)

Defined in `src/tongs/mcp/plugin.py`. Demonstrates the plugin pattern:

- `name = "mcp"`, `version = "0.2.0"`
- `get_commands()` returns one command: "Start MCP Server" which launches `tongs-mcp` as a subprocess
- No lifecycle hooks, no screens
- Registered as the `mcp` entry point in `pyproject.toml`

## MCP Server

Defined in `src/tongs/mcp/server.py`. Runs as a separate process (not inside the TUI).

**Entry point:** `tongs-mcp` (console script in `pyproject.toml`).

**Server framework:** `mcp.server.fastmcp.FastMCP("tongs")`.

**Tools (6):**

| Tool | Method | Description |
|---|---|---|
| `list_mrs` | GET | List MRs for a repo (filtered by state) |
| `get_mr` | GET | Get detailed MR information |
| `get_mr_diff` | GET | Get unified diff text |
| `post_comment` | POST | Post a general comment |
| `approve_mr` | POST | Approve an MR |
| `list_pipelines` | GET | List pipelines for an MR |

**Input format:** All tools take `repo_path` as `hostname/owner/repo` (e.g., `github.com/acme/app`). Validated by `_parse_host_repo()` with regex.

**Security:** Destructive actions (merge, close, reopen, cancel) are excluded. No raw API exposure. No auth in tool I/O.

**Registry:** `_get_registry()` lazily creates a `ForgeRegistry` from `load_config()` on first tool call.

## Writing a New Plugin

1. Create a Python package with a class extending `TongsPlugin`.
2. Implement `name` (required) and any optional methods.
3. Register as an entry point in your package's `pyproject.toml`:
   ```toml
   [project.entry-points."tongs.plugins"]
   your_plugin = "your_package.module:YourPlugin"
   ```
4. Install the package in the same environment as tongs.
5. The plugin will be discovered on next app launch.

## PluginContext supported interface

Defined in `src/tongs/plugins/context.py`. Terminal plugins receive a
`PluginContext` instead of the raw `TongsApp` instance as their supported
interface. This narrows normal access, but is not a security boundary against
trusted in-process Python code.

**Exposed properties (read-only):**
- `forge_registry` -- access to authenticated forge clients
- `cache` -- access to the SQLite cache store
- `config` -- application configuration
- `repos` -- list of discovered repositories

**Exposed methods:**
- `notify(message, severity)` -- send a user-visible notification
- `push_screen(screen)` -- push a screen onto the TUI stack
- `pop_screen()` -- pop the current screen
- `plugin_config(plugin_name)` -- return the config section for a specific plugin (returns a copy, not a reference)

In lifecycle hooks, access forge data via `ctx.forge_registry`, cache via `ctx.cache`, and repos via `ctx.repos`. Do not access raw HTTP clients or tokens.

## Production desktop provider SDK

The public provider declarations are in `src/tongs/plugins/desktop.py`.
`src/tongs/plugins/desktop_registry.py` owns independent discovery, compatibility,
and bounded lifecycle management. `src/tongs/plugins/desktop_resources.py`
validates packaged resources. The user-facing contract is documented in
`docs/plugins/provider.md`, and `examples/desktop-plugin` is the installable
reference distribution.

### Provider and manifest

The desktop entry point constructs an object satisfying
`DesktopPluginProvider`:

```python
class DesktopPluginProvider(Protocol):
    def manifest(self) -> DesktopPluginManifest: ...
    async def start(self, context: DesktopPluginContext) -> None: ...
    async def call(
        self,
        method: str,
        params: FrozenJsonObject,
        context: DesktopCallContext,
    ) -> JsonValue: ...
    async def stop(self) -> None: ...
```

`DESKTOP_PLUGIN_API_MAJOR` is `1`. `DesktopCompatibility` requires an exact API
major and can declare a PEP 440 `minimum_host_version`. The manifest ID must
match the entry-point name. Manifests contain frozen declarations for modules,
asset bundles, navigation, commands, methods, events, focus targets, help, and
reads. IDs and all internal references are validated before startup.

Supported reads are `repositories`, `reviews`, `review`, `discussions`,
`commits`, `pipelines`, `jobs`, and `log`. Providers receive immutable config
without the registry-owned `enabled` key. `DesktopPluginContext` exposes scoped
read, invoke, notification, event publication, current-location, and focus
operations. It does not expose `ForgeRegistry`, credentials, raw clients, cache,
or Textual state.

### Resources

A `DesktopAssetBundle` names an importable Python package, a normalized
resource root, and declared files. Modules accept `.js` and `.mjs`, stylesheets
accept `.css`, and help accepts `.md`. Defaults are 1 MiB per file and 8 MiB per
bundle. Absolute host caps are 8 MiB per file and 32 MiB per bundle.

Resource validation rejects missing files, path traversal, filesystem symlinks,
duplicate Unicode-normalized case-folded paths, extension/type mismatches, and
effective size-limit violations. Validated resources later become opaque,
session-local handles. Package and filesystem paths are not renderer inputs.

### Discovery, lifecycle, and errors

`DesktopPluginRegistry.discover()` is single-use. It produces stable-ID-ordered
immutable records with `discovered`, `disabled`, `terminal_only`, `incompatible`,
or `failed` state. `start_all()` moves successful providers to `started`.
`stop_all()` processes providers in reverse ID order and moves successful
providers to `stopped`; a retained earlier failure is not erased.

Default deadlines are 10 seconds for start, 30 seconds for a call, and 2 seconds
for aggregate provider cleanup. Shutdown signals lifecycle and per-call
cancellation, cancels pending tasks, and bounds cleanup even when trusted plugin
code suppresses cancellation. A timed-out invocation ID remains reserved until
its provider task actually exits. Failures are typed `DesktopPluginError`
records and remain isolated to the provider.

JSON crossing the provider boundary is frozen and validated. Limits are depth
16, 4,096 items, and 256 KiB UTF-8 for each string or object key. Keys must be
strings and floats must be finite.

### Verification map

- `tests/plugins/test_desktop_contract.py`: immutable manifest and JSON bounds
- `tests/plugins/test_desktop_discovery.py`: independent groups, disabled-before-import, compatibility, and duplicate IDs
- `tests/plugins/test_desktop_lifecycle.py`: scoped operations, cancellation, timeouts, and failure isolation
- `tests/plugins/test_desktop_resources.py`: containment, symlinks, normalized paths, and size limits
- `examples/desktop-plugin/tests/`: reference provider and compiled module behavior

## Testing

Tests in `tests/test_plugins/test_plugin_system.py` cover:
- TongsPlugin ABC enforcement
- PluginRegistry discovery with mocked entry points
- Config-based plugin disabling
- Graceful failure on bad plugins
- Lifecycle hook invocation

Tests in `tests/test_mcp/test_server.py` cover:
- MCP tool functions with mocked ForgeRegistry
- Input validation (repo_path format)
- Tool output structure
