---
title: MCP server
description: "Run tongs-mcp so an agent can read your reviews, post a comment and approve."
lead: "tongs-mcp is a Model Context Protocol server. It gives an AI assistant six tools for reviews on GitHub and GitLab, using the same forge configuration and credentials as the terminal app."
---

The server speaks MCP over stdio. Your MCP client starts it as a subprocess
and talks to it through standard input and output, so there is no port to open
and no daemon to keep running. It works with any MCP client, including Codex,
Claude Code and Claude Desktop.

## Install

The server needs the optional `mcp` extra:

```bash
pipx install "tongs[mcp]"
```

If tongs is already installed with pipx, reinstall it with the extra:
`pipx install --force "tongs[mcp]"`. `--force` keeps only the extras you name,
so include any you already use, for example `"tongs[mcp,keyring]"`. With uv,
run `uv tool install "tongs[mcp]"`, adding `--reinstall` if tongs is already
installed.

With plain pip, run `pip install "tongs[mcp]"` in the environment you use for
tongs. Every install puts a `tongs-mcp` command next to `tongs`, but it only
runs when the extra is installed.

On Fedora, the `python3-tongs+mcp` RPM attached to each GitHub Release
provides `/usr/bin/tongs-mcp`. Install it together with `python3-tongs` using
`dnf install ./*.rpm`.

Sign in to your forges first, as described in
[Install and sign in](/getting-started/). The server resolves a token for each
host the same way the terminal app does: the `gh` or `glab` credential store,
then a `~/.netrc` entry whose `machine` names the host, then the system
keyring. When a forge call fails, the tool returns a fixed message such as
"Forge authentication is unavailable." and never the underlying error text.

## Connect a client

You do not run `tongs-mcp` by hand. Register it with your client, and the
client launches it when it needs a tool. The `tongs-mcp` executable, and the
`gh` or `glab` CLI if you sign in through them, must be available in the
environment where the client runs.

### Claude Code

```bash
claude mcp add tongs -- tongs-mcp
claude mcp list
```

Everything after the standalone `--` is the command Claude Code launches. Use
an absolute path when `tongs-mcp` is not on `PATH`:

```bash
claude mcp add tongs -- "$HOME/.local/bin/tongs-mcp"
```

`claude mcp add` uses the `local` scope by default, which applies to the
current project only. Add `--scope user` to make the server available in all
your projects, or `--scope project` to write a shared `.mcp.json` into the
repository.

Claude Desktop is a separate application. It reads its servers from its own
`claude_desktop_config.json`.

### Codex

```bash
codex mcp add tongs -- tongs-mcp
codex mcp list
```

You can also add the server to `~/.codex/config.toml`. Use the absolute path
to the executable:

```toml title="~/.codex/config.toml"
[mcp_servers.tongs]
command = "/home/you/.local/bin/tongs-mcp"
```

The [Codex MCP configuration guide](https://developers.openai.com/codex/mcp)
lists the other options.

### From a source checkout

Install the extra into the checkout's virtual environment and register the
executable by its absolute path:

```bash
source .venv/bin/activate
python -m pip install -e ".[dev,mcp]"
claude mcp add tongs -- "$PWD/.venv/bin/tongs-mcp"
```

The same path works with `codex mcp add`.

## Tools

Every tool takes a `repo_path` in `hostname/owner/repo` form, for example
`github.com/acme/app`. GitLab subgroups add segments:
`gitlab.com/acme/platform/api`. `number` is the pull request or merge request
number.

| Tool | Arguments | What it returns |
| --- | --- | --- |
| `list_mrs` | `repo_path`, `state` | Reviews in the repository, filtered by `state`: `open` (the default) or `closed`, and on GitLab also `merged`. On GitHub, `closed` includes merged pull requests, and `merged` is not accepted. Each entry has the number, title, author, source and target branch, CI status and web URL. |
| `get_mr` | `repo_path`, `number` | One review in detail: description, state, draft flag, conflicts, additions and deletions, approvals, reviewers, labels and CI status. |
| `get_mr_diff` | `repo_path`, `number` | The changes as unified diff text, with `---` and `+++` headers for each file. |
| `list_pipelines` | `repo_path`, `number` | The review's pipelines or workflow runs: ID, status, ref, short SHA, source, duration in seconds and web URL. |
| `post_comment` | `repo_path`, `number`, `body` | Posts `body` as a general comment on the review. Markdown is supported. |
| `approve_mr` | `repo_path`, `number` | Approves the review. |

Ask for what you want in plain language, and the assistant picks the tools.
For example: "Use tongs to list open PRs for `github.com/acme/app`", or
"Summarize the diff of merge request 42 in `gitlab.com/acme/api`".

## Safety

- **The server acts as you.** It uses your own forge token. A comment or an
  approval from an agent appears under your name, exactly as if you had
  posted it.
- **Two tools write.** `post_comment` and `approve_mr` change state on the
  forge. The other four only read. Your MCP client decides whether to ask
  before each tool call; keep confirmation on for these two.
- **No destructive actions.** There is no tool to merge, close, reopen or
  cancel anything. This is a deliberate boundary, not a missing feature.
- **General comments only.** The server cannot post inline comments,
  suggested changes or review drafts. Use the terminal app or the desktop app (beta) for
  those.
- **Checked repository paths.** A `repo_path` must have a hostname and at
  least an owner and a repository, may contain only ASCII letters, digits,
  `_`, `.` and `-` in each segment, and may not contain `..`. A GitHub path
  must be exactly `hostname/owner/repo`; a GitLab path may include nested
  groups. Anything else is rejected before a request is made.
- **Configured hosts only.** github.com and gitlab.com work out of the box.
  For a self-hosted forge, add it under `[hosts.*]` in the
  [configuration](/reference/configuration/) file, which the server reads at
  startup. Any other host is rejected before a token is looked up or a
  request is made, even if its name contains "github" or "gitlab". This is
  the same host set the terminal and desktop apps use.
- **No cache.** Each tool call goes to the forge API directly. The server does
  not read or write the tongs response cache.

## The palette command

When the `mcp` extra is installed, the built-in `mcp` terminal plugin adds a
**Start MCP Server** command to the command palette (++ctrl+p++). It launches
the server in the background as `python -m tongs.mcp.server`. MCP clients start
the server themselves, so you do not need this command to connect one.

Without the extra, the plugin adds no command. To remove it even when the
extra is installed, disable the plugin in `config.toml`:

```toml title="~/.config/tongs/config.toml"
[plugins.mcp]
enabled = false
```

This only hides the palette command. The `tongs-mcp` executable keeps working.
