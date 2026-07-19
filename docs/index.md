---
hide:
  - navigation
  - toc
---

<div class="hero" markdown>

![tongs](assets/hero-banner.png){ width="600" }

# One TUI. Every forge. Full review.

A terminal-native code review inbox for developers who work across GitHub and GitLab.

[Get started](getting-started.md){ .md-button .md-button--primary }
[View on GitHub](https://github.com/andre-motta/tongs){ .md-button }

</div>

---

Your team uses GitHub. Another uses GitLab. You live in the terminal.
tongs gives you a single review inbox across forges, with syntax-highlighted
diffs and inline comments, right where you already work.

> lazygit is great for git operations. tongs picks up where it stops -- at code review.

---

## Multi-forge inbox

GitHub PRs and GitLab MRs in one view, auto-detected from your git remotes.
Three tabs let you triage everything you need to review.

```
 Inbox ─────────────────────────────────────────────────────────
  [My Reviews]  My MRs  All Open

  GL  platform/auth-service    !482  Fix OAuth token refresh     ● CI passed    3 comments
  GH  andre-motta/tongs        #127  Add pipeline retry action   ● CI passed    1 comment
  GL  platform/api-gateway     !901  Rate limiter middleware     ◌ CI running   0 comments
  GH  infra/helm-charts        #44   Bump ingress controller    ● CI passed    5 comments
  GL  ml/training-pipeline     !223  Fix GPU memory leak        ✗ CI failed    2 comments
```

Zero configuration. tongs scans `~/git`, reads your git remotes, and populates
the inbox. Auth tokens come from your existing `gh` and `glab` CLI logins.

```toml
# ~/.config/tongs/config.toml (optional -- defaults work out of the box)
[general]
scan_root = "~/git"
scan_depth = 5
```

---

## Real diffs in the terminal

Split-pane viewer with a file tree, syntax highlighting across 500+ languages,
and word-level diff highlighting that shows exactly what changed.

```
 Diff ──────────────────────────────────────────────────────────
  src/
  ├─ M  auth.py          │  @@ -42,7 +42,9 @@ def validate_token(token: str):
  ├─ M  config.py         │       if token.expired:
  ├─ A  middleware.py      │  -        return None
  └─ D  legacy.py          │  +        logger.warning("Token expired for %s", token.sub)
                           │  +        raise TokenExpiredError(token.sub)
                           │  +
                           │       claims = decode_jwt(token.raw)
                           │       return claims
```

Context folding collapses unchanged sections. Markdown files get a rendered
preview toggle with ++m++.

---

## Inline comments and suggestions

Select lines, write comments, and post suggested changes without leaving the
terminal.

```
 Comment ───────────────────────────────────────────────────────
  src/auth.py:44

  > This should also revoke the refresh token to prevent reuse.
  > Consider calling `revoke_refresh(token.jti)` here.

  [Ctrl+S] Submit    [F2] External editor    [F3] Suggest change    [Esc] Cancel
```

Discussion threads render inline in the diff gutter. Reply and resolve from the
keyboard. Suggested changes open your `$EDITOR` with the selected code -- edit
it, and tongs posts a suggestion block using the forge's native syntax.

---

## Pipeline drill-down

Browse pipelines, drill into jobs grouped by stage, read full ANSI-rendered logs.
Cancel or retry directly from the TUI.

```
 Pipeline #18204 ── passed ─────────────────────────────────────
  Stage: build
    ● build-linux     passed    2m 14s
    ● build-macos     passed    3m 01s

  Stage: test
    ● unit-tests      passed    1m 42s
    ● integration     passed    4m 33s

  Stage: deploy
    ◌ staging         running   0m 45s

  [Enter] View log    [R] Retry    [C] Cancel    [o] Open in browser
```

---

## Built for the keyboard

Every action is reachable from the keyboard. A few highlights:

| Key | Action |
|-----|--------|
| ++ctrl+p++ | Command palette (fuzzy search across all actions) |
| ++1++ - ++5++ | Switch MR detail tabs |
| ++c++ | Comment on current line or selection |
| ++f3++ | Suggest changes (opens `$EDITOR`) |
| ++a+shift++ | Approve (double-press to confirm) |
| ++m+shift++ | Merge (double-press to confirm) |
| ++j++ / ++k++ | Navigate everywhere |

[Full keybinding reference](reference/keybindings.md){ .md-button }

---

## tongs is for you if

- You review code across both GitHub and GitLab
- You want diffs, inline comments, and approvals in your terminal
- You manage many repos and want a single inbox
- You prefer keyboard-driven workflows over browser tabs

---

## Comparison

| Feature | tongs | gh-dash | GitHub/GitLab web |
|---|---|---|---|
| GitHub + GitLab | Yes | GitHub only | One at a time |
| Terminal-native diffs | Yes | No | No |
| Inline comments | Yes | No | Yes |
| Discussion threads | Yes | No | Yes |
| Suggested changes | Yes | No | Yes |
| Pipeline / CI drill-down | Yes | No | Yes |
| Approve / Merge | Yes | No | Yes |
| Command palette | Yes | No | N/A |
| Zero config auth | Yes | Yes | N/A |
| Self-hosted forges | Yes | Yes | N/A |

---

<div class="links" markdown>

[PyPI](https://pypi.org/project/tongs/) --
[GitHub](https://github.com/andre-motta/tongs) --
[License (MIT)](https://github.com/andre-motta/tongs/blob/main/LICENSE)

</div>
