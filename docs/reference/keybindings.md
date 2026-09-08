# Keybindings

Complete keybinding reference organized by context.

## Global

These bindings work everywhere in the application.

| Key | Action |
|-----|--------|
| ++ctrl+p++ | Open command palette |
| ++question++ | Show help |
| ++q++ | Quit or go back |
| ++ctrl+r++ | Refresh current view |
| ++o++ | Open current item in browser |

## Inbox

| Key | Action |
|-----|--------|
| ++1++ | Switch to My Reviews tab |
| ++2++ | Switch to My MRs tab |
| ++3++ | Switch to All Open tab |
| ++s++ | Cycle sort order (updated / title / CI / author) |
| ++r++ | Toggle repo list |
| ++enter++ | Open MR detail view |

## Repo list

| Key | Action |
|-----|--------|
| ++slash++ | Filter repos by name |
| ++f++ | Cycle forge filter (All / GH / GL) |
| ++s++ | Cycle sort order (name / forge / host) |
| ++enter++ | Open scoped inbox for selected repo |
| ++escape++ | Go back |
| ++ctrl+r++ | Refresh repo list |

## MR detail

| Key | Action |
|-----|--------|
| ++1++ | Overview tab |
| ++2++ | Diff tab |
| ++3++ | Commits tab |
| ++4++ | Discussion tab |
| ++5++ | Pipeline tab |
| ++c++ | Add comment (general comment from Overview, inline from Diff) |
| ++a+shift++ | Approve (double-press to confirm) |
| ++u+shift++ | Unapprove (double-press to confirm) |
| ++m+shift++ | Merge (double-press to confirm) |
| ++x+shift++ | Close (double-press to confirm) |
| ++ctrl+y++ | Copy MR URL to clipboard |
| ++ctrl+g++ | Start review mode, or open the review draft screen once a draft exists |
| ++alt+close-bracket++ | Cycle to the next recovered review draft for this MR |

## Diff viewer

| Key | Action |
|-----|--------|
| ++j++ / ++k++ | Move cursor down / up |
| ++j+shift++ / ++k+shift++ | Extend selection down / up |
| ++ctrl++ + click | Extend selection to clicked line |
| ++close-bracket++ / ++open-bracket++ | Jump to next / previous comment |
| ++d++ | Expand / collapse discussion thread on current line |
| ++r++ | Reply to discussion on current line |
| ++r+shift++ | Resolve / unresolve discussion (double-press) |
| ++c++ | Comment on current line or selection |
| ++f3++ | Suggest changes (opens `$EDITOR`) |
| ++n++ / ++n+shift++ | Next / previous file |
| ++m++ | Toggle Markdown preview |
| ++v++ | Toggle unified/split diff mode |
| ++escape++ | Clear selection |

In split diff mode, ++h++ and ++l++ move focus between the old and new side
columns.

## Comment editor

| Key | Action |
|-----|--------|
| ++ctrl+s++ | Submit comment |
| ++escape++ | Cancel (double-press if text has been entered) |
| ++f2++ | Open in external editor |

## Review draft

Press ++ctrl+g++ from MR detail to start review mode or, once a draft exists,
to open the review draft screen and inspect it. It submits, edits, or
discards the local review draft for the current MR.

| Key | Action |
|-----|--------|
| ++escape++ | Close (double-press to discard unsaved summary or verdict changes) |
| ++v++ | Cycle verdict (Comment / Approve, plus Request changes on GitHub) |
| ++ctrl+s++ | Submit the review |
| ++e++ | Edit the selected draft comment |
| ++x++ | Remove the selected draft comment (double-press to confirm) |
| ++d+shift++ | Discard the local review draft (double-press to confirm) |
| ++n+shift++ | Start a new revision (only when the draft is stale) |
| ++r++ | Resume a paused submission attempt |

When a submission attempt is interrupted with an unknown outcome, three
additional bindings reconcile it (each requires a second press to confirm):

| Key | Action |
|-----|--------|
| ++1++ | Retry the remaining, unconfirmed comments |
| ++2++ | Return the remaining comments to local editing |
| ++3++ | Mark the remaining comments as already submitted |

## Discussion tab

| Key | Action |
|-----|--------|
| ++j++ / ++k++ | Move between discussion cards |
| ++enter++ | Jump to diff location (inline discussions) |
| ++r++ | Reply to focused discussion |
| ++r+shift++ | Resolve / unresolve (double-press) |
| ++f++ | Cycle filter (All / Unresolved / Resolved) |
| ++close-bracket++ / ++open-bracket++ | Jump to next / previous unresolved |

## Pipeline tab

| Key | Action |
|-----|--------|
| ++j++ / ++k++ | Move between pipeline / job cards |
| ++enter++ | Drill into jobs (from pipeline) or log (from job) |
| ++escape++ | Drill out one level |
| ++c+shift++ | Cancel pipeline or job (double-press) |
| ++r+shift++ | Retry pipeline or job (double-press) |
| ++o++ | Open pipeline / job in browser |
| ++f2++ | Open job log in external editor |
| ++slash++ | Search job log text |
