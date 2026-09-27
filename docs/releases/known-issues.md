---
title: Known issues
description: "Defects known in tongs 1.0.3, what you will see, and the release each fix is planned for."
---

These defects are known in tongs 1.0.3.
Each entry links to the issue that tracks the fix and names the milestone it is
planned for. Milestones can move; the issue always has the current plan.
Defects fixed in 1.0.3 are listed in the
[1.0.3 release notes](/releases/v1.0.3/).

[Troubleshooting](/desktop/troubleshooting/) covers desktop recovery states and
launch problems that have a workaround today.

## Desktop app (beta)

| Issue | Milestone | What you will see |
|---|---|---|
| [#224](https://github.com/andre-motta/tongs/issues/224) | v1.1.0 | When the desktop app's Python process stops, the Retry button on a failed read does not restart it, so every retry fails. Relaunch the app instead. |
| [#223](https://github.com/andre-motta/tongs/issues/223) | v1.1.0 | A large diff that is mostly additions renders as one block of green with no syntax color. |
| [#177](https://github.com/andre-motta/tongs/issues/177) | v1.1.0 | In the pipelines and logs view, ++slash++ does not focus the log search box. Click the box to search. |
| [#184](https://github.com/andre-motta/tongs/issues/184) | v1.1.0 | An empty optional forge field, such as a GitHub job with no workflow name, makes the pipeline, job, commit or review view fail to load. |

## Review submission

| Issue | Milestone | What you will see |
|---|---|---|
| [#300](https://github.com/andre-motta/tongs/issues/300) | v1.0.4 | A review draft can stay stuck after a failed or interrupted submission. |
| [#251](https://github.com/andre-motta/tongs/issues/251) | v1.1.0 | Stale per-attempt review submission lock files are never deleted. |

## Terminal app

| Issue | Milestone | What you will see |
|---|---|---|
| [#198](https://github.com/andre-motta/tongs/issues/198) | v1.1.0 | After the terminal app crashes, the process does not exit and you have to kill it. |

## Supported platform

The desktop app targets Linux on Fedora 44, x86_64. Other distributions,
architectures and operating systems are outside the supported configuration.
[Supported platform](/reference/desktop-lifecycle/#supported-platform) in the desktop lifecycle reference has the details.
