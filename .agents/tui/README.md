# TUI

## Runtime boundary

`src/tongs/app.py:TongsApp` is the Textual frontend for the shared Python
services. It creates or accepts an `ApplicationSession`, then constructs
`TUIServiceAdapter` as `app.services`. Screens and widgets use this adapter for
repository discovery, review reads, diffs, discussions, commits, pipelines,
logs, comments, review drafts, MR actions, and CI mutations. They do not import
or select a concrete forge client.

The terminal starts independently through the `tongs` entry point. It does not
start Electron or the production desktop sidecar. Local `Repo` paths remain in
trusted terminal/Python memory; the adapter maps them to service-issued
`RepositoryRef` values before shared-service calls.

## Textual patterns

- Use `Screen` for top-level navigation and constructor arguments for the
  selected `Repo` or `MRSummary`.
- Use named `@work` groups for asynchronous reads and mutations. Set
  `exclusive=True` when a newer refresh should cancel an older worker.
- Use messages for child-widget actions and let the owning screen call services.
- Keep loading and error state observable. Translate `ServiceError` to a clear
  notification and preserve the last usable view when a secondary read fails.
- Use reactive attributes for shared application state, and ordinary attributes
  for one screen's loading, selection, and pending-confirmation state.
- Keep bindings in `BINDINGS`; use `check_action()` when availability depends on
  drill-down level, capability, or current state.

## TongsApp lifecycle

Key attributes are:

- `config: Config`, loaded or injected before session startup
- `session: ApplicationSession`, owner of cache, drafts, forge registry, and
  service lifecycle
- `services: TUIServiceAdapter`, the supported data/mutation path for views
- `plugin_registry: PluginRegistry`, the terminal plugin registry
- `repos: list[Repo]`, the latest trusted local discovery result
- reactive `current_repo`, `current_mr_number`, `mr_filter`, and `offline`

`cache` and `forge_registry` properties are compatibility aliases to resources
owned by the started session. New UI code should use `app.services` rather than
those aliases.

On mount, the app starts the session, takes its loaded configuration, discovers
and starts terminal plugins, pushes the inbox, and starts a repository discovery
worker. `TUIServiceAdapter.discover_repositories()` assigns generations so an
older result cannot publish after a newer refresh. On unmount, the app
invalidates discovery, cancels and joins workers, runs plugin shutdown, and
closes the session.

## Command palette

`src/tongs/commands.py:TongsCommandProvider` supplies the `Ctrl+P` palette. It
combines global commands, terminal plugin commands, and commands for the active
screen. `discover()` supplies the unfiltered view; `search(query)` uses Textual's
matcher for fuzzy ranking and highlighting.

Keep palette actions and keyboard actions routed to the same screen method.
Context-specific actions must apply the same capability, state, revision, and
confirmation checks regardless of how they were invoked.

## Inbox and repository list

`InboxScreen` has My Reviews, My MRs, and All Open tabs. In global mode it uses
the service scopes across admitted hosts; in scoped mode it passes the selected
local repository to `TUIServiceAdapter.list_reviews()`. Per-host failures are
shown without discarding successful hosts. Each `MRSummary` displayed by the
adapter is remembered with its service-issued `ReviewRef` for later reads.

Tabs load lazily and use the repository generation to invalidate results after
discovery. `MRTable` owns row-to-summary mapping and sort cycling. `RepoListScreen`
filters and sorts the trusted discovered `Repo` objects, but asks the adapter to
confirm a repository reference before navigation.

Common bindings:

- Inbox: `1` to `3` select scope, `r` opens repositories or returns from scoped
  mode, `o` opens the selected review's URL in the browser, `Ctrl+R` refreshes.
- Repository list: `/` filters, `f` cycles forge, `s` cycles sort, and
  `Ctrl+R` refreshes discovery.

## MR detail

`MRDetailScreen` receives a displayed `MRSummary`. On mount it calls
`app.services.get_review()`, retaining the current `ReviewRevision` and
capabilities. Diff, commits, discussions, and pipelines load lazily through
adapter methods. Refresh clears tab caches and fetches a new detail/revision.

The screen contains:

1. Overview metadata and Markdown description
2. `DiffPanel` with unified/split display, comments, selection, and suggestions
3. commit list
4. `DiscussionPanel` with filtering, reply/resolve, and jump-to-diff
5. `PipelinePanel` with pipeline, job, and log drill-down
6. `ReviewDraftBar` and bottom-docked `CommentEditor`

Diff and discussion reads can run concurrently. The displayed diff revision is
kept with its converted files. Discussion failure may leave the diff usable,
but inline writes are still bound to the displayed revision and service-issued
review identity.

MR actions use uppercase keys and two-press confirmation. `approve`,
`unapprove`, `merge`, and `close_review` go through adapter methods that build
operation IDs, fingerprints, and revision-bound service targets. State and
capability checks run before the service call. A successful receipt triggers a
fresh detail load and invalidates the parent inbox when returning.

MR-detail bindings include `1` to `5` for tabs, `c` for an immediate general
comment, `A` approve, `U` unapprove, `M` merge, `X` close, `o` open in browser,
`Ctrl+Y` copy URL, `Ctrl+R` refresh, and `Ctrl+G` enter or inspect review mode.
Most secondary bindings are hidden from the footer and remain discoverable in
the command palette.

## Immediate comments and durable review mode

