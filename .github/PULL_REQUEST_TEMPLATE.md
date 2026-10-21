## Summary

What changed and why. Link the issue it fixes, for example `Fixes #123`.

## Testing

What you ran or checked by hand.

## Checklist

- [ ] `pytest`, `ruff check src/ tests/ packaging/ scripts/ .github/scripts/ examples/`, `ruff format --check` on those same paths, and `python scripts/check_control_chars.py` pass ([Making a change](https://github.com/andre-motta/tongs/blob/main/CONTRIBUTING.md#making-a-change))
- [ ] Desktop changes: `npm run format:check --prefix desktop`, `npm run lint --prefix desktop`, `npm run build --prefix desktop`, and the desktop tests pass ([Desktop changes](https://github.com/andre-motta/tongs/blob/main/CONTRIBUTING.md#desktop-changes))
- [ ] Docs changes: `npm ci --prefix site && npm run build --prefix site` passes, then the Markdown lint in [Documentation changes](https://github.com/andre-motta/tongs/blob/main/CONTRIBUTING.md#documentation-changes)
- [ ] If this fixes an issue listed in `docs/releases/known-issues.md`, that page is updated
- [ ] Commits are signed off (`git commit -s`)
