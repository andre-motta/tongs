# tongs

Terminal-native multi-forge MR/CI management TUI. Scans local git repos, detects GitHub/GitLab forges from remotes, and provides a unified interface for merge request review, inline commenting, and CI/CD pipeline management. Python 3.12+, Textual framework, async httpx transport.

## Tech Stack

- **Framework:** Textual (TUI), Rich (syntax highlighting)
- **HTTP:** httpx (async), no CLI subprocess per operation
- **Config:** TOML via tomllib, platformdirs for cross-platform paths
- **Build:** hatchling + hatch-vcs, `pip install -e ".[dev]"` for development
- **Distribution:** `pipx install tongs` or `uvx tongs`
- **Entry points:** `tongs` (TUI), `tongs-mcp` (MCP server, planned)

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
  config.py            # TOML config loader, Config dataclass, platformdirs paths
  errors.py            # ForgeError hierarchy + credential redaction

  scanner/             # Repo discovery from filesystem
    repo.py            # Repo, Remote, ForgeType dataclasses
    remote.py          # Remote URL parsing (SSH/SCP/HTTPS), forge detection
    discovery.py       # Filesystem walk, git remote reading, primary remote selection

  forges/              # Forge abstraction layer
    base.py            # ForgeClient ABC (MR, comment, review, pipeline ops)
    models.py          # Shared dataclasses (MRSummary, MRDetail, Pipeline, etc.)
    auth.py            # Token resolution cascade: CLI -> .netrc -> error
    http.py            # httpx transport: create_client, request, paginate, error mapping
    gitlab.py          # GitLabClient implementation (API v4)
    registry.py        # ForgeRegistry: hostname -> authenticated ForgeClient

  diff/                # Diff parsing engine (Phase 2)
    models.py          # DiffFile, DiffHunk, DiffLine, LineType, FileStatus
    parser.py          # Unified diff parser (plain + git format)

  state/               # App state management
    app_state.py       # MRFilter, ReviewDraft dataclasses

  views/               # Textual Screens
    inbox.py           # InboxScreen: MR inbox with tabs (My Reviews/My MRs/All Open)
    repo_list.py       # RepoListScreen: tree grouped by namespace
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

## Development Status

Phase 1 complete (scanner, forge layer, TUI shell, GitLab client). Phase 2 in progress (diff parser done, diff views next). GitHub client planned for Phase 3.

## Gate Process

Every feature goes through four review areas before merge:

1. **Architecture** -- fits existing abstractions, extends cleanly
2. **Security** -- no token leaks, proper credential handling
3. **UX** -- TUI flow, keybinding consistency, ASCII mode support
4. **QE** -- test coverage, edge cases, runs without network

Verdicts: APPROVED WITH NOTES (address notes, proceed) or NEEDS CHANGES (fix and re-review).
