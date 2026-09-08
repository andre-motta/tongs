<p align="center">
  <img src="docs/assets/hero-banner.png" alt="tongs - Unified code review for the terminal" width="100%">
</p>

<p align="center">
  <strong>One TUI. Every forge. Full review.</strong><br>
  A terminal-native code review inbox for developers who work across GitHub and GitLab.
</p>

<p align="center">
  <a href="https://pypi.org/project/tongs/"><img src="https://img.shields.io/pypi/v/tongs" alt="PyPI"></a>
  <a href="https://pypi.org/project/tongs/"><img src="https://img.shields.io/pypi/pyversions/tongs" alt="Python"></a>
  <a href="https://github.com/andre-motta/tongs/blob/main/LICENSE"><img src="https://img.shields.io/github/license/andre-motta/tongs" alt="License"></a>
  <a href="https://github.com/andre-motta/tongs"><img src="https://img.shields.io/github/stars/andre-motta/tongs" alt="Stars"></a>
</p>

<!-- TODO: Replace with a 30-second GIF/recording showing: inbox -> diff -> comment -> approve -->

Your team uses GitHub. Another uses GitLab. You live in the terminal. tongs gives you a single review inbox across forges, with syntax-highlighted diffs and inline comments, right where you already work.

> lazygit is great for git operations. tongs picks up where it stops, at code review.

