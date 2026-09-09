# Desktop work item: <outcome>

Parent: #17 or <scoped parent>. State: planned.
Issue: <number/link>. Owner/model: <actual runtime and reasoning>.
Independent reviewer: a senior reviewer, not the author. Integration gate: the
orchestrator.

## Outcome and ownership

Describe one independently verifiable result. List scope and exclusions, owned
files/directories, stable interfaces, and any shared-file coordination. Explain
how this item can run alongside other ready work. Split oversized items before
dispatch, preserving meaningful acceptance boundaries.

## Dependencies and readiness

- Prerequisite issues and native GitHub blocking links: <list or none>.
- Approved design/interface decisions: <record>.
- Verified prerequisite merge SHAs on feat/desktop-app: <exact commits>.
- Blocker and next action, separately from state: <reason or none>.
- Readiness evidence checked by the orchestrator: <date, branch head, checks>.

An open prerequisite can be ready for dependents after its feature-branch merge
is verified. An issue marked closed without a verified merge is insufficient.

## Assignment and Git authority

Branch: feat/desktop-<issue>-<slug>. Worktree: <local record, omit private paths
from public issue>. Base: <latest verified feat/desktop-app SHA>.

The contributor may commit with git commit -s, push this assigned branch and open
or update its PR into feat/desktop-app. Include a title, one-line body and actual
Codex model co-author with noreply@openai.com. Only the orchestrator may integrate
or edit shared issue/dependency tracking. Main, tags and releases remain outside
authority.

## Acceptance and checks

- Observable acceptance criteria: <specific outcomes>.
- Commands/environments and required CI: <checks>.
- Native application/GPU/plugin/TUI evidence as applicable: <proof>.
- Failure/recovery and compatibility cases: <cases>.
- Explicit exclusions or CTO waivers: <none unless recorded>.

## Handoff and progress

PR: <link>. Exact tested head: <SHA>. Artifact hashes: <values>.
Evidence: <durable links; distinguish synthetic and live>.
Independent findings, corrections and current-head review: <record>.
Orchestrator integration result/merge SHA and verified checks: <record>.
Resume notes, pending work and next-ready dependents: <record>.

Keep issue progress current at assignment, review/rejection, correction,
integration and gate changes. Use Refs in intermediate PRs. The orchestrator or an
explicitly delegated tracker agent may close a completed scoped item after its full
acceptance, independent review, feature merge and required post-merge checks are
verified under the [project SDLC profile](../../SDLC.md). Record the accepted
head, merge and evidence. Issue closure does not imply final CTO acceptance or main
delivery.
