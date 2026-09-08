# Architecture

## Runtime boundaries

Tongs has one Python domain and two independent UI processes:

```text
Textual views/widgets -> TUIServiceAdapter ---------+
                                                     |
Electron renderer -> preload allowlist -> main IPC -> NDJSON sidecar
                                                     |
                                                     v
                       ApplicationSession and typed services
                                     |
             ForgeRegistry -> CachedForgeClient -> ForgeClient
                                     |
                    CacheStore and DraftStore
```

`src/tongs/services/session.py:ApplicationSession` owns configuration,
repository discovery, the shared SQLite cache, durable review drafts, and the
authenticated `ForgeRegistry`. It exposes typed repository, review, discussion,
commit, diff, pipeline, job-log, event, and mutation operations. Service-issued
`RepositoryRef`, `ReviewRef`, revisions, and mutation targets keep UI code from
constructing arbitrary resources.

The terminal starts directly through `tongs` and does not start Electron or the
desktop sidecar. `src/tongs/app.py:TongsApp` owns an `ApplicationSession` and a
`src/tongs/tui_services.py:TUIServiceAdapter`. The adapter preserves terminal
models while retaining service-issued identities and revisions for later
mutations. Textual views call `app.services`; they do not import concrete forge
clients.

The production desktop starts from top-level `desktop/`. Electron main owns the
Python sidecar lifecycle and all privileged OS integrations. The preload exposes
an allowlisted, typed bridge into a context-isolated React renderer. The Python
sidecar in `src/tongs/desktop/sidecar.py` serves protocol major 1 over bounded
newline-delimited JSON on stdio. The protocol validates frames, capabilities,
method parameters, opaque session-local handles, parent associations,
revisions, paging snapshots, and cancellation before it calls the same
`ApplicationSession` services.

The renderer never receives local repository paths, credentials, Python package
paths, plugin filesystem paths, or arbitrary IPC, shell, or filesystem
authority. Its narrow external-link bridge accepts a URL from DTO or Markdown
content, but Electron main authorizes the sender and validates a bounded,
credential-free HTTPS URL immediately before the OS open. Desktop DTOs are
projections owned by `src/tongs/desktop/protocol/`; they are not the domain
models themselves.

## Source map

- `src/tongs/services/`: session-owned reads, typed mutation authorities,
  revision-bound MR and CI actions, durable review submission, and workspace
  utilities.
- `src/tongs/tui_services.py`: terminal adapter, legacy model conversion, and
  remembered service identities.
- `src/tongs/forges/`: `ForgeClient` ABC, GitHub/GitLab implementations, auth,
  HTTP transport, registry, and shared forge response models.
- `src/tongs/scanner/`: local repository discovery, safe remote parsing, and
  forge detection.
- `src/tongs/cache/`: async SQLite response cache and forge client wrapper.
- `src/tongs/state/drafts/`: persistent review draft state and recovery.
- `src/tongs/diff/`: unified diff models, conversion, parsing, rendering
  position helpers.
- `src/tongs/views/` and `src/tongs/widgets/`: Textual screens and widgets.
- `src/tongs/desktop/`: sidecar, bounded protocol, artifact contract,
  installer, asset catalog, and editor-export reservations.
- `desktop/src/main/`: Electron lifecycle, IPC admission, sidecar adapter,
  local utilities, and browser security.
- `desktop/src/preload/`: context-isolated renderer bridge.
- `desktop/src/renderer/`: React application, feature modules, navigation,
  safe Markdown, presentation, and bounded list/query helpers.
- `desktop/src/shared/`: TypeScript contracts shared across Electron processes.
- `src/tongs/plugins/`: independent terminal plugin registry and desktop
  provider registry/resources.
- `src/tongs/mcp/`: separate FastMCP stdio process and first-party terminal
  plugin.

## Repository and forge flow

1. `ApplicationSession.discover_repositories()` runs
   `scanner.discovery.discover_repos()` outside the event loop. Discovery skips
   symlinks and nested repositories, reads Git remotes, strips URL userinfo, and
   selects a primary remote.
2. The session publishes renderer-safe `RepositorySnapshot` values and retains
   actual local `Repo` paths only in trusted Python memory. The TUI adapter maps
   discovered `Repo` objects to service-issued `RepositoryRef` values.
3. A read resolves only a configured or previously issued hostname/repository.
   `ForgeRegistry.get_client()` lazily resolves credentials, creates an
   `httpx.AsyncClient`, selects `GitHubClient` or `GitLabClient`, and wraps it in
   `CachedForgeClient` when the session cache is enabled.
4. Forge implementations return shared dataclasses. The session maps failures to
   stable `ServiceError` codes and returns immutable service snapshots.
5. The TUI adapter converts snapshots to existing terminal models. The desktop
   protocol validates handles and projects snapshots to bounded DTOs.

Forge HTTP operations use `httpx`, not `gh` or `glab` per operation. The auth
cascade may invoke those CLIs once per host to read their credential stores,
then falls back to `.netrc` and the optional system keyring. Tokens stay in the
trusted Python client and must not enter logs, caches, protocol frames, renderer
state, or plugin resource declarations.

