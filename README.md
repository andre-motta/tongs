<p align="center">
  <img src="docs/assets/hero-banner.png" alt="tongs - Unified code review for the terminal" width="100%">
</p>

<p align="center">
  <strong>Code review without leaving the terminal.</strong><br>
  A terminal-native TUI for merge request review and CI/CD management across GitHub and GitLab.
</p>

<p align="center">
  <a href="https://pypi.org/project/tongs/"><img src="https://img.shields.io/pypi/v/tongs" alt="PyPI"></a>
  <a href="https://github.com/andre-motta/tongs/blob/main/LICENSE"><img src="https://img.shields.io/github/license/andre-motta/tongs" alt="License"></a>
  <a href="https://github.com/andre-motta/tongs"><img src="https://img.shields.io/github/stars/andre-motta/tongs" alt="Stars"></a>
</p>

Point it at any directory, and tongs discovers your repos, detects their forges, and gives you a unified inbox for everything that needs your attention.

No browser tabs. No context switching. Just your terminal.

> Think of it as [lazygit](https://github.com/jesseduffield/lazygit) for code review, or [gh-dash](https://github.com/dlvhdr/gh-dash) that also speaks GitLab.

## Why?

Every code review starts the same way: you leave your editor, open a browser, navigate to the MR, lose your terminal context, and then try to remember what you were doing when you get back. Multiply that by a dozen repos across GitHub and GitLab and it adds up fast.

tongs keeps you in the terminal. It scans your local repos, figures out which forges they live on, and pulls your review queue into a single dashboard. Diffs render with syntax highlighting. Comments happen inline. Pipelines show their status right next to the MR. You approve, merge, or comment without ever opening a browser.

## Status

**Early stage, actively developed.** Phase 1 is complete: repo scanner, forge abstraction (GitLab client), and the TUI shell with inbox and repo list. 200+ tests. Published on [PyPI](https://pypi.org/project/tongs/). The core plumbing works and the roadmap is clear.

This is a good time to get involved if you want to shape the direction.

## Features

### Built (Phase 1)

- **Repo scanner** -- walks any directory, finds git repos, auto-detects GitHub vs GitLab from remotes
- **Unified inbox** -- "My Reviews" and "My MRs" tabs across all discovered repos and forges
- **Repo list** -- tree view grouped by namespace/org with forge indicators
- **Forge abstraction** -- clean `ForgeClient` interface with GitLab implementation
- **Smart auth** -- delegates to `gh`/`glab` CLI credential stores with `.netrc` fallback (tongs never stores your tokens)
- **TOML config** -- `~/.config/tongs/config.toml` with sane defaults for everything
- **Cross-platform** -- Linux, macOS, Windows via platformdirs
- **ASCII mode** -- works on any terminal, Nerd Fonts are optional polish

### Planned

- Full diff viewer with syntax highlighting, word-level diff, context folding
- Inline comments on diff lines, thread replies, resolve/unresolve discussions
- Approve, merge, close, reopen with confirmation dialogs
- Pipeline/CI management: job logs, retry, cancel, trigger
- GitHub client (forge abstraction is ready, implementation is next)
- Plugin system (fleet monitor is the first plugin, more planned)
- MCP server for AI tool integration
- Neovim integration (optional, never required)

## Install

```bash
# Recommended: isolated install with pipx or uvx
pipx install tongs
# or
uvx install tongs

# Or plain pip
pip install tongs
```

Requires **Python 3.12+**. No other system dependencies are required.

### Optional extras

```bash
# MCP server support (for AI tool integration)
pipx install 'tongs[mcp]'

# Fleet monitor plugin
pipx install 'tongs[fleet]'
```

## Quick start

```bash
# Run tongs -- it scans ~/git by default
tongs

# Point at a different directory
tongs --scan-root ~/projects
```

tongs discovers repos under your scan root, reads their git remotes, and populates the inbox. Auth tokens come from your existing `gh` and `glab` CLI logins automatically.

### First-time setup

If you haven't logged in to your forges yet:

```bash
# GitHub
gh auth login

# GitLab (gitlab.com)
glab auth login

# GitLab (self-hosted)
glab auth login --hostname gitlab.example.com
```

### Configuration

tongs uses `~/.config/tongs/config.toml` (or the platform-appropriate config directory). All settings have sensible defaults. Example:

```toml
[general]
scan_root = "~/git"
scan_depth = 5

[ui]
theme = "monokai"
diff_style = "unified"
ascii_mode = false

[cache]
mr_list_ttl = 60
diff_ttl = 300

[hosts.my-gitlab]
hostname = "gitlab.example.com"
forge_type = "gitlab"
```

## Keybindings

| Key | Action |
|-----|--------|
| `1` | My Reviews tab |
| `2` | My MRs tab |
| `r` | Switch to repo list |
| `o` | Open MR in browser |
| `Enter` | Open MR detail |
| `Ctrl+R` | Refresh |
| `Ctrl+P` | Command palette |
| `?` | Help |
| `q` | Quit / Back |

## Architecture

```
tongs/
  scanner/        # Walks filesystem, discovers repos, parses remotes
  forges/         # Abstract ForgeClient + concrete implementations (GitLab, GitHub)
    auth.py       # Token resolution: CLI cred stores -> .netrc -> helpful error
    base.py       # ABC defining the forge interface
    gitlab.py     # GitLab API v4 client
    models.py     # Shared data models (MRSummary, MRDetail, Pipeline, etc.)
    registry.py   # Maps hostnames to authenticated client instances
  views/          # Textual screens (inbox, repo list, and future diff/detail views)
  widgets/        # Reusable TUI components
  plugins/        # Plugin system (extensible)
  mcp/            # MCP server for AI tool integration
  cache/          # SQLite-backed response cache
  state/          # Application state management
  config.py       # TOML config loader with platformdirs
  app.py          # Main Textual application
```

Key design decisions:

- **Forge abstraction** -- `ForgeClient` is an ABC. Adding a new forge means implementing the interface; the TUI never knows which forge it's talking to.
- **Auth delegation** -- tongs never stores tokens. It reads from `gh auth token` / `glab auth token` at runtime, falling back to `.netrc`.
- **Scanner, not config** -- you don't list repos in a config file. tongs walks your filesystem and discovers them, just like your shell does.
- **Lazy client creation** -- `ForgeRegistry` creates and caches API clients on first access. No network calls until something actually needs data.

## Roadmap

| Phase | What | Status |
|-------|------|--------|
| 1 | Scanner, forge abstraction, GitLab client, TUI shell, inbox, repo list | Done |
| 2 | MR detail view, diff viewer with syntax highlighting, inline comments | Next |
| 3 | MR actions (approve, merge, close, reopen), confirmation dialogs | Planned |
| 4 | Pipeline/CI management (job logs, retry, cancel) | Planned |
| 5 | GitHub client implementation | Planned |
| 6 | Plugin system, fleet monitor plugin, MCP server | Planned |
| 7 | Neovim integration, advanced search, cross-forge workflows | Planned |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, code style, and how to add plugins or forge backends.

tongs is early enough that contributions shape the architecture. The plugin system, GitHub client, and diff viewer are all open for contributors. Check the [issues](https://github.com/andre-motta/tongs/issues) for good starting points, or open one to discuss what you'd like to build.

## Why "tongs"?

In metalworking, tongs grip hot work across the forge. This tool grips your merge requests across forges.

## License

[MIT](LICENSE)
