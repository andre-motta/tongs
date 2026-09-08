# Contributing

Tongs is developed in the open at
[github.com/andre-motta/tongs](https://github.com/andre-motta/tongs).
[CONTRIBUTING.md](https://github.com/andre-motta/tongs/blob/main/CONTRIBUTING.md)
in the repository is the authoritative contributor guide; this page summarizes it
so the site links to a single source instead of restating it.

Check the [open issues](https://github.com/andre-motta/tongs/issues) for current
work, and discuss a new direction in an issue before starting a large change.

## Development setup

Tongs requires Python 3.12 or newer. Run commands from the checkout root and keep
the virtual environment inside that checkout:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,mcp]" ruff
```

`uv venv --python 3.12` and `uv pip install` are valid equivalents. The `mcp`
extra is needed so the MCP tests execute rather than skip on import.

Building the production desktop shell additionally requires Node.js 22.12 or
newer, with locked dependency installation through `npm ci --prefix desktop`.

## Checks before you open a pull request

Python changes:

```bash
pytest tests/ --ignore=tests/test_mcp -v
ruff check src/ tests/
ruff format --check src/ tests/
```

Desktop shell changes also build and test the Electron and React shell:

```bash
npm ci --prefix desktop
npm run build --prefix desktop
TONGS_TEST_PYTHON="$(command -v python)" npm test --prefix desktop
```

Documentation changes build the site strictly:

```bash
mkdocs build --strict
```

`CONTRIBUTING.md` lists the remaining suites, including the MCP report check, the
comparison fixtures, the installable provider example, the Fedora container
harness, and the memory bounds required for local Node work.

## Code style

- Format and lint Python with Ruff before submitting.
- Type every parameter and return value, and put
  `from __future__ import annotations` at the top of each module.
- Use module-level imports unless a function-level import is required, such as to
  avoid a circular dependency.
- Use frozen dataclasses for immutable data and regular dataclasses for mutable
  state.
- Write commits as a title, a blank line, and a one-line description body, and
  commit with `git commit -s`. Do not use em dashes in prose or commit messages.

## Pull request flow

Open the pull request with its dependencies, the exact tested commit, the checks
you ran, and functional evidence. Pull requests run
`.github/workflows/ci.yml`, whose required `Desktop pre-merge aggregate` accepts a
revision only when Ruff, the Python 3.12 and 3.13 core and MCP tests, the desktop
fixture and production shell tests, and the Fedora 44 Podman probe all succeed. A
skipped, cancelled, missing, or failed required job fails the aggregate, and a new
commit supersedes earlier results.

Every change is reviewed for architecture, security, UX, and quality engineering.

!!! warning "Unreleased feature"

    The production desktop release assembly gate is separate from the pre-merge
    aggregate. Native hardware-accelerated Electron on the supported Fedora host
    is its own gate. A fixture, Podman, or headless run is not native, GPU,
    installer, signing, RPM, or release acceptance.

## Reporting a security issue

Do not open a public issue for a vulnerability. Follow
[SECURITY.md](https://github.com/andre-motta/tongs/blob/main/SECURITY.md).

## Code of conduct

The project follows the
[Contributor Covenant](https://github.com/andre-motta/tongs/blob/main/CODE_OF_CONDUCT.md).
