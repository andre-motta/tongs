## Summary

What changed and why. Link the issue it fixes, for example `Fixes #123`.

## Testing

What you ran or checked by hand.

## Checklist

- [ ] `pytest`, `ruff check src/ tests/` and `ruff format --check src/ tests/` pass
- [ ] Desktop changes: `npm run build --prefix desktop` and the desktop tests pass
- [ ] Docs changes: `npm ci --prefix site && npm run build --prefix site` passes
- [ ] If this fixes an issue listed in `docs/releases/known-issues.md`, that page is updated
- [ ] Commits are signed off (`git commit -s`)
