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

**[Documentation](https://tongs.tools)** | **[PyPI](https://pypi.org/project/tongs/)** | **[Issues](https://github.com/andre-motta/tongs/issues)**

## Install

```bash
# Recommended: install with pipx or uvx for isolation
pipx install tongs
# or
uvx install tongs

# Or plain pip
pip install tongs
```

Requires **Python 3.12+**. No other system dependencies.

### From source

```bash
git clone https://github.com/andre-motta/tongs.git
cd tongs
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
```

## Quick start

```bash
# Run tongs -- it scans ~/git by default
tongs

# Configure a different scan root in ~/.config/tongs/config.toml:
#   [general]
#   scan_root = "~/projects"
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

tongs never stores your tokens. It delegates to `gh auth token` / `glab auth token` at runtime, falling back to `~/.netrc`.

## Features

### Inbox

- **Multi-forge inbox** -- GitHub PRs and GitLab MRs in one view, auto-detected from your git remotes
- **Three tabs** -- My Reviews, My MRs, All Open with lazy loading and parallel fetching
- **Per-repo scope** -- click any repo in the repo list to filter the inbox to just that project
- **Repo list** -- searchable, filterable by forge type (GH/GL/All), grouped by namespace

### Diff review

- **Split-pane viewer** -- file tree on the left, diff content on the right
- **Syntax highlighting** -- 500+ languages via Pygments, applied per-line inside the diff
- **Word-level diffs** -- changed words highlighted bold+underline within modified lines
- **Context folding** -- long unchanged sections collapse to "... N unchanged lines ..." markers
- **Markdown preview** -- toggle rendered preview for `.md` files with `m`
- **File navigation** -- jump between files with `n` / `Shift+N`

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
- **Per-key TTL** -- MR lists (60s) and diffs (300s) have configurable time-to-live; expired entries are pruned automatically
- **LRU eviction** -- size-capped at 100 MB by default; recently accessed entries are protected from eviction
- **WAL mode** -- concurrent reads never block the UI event loop

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

## Keybindings

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
| `r` | Repo list / Back |
| `Enter` | Open MR detail |

### Repo list

| Key | Action |
|-----|--------|
| `/` | Filter repos |
| `f` | Cycle forge filter (All/GH/GL) |
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
| `Escape` | Clear selection |

### Comment editor

| Key | Action |
|-----|--------|
| `Ctrl+S` | Submit comment |
| `Escape` | Cancel (press twice if text entered) |
| `F2` | Open in external editor |

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

tongs uses `~/.config/tongs/config.toml` (or the platform-appropriate config directory). All settings have sensible defaults.

```toml
[general]
scan_root = "~/git"
scan_depth = 5

[ui]
theme = "monokai"
diff_style = "unified"
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

## MCP server

tongs ships an optional [Model Context Protocol](https://modelcontextprotocol.io) server that lets AI assistants interact with your merge requests programmatically.

### Install

```bash
pip install tongs[mcp]
```

### Start

```bash
tongs-mcp
```

The server communicates over stdio, so you can wire it into any MCP-compatible client (Claude Code, Claude Desktop, etc.). It reuses the same forge configuration and auth tokens as the TUI, so no extra setup is needed.

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
| SQLite offline cache | Yes | No | N/A |
| Zero config auth | Yes | Yes | N/A |
| Self-hosted forges | Yes | Yes | N/A |

## Roadmap

| Phase | What | Status |
|-------|------|--------|
| 1 | Scanner, forge abstraction, GitLab client, TUI shell, inbox, repo list | Done |
| 2 | MR detail view, diff viewer, syntax highlighting, GitHub client, commits tab | Done |
| 3 | MR actions (approve, merge, close), inline comments, suggested changes | Done |
| 4 | Discussion threads (inline + card-based tab with diff snippets), command palette, comment navigation, cross-tab jump-to-diff | Done |
| 5 | Pipeline/CI management (three-level drill-down, job logs, retry, cancel, log search) | Done |
| 6 | Plugin system | Deferred ([#2](https://github.com/andre-motta/tongs/issues/2)) |
| 7 | SQLite caching, MCP server, GitHub thread resolution, MkDocs site | Done |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, code style, and how to add forge backends. 551 tests and growing.

tongs is early enough that contributions shape the architecture. The plugin system ([#2](https://github.com/andre-motta/tongs/issues/2)) is the main open design area. Check the [issues](https://github.com/andre-motta/tongs/issues) for good starting points, or open one to discuss what you'd like to build.

## Why "tongs"?

In metalworking, tongs grip hot work across the forge. This tool grips your merge requests across forges.

## License

[MIT](LICENSE)
