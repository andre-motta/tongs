---
title: Known issues
description: "Defects known in tongs 1.0.0, what you will see, and the release each fix is planned for."
---

These defects are known in tongs 1.0.0.
Each entry links to the issue that tracks the fix and names the milestone it is
planned for. Milestones can move; the issue always has the current plan.

[Troubleshooting](/desktop/troubleshooting/) covers desktop recovery states and
launch problems that have a workaround today.

## Desktop app (beta)

| Issue | Milestone | What you will see |
|---|---|---|
| [#224](https://github.com/andre-motta/tongs/issues/224) | v1.1.0 | When the desktop app's Python process stops, the Retry button on a failed read does not restart it, so every retry fails. Relaunch the app instead. |
| [#223](https://github.com/andre-motta/tongs/issues/223) | v1.1.0 | A large diff that is mostly additions renders as one block of green with no syntax color. |
| [#177](https://github.com/andre-motta/tongs/issues/177) | v1.1.0 | In the pipelines and logs view, ++slash++ does not focus the log search box. Click the box to search. |
| [#244](https://github.com/andre-motta/tongs/issues/244) | v1.0.1 | With more than 64 repositories found, every inbox tab fails with "Too many desktop requests are pending". |
| [#184](https://github.com/andre-motta/tongs/issues/184) | v1.1.0 | An empty optional forge field, such as a GitHub job with no workflow name, makes the pipeline, job, commit or review view fail to load. |
| [#183](https://github.com/andre-motta/tongs/issues/183) | v1.1.0 | ++f2++ on the job log does nothing unless a log control has focus. |
| [#250](https://github.com/andre-motta/tongs/issues/250) | Fixed for the next release | In the light theme, diff hunk headers keep a dark background. |

## Review submission

| Issue | Milestone | What you will see |
|---|---|---|
| [#231](https://github.com/andre-motta/tongs/issues/231) | v1.0.1 | A submission interrupted before it sent anything still asks you to reconcile zero steps. |
| [#232](https://github.com/andre-motta/tongs/issues/232) | Fixed for the next release | A merge the forge refused because of conflicts shows the message for a review that changed remotely. |
| [#233](https://github.com/andre-motta/tongs/issues/233) | v1.1.0 | An unexpected submission error shows the generic advice to refresh, and the cause is not logged. |
| [#251](https://github.com/andre-motta/tongs/issues/251) | v1.1.0 | Stale per-attempt review submission lock files are never deleted. |

## Credentials

| Issue | Milestone | What you will see |
|---|---|---|
| [#186](https://github.com/andre-motta/tongs/issues/186) | Fixed for the next release | tongs resolves each forge token once per session, so it does not pick up a rotated or expired token. Restart tongs after you rotate a token. The fix retries once with a fresh token after a rejected request and ships in the next release. |

## Terminal app

| Issue | Milestone | What you will see |
|---|---|---|
| [#221](https://github.com/andre-motta/tongs/issues/221) | Fixed for the next release | The status line covers the log search box, so the text you type is hidden. |
| [#222](https://github.com/andre-motta/tongs/issues/222) | v1.1.0 | ++f2++ in the log view exports raw ANSI color codes into your editor. |
| [#253](https://github.com/andre-motta/tongs/issues/253) | v1.0.1 | Retrying or cancelling a job does not refresh the job list, and ++ctrl+r++ does not either. |
| [#198](https://github.com/andre-motta/tongs/issues/198) | v1.1.0 | After the terminal app crashes, the process does not exit and you have to kill it. |
| [#255](https://github.com/andre-motta/tongs/issues/255) | Fixed for the next release | "No forges discovered yet" flashes briefly at startup before repository discovery finishes. |

## Supported platform

The desktop app targets Linux on Fedora 44, x86_64. Other distributions,
architectures and operating systems are outside the supported configuration.
[Supported platform](/reference/desktop-lifecycle/#supported-platform) in the desktop lifecycle reference has the details.
