# Plugins

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

    async def on_app_ready(self, app) -> None: ...    # After TUI mount
    async def on_app_shutdown(self, app) -> None: ... # Before app exit

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

**Lifecycle hooks:** `on_app_ready(app)` and `on_app_shutdown(app)` iterate all loaded plugins and call each hook. Each call is wrapped in its own `try/except` so a failing plugin does not affect others.

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

In lifecycle hooks, access forge data via `app.forge_registry`, cache via `app.cache`, and repos via `app.repos`. Do not access raw HTTP clients or tokens.

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
