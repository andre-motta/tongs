---
title: Configuration
description: "Every tongs configuration key, its type, its default and what reads it."
lead: tongs reads one TOML file. Every key is optional, and a missing file means every default applies.
---

## Where the file lives

tongs finds its configuration directory with
[platformdirs](https://pypi.org/project/platformdirs/).

| Platform | Path |
|----------|------|
| Linux | `~/.config/tongs/config.toml`, or `$XDG_CONFIG_HOME/tongs/config.toml` |
| macOS | `~/Library/Application Support/tongs/config.toml` |
| Windows | `%LOCALAPPDATA%\tongs\tongs\config.toml` |

The terminal app, the desktop app and the MCP server all read the same file.

## Example

```toml title="~/.config/tongs/config.toml"
[general]
scan_root = "~/git"
scan_depth = 5

[editor]
command = "code --wait"
external_editor_enabled = true

[ui]
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

## `[general]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `scan_root` | string | `"~/git"` | Directory to scan for git repositories. `~` is expanded. |
| `scan_depth` | integer | `5` | How many directory levels to walk below `scan_root`. |

`tongs --scan-root DIR` (or `-d DIR`) overrides `scan_root` for one run.

## `[editor]`

:::note[Desktop only]
The `[editor]` keys apply to the desktop app, which is a beta. The terminal
app does not read them.
:::

In the terminal, ++f2++ opens `$VISUAL`, then `$EDITOR`, then the first of
`nvim`, `vim`, `vi` or `nano` found on your `PATH`. The pipeline log viewer
also accepts `less`. Setting `command` does not change what ++f2++ opens in
the terminal.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `command` | string | `""` | The editor command the desktop app starts for external editing. When empty, it uses `$VISUAL`, then `$EDITOR`. |
| `external_editor_enabled` | boolean | `true` | Whether the desktop app offers external editing. When `false`, it reports external editor access as disabled. |

The desktop app appends the path of a private log export to the command and
starts it without a shell. Use an editor that opens a window and waits, such as
`code --wait` or `kate --block`. Known terminal-only editors, such as `vim`,
`nano` or `less`, are reported as unsupported, because they cannot attach to
the desktop window. A wrapper script must itself start a graphical editor and
wait for it. The desktop app reports that the editor started separately from
confirmation that it read the file.

While the editor runs, the desktop app keeps the export open so that cleanup
removes only that exact file. After 23 hours it lets go and leaves the export in
place. A later export can reclaim it once 24 hours have passed. This timing
assumes the app resumes normally after the computer sleeps. Closing the
desktop app never closes the editor.

## `[ui]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `ascii_mode` | boolean | `false` | Show the inbox's CI status as ASCII labels, such as `OK` and `FAIL`, instead of Unicode symbols. Turn it on for terminals that do not render those symbols. |

### Keys that are read but have no effect

`theme`, `diff_style` and `show_draft_mrs` are still accepted, so a file that
sets them keeps loading without an error. Nothing uses their values.

| Key | Type | Default | What happens today |
|-----|------|---------|--------------------|
| `theme` | string | `"monokai"` | Nothing. Diff syntax highlighting always uses the `monokai` style. |
| `diff_style` | string | `"unified"` | Nothing. Press ++v++ in the diff viewer to switch between unified and split. The choice is not saved. |
| `show_draft_mrs` | boolean | `true` | Nothing. Draft MRs are always listed. The inbox marks them `D` and MR detail shows `DRAFT`. |

## `[cache]`

tongs keeps forge responses in a local SQLite database, so that returning to a
screen does not wait on the network.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `mr_list_ttl` | integer | `60` | How long, in seconds, a cached list of MRs stays fresh. |
| `diff_ttl` | integer | `300` | How long, in seconds, a cached diff stays fresh. |
| `max_size_mb` | integer | `100` | The size limit of the cache database, in megabytes. |

- **What is cached.** MR lists and diffs. Job logs are never cached. Reads that
  check a review's current revision always go to the forge.
- **When it refreshes.** An entry older than its time-to-live is fetched again.
  Approving, merging, commenting or submitting a review clears the affected
  entries, so the next read is fresh.
- **When it is full.** When the stored entries pass `max_size_mb`, tongs
  removes the least recently used quarter.
- **Where it lives.** `cache.db` in the platform cache directory:
  `~/.cache/tongs/` on Linux, `~/Library/Caches/tongs/` on macOS and
  `%LOCALAPPDATA%\tongs\tongs\Cache\` on Windows. The directory is private to
  your user, and the file is created with mode `0600`.
- **Clearing it.** Run **Clear Cache** from the command palette (++ctrl+p++),
  or use **Clear cache** in the desktop app. Clearing removes cached responses
  only. It never touches your review drafts.

## `[concurrency]`

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `max_parallel` | integer | `8` | The most forge requests tongs runs at once. It must be greater than zero. |
| `request_timeout` | integer | `30` | The HTTP request timeout, in seconds. |

## `[hosts.<name>]`

`github.com` and `gitlab.com` work without configuration. Add a table for each
self-hosted instance, so tongs recognizes its remotes when it scans your
repositories. The table name is your own label.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `hostname` | string | (required) | The instance's hostname, for example `gitlab.example.com`. |
| `forge_type` | string | `""` | `"gitlab"` or `"github"`. A host with any other value is ignored. |

```toml
[hosts.work-gitlab]
hostname = "gitlab.example.com"
forge_type = "gitlab"

[hosts.work-github]
hostname = "github.corp.com"
forge_type = "github"
```

:::caution[Sign in to each host]
tongs uses the credentials of the forge CLI. Sign in `glab` or `gh` against
each self-hosted hostname:

```bash
glab auth login --hostname gitlab.example.com
gh auth login --hostname github.corp.com
```

A `~/.netrc` entry whose `machine` names the host, or a token in the system
keyring, also works. For the
keyring, install tongs with the `keyring` extra (see
[Install and sign in](/getting-started/#optional-extras)), then store the token
under the service name `tongs`:

```bash
# prompts for the token
pipx run keyring set tongs gitlab.example.com
```

See [Credentials](/reference/security/#credentials) for the order tongs
tries them in.
:::

## `[plugins.<name>]`

Each plugin reads its own table. The keys are defined by the plugin.

```toml
[plugins.fleet]
monitor_interval = 30
```

tongs passes the table to the plugin as a dictionary. One key is reserved:
`enabled = false` stops tongs from loading that plugin at all. See
[Terminal plugins](/extend/terminal-plugins/) and
[Desktop plugin providers](/extend/desktop-providers/).
