# Configuration

tongs reads its configuration from `~/.config/tongs/config.toml` on Linux, or
the platform-appropriate config directory via
[platformdirs](https://pypi.org/project/platformdirs/). All settings are
optional and have sensible defaults.

## Example config

```toml
[general]
scan_root = "~/git"
scan_depth = 5

[editor]
command = "nvim"
external_editor_enabled = true

[ui]
theme = "monokai"
diff_style = "unified"
show_draft_mrs = true
ascii_mode = false

[cache]
mr_list_ttl = 60
diff_ttl = 300
max_size_mb = 100

[concurrency]
max_parallel = 8
request_timeout = 30

[hosts.work-gitlab]
hostname = "gitlab.example.com"
forge_type = "gitlab"
```

---

## `[general]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `scan_root` | string | `"~/git"` | Root directory to scan for git repositories. Supports `~` expansion. |
| `scan_depth` | integer | `5` | Maximum directory depth to walk when searching for repos. |

## `[editor]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `command` | string | `""` (uses `$EDITOR`) | Editor command for external editing. Falls back to `$EDITOR` / `$VISUAL` if empty. |
| `external_editor_enabled` | boolean | `true` | Whether ++f2++ opens an external editor from the comment editor. |

## `[ui]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `theme` | string | `"monokai"` | Pygments theme for syntax highlighting. Any valid Pygments style name works. |
| `diff_style` | string | `"unified"` | Diff display style. |
| `show_draft_mrs` | boolean | `true` | Whether draft/WIP merge requests appear in the inbox. |
| `ascii_mode` | boolean | `false` | Use ASCII-only characters for borders and indicators. Enable this for minimal terminals that do not render Unicode box-drawing characters. |

## `[cache]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `mr_list_ttl` | integer | `60` | Time-to-live in seconds for cached MR list data. |
| `diff_ttl` | integer | `300` | Time-to-live in seconds for cached diff data. |
| `max_size_mb` | integer | `100` | Maximum cache size in megabytes. |

## `[concurrency]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `max_parallel` | integer | `8` | Maximum number of parallel API requests when fetching MR data. |
| `request_timeout` | integer | `30` | HTTP request timeout in seconds. |

## `[hosts.*]`

Define self-hosted forge instances. Each entry adds a hostname that tongs
recognizes when scanning git remotes.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `hostname` | string | (required) | The hostname of the self-hosted instance (e.g., `gitlab.example.com`). |
| `forge_type` | string | `""` | Either `"gitlab"` or `"github"`. |

You can define multiple hosts:

```toml
[hosts.work-gitlab]
hostname = "gitlab.example.com"
forge_type = "gitlab"

[hosts.work-github]
hostname = "github.corp.com"
forge_type = "github"
```

!!! important
    You must authenticate the `glab` or `gh` CLI against each self-hosted
    hostname before tongs can access it:

    ```bash
    glab auth login --hostname gitlab.example.com
    gh auth login --hostname github.corp.com
    ```

## `[plugins.*]`

Plugin-specific configuration. Each plugin uses its own sub-table with
plugin-defined keys.

```toml
[plugins.fleet]
monitor_interval = 30
```

Plugin configuration is passed through to the plugin as a dictionary. See each
plugin's documentation for available keys.

---

## Config file location

tongs uses [platformdirs](https://pypi.org/project/platformdirs/) to determine
the config directory:

| Platform | Path |
|----------|------|
| Linux | `~/.config/tongs/config.toml` |
| macOS | `~/Library/Application Support/tongs/config.toml` |
| Windows | `%APPDATA%\tongs\config.toml` |
