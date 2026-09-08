# Tongs SDLC profile

Tongs adopts Agent SDLC **0.1.0**, maintained in the private
[agent-sdlc repository](https://github.com/andre-motta/agent-sdlc).
Source commit: `4e851d1b8a903aa8bebceea078860a21152ee8e8`.
Read the installed `agent-sdlc` skill's `references/workflow.md` with this profile.
Contributors without access to the private package can still follow the project
review process in [CONTRIBUTING.md](https://github.com/andre-motta/tongs/blob/main/CONTRIBUTING.md); the private skill is an
orchestration aid, not a prerequisite for ordinary contributions.

## Project settings

| Setting | Value |
| --- | --- |
| Repository / tracker | `andre-motta/tongs`, GitHub Issues; reuse existing issues and native sub-issue/blocking links where available |
| Integration target | Desktop: `feat/desktop-app`, owned by the orchestrator. Final PR targets `main` for CTO review |
| Worktrees | Isolated worktree per item; desktop contributors use `feat/desktop-<issue>-<slug>`. Existing comparison `codex/` branches remain historical |
| Runtime | Python 3.12+; current CI tests 3.12 and 3.13 |
| Setup | Create checkout-local `.venv`; activate per shell call; install editable `.[dev]` plus Ruff; add `mcp` extra for MCP tests |
| Focused checks | Tests for changed subsystems, plus relevant lint/format checks |
| Integrated checks | `pytest`, `ruff check src/ tests/`, `ruff format --check src/ tests/`; include optional dependencies required by changed features |
| Documentation checks | Local links, command examples, `git diff --check`; `mkdocs build --strict` for site inputs/navigation |
| Functional proof | Exercise affected TUI/desktop workflows and attach observable evidence; distinguish mocked APIs from live forge calls |
| Commit format | Title, blank line, one-line body; use `git commit -s`; the co-author trailer uses the address of the vendor that produced the commit, `noreply@openai.com` for Codex or `noreply@anthropic.com` for Claude, and never claims the other |
| Upstream path | Desktop agent PRs into `feat/desktop-app`, the orchestrator integrates; final feature PR into `main`, CTO gates final acceptance and merge. Tags/releases/deployments need separate authority |
| Initiative records | `docs/work/<initiative>.md`; keep public-safe summaries and use access-appropriate locations for sensitive artifacts |

Use `python -m pip install -e ".[dev,mcp]" ruff` in the activated environment for
full core/MCP validation. The test suite mocks forge traffic. Installation may
need network access; a skipped optional test is not proof of that subsystem.

## Authority and integration

The owner approved adoption of this workflow. Three roles are used, named by
function rather than by any vendor's model codename:

- **Orchestrator.** Owns overarching architecture, design, writing direction,
  scheduling, and integration.
- **Senior contributor** and **senior reviewer.** The same seniority level, used
  for senior implementation and for independent review. A senior contributor's
  work is reviewed by a different senior reviewer.
- **Bounded contributor.** Handles well-specified assignments under senior
  review.

The reusable SDLC package is vendor neutral. This profile maps the roles onto
whichever assistant install is orchestrating, and each pull request records the
actual author model and effort setting used, rather than a codename.

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

The orchestrator owns `feat/desktop-app` on GitHub as the shared integration branch
and final engineering review gate. Initialize it from the independently reviewed comparison
and evidence at `58cf120`, plus this reviewed policy update. This one-time bootstrap
is explicitly recorded; subsequent contributor changes enter through PRs. Main's
dirty checkout is preserved. No contributor may push directly to the integration
branch or main, merge PRs, create releases, or change shared tracking independently.

Contributors create `feat/desktop-<issue>-<slug>` branches from the latest verified
integration commit in separate worktrees, commit with `git commit -s`, push their
assigned branches and open PRs with base `feat/desktop-app`. No repeated CTO
approval is needed for those scoped pushes or PRs. Use the actual assigned model,
a title and one-line commit body, and the co-author trailer for the vendor that
produced the commit: `Co-Authored-By: Codex <model> <noreply@openai.com>` or
`Co-Authored-By: Claude <model> <noreply@anthropic.com>`, with the real model name
and no context-window annotation. After review, agents push
corrections to their own branches. Keep published history intact; prefer merging
updated integration history rather than force-pushing shared or reviewed commits.

Independent senior review is required, including for bounded contributor work and
for another senior contributor's implementation. The orchestrator reviews the
resulting PR, directs corrections, checks that required checks and relevant
functional evidence apply to the exact current head, and serializes integration.
Only the orchestrator merges to `feat/desktop-app`. Prefer merge commits to
preserve signed-off contributor commits and dependency history. The orchestrator
may resolve conflicts locally in an isolated worktree; resolutions invalidate
affected checks/reviews and must be verified before the resolved result is pushed.
Record the resolution and reviewer decision in the linked issue/PR. A changed PR
head requires renewed affected review and checks; never merge an unreviewed head.

These roles are an agent workflow, not separate GitHub identities. Record the
independent reviewer's findings and the orchestrator's disposition in the PR even
when tool calls share the owner's GitHub account. Do not claim GitHub-enforced branch protection
unless it has actually been configured and verified.

When the feature is complete, the orchestrator opens a PR from `feat/desktop-app`
into `main` for the CTO's final review. Creating that PR is authorized; merging it, pushing main,
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
The orchestrator updates issue progress at assignment, handoff, review/correction, integration,
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
still must satisfy required integration checks. The orchestrator schedules ready items within
available concurrency, queues excess work, and reschedules after every integration
or newly discovered dependency. A blocker is recorded separately from progress.

For this initiative, track:
`planned -> ready -> assigned -> review -> feature integrated -> CTO accepted -> main delivered`.
Record local candidate integration and the actual remote merge SHA separately.
A merge to the feature branch is not delivery to main. The CTO authorized closing
completed scoped work items after independent review, feature integration and
required post-merge checks are verified. The orchestrator, or an explicitly
delegated tracker agent, checks the item's full acceptance criteria before closing it and records the
accepted PR head, merge commit and evidence. Closure means that scoped work is
complete on `feat/desktop-app`; it does not imply CTO acceptance or main delivery.
Keep the parent feature, incomplete work, deferred RFEs and outstanding production
acceptance gates open. Readiness still depends on verified prerequisite commits.
This exception is recorded in [issue #23](https://github.com/andre-motta/tongs/issues/23).

Contributors continue using `Refs #...` in intermediate PRs and do not close issues
automatically. Preserve rejected/interrupted worktrees, review findings,
assignments, evidence and next actions for safe resumption.

Use the [desktop PR template](https://github.com/andre-motta/tongs/blob/main/.github/PULL_REQUEST_TEMPLATE/desktop.md)
for intermediate work. Baseline CI/lint/dependency failures remain unmet; branch
creation does not waive them. CI readiness and hardware GPU work are separate,
issue-tracked items that can be investigated independently after bootstrap.

### Periodic branch and worktree cleanup

The CTO authorized periodic cleanup in
[issue #23](https://github.com/andre-motta/tongs/issues/23#issuecomment-5577199044).
After three verified integrations, or when storage pressure warrants it, the
orchestrator allocates a bounded cleanup pass to a bounded contributor. Use available capacity and
resume paused implementation promptly after the pass. This is maintenance within
the initiative, not permission to clean unrelated projects.

The agent first inventories exact branch tips, merged PRs, ancestry in the latest
verified integration commit, checkout changes, ignored files and active users of
each worktree. A separate senior reviewer checks the proposed batch, and the
orchestrator authorizes the concrete list. Immediately before removal, recheck that those
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
- Pushes to `main` trigger the MkDocs GitHub Pages deployment. Every Markdown file
  under `docs/` is site input even when absent from navigation, unless `mkdocs.yml`
  lists it under `exclude_docs`. This file, `docs/site-plan.md` and `docs/work/`
  are excluded and stay repository-only; everything else under `docs/` must be
  public-safe.
- `v*` tag pushes trigger the PyPI publication workflow using the `pypi` environment.
- Contributor PR pushes permit orchestrator integration into `feat/desktop-app` under
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
