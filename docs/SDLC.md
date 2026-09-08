# Tongs SDLC profile

Tongs adopts Agent SDLC **0.1.0**, maintained in the private
[agent-sdlc repository](https://github.com/andre-motta/agent-sdlc).
Source commit: `4e851d1b8a903aa8bebceea078860a21152ee8e8`.
Read the installed `agent-sdlc` skill's `references/workflow.md` with this profile.
Contributors without access to the private package can still follow the project
review process in [CONTRIBUTING.md](https://github.com/andre-motta/tongs/blob/feat/desktop-app/CONTRIBUTING.md); the private skill is an
orchestration aid, not a prerequisite for ordinary contributions.

## Project settings

| Setting | Value |
| --- | --- |
| Repository / tracker | `andre-motta/tongs`, GitHub Issues; reuse existing issues and native sub-issue/blocking links where available |
| Integration target | Desktop: `feat/desktop-app`, owned by Astra. Final PR targets `main` for CTO review |
| Worktrees | Isolated worktree per item; desktop contributors use `feat/desktop-<issue>-<slug>`. Existing comparison `codex/` branches remain historical |
| Runtime | Python 3.12+; current CI tests 3.12 and 3.13 |
| Setup | Create checkout-local `.venv`; activate per shell call; install editable `.[dev]` plus Ruff; add `mcp` extra for MCP tests |
| Focused checks | Tests for changed subsystems, plus relevant lint/format checks |
| Integrated checks | `pytest`, `ruff check src/ tests/`, `ruff format --check src/ tests/`; include optional dependencies required by changed features |
| Documentation checks | Local links, command examples, `git diff --check`; `mkdocs build --strict` for site inputs/navigation |
| Functional proof | Exercise affected TUI/desktop workflows and attach observable evidence; distinguish mocked APIs from live forge calls |
| Commit format | Title, blank line, one-line body; use `git commit -s`; Codex co-author uses `noreply@openai.com` |
| Upstream path | Desktop agent PRs into `feat/desktop-app`, Astra integrates; final feature PR into `main`, CTO gates final acceptance and merge. Tags/releases/deployments need separate authority |
| Initiative records | `docs/work/<initiative>.md`; keep public-safe summaries and use access-appropriate locations for sensitive artifacts |

Use `python -m pip install -e ".[dev,mcp]" ruff` in the activated environment for
full core/MCP validation. The test suite mocks forge traffic. Installation may
need network access; a skipped optional test is not proof of that subsystem.

## Authority and integration

Andre approved adoption of this workflow. Astra owns overarching architecture,
design, writing direction, scheduling, and integration. Sol at `high` performs
senior implementation and independent review; Luna at `xhigh` handles bounded
assignments under Sol review. A Sol author has a separate Sol reviewer.

Approved initiatives allow signed-off local commits and validated local
integration without per-commit approval. Preserve unrelated changes and dirty
or occupied default-branch checkouts; leave promotion pending when necessary.
Use the existing four review areas: architecture, security, UX, and QE.

## Desktop project override, approved 2026-09-07

This Tongs-specific policy implements the CTO's explicit authorization in
[issue #23](https://github.com/andre-motta/tongs/issues/23). It overrides the base
workflow's `codex/` branch convention, contributor push prohibition, local-main
promotion and per-push CTO gate for this initiative only. It does not change the
reusable private SDLC package or other projects. Architecture scope approval and
independent review still apply.

Astra owns `feat/desktop-app` on GitHub as the shared integration branch and final
engineering review gate. Initialize it from the independently reviewed comparison
and evidence at `58cf120`, plus this reviewed policy update. This one-time bootstrap
is explicitly recorded; subsequent contributor changes enter through PRs. Main's
dirty checkout is preserved. No contributor may push directly to the integration
branch or main, merge PRs, create releases, or change shared tracking independently.

Contributors create `feat/<work-item>` branches from the latest verified integration
commit in separate worktrees, commit with `git commit -s`, push their assigned
branches and open PRs with base `feat/desktop-app`. No repeated CTO approval is
needed for those scoped pushes or PRs. Use the actual assigned model, a title and
one-line commit body, and the OpenAI co-author address. After review, agents push
corrections to their own branches. Keep published history intact; prefer merging
updated integration history rather than force-pushing shared or reviewed commits.

Independent Sol high review is required, including for Luna work and another Sol's
implementation. Astra reviews the resulting PR, directs corrections, checks that
required checks and relevant functional evidence apply to the exact current head,
and serializes integration. Only Astra merges to `feat/desktop-app`. Prefer merge
commits to preserve signed-off contributor commits and dependency history. Astra
may resolve conflicts locally in an isolated worktree; resolutions invalidate
affected checks/reviews and must be verified before the resolved result is pushed.
Record the resolution and reviewer decision in the linked issue/PR. A changed PR
head requires renewed affected review and checks; never merge an unreviewed head.

These model roles are an agent workflow, not separate GitHub identities. Record
Sol's independent findings and Astra's disposition in the PR even when tool calls
share the owner's GitHub account. Do not claim GitHub-enforced branch protection
unless it has actually been configured and verified.

When the feature is complete, Astra opens a PR from `feat/desktop-app` into `main`
for Andre's final review. Creating that PR is authorized; merging it, pushing main,
tagging, releasing or deploying is not. The final PR must retain or link durable
acceptance artifacts: acceptance criteria and observed results; exact tested
commits and artifact hashes; environment and commands; CI and native application
proof; GPU evidence; plugin/TUI compatibility; independent findings and resolutions;
limitations, skipped/failed checks, upgrade/recovery implications and proposed
publication actions. Distinguish synthetic demonstrations from real operations.

## Issue-driven decomposition and scheduling

Every change must be traceable to an issue, including code, tests, documentation,
workflow/CI changes, design decisions, review corrections and conflict resolutions.
Use an existing scoped issue when appropriate; create a separate child when a change
has its own acceptance, ownership or dependency boundary. Do not create a new issue
for each incidental line edit. Each PR and substantive commit references its issue.
Astra updates issue progress at assignment, handoff, review/correction, integration,
gate changes and interruption, including exact commits and next actions.

Break initiatives into small, independently verifiable items to maximize useful
parallel work. Each item uses the [work-item template](work/templates/desktop-item.md)
and records scope/exclusions, owner/model, files/interfaces, prerequisites,
acceptance criteria, checks, artifacts, branch/PR and Git authority. Publish native
GitHub parent/child and blocking links, with readable dependency lists. Keep the
graph acyclic. Split compatible tests, fixtures, documentation and implementation
when they have independently stable interfaces; serialize overlapping ownership.

An item is ready only when its required design is approved, interfaces and ownership
are clear, and every prerequisite merge commit is present in `feat/desktop-app`
and verified. Open/closed issue status alone is not a readiness signal. Independent
investigations may proceed together when their inputs are stable, while their PRs
still must satisfy required integration checks. Astra schedules ready items within
available concurrency, queues excess work, and reschedules after every integration
or newly discovered dependency. A blocker is recorded separately from progress.

For this initiative, track:
`planned -> ready -> assigned -> review -> feature integrated -> CTO accepted -> main delivered`.
Record local candidate integration and the actual remote merge SHA separately.
A merge to the feature branch is not delivery to main. Keep implementation issues
open until the final accepted change lands in main; use `Refs #...` in intermediate
PRs rather than premature closing language. Preserve rejected/interrupted worktrees,
review findings, assignments, evidence and next actions for safe resumption.

Use the [desktop PR template](https://github.com/andre-motta/tongs/blob/feat/desktop-app/.github/PULL_REQUEST_TEMPLATE/desktop.md)
for intermediate work. Baseline CI/lint/dependency failures remain unmet; branch
creation does not waive them. CI readiness and hardware GPU work are separate,
issue-tracked items that can be investigated independently after bootstrap.

### Periodic branch and worktree cleanup

The CTO authorized periodic cleanup in
[issue #23](https://github.com/andre-motta/tongs/issues/23#issuecomment-5577199044).
After three verified integrations, or when storage pressure warrants it, Astra
allocates a bounded cleanup pass to Luna at `xhigh`. Use available capacity and
resume paused implementation promptly after the pass. This is maintenance within
the initiative, not permission to clean unrelated projects.

The agent first inventories exact branch tips, merged PRs, ancestry in the latest
verified integration commit, checkout changes, ignored files and active users of
each worktree. A separate Sol reviewer checks the proposed batch, and Astra
authorizes the concrete list. Immediately before removal, recheck that those
facts still hold. Skip any changed or uncertain candidate.

Protect main and integration, active or dirty work, pending corrections, unmerged
or failed investigations, and environments still borrowed by other contributors.
Required evidence must remain available. Before retiring a worktree containing
such artifacts, move them to a retained evidence location, verify their hashes
and update references. Ignored files are not automatically disposable; remove
only inventoried generated content. Do not run broad cleanup or prune commands
that also affect unapproved worktrees.

Remove only the approved worktrees and local task branches. Delete a remaining
remote task branch only when it still names the approved merged tip, using an
exact old-tip guard. Record removed paths and refs, source tips, merge proof,
command outcomes, retained evidence and exclusions in a cleanup receipt. Verify
the resulting Git inventory and preserve enough information to recreate a
checkout from its retained merge history. Report storage savings as measured or
estimated. The first pass is recorded in
[issue #23](https://github.com/andre-motta/tongs/issues/23#issuecomment-5577309137).

## Hardware GPU production and release gate

Electron is selected. Hardware GPU acceleration must work in the installed
production application on the supported Fedora 44 KDE x86_64 host. The current
comparison's `--disable-gpu` run does not satisfy this gate. Software-rendering
fallback may be offered explicitly, but cannot replace the required accelerated path.

Evidence must identify GPU, driver, display backend, Electron/Chromium versions,
launch configuration and exact packaged artifact/commit. Capture feature status
and renderer/device diagnostics showing physical GPU acceleration, not just an
enabled flag or a GPU process. Software renderers do not count as a pass. Exercise
actual native review/diff/plugin workflows, repeated startup/shutdown and renderer
stability with acceleration and sandboxing enabled. Do not relax SELinux or renderer
sandbox protections to satisfy the gate. Retest affected evidence after runtime,
graphics, driver, launch or packaging changes and on the final release artifacts.

The production gate is currently unmet. Issue #25 established a passing installed
prototype on Fedora 44 KDE through XWayland with the physical RTX 5090 and retained
process/sandbox diagnostics. That result guides production implementation; it does
not accept a different final artifact. Repeat the gate on the installed production
build. Missing GPU infrastructure or a headless CI pass cannot be presented as
hardware acceleration proof.

## Publication effects

- Pushes to `main` or `feat/desktop-app`, and PRs targeting either, trigger CI.
  Hosted core CI does not replace the native Fedora GPU/application evidence.
- Pushes to `main` trigger the MkDocs GitHub Pages deployment; documentation
  under `docs/` is site input even when absent from navigation. Keep it public-safe.
- `v*` tag pushes trigger the PyPI publication workflow using the `pypi` environment.
- Contributor PR pushes permit Astra integration into `feat/desktop-app` under
  this override. The final main PR still requires CTO acceptance; release and
  deployment authority remains separate.

## Desktop initiative

See [the desktop planning record](work/desktop.md). The CTO selected Electron
and approved this branch/PR workflow and mandatory GPU gate. Detailed production
service/plugin/draft/installer contracts and their implementation graph are in the
[production baseline](work/desktop-production.md), with
[first-wave assignments](work/desktop-first-wave.md). The CTO's continuation
instruction and explicit installation, draft-mode and GitHub-signing decisions
authorize this baseline. Dispatch follows independent review, feature integration
and verified prerequisite checks; material changes still return to the design gate.
