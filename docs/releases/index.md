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
| 1.0.1 | 2026-09-27 | [Release notes](/releases/v1.0.1/). First patch release: token refresh, pipeline refresh fixes, review actions on every desktop tab and inbox tabs for any number of repositories. |
| 1.0.0 | 2026-09-10 | [Release notes](/releases/v1.0.0/). First stable release, split diffs, durable review drafts and the first desktop app beta. |

## Known issues

[Known issues](/releases/known-issues/) lists the defects known in the latest
release, what you will see, and the milestone each fix is planned for.

## What comes next

Planned work is tracked in the
[milestones](https://github.com/andre-motta/tongs/milestones). v1.1.0 collects
the next round of improvements.

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
