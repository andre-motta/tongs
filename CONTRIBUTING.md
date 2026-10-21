# Contributing to tongs

Thanks for your interest in tongs! Bug reports, fixes, docs improvements, and
new ideas are all welcome, and you do not need to know the whole codebase to
help.

## Finding something to work on

- Issues labelled
  [good first issue](https://github.com/andre-motta/tongs/issues?q=is%3Aopen+label%3A%22good+first+issue%22)
  are small and self-contained, and most say which file to open and which test
  to run.
- Issues in the current
  [milestones](https://github.com/andre-motta/tongs/milestones) are what we plan
  to ship next.
- Found a bug? [Open an issue](https://github.com/andre-motta/tongs/issues/new/choose).
- For a larger change or a new feature, open an issue first so we can agree on
  the approach before you spend time on it.

Leave a comment on an issue if you want to pick it up or have a question. Asking
is always fine.

## Setting up

You need Python 3.12 or newer. From the root of your clone:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,lint,mcp]"
```

With `uv`, use `uv venv --python 3.12` and `uv pip install` instead. Tests mock
all GitHub and GitLab traffic, so you do not need a forge account or token to
run them.

Working on the desktop app also needs Node.js 22.12 or newer:

```bash
npm ci --prefix desktop
```

## Making a change

1. Create a branch and make your change.
2. Run the tests closest to what you changed, for example
   `pytest tests/test_widgets/test_pipeline_panel.py -q`.
3. Before opening the pull request, run:

   ```bash
   pytest
   ruff check src/ tests/ packaging/ scripts/ .github/scripts/ examples/
   ruff format --check src/ tests/ packaging/ scripts/ .github/scripts/ examples/
   python scripts/check_control_chars.py
   ```

   Run `ruff format <file>` to fix formatting. The last command fails if a
   text file holds a NUL byte or another control character other than tab,
   line feed and CRLF line endings.
4. Open a pull request and fill in the short template. CI runs the checks for
   the paths you changed, and the `CI aggregate` check reports the result. A
   maintainer will review it.

Small, focused pull requests are easier to review and land faster than large
ones.

### Desktop changes

The desktop build (`npm run build --prefix desktop`) and its tests
(`npm test --prefix desktop`, with `TONGS_TEST_PYTHON` set to your Python) can
use a lot of memory, so run them under the memory limit in the
[testing guide](.agents/testing/README.md#bounded-local-node-procedure). It
shows how to run the build and a single test file, which is usually all you
need while developing. The `test` script runs one test file at a time. In
renderer tests, assert on text, attributes, and counts rather than on DOM nodes:
a failing assertion on a DOM node can use more than a gigabyte of memory while
it formats the error.

The desktop sources are formatted by Prettier with its default options and
linted by ESLint with typescript-eslint's recommended rules. Both live in their
own pinned package, so install it once and run the checks from the root of
your clone:

```bash
npm ci --prefix desktop/tools/lint
npm run format:check --prefix desktop
npm run lint --prefix desktop
```

`npm run format --prefix desktop` fixes formatting in place. One commit
reformatted the whole desktop tree; to keep it out of `git blame`, run
`git config blame.ignoreRevsFile .git-blame-ignore-revs` once.

### Pre-commit hooks

`.pre-commit-config.yaml` runs the same Ruff, control-character and desktop
checks as CI before each commit. With your `.venv` active (it provides the
pinned Ruff) and the desktop lint package installed (it needs Node.js 22.13
or newer):

```bash
python -m pip install pre-commit
pre-commit install
```

### Documentation changes

Docs live in `docs/` and are published to [tongs.tools](https://www.tongs.tools)
by the Astro site in `site/`. A pull request that changes them runs the docs
check, which builds the site and lints the Markdown. Building it needs Node.js
22.12 or newer, and the build fails on any broken link or anchor. Build it
locally first:

```bash
npm ci --prefix site
npm run build --prefix site
```

The pages land in `site/dist`. CI runs the same build on pull requests that
touch the docs. Like the desktop suite, it is best run under the memory limit in
the [testing guide](.agents/testing/README.md#bounded-local-node-procedure).
The site build is the exception there: use a task limit of 256, not 64.

The same docs job then lints Markdown with the pinned linter in
`.github/linters`. Run it from the root of your clone:

```bash
npm ci --prefix .github/linters --ignore-scripts --no-audit --no-fund
.github/linters/node_modules/.bin/markdownlint-cli2 --config .github/linters/.markdownlint-cli2.jsonc "docs/**/*.md" "*.md" ".agents/**/*.md" ".github/*.md" "site/README.md"
```

## Code style

- Put `from __future__ import annotations` at the top of every module.
- Type every parameter and return value.
- Use module-level imports, unless a local import is needed to avoid a circular
  dependency.
- Use frozen dataclasses for immutable data and regular dataclasses for mutable
  state.
- Comments explain why, not what.
- Avoid em dashes in prose and commit messages.

## Commits

Write a short title, a blank line, and a one-line description:

```text
Fix log search input hidden behind status bar

Dock the search input and status line in one container.
```

Sign off your commits with `git commit -s`. This adds a `Signed-off-by` line
certifying the [Developer Certificate of Origin](https://developercertificate.org/),
which is required for every commit.

If an AI assistant wrote part of a commit, credit it with a
`Co-Authored-By:` trailer naming the actual tool and model.

## Where things are

```text
src/tongs/
  services/        # Shared application logic used by both frontends
  forges/          # GitHub and GitLab clients, authentication, HTTP
  scanner/         # Finds local repositories and parses their remotes
  cache/           # SQLite response cache
  diff/, state/    # Diff models and review drafts
  views/, widgets/ # Terminal UI (Textual) screens and widgets
  desktop/         # Python side of the desktop app, installer
  plugins/         # Plugin registries
  mcp/             # Optional MCP server
desktop/src/       # Desktop app (Electron main process and React UI)
tests/             # Mirrors the source layout
```

Both frontends talk to forges only through `ApplicationSession` in
`src/tongs/services/session.py`. The terminal UI calls it directly, and the
desktop app reaches it through a Python helper process (the "sidecar").

Each subsystem has a short guide under [`.agents/`](.agents/) (for example
[TUI](.agents/tui/README.md), [forges](.agents/forges/README.md), and
[testing](.agents/testing/README.md)). These guides are written for both people
and coding assistants. [AGENTS.md](AGENTS.md) gives the overview.

### Adding a forge backend

1. Implement `ForgeClient` in `src/tongs/forges/your_forge.py`, returning the
   shared models from `src/tongs/forges/models.py`.
2. Update remote detection in `src/tongs/scanner/remote.py` and client creation
   in `src/tongs/forges/registry.py`.
3. Add a credential source to `src/tongs/forges/auth.py`.
4. Add mocked tests under `tests/test_forges/` and `tests/services/`.

### Writing a plugin

Terminal plugins use the `tongs.plugins` entry point and desktop plugins use
`tongs.desktop_plugins`. See the [plugin guide](docs/extend/terminal-plugins.md), the
[desktop provider guide](docs/extend/desktop-providers.md), and the
[desktop example](examples/desktop-plugin/README.md).

## Maintainers

CI workflows, which checks a change runs, release steps, packaging checks, and
what to do when a CI job fails are covered in the
[CI and release guide](.agents/ci/README.md).

## Security

Do not report vulnerabilities in a public issue. Follow [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your contributions will be licensed under the
MIT License.