**[Documentation](https://www.tongs.tools)** | **[PyPI](https://pypi.org/project/tongs/)** | **[Issues](https://github.com/andre-motta/tongs/issues)**

## Install

```bash
# Recommended: install with pipx for isolation
pipx install tongs
# Or run directly in an isolated environment with uvx
uvx tongs

# Or install it persistently as a uv tool
uv tool install tongs

# Or plain pip
pip install tongs
```

Optional extras:

```bash
pip install "tongs[mcp]"      # MCP server for AI assistants
pip install "tongs[keyring]"  # system keyring as a credential source
```

Requires **Python 3.12+**. tongs needs no compiler and no system libraries of its own. It does read your forge credentials from the [`gh`](https://cli.github.com) and [`glab`](https://gitlab.com/gitlab-org/cli) CLIs when they are installed, falling back to `~/.netrc` and then the optional system keyring, so at least one of those credential sources has to be set up before the inbox can load. See [First-time auth setup](#first-time-auth-setup).

### From source

```bash
git clone https://github.com/andre-motta/tongs.git
cd tongs
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]" ruff
```

Use Python 3.12 or newer. If you prefer `uv`, use `uv venv --python 3.12` to create the environment and `uv pip install -e ".[dev]" ruff` after activation.

## Quick start

```bash
# Run tongs -- it scans ~/git by default
tongs

# Override the scan root for a single run
tongs --scan-root ~/projects
```

To change the default permanently, set `scan_root` in `~/.config/tongs/config.toml`:

```toml
[general]
scan_root = "~/projects"
```

tongs discovers repos under your scan root, reads their git remotes, and populates the inbox. Auth tokens come from your existing `gh` and `glab` CLI logins automatically.

### First-time auth setup

```bash
# GitHub
gh auth login

# GitLab (gitlab.com)
glab auth login

# GitLab (self-hosted)
glab auth login --hostname gitlab.example.com
```

tongs never stores your tokens. It delegates to `gh auth token` / `glab auth token` at runtime, falling back to `~/.netrc` and then the system keyring (if the optional `keyring` package is installed).

## Features

### Inbox

- **Multi-forge inbox** -- GitHub PRs and GitLab MRs in one view, auto-detected from your git remotes
- **Three tabs** -- My Reviews, My MRs, All Open with lazy loading and parallel fetching
- **Per-repo scope** -- click any repo in the repo list to filter the inbox to just that project
- **Sortable** -- press `s` to cycle sort order (updated, title, CI status, author)
- **Repo list** -- searchable, filterable by forge type (GH/GL/All), sortable by name/forge/host

### Diff review

- **Split-pane viewer** -- file tree on the left, diff content on the right
- **Syntax highlighting** -- 500+ languages via Pygments, bulk-highlighted per file for performance
- **Word-level diffs** -- changed words highlighted bold+underline within modified lines
- **Context folding** -- long unchanged sections collapse to "... N unchanged lines ..." markers
- **Truncated diff handling** -- files too large for the API appear in the file tree with +/- stats and a "view in browser" prompt
- **Markdown preview** -- toggle rendered preview for `.md` files with `m`
- **File navigation** -- jump between files with `n` / `Shift+N`
- **Unified or split view** -- press `v` to toggle; in split view `h` / `l` move focus between the old and new side

### Commenting

- **Inline comments** -- press `c` on any diff line to open the bottom-docked comment editor
- **Discussion threads** -- existing inline comments appear as gutter markers in the diff; press `d` to expand/collapse threads with full Markdown rendering, replies, and resolution status
- **Discussion tab** -- card-based view of all MR discussions with diff snippets, Rich Markdown threads, reply/resolve actions, and cross-tab jump-to-diff navigation
- **Comment navigation** -- jump between comments with `]` / `[`, wrapping around
- **Reply to threads** -- press `r` to reply to a discussion from the diff or discussion tab
- **Resolve threads** -- press `R` to resolve/unresolve a discussion (GitHub and GitLab; double-press to confirm)
- **Visual line selection** -- select multiple lines with `Shift+J` / `Shift+K` or `Ctrl+Click` for multi-line comments
- **Suggested changes** -- press `F3` to open your `$EDITOR` with the selected code; edit it, and tongs posts a suggestion block (GitHub `suggestion` / GitLab `suggestion:-0+N` syntax)
- **External editor** -- press `F2` inside the comment editor to switch to your preferred editor
- **General comments** -- press `c` from the overview tab to post a top-level MR comment
- **Durable review drafts** -- press `Ctrl+G` to start review mode, collect comments locally, then submit them as one review with a verdict

### Desktop review workspace (unreleased)

The terminal TUI remains the default interface. The desktop workspace is an
unreleased optional interface over the same repositories, reviews, and drafts,
documented in the [desktop workspace guide](docs/desktop/workspace.md). It has
its own installation path, described in
[Desktop application](#desktop-application-unreleased) below.

The desktop sidebar scans the configured local `scan_root` and `scan_depth`.
It supports display-name search, GitHub/GitLab filtering, and Name, Forge, or
Host sorting, while retaining entries whose host is missing. Its inbox uses
the labels **My Reviews**, **My MRs**, **All Open**, **Open**, and **Closed &
merged**. **All Open** is the scope that provides closed and merged reviews.
Refreshing retains the current context and available review data; a partial
repository failure leaves successful results visible. Selecting another
repository resets the inbox query to that repository's default context.

In the desktop Discussions panel, **Quick comment** and **Quick inline
comment** act immediately. **Start review** or **Resume review** opens a
durable draft. Draft edits are revision-bound, and a stale-version conflict
keeps unsaved text for deliberate resolution. Submissions report partial or
unknown outcomes and require explicit reconciliation when the remote result
cannot be established. Uncertain writes are not replayed automatically.

Desktop suggestions require a complete current diff selection containing
contiguous new-side lines. **Unified** and **Split** are display layouts;
deletions, old-side lines, partial diffs, unavailable files, and earlier
revisions cannot be suggested. The replacement remains editable and uses the
forge's supported suggestion syntax for GitHub or GitLab.

Desktop descriptions and discussions render ordinary Markdown while keeping
raw HTML inert and images as text placeholders. Large or complex content can
fall back to a bounded plain-text preview, and an aggregate discussion budget
can omit later Markdown. HTTPS links open externally only after explicit
activation.

### Pipeline / CI

- **Three-level drill-down** -- browse pipelines, drill into jobs grouped by stage, drill into full job logs
- **ANSI log rendering** -- CI color output rendered natively via `Text.from_ansi()` with line numbers
- **Cancel / Retry** -- cancel running pipelines or individual jobs (`C`), retry failed pipelines or jobs (`R`), with double-press confirmation
- **Log search** -- press `/` in the log view to search for text across the full job output
- **Open in editor** -- press `F2` to open the job log in your `$EDITOR` for deeper analysis
- **Open in browser** -- press `o` to jump to the pipeline or job in the web UI
- **MR-scoped** -- Pipeline tab shows only pipelines associated with the current MR/PR
- **Lazy loading** -- pipeline data is fetched only when the Pipeline tab is first opened

### Caching

- **SQLite-backed cache** -- API responses are cached locally in SQLite (via aiosqlite) for snappy navigation
- **Transparent caching layer** -- `CachedForgeClient` wraps forge clients, caching reads and invalidating on mutations
- **Per-key TTL** -- MR lists (60s) and diffs (300s) have configurable time-to-live; expired entries are pruned automatically
- **LRU eviction** -- size-capped at 100 MB by default; recently accessed entries are protected from eviction
- **WAL mode** -- concurrent reads never block the UI event loop
- **Clear Cache** -- available from the command palette (`Ctrl+P`) to force a fresh fetch

### Actions

- **Approve** (`A`) -- approve the MR/PR with double-press confirmation
- **Unapprove** (`U`) -- revoke your approval (GitLab)
- **Merge** (`M`) -- merge with double-press confirmation
- **Close** (`X`) -- close with double-press confirmation
- **Merge readiness** -- visual indicator showing blockers (draft, conflicts, CI failing, merge status)

### Navigation

- **Command palette** (`Ctrl+P`) -- context-aware fuzzy search across all available actions
- **Commits tab** -- browse the full commit history for any MR/PR
- **Open in browser** (`o`) -- jump to the MR/PR in your default browser
- **Copy URL** (`Ctrl+Y`) -- yank the MR URL to clipboard
- **Refresh** (`Ctrl+R`) -- reload the current view

## Desktop application (unreleased)

> **No public desktop artifact exists yet.** There is no published archive, production tag, GitHub Release asset, RPM, or package repository to install from today. The commands and package names below describe the implemented contract that the first production release will use. Hardware-accelerated Electron on the supported Fedora host, packaging acceptance, and the release decision are separate gates that have not been met.

The desktop application is optional. Plain `tongs` keeps scanning your repositories and opening the TUI; it never downloads, activates, or starts the desktop shell on its own.

The release contract matches Linux, Fedora 44, x86_64, and the GNU ABI. The native acceptance policy covers Fedora 44 KDE on x86_64 using XWayland. Other distributions, desktop environments, operating systems, architectures, and native Wayland are outside that policy, and the release metadata check rejects an unsupported target before anything is downloaded.

### Per-user archive

```bash
tongs --install-desktop                  # exactly the same as: tongs desktop install
tongs desktop install --version 1.2.3    # install one exact release
tongs desktop update                     # install the newest verified release
tongs desktop status                     # inspect health; add --json for machine output
tongs desktop repair                     # recover activation or menu state
tongs desktop repair --redownload        # download a verified replacement if local recovery fails
tongs desktop uninstall                  # remove only the per-user activation
```

The archive installs under `$XDG_DATA_HOME`, or `~/.local/share` when that variable is unset or relative. It needs no root, owns only its own menu entry, and never writes to `/usr`.

Every release is verified before anything is extracted: fixed-repository release discovery, a GitHub-managed attestation bound to an exact workflow and tag identity, manifest and archive hashes, and a local downgrade guard. See [Security and signing](docs/reference/security.md) for the full contract and its limits, and the [desktop installation guide](docs/desktop/installation.md) for the recovery states and the `status` fields.

### Fedora RPMs

A separate, optional RPM path builds three packages:

| Package | Owns |
|---|---|
| `python3-tongs` | `/usr/bin/tongs`, the Python modules, and their metadata |
| `python3-tongs+mcp` | `/usr/bin/tongs-mcp` only, which keeps the MCP dependency out of the base closure |
| `tongs-desktop` | the Electron runtime under `/usr/libexec/tongs-desktop`, its system launcher, desktop entry, icon, AppStream metadata, and man page |

`tongs-desktop` requires the exact `python3-tongs` build it was made against, plus `xorg-x11-server-Xwayland`. When a repository is published, those are the names to install with `dnf`. None is published today: there is no COPR repository, no signed package, and no Fedora review submission. The packaging sources and the local rebuild harness are under [`packaging/rpm/`](packaging/rpm/).

The two methods are independent. The per-user commands never install RPM packages, elevate privileges, write to `/usr`, or remove RPM-owned files, and `tongs desktop status` reports when a per-user activation and an RPM installation coexist.

## Keybindings

The keybindings below describe the terminal TUI. The unreleased desktop
workspace uses labelled controls and does not change these terminal commands.

### Global

| Key | Action |
|-----|--------|
| `Ctrl+P` | Command palette |
| `?` | Help |
| `q` | Quit / Back |
| `Ctrl+R` | Refresh current view |
| `o` | Open in browser |

### Inbox

| Key | Action |
|-----|--------|
| `1` | My Reviews tab |
| `2` | My MRs tab |
| `3` | All Open tab |
| `s` | Cycle sort order |
| `r` | Repo list / Back |
| `Enter` | Open MR detail |

### Repo list

| Key | Action |
|-----|--------|
| `/` | Filter repos |
| `f` | Cycle forge filter (All/GH/GL) |
| `s` | Cycle sort order (name/forge/host) |
| `Enter` | Open scoped inbox for repo |

### MR detail

| Key | Action |
|-----|--------|
| `1`-`5` | Switch tabs (Overview/Diff/Commits/Discussion/Pipeline) |
| `c` | Add comment |
| `A` | Approve (press twice) |
| `U` | Unapprove (press twice) |
| `M` | Merge (press twice) |
| `X` | Close (press twice) |
| `Ctrl+Y` | Copy MR URL |
| `Ctrl+G` | Start review mode, or open the review draft screen once a draft exists |
| `Alt+]` | Cycle to the next recovered review draft for this MR |

### Diff viewer

| Key | Action |
|-----|--------|
| `j` / `k` | Move cursor down / up |
| `J` / `K` | Extend selection down / up |
| `Ctrl+Click` | Extend selection to clicked line |
| `]` / `[` | Jump to next / previous comment |
| `d` | Expand / collapse discussion thread |
| `r` | Reply to discussion on current line |
| `R` | Resolve / unresolve discussion (press twice) |
| `c` | Comment on current line or selection |
| `F3` | Suggest changes (opens `$EDITOR`) |
| `n` / `Shift+N` | Next / previous file |
| `m` | Toggle Markdown preview |
| `v` | Toggle unified/split diff mode |
| `Escape` | Clear selection |

In split diff mode, `h` and `l` move focus between the old and new side columns.

### Comment editor

| Key | Action |
|-----|--------|
| `Ctrl+S` | Submit comment |
| `Escape` | Cancel (press twice if text entered) |
| `F2` | Open in external editor |

### Review draft

| Key | Action |
|-----|--------|
| `Escape` | Close (double-press to discard unsaved summary or verdict changes) |
| `v` | Cycle verdict (Comment / Approve, plus Request changes on GitHub) |
| `Ctrl+S` | Submit the review |
| `e` | Edit the selected draft comment |
| `x` | Remove the selected draft comment (press twice) |
| `Shift+D` | Discard the local review draft (press twice) |
| `Shift+N` | Start a new revision (only when the draft is stale) |
| `r` | Resume a paused submission attempt |

See the [full keybinding reference](https://www.tongs.tools/reference/keybindings/) for the three additional bindings that reconcile an interrupted submission.

### Discussion tab

| Key | Action |
|-----|--------|
| `j` / `k` | Move between discussion cards |
| `Enter` | Jump to diff location (inline discussions) |
| `r` | Reply to focused discussion |
| `R` | Resolve / unresolve (press twice) |
| `f` | Cycle filter (All / Unresolved / Resolved) |
| `]` / `[` | Jump to next / previous unresolved |

### Pipeline tab

| Key | Action |
|-----|--------|
| `j` / `k` | Move between pipeline / job cards |
| `Enter` | Drill into jobs (from pipeline) or log (from job) |
| `Escape` | Drill out one level |
| `C` | Cancel pipeline or job (press twice) |
| `R` | Retry pipeline or job (press twice) |
| `o` | Open pipeline / job in browser |
| `F2` | Open job log in external editor |
| `/` | Search job log text |

## Configuration

tongs uses `~/.config/tongs/config.toml` (or the platform-appropriate config directory). All settings have sensible defaults. The [configuration reference](https://www.tongs.tools/reference/configuration/) documents every key, including three keys that older configs may still set and that no code currently reads.

```toml
[general]
scan_root = "~/git"
scan_depth = 5

[ui]
ascii_mode = false          # Set true for minimal terminals

[cache]
mr_list_ttl = 60
diff_ttl = 300
max_size_mb = 100

[concurrency]
max_parallel = 8
request_timeout = 30

# Self-hosted forges
[hosts.my-gitlab]
hostname = "gitlab.example.com"
forge_type = "gitlab"

[hosts.my-ghes]
hostname = "github.corp.com"
forge_type = "github"
```

## Plugin system

tongs uses a plugin architecture based on Python entry points. The MCP server, for example, is itself a plugin. You can extend tongs with new commands, screens, and lifecycle hooks by writing your own.

### Writing a plugin

1. Subclass `TongsPlugin`:

```python
from tongs.plugins.base import TongsPlugin

class MyPlugin(TongsPlugin):
    @property
    def name(self) -> str:
        return "my-plugin"

    @property
    def version(self) -> str:
        return "0.1.0"

    async def on_app_ready(self, app) -> None:
        """Called after the TUI app is mounted."""

    async def on_app_shutdown(self, app) -> None:
        """Called before app exit."""

    def get_commands(self) -> list[tuple[str, str, object]]:
        """Return (display, help_text, callback) tuples for the command palette."""
        return [("My Action", "Does something useful", self._do_it)]

    def get_screens(self) -> dict[str, type]:
        """Return screen_name -> Screen class mappings."""
        return {}
```

2. Register it as an entry point in your package's `pyproject.toml`:

```toml
[project.entry-points."tongs.plugins"]
my-plugin = "my_package.plugin:MyPlugin"
```

3. Install your package (or `pip install -e .` for development) and tongs discovers it automatically on startup.

### Plugin configuration

Plugins are enabled by default. To disable a plugin, add a `[plugins.<name>]` section to your config:

```toml
# ~/.config/tongs/config.toml
[plugins.mcp]
enabled = false
```

### Plugin hooks

| Hook | When it fires |
|------|---------------|
| `on_app_ready(app)` | After the TUI mounts |
| `on_app_shutdown(app)` | Before app exit |
| `get_commands()` | Command palette collects entries |
| `get_screens()` | Screen registry collects routes |

## MCP server (plugin)

tongs ships an optional [Model Context Protocol](https://modelcontextprotocol.io) server, packaged as a built-in plugin, that lets AI assistants interact with your merge requests programmatically.

### Install

```bash
pip install "tongs[mcp]"
```

### Start

```bash
tongs-mcp
```

The server communicates over stdio, so you can wire it into any MCP-compatible client, including Codex, Claude Code, and Claude Desktop. It reuses the same forge configuration and auth tokens as the TUI.

When the `mcp` extra is installed, the built-in plugin also adds a **Start MCP Server** command to the command palette, which launches the server as `python -m tongs.mcp.server`. On a default install without the extra, the plugin contributes no command.

### Connect to Codex

After installing `tongs[mcp]` and completing the forge login above, register the server with the Codex CLI:

```bash
codex mcp add tongs -- tongs-mcp
codex mcp list
```

Codex launches the stdio server itself. The `tongs-mcp` executable and any authentication CLI (`gh` or `glab`) must be available in the environment where Codex runs.

For a source checkout, install the MCP extra in the activated virtual environment and register the executable by absolute path:

```bash
source .venv/bin/activate
python -m pip install -e ".[dev,mcp]"
codex mcp add tongs -- "$PWD/.venv/bin/tongs-mcp"
```

Alternatively, add this to `~/.codex/config.toml`, replacing the path with your checkout's absolute path:

```toml
[mcp_servers.tongs]
command = "/absolute/path/to/tongs/.venv/bin/tongs-mcp"
```

See the [official Codex MCP configuration guide](https://developers.openai.com/codex/mcp) for configuration options. Example request: "Use tongs to list open PRs for `github.com/acme/app`."

### Connect to Claude Code

```bash
claude mcp add tongs -- tongs-mcp
claude mcp list
```

Everything after `--` is the command Claude Code launches, so use an absolute path when `tongs-mcp` is not on `PATH`:

```bash
claude mcp add tongs -- "$PWD/.venv/bin/tongs-mcp"
```

`claude mcp add` defaults to the `local` scope, which applies to the current project only. Add `--scope user` to make the server available in all your projects, or `--scope project` to write a shared `.mcp.json` into the repository. Claude Desktop is a separate application with its own `claude_desktop_config.json`.

### Tools

| Tool | Description |
|------|-------------|
| `list_mrs` | List open/closed/merged MRs for a repository |
| `get_mr` | Get detailed MR info (description, approvals, labels, conflicts) |
| `get_mr_diff` | Get the unified diff for an MR |
| `post_comment` | Post a general comment on an MR |
| `approve_mr` | Approve an MR |
| `list_pipelines` | List CI pipelines for an MR |

All tools accept a `repo_path` in `hostname/owner/repo` format (e.g. `github.com/acme/app`). Destructive actions (merge, close, cancel) are intentionally excluded as a security boundary.

## tongs is for you if

- You review code across both GitHub and GitLab
- You want diffs, inline comments, and approvals in your terminal
- You manage many repos and want a single inbox
- You prefer keyboard-driven workflows over browser tabs

| | tongs | gh-dash | GitHub/GitLab web |
|---|---|---|---|
| GitHub + GitLab | Yes | GitHub only | One at a time |
| Terminal-native diffs | Yes | No | No |
| Inline comments | Yes | No | Yes |
| Discussion threads + tab | Yes | No | Yes |
| Suggested changes | Yes | No | Yes |
| Pipeline / CI drill-down | Yes | No | Yes |
| Approve / Merge | Yes | No | Yes |
| Command palette | Yes | No | N/A |
| MCP server (AI integration) | Yes | No | No |
| Plugin system | Yes | No | No |
| SQLite offline cache | Yes | No | N/A |
| Rate limit auto-retry | Yes | No | N/A |
| Zero config auth | Yes | Yes | N/A |
| Self-hosted forges | Yes | Yes | N/A |

## Current state

The terminal application is the released, supported product. The version you get from PyPI today provides the multi-forge inbox, the diff viewer, inline comments and suggested changes, discussion threads, MR actions, pipeline and CI drill-down, the SQLite cache, the plugin system, and the MCP server.

Two terminal features described above are not in that release yet: the split diff view with its `v` toggle and `h` / `l` side focus, and durable review drafts with the `Ctrl+G` review flow. Both are implemented on the development branch and arrive with the next release.

The desktop application, its per-user installer, and the Fedora RPM packaging are also implemented in this tree and unreleased. Hardware-accelerated Electron on the supported Fedora host, packaging acceptance, and the release decision remain separate, unmet gates.

Track what is planned and what is blocked through the [open issues](https://github.com/andre-motta/tongs/issues) and their dependency links rather than through a phase table.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, code style, and how to add forge backends, and the [Contributing page](https://www.tongs.tools/contributing/) for a summary of the checks a pull request has to pass.

### Working on tongs with a coding agent

Run your agent from the repository root after the [source setup](#from-source). Both Codex and Claude Code read [AGENTS.md](AGENTS.md) for the development workflow, validation commands, commit rules, and links to the subsystem guides under `.agents/`. See [OpenAI's AGENTS.md guide](https://developers.openai.com/codex/guides/agents-md) for how Codex discovers those instructions.

The MCP connection above is optional for contributing to tongs. It exposes forge operations to the assistant; repository development itself uses the local source tree and tests.

tongs is early enough that contributions shape the architecture. The [plugin system](#plugin-system) makes it easy to add new commands, screens, and lifecycle hooks without touching core code. Check the [issues](https://github.com/andre-motta/tongs/issues) for good starting points, or open one to discuss what you'd like to build.

## Why "tongs"?

In metalworking, tongs grip hot work across the forge. This tool grips your merge requests across forges.

## License

[MIT](LICENSE)
