---
title: Contributing
description: "How to find an issue, run the tests and open a pull request for tongs."
---

tongs is made by <a href="https://alustos.us/" rel="author">Andre Motta</a> and
developed in the open at
[github.com/andre-motta/tongs](https://github.com/andre-motta/tongs), and
contributions of all sizes are welcome: bug reports, fixes, documentation, and
new ideas. The full guide is
[CONTRIBUTING.md](https://github.com/andre-motta/tongs/blob/main/CONTRIBUTING.md);
this page is the short version.

## Find something to work on

- [Good first issues](https://github.com/andre-motta/tongs/issues?q=is%3Aopen+label%3A%22good+first+issue%22)
  are small and self-contained, and most point to the file to open and the test
  to run.
- The [milestones](https://github.com/andre-motta/tongs/milestones) show what is
  planned for the next releases.
- For a larger change, open an issue first so the approach can be agreed before
  you spend time on it. Questions in issues are always welcome.

## Set up and run the tests

You need Python 3.12 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,lint,mcp]"
pytest
ruff check src/ tests/ packaging/ scripts/ .github/scripts/ examples/
ruff format --check src/ tests/ packaging/ scripts/ .github/scripts/ examples/
```

Tests mock all GitHub and GitLab traffic, so no account or token is needed.

Desktop app (beta) work also needs Node.js 22.12 or newer. See
[Desktop changes](https://github.com/andre-motta/tongs/blob/main/CONTRIBUTING.md#desktop-changes)
for the desktop build and tests.

Documentation lives in `docs/` and builds with the Astro site in `site/`, which
also needs Node.js 22.12 or newer. The build fails on a broken link or anchor:

```bash
npm ci --prefix site
npm run build --prefix site
```

See [Documentation changes](https://github.com/andre-motta/tongs/blob/main/CONTRIBUTING.md#documentation-changes)
for how to run it under a memory limit.

## Open a pull request

Keep pull requests small and focused, fill in the short template, and sign off
each commit with `git commit -s`
([Developer Certificate of Origin](https://developercertificate.org/)). CI runs
the checks that match the paths you changed, from the docs build to the full
test suite on Python 3.12 and 3.13 plus the desktop tests, and reports them in
one `CI aggregate` check. The
[CI guide](https://github.com/andre-motta/tongs/blob/main/.agents/ci/README.md#hosted-workflows)
lists which paths run which checks. A maintainer reviews the change.

## Reporting a security issue

Do not open a public issue for a vulnerability. Follow
[SECURITY.md](https://github.com/andre-motta/tongs/blob/main/SECURITY.md), and see
[Security and signing](/reference/security/) for the trust boundaries that the
report scope depends on.

## Code of conduct

The project follows the
[Contributor Covenant](https://github.com/andre-motta/tongs/blob/main/CODE_OF_CONDUCT.md).
