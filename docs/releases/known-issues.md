---
title: Known issues
description: "Defects known in tongs 1.1.1, what you will see, and the release each fix is planned for."
---

These defects are known in tongs 1.1.1.
Each entry links to the issue that tracks the fix and names the milestone it is
planned for, or says it is not yet scheduled. Milestones can move; the issue
always has the current plan.
Defects fixed in 1.1.1 are listed in the
[1.1.1 release notes](/releases/v1.1.1/).

[Troubleshooting](/desktop/troubleshooting/) covers desktop recovery states and
launch problems that have a workaround today.

## Desktop app (beta)

| Issue | Milestone | What you will see |
|---|---|---|
| [#223](https://github.com/andre-motta/tongs/issues/223) | Not yet scheduled | A long run of added lines, such as a new file, reads as one solid green block. Syntax colour helps, but it is off for files over 2,000 diff lines or 256 Ki characters. |
| [#177](https://github.com/andre-motta/tongs/issues/177) | Not yet scheduled | After you click the page background, ++slash++ no longer focuses the log search box, ++f2++ no longer opens the log in your editor, and the review keys stop working. Click the log search box to search. |

## Supported platform

The desktop app targets Linux on Fedora 44, x86_64. Other distributions,
architectures and operating systems are outside the supported configuration.
[Supported platform](/reference/desktop-lifecycle/#supported-platform) in the desktop lifecycle reference has the details.
