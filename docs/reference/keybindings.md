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
| ++enter++ (in filter) | Keep the filter and move to the list |
| ++enter++ (on list) | Open scoped inbox for selected repo |
| ++escape++ | Close the filter and restore the full list, or go back |
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
| ++escape++ | Close the log search, or drill out one level |
| ++c+shift++ | Cancel pipeline or job (double-press) |
| ++r+shift++ | Retry pipeline or job (double-press) |
| ++o++ | Open pipeline / job in browser |
| ++f2++ | Open job log in external editor |
| ++slash++ | Search job log text (live, with match count) |
| ++n++ / ++n+shift++ | Jump to next / previous match (wraps around) |

## Desktop review workflow

These keys belong to the desktop application's review surfaces, not to the
terminal interface. They act from anywhere on the review page, including with
nothing focused, and they stand down while a text field, a `contenteditable`
region, a select control, or a modal dialog holds the keyboard, and whenever a
modifier the binding does not name is held.

| Key | Desktop | Terminal interface |
|-----|---------|--------------------|
| `c` | Comment on the focused row or the current selection | `c` (diff viewer, MR detail) |
| `Shift+C` | Open **Your review** | `Ctrl+G` (MR detail) |
| `]` / `[` | Next / previous changed file, wrapping | `n` / `Shift+N` (diff viewer) |
| `n` / `p` | Next / previous thread or pending comment, wrapping | `]` / `[` (diff viewer, next / previous comment) |
| `r` | Reply to the focused thread | `r` (diff viewer, discussion tab) |
| `Ctrl+Enter` / `Cmd+Enter` | Primary composer action | `Ctrl+S` (comment editor) |
| `Esc` | Close the composer and keep the text | `Esc` (comment editor, cancel) |
| `v` | Cycle the verdict in **Your review** | `v` (review draft) |

Where the desktop and the terminal interface differ, the difference is
deliberate:

- The terminal interface uses `]` and `[` for the next and previous comment and
  `n` and `Shift+N` for the next and previous file. The desktop map swaps the
  two pairs: `]` and `[` move between files and `n` and `p` move between the
  threads and pending comments of the file on screen. This is the only
  reassignment in the table, and it is the one the design fixed, so the two
  interfaces do not agree on these four keys.
- `v` toggles unified and split layout in the terminal diff viewer. The desktop
  has toolbar buttons for the layout, so `v` is free for the verdict, which is
  the terminal interface's own meaning for it on the review draft screen.
- `Shift+C` opens **Your review**; the terminal interface reaches the same
  screen with `Ctrl+G`. It only opens: a drawer already on screen owns the
  keyboard, and `Esc` is what closes it.
- `Ctrl+Enter` (or `Cmd+Enter` on macOS) runs whatever the composer's primary
  button would run, so it carries the same refusals: an empty comment or a
  draft held by a save is refused exactly as pressing the button is.
- `n`, `p`, `]` and `[` wrap at both ends.
- `Esc` closes the in-diff composer and keeps the typed text, which returns to
  the same review and the same anchor. Inside the composer it answers the
  nearest question first: an armed **Discard review** confirmation, then the
  **More review actions** overflow, and only then the composer itself.
