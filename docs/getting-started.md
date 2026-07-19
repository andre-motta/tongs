# Getting started

## Requirements

- **Python 3.12** or newer
- **`gh` CLI** for GitHub repositories (install from [cli.github.com](https://cli.github.com))
- **`glab` CLI** for GitLab repositories (install from [glab-cli.io](https://glab-cli.io))

You only need the CLI(s) for the forge(s) you use. If you only review GitHub PRs,
you do not need `glab` installed.

## Installation

=== "pipx (recommended)"

    ```bash
    pipx install tongs
    ```

    pipx installs tongs in an isolated virtual environment while making the `tongs`
    command available globally.

=== "uvx"

    ```bash
    uvx install tongs
    ```

=== "pip"

    ```bash
    pip install tongs
    ```

=== "From source"

    ```bash
    git clone https://github.com/andre-motta/tongs.git
    cd tongs
    uv venv && source .venv/bin/activate
    uv pip install -e ".[dev]"
    ```

## Auth setup

tongs never stores your tokens. It delegates to the `gh` and `glab` CLIs at
runtime, so you authenticate once and tongs picks up the tokens automatically.

### GitHub

```bash
gh auth login
```

Follow the interactive prompts. When complete, `gh auth token` returns a valid
token that tongs will use.

### GitLab (gitlab.com)

```bash
glab auth login
```

### GitLab (self-hosted)

```bash
glab auth login --hostname gitlab.example.com
```

### Fallback: .netrc

If neither CLI is available, tongs falls back to `~/.netrc` entries:

```
machine github.com
  login your-username
  password ghp_your_token

machine gitlab.com
  login your-username
  password glpat-your_token
```

## First run

```bash
tongs
```

On launch, tongs:

1. Scans directories under your **scan root** (defaults to `~/git`)
2. Reads each repo's git remotes to identify the forge (GitHub or GitLab)
3. Fetches open MRs/PRs using the appropriate forge API
4. Populates the inbox with three tabs: My Reviews, My MRs, All Open

!!! tip
    If tongs finds no repos, check that your scan root is correct.
    Set it in `~/.config/tongs/config.toml`:

    ```toml
    [general]
    scan_root = "~/projects"
    ```

## Configuration basics

tongs uses `~/.config/tongs/config.toml` (or the platform-appropriate config
directory). All settings have sensible defaults, so configuration is optional.

```toml
[general]
scan_root = "~/git"
scan_depth = 5
```

### Self-hosted forges

To connect to a self-hosted GitLab or GitHub Enterprise instance, add a
`[hosts.*]` section:

```toml
[hosts.work-gitlab]
hostname = "gitlab.example.com"
forge_type = "gitlab"

[hosts.work-github]
hostname = "github.corp.com"
forge_type = "github"
```

Make sure you have authenticated the `glab` or `gh` CLI against that hostname
first.

See the [Configuration reference](reference/configuration.md) for every available
setting.

## Troubleshooting

### No repos found

- Verify that your scan root contains git repositories with remotes pointing to
  GitHub or GitLab.
- Increase `scan_depth` if your repos are nested deeper than 5 levels.

### Auth errors

- Run `gh auth status` or `glab auth status` to confirm your CLI session is
  valid.
- For self-hosted instances, confirm the hostname matches exactly between your
  `config.toml` `[hosts.*]` entry and the `glab auth login --hostname` value.

### Slow startup

- Reduce `scan_depth` if tongs scans too many directories.
- Lower `max_parallel` in `[concurrency]` if you hit API rate limits.
