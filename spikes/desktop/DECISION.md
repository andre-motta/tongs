# Desktop shell decision worksheet

Status: comparison in progress. This document does not select a production shell.
The CTO approved the bounded comparison; the measured result returns to the design
gate before production implementation. Fixture bridge and module interfaces are
experimental and must not become a public SDK by accident.

## Decision criteria

| Criterion | Required observation |
| --- | --- |
| Terminal independence | Production TUI remains usable without desktop runtimes; spike does not change production dependencies |
| Installation | Built distribution launches outside checkout using installed assets; record interpreter, native dependencies, build tools, download and installed sizes |
| Fedora RPM path | Upstream RPM/COPR first; account for system Python, split dependencies, desktop files, licenses, runtime updates, prepared sources and offline build gaps |
| Plugin opt-in | Independently installed Python plugin mounts bundled ESM, performs a Python call, opens bundled help; legacy hooks never run in desktop |
| Actual application | Native Fedora KDE window, interactive control results, screenshot and lifecycle proof; browser-only proof labeled separately |
| Responsiveness | Same 20,000-row fixture, bounded DOM, measured loading and process-tree RSS; report sampling method and environment |
| Accessibility | Keyboard controls, visible focus, resize behavior and light/dark mode; screen-reader and full release audit remain separate |
| Security | Trusted installed plugin code, restricted renderer bridge, no remote navigation access to privileged APIs, no supported sandbox bypass |
| Initial platform | Existing Fedora 44 KDE x86_64 system; record actual Wayland/XWayland backend. Other environments deferred by CTO |
| Maintainability | Bridge complexity, failure handling, runtime ownership, testing facilities and production migration gaps |

Cold install size, warm startup, and process-tree RSS measure different costs.
Record both shells on the same frontend build, fixture backend and plugin version.
Do not infer real forge performance from fixture results. Use actual observation
rather than ranking a shell from framework reputation.

## Packaging observations

The [official COPR guide](https://docs.copr.fedorainfracloud.org/user_documentation.html)
supports the chosen upstream distribution path: projects can submit an SRPM and
provide their own repository before inclusion in Fedora's standard repositories.
COPR still has license/content restrictions. Publishing a COPR project or enabling
automatic builds is a later upstream action, not authorized by this local spike.

The [Fedora package catalog](https://packages.fedoraproject.org/pkgs/python-pyside6/python3-pyside6/)
lists PySide6 for Fedora 44. The shell evaluation must verify the actual required
WebEngine bindings and native launch, rather than treating package availability
as proof. The [pywebview renderer documentation](https://pywebview.flowrl.com/guide/web_engine.html)
describes its Qt and GTK renderer choices. Installed system dependencies and the
larger pip wheel dependency route need separate accounting.

Fedora's current packaging-guideline pages returned an access-denied response
during research. Official-repository compliance is not claimed here and will need
a complete guideline/license/dependency review if that later target is pursued.

## Production design work after comparison

The proposed boundaries below organize the next design, not dispatched production
assignments. The CTO must approve the final interfaces and work graph.

1. Extract lifecycle and application use cases from Textual screens into shared
   async Python services using existing forge/cache/scanner/diff abstractions.
   Keep credentials in Python; publish narrow serializable application operations
   to desktop, not a raw HTTP client or arbitrary Python execution endpoint.
2. Define additive plugin capabilities using current `tongs.plugins` discovery and
   enablement. Preserve existing TUI hooks and separate desktop lifecycle, module
   contributions, compatibility, packaged assets and documentation. Installed
   plugins are trusted code. A facade is not a plugin sandbox.
3. Add durable drafts in application state storage, not the evictable response
   cache. Key by forge/repository/review/head revision, assign stable comment IDs,
   and use transactional revision checks to prevent simultaneous TUI/desktop edits
   from silently overwriting one another. Define conflict UX and migrations.
4. Design submission recovery by forge. Track per-comment outcomes for sequential
   GitLab operations. An unknown network outcome requires reconciliation before a
   retry, rather than blindly posting again. Head changes invalidate stale anchors.
   Approvals and final review submission must reflect the actual completed state.
5. Share diff alignment/position semantics across TUI and desktop while using
   surface-appropriate rendering. Side-by-side views and persistent drafts are
   required in both interfaces. Existing issues #3 and #16 remain related work.
6. Specify the production desktop bridge, long-running tasks, cancellation,
   notifications, errors, trusted local content and untrusted forge Markdown.
   Then map current review/discussion/CI features to release acceptance criteria.
7. Define reproducible wheel/sdist asset inclusion, separate desktop dependencies,
   launch commands, independently distributed plugins, then the future RPM spec
   and COPR publication pipeline under its own acceptance and publication gates.

### Candidate production work graph

These are planning identifiers, not new GitHub issues or authorized assignments.
The final shell decision and service/plugin/storage contracts must precede their
publication. Existing issues should be reused where they match the outcome.

| Item | Dependency | Outcome |
| --- | --- | --- |
| Services | Approved production design | Surface-independent lifecycle, discovery, review and CI use cases |
| Draft storage | Approved draft identity and conflict design | Durable versioned store with transactional conflict detection and recovery metadata |
| Diff alignment | Approved diff contract; related #3 | Shared side-by-side alignment preserving forge positions |
| Plugin capabilities | Services | Additive surface capabilities, desktop lifecycle and versioned contribution contract |
| TUI drafts | Services, Draft storage; related #16 | Persistent drafts and safe submission in the primary interface |
| TUI split diff | Diff alignment; related #3 | First-class terminal side-by-side rendering |
| Desktop shell | Services, Plugin capabilities, selected shell | Production bridge, trusted asset loading and lifecycle |
| Desktop review | Desktop shell, Draft storage, Diff alignment | Inbox, detail, discussions, review mutations and persistent drafts |
| Desktop CI | Desktop shell, Services | Pipelines, jobs, logs and existing action parity |
| Plugin authoring | Plugin capabilities, Desktop shell | Independent example, packaging guide, migration and in-app help |
| Release validation | All interface and plugin items | Real application parity, compatibility, failure recovery and platform evidence |
| Python distribution | Desktop shell; final verification after Release validation | Optional extra, built assets, clean install and upgrade proof |
| Fedora RPM/COPR | Python distribution, approved RPM release plan | Split packages, source inputs, clean build/install/upgrade and publication evidence |

Parallelism depends on disjoint file ownership as well as this graph. Initial
service extraction and draft-storage work may still need serialized edits to
shared models. A dependency is ready only after integration and verification,
not merely because its API proposal exists.

## Evidence and unresolved checks

Evidence is still being collected. No native shell or production release pass is
claimed by this worksheet. Existing baseline lint and dependency issues must be
reported alongside candidate results and cannot be silently treated as passing.