CommentEditor supports general, inline, suggestion, reply, and durable-draft
editing. With no active review draft, comment events call
`post_general_comment()`, `post_inline_comment()`, or `post_reply()` on the
adapter, and resolve actions call `resolve_discussion()`. Those operations
publish to the forge as soon as the service returns success. With an active
editable draft, comment and reply composers save draft items instead of
publishing them immediately.

Review mode is persistent and versioned:

1. `Ctrl+G` creates or opens a draft bound to the current `ReviewRevision`.
2. General, inline, reply, and verdict edits are saved through the draft store.
   Inline anchors capture source context and reject partial/truncated positions.
3. `ReviewSubmitScreen` displays the exact draft version and selected verdict.
4. Submission creates a durable attempt and advances through stable service
   steps. Its progress survives process restart.
5. Unknown or partial outcomes must be resumed or explicitly reconciled. A stale
   revision can be kept for inspection or copied into a separate current-revision
   draft; it is never silently retargeted.

Do not report a saved draft as submitted. UI retries must reuse the adapter's
known attempt identity and the service's reconciliation rules.

## DiffPanel

`DiffPanel` owns `DiffFileTree` and `DiffContent`. `DiffOptionList` maps rendered
options back to `DiffLine` values, maintains selection, indexes discussion
anchors, and inserts expanded thread blocks. `SplitDiffView` provides the split
view, built from synchronized `SplitDiffColumn` widgets. `v` toggles
unified/split mode; the same parsed files, discussion markers, and durable
draft markers feed both views.

`DiffRenderer` does one bulk Rich/Pygments highlight pass per file, then applies
line backgrounds in Textual's `VisualStyle` before option rendering. Preserve
that foreground/background split. Empty, binary, metadata-only, or forge-
truncated files remain visible with their metadata and an unavailable-content
message.

Selection keys are `J` and `K`; `c` starts an inline comment and `F3` starts a
suggestion. `]` and `[` navigate discussion anchors; `d`, `r`, and `R` toggle,
reply, and resolve. Draft markers are display-only and protect their captured
lines from context folding.

`src/tongs/views/suggestion.py` contains pure helpers. GitLab suggestion fences
describe how many original lines replacement content covers. GitHub anchors a
multi-line suggestion at the final new line and supplies
`start_line`/`start_side`. The forge clients also support explicit multi-line
API position transport for ordinary inline comments. Keep forge-specific
formatting in helpers and service mappings, not widget branches.

## DiscussionPanel

The discussion tab sorts unresolved threads first and supports all, unresolved,
and resolved filters. Inline cards use cached diff files to render source
context. Enter switches to the Diff tab, selects the file, expands the thread,
and scrolls to its line. General replies do not invent a diff anchor.

After reply or resolve, invalidate both discussion and diff state because gutter
counts and expanded thread content share the same source.

## PipelinePanel

The pipeline widget has three levels: pipelines, jobs, and job log. It stores
selection when drilling in and restores it when drilling out. `MRDetailScreen`
handles load, retry, and cancel messages through `TUIServiceAdapter`; the widget
never calls a forge client.

`C` and `R` are confirmed mutations whose target depends on the current level.
Capabilities control job-cancel availability. `o` opens the admitted pipeline or
job URL in the browser. At log level, `/` searches the rendered log and `F2`
opens a private copied log with the configured terminal editor. Job logs are not
stored in the shared API cache.

While PipelinePanel is drilled in, MR-level mutating bindings are suppressed and
Escape drills out before it pops the screen.

## External editors

Terminal editor integration is separate from the desktop graphical-editor
utility. Comment/review editors and job-log viewing create owner-only temporary
files, parse the configured editor with `shlex.split()`, invoke an argument
vector without a shell, suspend the Textual app where supported, read only the
expected file when content is returned, and clean up in `finally`.

Do not accept a renderer-supplied command or reuse the Electron editor-export
lifecycle here. On platforms where Textual suspension is unavailable, show an
actionable unsupported message instead of opening a terminal automatically.

## Adding a screen or widget

1. Put a top-level screen in `src/tongs/views/` or a reusable component in
   `src/tongs/widgets/`.
2. Define bindings and public messages before wiring parent behavior.
3. Route data and mutations through `app.services`; retain the revision or
   service target that produced displayed mutable state.
4. Use a named worker group and define what refresh/cancellation means.
5. Register a named top-level screen in `TongsApp.SCREENS` only when callers use
   name-based navigation; constructor-specific detail screens can be pushed as
   instances.
6. Add Textual tests through `app.run_test()`/Pilot and mocked service methods.

All visual status symbols need an ASCII fallback selected from
`config.ascii_mode` at presentation time. Do not put presentation glyphs in
service or forge models.

## Terminal plugins

`PluginRegistry` discovers only `tongs.plugins` entries after the session starts.
It filters disabled entries before loading them, then passes the supported
`PluginContext` to lifecycle hooks. Commands are added to the palette. Screen
mappings are collected, but plugins register or push their screens through the
supported context lifecycle rather than mutating core source.

The context exposes terminal configuration, repositories, compatibility views
of forge/cache resources, notifications, and screen navigation. Installed
plugins are trusted in-process Python code; the context is a supported interface,
not a hostile-code sandbox. Production desktop providers use the independent
`tongs.desktop_plugins` registry and do not participate in this lifecycle.
