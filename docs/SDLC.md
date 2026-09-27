# Tongs SDLC profile

Tongs uses Agent SDLC **0.2.3**, maintained in the private
[agent-sdlc repository](https://github.com/andre-motta/agent-sdlc)
(source commit `25b7d7a`). The installed `agent-sdlc` skill describes the
lifecycle and workflows; this profile holds the Tongs-specific values. The
skill is an orchestration aid, not a prerequisite: contributors without it
follow [CONTRIBUTING.md](https://github.com/andre-motta/tongs/blob/main/CONTRIBUTING.md).

Small fixes skip the lifecycle: make the change, run the relevant checks, open a
PR. The lifecycle is for multi-item, architectural, or risky initiatives.

## Project settings

| Setting | Value |
| --- | --- |
| Tracker | GitHub Issues on `andre-motta/tongs`; milestones per release (for example `v1.0.1`); native sub-issue and blocking links |
| Default branch | `main`. PRs merge with merge commits |
| Integration | Local integration branch `claude/<initiative>` in a worktree; one PR into `main` per initiative, or per coherent slice when the CTO wants earlier review. A long-running shared `feat/<initiative>` branch only when the CTO asks for one |
| Worktree root | `.worktrees/` (git-ignored). Item branches `claude/<initiative>-<item>`, so they never collide with the integration branch `claude/<initiative>`. Remove worktrees when they merge |
| Setup | Checkout-local `.venv`; `python -m pip install -e ".[dev,mcp]" ruff`; desktop: `npm ci --prefix desktop` |
| Focused checks | Tests for the changed subsystem plus `ruff check` and `ruff format --check` on changed files |
| Integrated checks | `pytest`, `ruff check src/ tests/`, `ruff format --check src/ tests/`; desktop changes add `npm run build --prefix desktop` and the desktop suite; docs changes add `npm ci --prefix site && npm run build --prefix site` and the pinned markdownlint (`npm ci --prefix .github/linters`, then `.github/linters/node_modules/.bin/markdownlint-cli2 --config .github/linters/.markdownlint-cli2.jsonc "docs/**/*.md" "*.md"`), both under the Node memory guard |
| Local CI reproduction | Run the relevant `ci.yml` job locally before pushing; packaging and RPM changes use the Fedora 44 container locally (Podman) rather than iterating on hosted runs |
| Platforms | Python 3.12 and 3.13 required; desktop app on Fedora 44 KDE x86_64 required; other platforms best effort |
| Evidence level | `automated` by default; the CTO chooses `manual` for release acceptance sessions |
| Commit rules | [AGENTS.md](https://github.com/andre-motta/tongs/blob/main/AGENTS.md#git-commits): title, blank line, one-line body, `git commit -s`, co-author trailer naming the actual model |
| Upstream path | PR into `main`; the CTO merges. Tags, releases, and deployments need separate authorization |

## Agent rules

Pass these to workflows as the `rules` argument:

> Node and Electron test runs use the bounded procedure in `.agents/testing/README.md`
> (`systemd-run --user` with `MemoryMax`, `--test-concurrency=1`, never bare
> `npm test`). Assert on strings, counts, and booleans, never on DOM nodes inside
> `waitFor` or `assert`. If a run is OOM-killed, stop and report; do not retry.
> Reviewers do not run reverted-source negative controls of desktop tests.
> The core and Fedora probe JUnit checks reject any skipped test: never add
> `pytest.skip` to `tests/`. Tests that need a git work tree carry
> `@pytest.mark.needs_git`, which the Fedora probe deselects. Tests must pass
> from a checkout and from an installed wheel (the probe runs Python 3.14).
> Forge writes during testing go only to the `tongs-test-sandbox` repositories
> on GitHub and GitLab; never delete anything on a forge.

## Hardware GPU release gate

The installed desktop app must run with hardware GPU acceleration on the
supported Fedora 44 KDE x86_64 host. Evidence identifies GPU, driver, display
backend, Electron/Chromium versions, and the packaged artifact, and shows
physical acceleration in the renderer diagnostics; software renderers and
headless CI do not count. Do not relax SELinux or the renderer sandbox to pass.
Rerun the gate after graphics, runtime, launch, or packaging changes and on
release artifacts. This is a release gate, not a per-PR check.

## Publication effects

- PRs and pushes to `main` run CI (`ci.yml`). A PR runs only the lanes its
  changed paths select, with the `CI aggregate` as the required check; pushes
  to `main` and the `ci:full` label run the full graph. The desktop release
  workflow runs on its own path filters and on stable `vX.Y.Z` tags.
- Pushes to `main` deploy the Astro + Starlight site built from `site/`.
  Everything under `docs/` is public except this file, `docs/site-plan.md`, and
  `docs/work/`, which the content collection glob in `site/src/content.config.ts`
  excludes.
- Stable `vX.Y.Z` tags publish to PyPI (environment `pypi`) and build the desktop GitHub
  Release. A failed draft lookup is handled per the release runbook; never
  delete releases or tags.

## Records

Git and GitHub are the record: issues hold scope and acceptance, PRs hold
evidence. The orchestrator's checkpoint lives at `.worktrees/CHECKPOINT.md` and is
never committed. Decisions from the desktop initiative (v1.0.0) remain in
[docs/work/](work/desktop.md) as history; new initiatives do not add records there.
