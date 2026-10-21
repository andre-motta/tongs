---
title: Releases
description: "Every tongs release, with its notes and the issues known in it."
---

tongs is published to [PyPI](https://pypi.org/project/tongs/). Each version
also has a [GitHub Release](https://github.com/andre-motta/tongs/releases),
which carries the desktop app beta: the per-user archive with its attestation,
and unsigned Fedora RPMs.

| Version | Date | Notes |
|---|---|---|
| 1.1.1 | 2026-10-20 | [Release notes](/releases/v1.1.1/). A patch release: the inbox stays current after you act on a review, the command palette no longer stacks screens or loses typed text, review threads show on expanded lines, and archived projects leave Reviews and My MRs. |
| 1.1.0 | 2026-10-12 | [Release notes](/releases/v1.1.0/). A feature release: a paged All Open tab with CI for every list, expandable diff context and local diffs, faster and highlighted desktop diffs with Viewed files, working terminal settings, a validated config file, a desktop colour scheme and a safer `tongs-mcp`. |
| 1.0.5 | 2026-10-10 | [Release notes](/releases/v1.0.5/). A larger patch release: correct CI status, merged state and unresolved counts, clear rate limits, fresh review lists on Refresh, expiring confirmations, a parallel All Open tab and a desktop installer that keeps working as releases accumulate. |
| 1.0.4 | 2026-10-10 | [Release notes](/releases/v1.0.4/). Paused review submissions can return to editing, GitLab merge requests with over 1,000 changes open again, terminal key fixes and a release gate on tags. |
| 1.0.3 | 2026-09-27 | [Release notes](/releases/v1.0.3/). A paged desktop inbox for large repositories, and no more crashes from damaged drafts, long discussions or bracketed titles. |
| 1.0.2 | 2026-09-27 | [Release notes](/releases/v1.0.2/). Two credential security fixes, link destinations in the desktop app and pinned release builds. |
| 1.0.1 | 2026-09-27 | [Release notes](/releases/v1.0.1/). First patch release: token refresh, pipeline refresh fixes, review actions on every desktop tab and inbox tabs for any number of repositories. |
| 1.0.0 | 2026-09-10 | [Release notes](/releases/v1.0.0/). First stable release, split diffs, durable review drafts and the first desktop app beta. |

## Known issues

[Known issues](/releases/known-issues/) lists the defects known in the latest
release, what you will see, and the milestone each fix is planned for.

## What comes next

Planned work is tracked in the
[milestones](https://github.com/andre-motta/tongs/milestones). v1.1.2 collects
the next fixes.

## Upgrade

Upgrade the terminal app with the tool you installed it with:

```bash
pipx upgrade tongs
```

With uv, run `uv tool upgrade tongs`. With pip, run
`pip install --upgrade tongs`. If you use the per-user desktop app, install the desktop
release that matches the upgraded core:

```bash
tongs desktop update
```

The Fedora RPMs are not updated by `dnf` on its own. Download the next
release's RPMs and install them together with `dnf install ./*.rpm`.
