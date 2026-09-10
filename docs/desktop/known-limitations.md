# Known limitations

!!! warning "Unreleased feature"

    This page describes the state of the unreleased v1.0.0 candidate. There is
    no public desktop artifact, production tag, or package to install yet.

These defects are known and are shipping in v1.0.0. They were found by the
whole-product acceptance session and were triaged as things to fix and release
after v1.0.0, rather than things to hold it for. Each entry links to the issue
that tracks the fix and names the milestone it is currently assigned to.

Read this page together with [Troubleshooting](troubleshooting.md), which covers
recovery states and launch problems that have a workaround today.

## Desktop workspace

| Issue | Milestone | What you will see |
|---|---|---|
| [#228](https://github.com/andre-motta/tongs/issues/228) | v1.0.1 | Merge, Close, Reopen and Unapprove are reachable only from the Discussions tab, so they are not discoverable from the rest of the review. |
| [#224](https://github.com/andre-motta/tongs/issues/224) | v1.1.0 | When the sidecar dies, the Retry button on a failed read never restarts it, so retrying fails forever; relaunch the application instead. |
| [#223](https://github.com/andre-motta/tongs/issues/223) | v1.1.0 | A large, addition-heavy diff renders as an unbroken block of green with no syntax colouring. |
| [#177](https://github.com/andre-motta/tongs/issues/177) | v1.1.0 | In the pipelines and logs view, `/` does not focus the log search box; click the box to search. |

## Review submission

| Issue | Milestone | What you will see |
|---|---|---|
| [#231](https://github.com/andre-motta/tongs/issues/231) | v1.1.0 | A submission interrupted before it dispatched anything still asks you to reconcile zero steps. |
| [#232](https://github.com/andre-motta/tongs/issues/232) | v1.1.0 | A merge the forge refused because of conflicts is reported with the message for a review that changed remotely. |
| [#233](https://github.com/andre-motta/tongs/issues/233) | v1.1.0 | An unclassified submission refusal shows the generic advice to refresh, and stale per-attempt submission lock files are never cleaned up. |

## Credentials

| Issue | Milestone | What you will see |
|---|---|---|
| [#186](https://github.com/andre-motta/tongs/issues/186) | v1.1.0 | A forge client and its token are resolved once per session, so a rotated or expired credential is never picked up; restart the application after rotating a token. |

## Terminal

| Issue | Milestone | What you will see |
|---|---|---|
| [#221](https://github.com/andre-motta/tongs/issues/221) | v1.1.0 | The status bar overlaps the log search box, so the text you type into it is hidden. |
| [#222](https://github.com/andre-motta/tongs/issues/222) | v1.1.0 | `F2` in the log view exports raw ANSI escape sequences into your editor. |

## Acceptance coverage

The v1.0.0 acceptance rerun did not execute every planned scenario. The native
GPU verifier policy run, the remaining install-lifecycle and terminal scenarios,
and the plugin, packaging and regression groups were not run at the release
candidate head. Issue
[#234](https://github.com/andre-motta/tongs/issues/234) tracks running them for
v1.0.1. Treat the native support boundary on the
[installation page](installation.md) as the tested boundary, not as a claim that
every scenario behind it has been executed.
