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
| Portability | Fedora KDE Wayland, X11 and Ubuntu GNOME individually marked observed or unverified |
| Maintainability | Bridge complexity, failure handling, runtime ownership, testing facilities and production migration gaps |

Cold install size, warm startup, and process-tree RSS measure different costs.
Record both shells on the same frontend build, fixture backend and plugin version.
Do not infer real forge performance from fixture results. Use actual observation
rather than ranking a shell from framework reputation.

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

## Evidence and unresolved checks

Evidence is still being collected. No native shell or production release pass is
claimed by this worksheet. Existing baseline lint and dependency issues must be
reported alongside candidate results and cannot be silently treated as passing.
