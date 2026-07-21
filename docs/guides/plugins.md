# Plugins

tongs has a plugin system that lets you extend the TUI with new commands,
screens, and lifecycle hooks. Plugins are discovered automatically through
Python entry points, so they can be distributed as standalone packages.

## What plugins can do

A plugin can contribute any combination of:

- **Commands** -- entries that appear in the command palette (++ctrl+p++)
- **Screens** -- named Textual `Screen` subclasses the app can push
- **Lifecycle hooks** -- async callbacks that run when the app starts up
  (`on_app_ready`) or shuts down (`on_app_shutdown`)

Plugins receive the full `TongsApp` instance at lifecycle time, giving them
access to forge clients, configuration, and the cache layer.

## Writing a plugin

Every plugin subclasses `TongsPlugin` and implements the `name` property at
minimum.

### The TongsPlugin ABC

```python
from tongs.plugins.base import TongsPlugin


class TongsPlugin:
    """Base class -- the methods you can override."""

    @property
    def name(self) -> str:
        """Unique plugin identifier (required)."""
        ...

    @property
    def version(self) -> str:
        """Semver string. Defaults to '0.0.0'."""
        ...

    async def on_app_ready(self, app) -> None:
        """Called after the TUI app is mounted."""

    async def on_app_shutdown(self, app) -> None:
        """Called before app exit."""

    def get_commands(self) -> list[tuple[str, str, object]]:
        """Return (display, help_text, callback) tuples."""
        return []

    def get_screens(self) -> dict[str, type]:
        """Return screen_name -> Screen class mappings."""
        return {}
```

### Minimal example

A plugin that adds a single command to the palette:

```python
from tongs.plugins.base import TongsPlugin


class HelloPlugin(TongsPlugin):
    @property
    def name(self) -> str:
        return "hello"

    @property
    def version(self) -> str:
        return "0.1.0"

    def get_commands(self) -> list[tuple[str, str, object]]:
        return [
            ("Say Hello", "Print a greeting notification", self._greet),
        ]

    def _greet(self) -> None:
        # 'app' is available through Textual's global app reference
        from textual.app import get_app
        get_app().notify("Hello from the plugin!")
```

## Registering via entry points

tongs discovers plugins through the `tongs.plugins` entry point group. Register
your plugin class in your package's `pyproject.toml`:

```toml
[project.entry-points."tongs.plugins"]
hello = "my_tongs_hello.plugin:HelloPlugin"
```

The key (`hello`) is the plugin name used for configuration lookup. The value
is the dotted import path to the class.

After installing the package (`pip install .` or `pip install -e .` for
development), tongs picks it up on the next launch -- no extra import or
registration step needed.

!!! tip
    During development, install in editable mode so changes take effect
    without reinstalling:

    ```bash
    pip install -e .
    ```

## Configuration

Plugins are enabled by default. To disable a plugin or pass it plugin-specific
settings, add a `[plugins.<name>]` section to your `config.toml`:

```toml
# Disable the MCP server plugin
[plugins.mcp]
enabled = false
```

```toml
# Enable a plugin and pass it custom settings
[plugins.fleet]
enabled = true
monitor_interval = 30
```

The `enabled` key is handled by the plugin registry itself. All other keys in
the table are forwarded to the plugin as a dictionary, so each plugin can define
its own configuration schema.

If a `[plugins.<name>]` section is absent entirely, the plugin is loaded with
default settings.

## What the app object provides

Both `on_app_ready` and `on_app_shutdown` receive the `TongsApp` instance as
their `app` argument. The most useful attributes for plugin authors:

| Attribute | Type | Description |
|-----------|------|-------------|
| `app.forge_registry` | `ForgeRegistry` | Manages HTTP clients for GitHub and GitLab. Use it to make authenticated API calls against any configured forge. |
| `app.cache` | `CacheStore` | Key-value cache with TTL support. Plugins can read and write cached data to avoid redundant API calls. |
| `app.config` | `Config` | The resolved configuration object. Access `app.config.plugin_config` to read your plugin's settings. |
| `app.repos` | `list[Repo]` | All discovered git repositories under the scan root. May be empty during `on_app_ready` because discovery runs in a background thread. |
| `app.plugin_registry` | `PluginRegistry` | The registry itself, if you need to inspect other loaded plugins. |

### Accessing your plugin config

```python
async def on_app_ready(self, app) -> None:
    my_conf = app.config.plugin_config.get(self.name, {})
    interval = my_conf.get("monitor_interval", 60)
    # use interval...
```

## Built-in plugins

tongs ships with one built-in plugin registered via entry points.

### MCP server (`mcp`)

The MCP plugin adds a **Start MCP Server** command to the palette. When
triggered, it launches the `tongs-mcp` stdio server in a subprocess, allowing AI
agents (Claude Code, Cursor, etc.) to query MR data programmatically.

Entry point registration in tongs' own `pyproject.toml`:

```toml
[project.entry-points."tongs.plugins"]
mcp = "tongs.mcp.plugin:MCPPlugin"
```

Disable it with:

```toml
[plugins.mcp]
enabled = false
```

## Complete example: a "Quick Stats" plugin

This walkthrough builds a plugin from scratch that adds a command showing a
notification with the count of discovered repos and configured forge hosts.

### 1. Create the package

```
tongs-stats-plugin/
    pyproject.toml
    src/
        tongs_stats/
            __init__.py
            plugin.py
```

### 2. Write the plugin

```python title="src/tongs_stats/plugin.py"
"""Quick Stats plugin for tongs."""

from __future__ import annotations

from tongs.plugins.base import TongsPlugin


class StatsPlugin(TongsPlugin):
    """Shows a quick summary of discovered repos."""

    @property
    def name(self) -> str:
        return "stats"

    @property
    def version(self) -> str:
        return "1.0.0"

    async def on_app_ready(self, app) -> None:
        """Stash the app reference for later use."""
        self._app = app

    def get_commands(self) -> list[tuple[str, str, object]]:
        return [
            ("Quick Stats", "Show repo and host counts", self._show_stats),
        ]

    def _show_stats(self) -> None:
        repo_count = len(self._app.repos)
        host_count = len(self._app.get_repo_hostnames())
        self._app.notify(
            f"Tracking {repo_count} repos across {host_count} hosts"
        )
```

### 3. Configure pyproject.toml

```toml title="pyproject.toml"
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "tongs-stats-plugin"
version = "1.0.0"
requires-python = ">=3.11"
dependencies = ["tongs"]

[project.entry-points."tongs.plugins"]
stats = "tongs_stats.plugin:StatsPlugin"

[tool.hatch.build.targets.wheel]
packages = ["src/tongs_stats"]
```

### 4. Install and test

```bash
cd tongs-stats-plugin
pip install -e .
tongs
```

Open the command palette with ++ctrl+p++ and search for **Quick Stats**. The
notification should display the count of repos and forge hosts.

### 5. Optional: add a screen

To go further, you can register a full Textual screen:

```python
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static


class StatsScreen(Screen):
    def compose(self) -> ComposeResult:
        yield Static("Stats screen placeholder")


class StatsPlugin(TongsPlugin):
    # ...existing code...

    def get_screens(self) -> dict[str, type]:
        return {"stats": StatsScreen}
```

The app can then push this screen by name with `app.push_screen("stats")`.

!!! note
    Plugin errors are logged but never crash the app. If your plugin raises an
    exception in a lifecycle hook or in `get_commands`, tongs logs a warning and
    continues normally.