## Reviews, comments, and drafts

Review reads carry a `ReviewRevision` derived from the displayed source. Inline
positions and destructive MR actions are checked against the current admitted
review and revision before a mutation runs.

A terminal general comment, inline comment, reply, or resolve action is an
immediate mutation through `TUIServiceAdapter`. Review mode is different:
`ReviewSubmissionService` persists a `DraftSnapshot`, records a submission
attempt, sends its steps in a stable order, and retains progress for recovery or
reconciliation. Do not model an immediate comment as a draft or claim that a
draft is published merely because it was saved locally.

The desktop uses the same mutation and draft services through strict protocol
methods. The renderer supplies opaque handles and declared operation data; the
sidecar resolves trusted domain identities and returns typed receipts or safe
errors. Retrying must use the service's attempt and receipt semantics rather
than replaying a renderer request blindly.

## Diff and discussion flow

1. The session reads a revision-stable `RawDiffSnapshot` from the forge.
2. `TUIServiceAdapter.get_diff()` uses
   `src/tongs/diff/conversion.py:convert_forge_changes()` and returns terminal
   `DiffFile` objects with their revision.
3. `DiffPanel` renders file trees, syntax highlighting, context folding, comment
   gutters, and inline threads. `diff/position.py` maps selected lines to
   forge-specific anchors.
4. The desktop protocol performs its own bounded diff projection and paging in
   `src/tongs/desktop/protocol/diff_projection.py`. Paging tokens refer to
   connection-local snapshots and expire; they are not resource identifiers.

Diff and discussion reads may run concurrently. Discussion failure may leave the
TUI diff usable, but mutation code must still use the revision and service-issued
review identity retained for the displayed data.

## Pipeline and job flow

Pipelines and jobs use typed `PipelineRef` and `JobRef` values. The session
admits parent-child relationships and delegates cancel/retry operations to
`CIMutationService`. Forge capabilities determine which actions appear. Job
logs are deliberately excluded from the shared API cache.

The terminal renders ANSI logs with `RichLog` and can open a copied log in its
configured editor. The desktop fetches and pages logs through the sidecar. Copy
Review URL and external-editor export pass admitted review/job handles to narrow
Electron/main or Python utility authorities. Clear Cache is a parameterless
session-cache operation. Pipeline/job forge links and SafeMarkdown use the
preload's generic HTTPS opener, which main bounds and validates before every OS
open. None of these channels accepts a renderer shell command, filesystem path,
arbitrary clipboard value, or cache key.

## Cache ownership

`ApplicationSession` opens and closes `CacheStore` and passes it to
`ForgeRegistry`. `CachedForgeClient` caches selected read results and invalidates
related keys after mutations. The terminal Clear Cache command clears the session-owned cache through the
app compatibility alias and notifies the user. The desktop utility calls the
session clear method through its strict protocol handler, which then emits
`RESYNC_REQUIRED`. Neither path deletes durable review drafts or arbitrary
files.

The database is under the platform-specific Tongs cache directory, uses WAL,
and is created with private permissions. See the [cache guide](../cache/) for
keys, TTLs, exclusions, and tests.

## Plugin boundaries

Terminal and desktop plugins have separate entry-point groups and registries:

- `tongs.plugins` provides `TongsPlugin` commands, screens, and Textual lifecycle
  hooks through `PluginContext`.
- `tongs.desktop_plugins` provides `DesktopPluginProvider` manifests, bounded
  resources, reads, calls, events, focus targets, and navigation through a
  scoped desktop context.

Both registries check `[plugins.<name>].enabled` before import. A package may
publish both entry points under the same canonical name, but neither registry
imports the other's implementation. Installed Python, ESM, and CSS plugins are
trusted code. Contexts and declaration allowlists are supported interfaces and
fault-containment tools, not a sandbox for hostile installed code. See the
[plugin guide](../plugins/).

## MCP process

`tongs-mcp` is a separate FastMCP stdio process. It creates its own
`ForgeRegistry` lazily and exposes the small tool surface in
`src/tongs/mcp/server.py`. It does not share the TUI or desktop process lifecycle.
MCP input validation and its excluded destructive operations are part of that
server's authority boundary.

## Error handling

Forge clients map HTTP and transport failures to `ForgeError` subclasses.
Shared services translate those failures to stable `ServiceError` codes and
safe messages. The desktop protocol maps service failures to bounded protocol
errors; Electron main validates response envelopes again before exposing typed
results to the renderer. UI layers show failures and decide what to reload, but
they must not parse exception internals or expose credential-bearing command
output.

Cleanup is owned by the layer that created the resource. `ApplicationSession`
closes drafts, forge clients, and cache; Electron main terminates its sidecar and
removes listeners; plugin registries bound lifecycle cleanup. Preserve an
original failure when cleanup also fails and record the cleanup failure through
the owning typed channel.
