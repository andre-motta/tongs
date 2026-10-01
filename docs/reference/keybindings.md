---
title: Keybindings
description: "Every key in the tongs terminal app, screen by screen, and the desktop review key map."
lead: Every key in the terminal app, screen by screen, followed by the desktop review map. The footer of each terminal screen shows its most used keys.
---

## Terminal app

### Everywhere

| Key | Action |
|-----|--------|
| ++ctrl+p++ | Open the command palette |
| ++question++ | Show or hide the keybindings panel |

### Main screens

These keys work on the inbox, the repository list and MR detail.

| Key | Action |
|-----|--------|
| ++q++ | Quit from the inbox, or go back one screen |
| ++escape++ | Go back one screen (on the inbox, only from a repository's inbox) |
| ++ctrl+r++ | Refresh the current screen |
| ++o++ | Open the current item in the browser (inbox and MR detail) |

### Inbox

| Key | Action |
|-----|--------|
| ++1++ | Switch to the My Reviews tab |
| ++2++ | Switch to the My MRs tab |
| ++3++ | Switch to the All Open tab |
| ++s++ | Cycle the sort order: updated, title, CI, author |
| ++r++ | Open the repository list, or go back from a repository's inbox |
| ++enter++ | Open MR detail for the selected row |

### Repository list

| Key | Action |
|-----|--------|
| ++slash++ | Filter repositories by name |
| ++f++ | Cycle the forge filter: all, GitHub, GitLab |
| ++s++ | Cycle the sort order: name, forge, host |
| ++enter++ (in the filter) | Keep the filter and move to the list |
| ++enter++ (on the list) | Open the inbox for the selected repository |
| ++escape++ | Close the filter and restore the full list, or go back |
| ++ctrl+r++ | Refresh the repository list |

### MR detail

| Key | Action |
|-----|--------|
| ++1++ | Overview tab |
| ++2++ | Diff tab |
| ++3++ | Commits tab |
| ++4++ | Discussion tab |
| ++5++ | Pipeline tab |
| ++c++ | Add a comment: a general comment from Overview, an inline one from Diff |
| ++shift+a++ | Approve (press twice to confirm) |
| ++shift+u++ | Remove your approval (press twice to confirm) |
| ++shift+m++ | Merge (press twice to confirm) |
| ++shift+x++ | Close (press twice to confirm) |
| ++ctrl+y++ | Copy the MR URL to the clipboard |
| ++ctrl+g++ | Start review mode, or open the review draft once one exists |
| ++ctrl+n++ | Cycle to the next recovered review draft for this MR |

### Diff viewer

| Key | Action |
|-----|--------|
| ++j++ / ++k++ | Move the cursor down / up |
| ++shift+j++ / ++shift+k++ | Extend the selection down / up |
| ++ctrl++ + click | Extend the selection to the clicked line |
| ++close-bracket++ / ++open-bracket++ | Jump to the next / previous comment |
| ++d++ | Show or hide the discussion thread on the current line |
| ++r++ | Reply to the discussion on the current line |
| ++shift+r++ | Resolve or unresolve the discussion (press twice) |
| ++c++ | Comment on the current line or selection |
| ++f3++ | Suggest a change in your external editor |
| ++n++ / ++shift+n++ | Next / previous file |
| ++m++ | Toggle the Markdown preview |
| ++v++ | Toggle unified and split layout |
| ++escape++ | Clear the selection |

In the split layout, ++h++ and ++l++ move focus between the old and new side.

++f3++ works on the new side only, and not on deleted lines. It opens
`$VISUAL`, then `$EDITOR`, then the first of `nvim`, `vim`, `vi` or `nano` on
your `PATH`.

### Comment editor

| Key | Action |
|-----|--------|
| ++ctrl+s++ | Submit the comment |
| ++escape++ | Cancel (press twice once you have typed text) |
| ++f2++ | Open the text in your external editor |

++ctrl+j++ also submits.

### Review draft

++ctrl+g++ on MR detail starts review mode. Once a draft exists, the same key
opens the review draft screen, where you submit, edit or discard it. See
[Review drafts](/guides/review-drafts/).

| Key | Action |
|-----|--------|
| ++escape++ | Close (press twice to drop unsaved summary or verdict changes) |
| ++v++ | Cycle the verdict: Comment, Approve, and Request changes on GitHub |
| ++ctrl+s++ | Submit the review |
| ++e++ | Edit the selected draft comment |
| ++x++ | Remove the selected draft comment (press twice to confirm) |
| ++shift+d++ | Discard the local review draft (press twice to confirm) |
| ++shift+n++ | Start a new revision (only when the draft is stale) |
| ++r++ | Resume a paused submission |

When a submission is interrupted and its outcome is unknown, three more keys
reconcile it. Each one needs a second press to confirm.

| Key | Action |
|-----|--------|
| ++1++ | Retry the remaining, unconfirmed comments |
| ++2++ | Return the remaining comments to local editing |
| ++3++ | Mark the remaining comments as already submitted |

### Discussion tab

| Key | Action |
|-----|--------|
| ++j++ / ++k++ | Move between discussion cards |
| ++enter++ | Jump to the diff location of an inline discussion |
| ++r++ | Reply to the focused discussion |
| ++shift+r++ | Resolve or unresolve (press twice) |
| ++f++ | Cycle the filter: all, unresolved, resolved |
| ++close-bracket++ / ++open-bracket++ | Jump to the next / previous unresolved discussion |

++up++ and ++down++ also move between cards.

### Pipeline tab

| Key | Action |
|-----|--------|
| ++j++ / ++k++ | Move between pipeline or job cards |
| ++enter++ | Open a pipeline's jobs, or a job's log |
| ++escape++ | Close the log search, or step back one level |
| ++shift+c++ | Cancel the pipeline or job (press twice) |
| ++shift+r++ | Retry the pipeline or job (press twice) |
| ++o++ | Open the pipeline or job in the browser |
| ++f2++ | Open the job log in your external editor |
| ++slash++ | Search the job log, with a live match count |
| ++n++ / ++shift+n++ | Jump to the next / previous match, wrapping around |

++up++ and ++down++ also move between cards. See
[Pipelines and logs](/guides/pipelines/).

## Desktop app (beta)

:::note[Beta]
The desktop app is a beta. These keys belong to its review pages, not to the
terminal app. See [Reviewing on desktop](/desktop/reviewing/).
:::

The keys work from anywhere on the review page, including with nothing
focused. They stand down while a text field, an editable region, a select
control or a modal dialog has the keyboard, and whenever you hold a modifier
the key does not name.

The last column gives the terminal key for the same intent.

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

### Where the two apps differ

- **Files and threads swap keys.** The terminal uses ++close-bracket++ and
  ++open-bracket++ for the next and previous comment, and ++n++ and
  ++shift+n++ for the next and previous file. The desktop swaps the pairs:
  ++close-bracket++ and ++open-bracket++ move between files, and ++n++ and
  ++p++ move between the threads and pending comments of the file on screen.
  This is the only reassignment in the table.
- **++v++ picks the verdict.** In the terminal diff viewer, ++v++ toggles the
  unified and split layout. The desktop has toolbar buttons for the layout, so
  ++v++ cycles the verdict, which is what it does on the terminal's review
  draft screen.
- **++shift+c++ only opens.** It opens **Your review**, the screen the
  terminal reaches with ++ctrl+g++. Once the drawer is on screen it owns the
  keyboard, and ++escape++ closes it.
- **++ctrl+enter++ presses the primary button.** On macOS it is ++cmd+enter++.
  It carries the same refusals as the button: an empty comment, or a draft
  held by a save, is refused just as a click would be.
- **Navigation wraps.** ++n++, ++p++, ++close-bracket++ and ++open-bracket++
  wrap at both ends. On a review with one changed file, ++close-bracket++ and
  ++open-bracket++ do nothing. ++n++ and ++p++ follow the page's order, so in
  the **Split** layout they visit the old pane's threads and pending comments
  before the new pane's.
- **Your review owns the keyboard.** While it is open, the diff's keys stand
  down, even if the focus sits on the diff behind it. ++escape++ there cancels
  an armed confirmation first, and otherwise closes the drawer and returns the
  focus to the button that opened it.
- **++escape++ keeps your text.** It closes the in-diff composer and keeps what
  you typed, which comes back on the same review and the same line. Inside the
  composer it answers the nearest question first: an armed **Discard review**
  confirmation, then the **More review actions** menu, and only then the
  composer itself.
