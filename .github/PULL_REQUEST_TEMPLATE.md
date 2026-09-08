## Summary

What changed and at a high level how.

## Motivation

Why this change is needed.

## Testing

How you verified the change works. Include commands you ran or describe manual testing steps.

## Checklist

- [ ] `pytest` passes
- [ ] `ruff check src/ tests/` is clean
- [ ] `ruff format --check src/ tests/` is clean
- [ ] If `desktop/` changed, the production Electron shell builds and its tests pass: `npm ci --prefix desktop`, `npm run build --prefix desktop`, then `TONGS_TEST_PYTHON="$(command -v python)" npm test --prefix desktop`
- [ ] If `docs/` or `mkdocs.yml` changed, `mkdocs build --strict` is clean locally. No pull request check runs it; the deploy workflow only runs it after a push to `main`
- [ ] Documentation updated (if applicable)
- [ ] The Testing section above records the exact commit tested, the commands run, their results, and any check skipped along with the reason
- [ ] Reviewed [CONTRIBUTING.md](CONTRIBUTING.md) for code style and review expectations

## Targeting `feat/desktop-app`?

Use the desktop template instead of this one. Append `?template=desktop.md` to
the compare URL when opening the pull request, or replace this body with
`.github/PULL_REQUEST_TEMPLATE/desktop.md`. That template asks for the
dependency, evidence and review records the desktop workflows expect.
