# Security

## Credential model

`src/tongs/forges/auth.py:resolve_token()` resolves a token lazily per host:

1. GitLab `glab auth token --hostname <host>` or GitHub `gh auth token`
   (`--hostname` for enterprise)
2. `.netrc`, with owner-only permissions required on POSIX
3. optional system keyring lookup under service `tongs` and the hostname
4. a forge-specific `AuthError`

CLI calls use argument arrays, no shell, captured output, and a five-second
timeout. Tokens live only in trusted Python memory and the `httpx.AsyncClient`
Authorization header. Never put a token in configuration, environment variables,
cache keys or values, protocol frames, renderer state, plugin manifests, logs,
command arguments, or user-visible errors.

`src/tongs/errors.py:redact_credentials()` removes known GitLab/GitHub token
prefixes and generic Bearer or `PRIVATE-TOKEN` values. All new error paths that
can contain remote or subprocess output must redact before logging or display.
Do not log raw HTTP headers.

## Host and URL admission

The forge registry accepts known GitHub/GitLab hosts, explicitly configured
hosts, and hosts admitted through local repository discovery. Scanner remote
parsing removes URL userinfo before storing a `Remote`.

`GitHubClient._repo_path_from_api_url()` checks API-provided repository URLs
against the client's own API base before making a follow-up request. Apply the
same rule to any future endpoint derived from response data. Merge-time branch
deletion also verifies the head repository equals the target repository so a
cross-fork merge cannot delete an unrelated branch.

The desktop preload exposes a narrow `openExternal(url)` channel for links in
sidecar DTOs and renderer Markdown. Electron main authorizes the sender, bounds
the value, requires credential-free HTTPS through `assertHttpsExternalUrl()`,
and only then calls `shell.openExternal`. It does not accept a shell command or
non-HTTPS scheme. Copy Review URL is a separate handle-bound operation: the
sidecar resolves the current review URL before main writes it to the clipboard.
Do not broaden either operation into arbitrary IPC, clipboard, or shell
authority.

## Shared service authority

`ApplicationSession` owns authenticated clients, discovered local paths, cache,
and drafts. It issues semantic repository/review identities and revision-bound
mutation targets. The terminal adapter and desktop protocol retain these
identities rather than accepting arbitrary hostname/project/number tuples for
writes.

Immediate comments, replies, resolves, MR actions, review submissions, and CI
mutations use narrow service command objects. Mutation services re-read required
state and check revisions, parent relationships, capabilities, and operation
identity before calling a forge. Surface `ServiceErrorCode` and safe messages to
UI code instead of forge exception internals.

## Electron and sidecar boundary

Electron main is the privileged desktop boundary. The production window uses a
private `tongs://app` origin, context isolation, a restrictive content security
policy, no service workers, and an allowlisted preload bridge. Main accepts IPC
only from the owning web contents, its main frame, and the exact app document.
It validates method names, exact parameter keys, sizes, primitive types, and
result DTOs.

The Python sidecar is launched with a trusted absolute interpreter, a verified
safe working directory, and fixed arguments. Its protocol uses bounded NDJSON
frames, a fixed protocol major, declared capabilities and methods, cancellation,
request limits, opaque connection-local handles, parent associations, and
bounded expiring paging snapshots. The renderer never receives Python package
paths, local checkout paths, resource paths, or credentials.

Treat all renderer data as untrusted even after TypeScript validation. Add new
authority as a typed, narrowly named operation with validation on both sides;
do not add an arbitrary IPC, sidecar command, path, shell, environment, or raw
forge API escape hatch.

## Subprocess rules

Several subsystems intentionally start processes:

- auth reads `gh` or `glab` credential stores
- scanner discovery reads `git remote -v`
- terminal comment, review, and job-log editors start a configured editor
- the first-party terminal MCP plugin starts `tongs-mcp`
- the desktop installer probes and launches an installed archive
- Electron main starts the Python sidecar and a configured graphical editor

Every call must use an argument vector with shell execution disabled. Bound
startup or completion when the operation owns that duration, cap captured
output, handle missing executables and timeouts, and clean up only resources the
operation created. Do not interpolate user data into a shell string.

Terminal editor commands use `shlex.split()` and private temporary files while
the Textual app is suspended. The production desktop editor accepts the trusted
configured command or supported `VISUAL`/`EDITOR` fallback, parses it without a
shell, rejects unsupported terminal-only commands, and passes only a bounded
private job-log export. Process start means "Editor started"; it does not prove
a graphical editor read the file. Tongs does not kill a user's editor on app
shutdown.

## Files, cache, and drafts

`CacheStore` creates its directory privately and its SQLite database with mode
`0o600`, uses WAL, and excludes job/stream logs. Tokens must never be part of a
cache key or value. Clear Cache removes shared API response entries; the desktop protocol handler
also emits a resync event. It does not remove durable review drafts or arbitrary
user files.

`DraftStore` is persistent application data with its own state, attempt, and
reconciliation rules. Maintain the separation between cache eviction and draft
deletion.

Desktop editor exports are bounded private files under the trusted export root.
The Python reservation ledger enforces the global file/count budget across app
processes. Electron creates files exclusively without following symlinks, keeps
the original descriptor open while live identity cleanup is possible, and
unlinks only the same device/inode. Normal completion unlinks, closes, then
releases the reservation. The resource deadline closes the held descriptor but
leaves the file and reservation for a later editor export attempt to reclaim
under the documented stale policy. Never recurse into user paths or expose
release tokens/paths to the renderer.

Installer and artifact code must preserve descriptor-based, no-follow reads,
regular-file checks, declared size/hash validation, safe archive member paths,
and atomic activation. Signature, manifest, receipt, and installed-core evidence
are separate contracts. A passing synthetic fixture does not authorize release
or installation.

## MCP boundary

`tongs-mcp` runs separately from both UIs and creates its own registry. It
exposes only the high-level tools declared in `src/tongs/mcp/server.py`; it does
not expose raw HTTP, token, shell, merge, close, reopen, or CI cancellation
primitives. `_parse_host_repo()` validates `hostname/owner/repo` input and
rejects traversal-like forms. Keep auth material out of tool inputs and outputs.

## Plugin trust

Both plugin entry-point groups load installed packages from the same Python
environment as Tongs:

- `tongs.plugins` loads terminal `TongsPlugin` code.
- `tongs.desktop_plugins` loads `DesktopPluginProvider` code and validates its
  declared ESM, CSS, and help resources.

Disabled entries are filtered before import. Manifests, IDs, compatibility,
JSON, resource paths/sizes, calls, deadlines, and lifecycle transitions are
validated, and one provider failure is isolated from other providers. These are
supported interfaces and reliability boundaries. Installed Python, ESM, and CSS
plugins remain trusted code and are not sandboxed against a malicious package.

Terminal `PluginContext` narrows the supported app interface but in-process code
can still import project modules. Desktop provider contexts expose only declared
reads, calls, events, notifications, navigation, location, and focus operations;
they do not expose credentials, raw forge clients, cache, Textual state, or
filesystem paths.

## Security test expectations

Security-sensitive changes need meaningful negative tests. Depending on the
boundary, cover unauthorized sender/frame, unknown method, extra/missing fields,
oversize/deep JSON, wrong/stale handles, wrong parent, stale revision, unsafe
URL, symlink/non-regular file, replacement race, digest/size drift, path
traversal, subprocess timeout/output bounds, and partial cleanup. Assert safe
public errors and confirm credentials or local paths do not cross the boundary.
