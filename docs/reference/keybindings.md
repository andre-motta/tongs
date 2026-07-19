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
| ++r++ | Toggle repo list |
| ++enter++ | Open MR detail view |

## Repo list

| Key | Action |
|-----|--------|
| ++slash++ | Filter repos by name |
| ++f++ | Cycle forge filter (All / GH / GL) |
| ++enter++ | Open scoped inbox for selected repo |

## MR detail

| Key | Action |
|-----|--------|
| ++1++ | Overview tab |
| ++2++ | Diff tab |
| ++3++ | Commits tab |
| ++4++ | Pipeline tab |
| ++5++ | Discussion tab |
| ++c++ | Add comment (general comment from Overview, inline from Diff) |
| ++a+shift++ | Approve (double-press to confirm) |
| ++u+shift++ | Unapprove (double-press to confirm) |
| ++m+shift++ | Merge (double-press to confirm) |
| ++x+shift++ | Close (double-press to confirm) |
| ++ctrl+y++ | Copy MR URL to clipboard |

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
| ++escape++ | Clear selection |

## Comment editor

| Key | Action |
|-----|--------|
| ++ctrl+s++ | Submit comment |
| ++escape++ | Cancel (double-press if text has been entered) |
| ++f2++ | Open in external editor |

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
