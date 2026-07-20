# tongs

Terminal-native multi-forge MR/CI management TUI. Scans local git repos, detects GitHub/GitLab forges from remotes, and provides a unified interface for merge request review, inline commenting, and CI/CD pipeline management. Python 3.12+, Textual framework, async httpx transport.

## Tech Stack

- **Framework:** Textual (TUI), Rich (syntax highlighting)
- **HTTP:** httpx (async), no CLI subprocess per operation
- **Config:** TOML via tomllib, platformdirs for cross-platform paths
- **Build:** hatchling + hatch-vcs, `pip install -e ".[dev]"` for development
- **Distribution:** `pipx install tongs` or `uvx tongs`
- **Entry points:** `tongs` (TUI), `tongs-mcp` (MCP server)
- **Plugins:** entry point group `tongs.plugins`, config via `[plugins.*]` TOML sections
- **Docs:** MkDocs Material site at [tongs.tools](https://tongs.tools)

## Critical Rules

```bash
# Always activate venv first
source /home/alustosa/git/tongs/.venv/bin/activate

# Run tests (no network access needed, all mocked)
pytest

# Lint before committing
ruff check src/ tests/
ruff format src/ tests/

# Install editable with dev deps
pip install -e ".[dev]"
```

- `from __future__ import annotations` at the top of every module
- Module-level imports unless function-level is necessary to avoid circular deps
- Frozen dataclasses for immutable data, regular dataclasses for mutable state
- Type hints on all parameters and return values
- No em-dashes in text or commits
- Use `-s` flag on `git commit` for sign-off (DCO)

## Module Map

```
src/tongs/
  app.py              # TongsApp (Textual App), reactive state, discovery
  commands.py          # TongsCommandProvider (command palette, context-aware discover/search)
  config.py            # TOML config loader, Config dataclass, platformdirs paths
  errors.py            # ForgeError hierarchy + credential redaction

  scanner/             # Repo discovery from filesystem
    repo.py            # Repo, Remote, ForgeType dataclasses
    remote.py          # Remote URL parsing (SSH/SCP/HTTPS), forge detection
    discovery.py       # Filesystem walk, git remote reading, primary remote selection

  forges/              # Forge abstraction layer
    base.py            # ForgeClient ABC (MR, comment, review, pipeline, commit ops; list_mr_pipelines, retry_pipeline, cancel_job, supports_job_cancel)
    models.py          # Shared dataclasses (MRSummary, MRDetail, Pipeline, PipelineJob, Commit, etc.)
    auth.py            # Token resolution cascade: CLI -> .netrc -> error
    http.py            # httpx transport: create_client, request, paginate, error mapping
    gitlab.py          # GitLabClient implementation (API v4)
    github.py          # GitHubClient implementation (REST API)
    registry.py        # ForgeRegistry: hostname -> authenticated ForgeClient

  diff/                # Diff parsing engine (Phase 2)
    models.py          # DiffFile, DiffHunk, DiffLine, LineType, FileStatus
    parser.py          # Unified diff parser (plain + git format)
    position.py        # DiffPosition, forge-specific position converters

  state/               # App state management
    app_state.py       # MRFilter, ReviewDraft dataclasses

  widgets/             # Reusable Textual widgets
    comment_editor.py  # CommentEditor (bottom-docked, general + inline + reply + reply_general modes, focus save/restore)
    diff_panel.py      # DiffPanel (split-pane: DiffFileTree + DiffContent(DiffOptionList + Markdown)), discussion threading, jump_to_discussion()
    discussion_list.py # DiscussionPanel (card-based discussion tab), DiscussionCard, render_diff_snippet(), JumpToDiffDiscussion/DiscussionReplyRequested messages
    mr_table.py        # MRTable (DataTable subclass, setup_columns(show_repo) toggle)
    pipeline_panel.py  # PipelinePanel (three-level drill-down: pipelines -> jobs -> log), PipelineCard, JobCard, RichLog-based log viewer

  cache/               # API response caching
    store.py           # CacheStore: async SQLite (aiosqlite), TTL, LRU eviction, WAL mode, 0o600 perms

  plugins/             # Plugin system (Phase 6)
    base.py            # TongsPlugin ABC (name, version, get_commands, get_screens, lifecycle hooks)
    registry.py        # PluginRegistry: entry point discovery, config filtering, graceful failure

  mcp/                 # MCP server for AI agent integration
    server.py          # FastMCP server, 6 tools (list_mrs, get_mr, get_mr_diff, post_comment, approve_mr, list_pipelines)
    plugin.py          # MCPPlugin: first-party TongsPlugin, registers "Start MCP Server" command

  views/               # Textual Screens
    inbox.py           # InboxScreen: MR inbox with tabs (My Reviews/My MRs/All Open), supports scoped mode
    repo_list.py       # RepoListScreen: searchable DataTable with live filter and forge cycling
    mr_detail.py       # MRDetailScreen: tabbed MR detail (Overview/Diff/Discussion/Pipeline)
    suggestion.py      # Pure helpers for suggestion comments (template, fence, forge-specific blocks)
```

## Subsystem Guides

| Topic | Path | When to read |
|---|---|---|
| Architecture | [.agents/architecture/](.agents/architecture/README.md) | Understanding data flow, layering, error handling |
| Forges | [.agents/forges/](.agents/forges/README.md) | Working on forge clients, adding a new backend |
| TUI | [.agents/tui/](.agents/tui/README.md) | Working on screens, widgets, keybindings |
| Diff | [.agents/diff/](.agents/diff/README.md) | Working on diff parsing, rendering, position mapping |
| Testing | [.agents/testing/](.agents/testing/README.md) | Writing tests, understanding mock patterns |
| Security | [.agents/security/](.agents/security/README.md) | Auth, credentials, subprocess safety |
| Plugins | [.agents/plugins/](.agents/plugins/README.md) | Plugin system, MCP server, extending tongs |
| Cache | [.agents/cache/](.agents/cache/README.md) | SQLite cache store, TTL, LRU eviction |

## Development Status

Phase 1 complete (scanner, forge layer, TUI shell, GitLab client). Phase 2 complete (diff parser, position mapping, MR detail/list screens, diff viewer widget). Phase 3 complete (GitHub REST API client, Commit model, commits tab in MR detail, scrollable overview with Markdown description, SSRF prevention, cross-fork safety). Phase 4 complete (inline discussion threads with expand/collapse/reply/resolve, command palette, comment navigation, parallel diff+discussions fetch, file tree with comment counts, footer cleanup with context-aware bindings, card-based Discussion tab with diff snippets and Rich Markdown threads, cross-tab jump-to-diff navigation, diff caching between tabs, GitLab system note filtering, CommentEditor focus restoration). Phase 5 complete (Pipeline/CI management with three-level drill-down: pipelines, jobs, log viewer; cancel/retry pipelines and jobs; RichLog-based ANSI log rendering; F2 open in editor; / search in log; list_mr_pipelines, retry_pipeline, cancel_job backend methods; supports_job_cancel capability property). Phase 6 complete (plugin system with TongsPlugin ABC, PluginRegistry with entry point discovery and config filtering, MCPPlugin as first-party plugin, plugin commands merged into command palette, `tongs.plugins` entry point group in pyproject.toml). Phase 7 complete (CacheStore with aiosqlite/TTL/LRU/WAL/0o600 perms, GitHub GraphQL _graphql helper with resolve_discussion via mutations and supports_thread_resolution, MCP server with 6 read/comment/approve tools and tongs-mcp entry point, MkDocs Material site at tongs.tools).

## Gate Process

Every feature goes through four review areas before merge:

1. **Architecture** -- fits existing abstractions, extends cleanly
2. **Security** -- no token leaks, proper credential handling
3. **UX** -- TUI flow, keybinding consistency, ASCII mode support
4. **QE** -- test coverage, edge cases, runs without network

Verdicts: APPROVED WITH NOTES (address notes, proceed) or NEEDS CHANGES (fix and re-review).
