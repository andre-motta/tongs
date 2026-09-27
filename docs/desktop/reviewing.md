---
title: Reviewing on desktop
description: "Read a diff, comment, suggest changes and submit a review from the tongs desktop workspace."
lead: "Read the diff, write comments and suggestions, collect them in a durable draft, and submit them as one review."
---

:::note[Beta]
The desktop app is a beta. It is available on the v1.0.0 GitHub Release as a
per-user archive with an attestation, and as unsigned RPMs. The terminal app
remains the primary interface. See [Install the desktop app](/desktop/installation/).
:::

This page covers the review tabs of the desktop workspace: **Files changed**,
the composers, the **Your review** drawer and **Discussions**. For the rest of
the window, start with the [workspace tour](/desktop/workspace/). Drafts made
here are the same durable review drafts the terminal app uses; see
[Review drafts](/guides/review-drafts/).

## Read the diff

Open **Files changed** to see the changed-file list and the selected file's
content. The toolbar has **Unified**, **Split** and **Refresh**. The current
head revision is shown beside them, so you can tell which revision the diff
represents.

The file list shows each path, its additions and deletions, and any available
hunk context. A file can carry one of these badges:

| Badge | Meaning |
|-------|---------|
| **Binary** | The file has no text diff. |
| **Truncated** | The forge returned only a bounded portion. |
| **Empty** | The file has no changed rows. |
| **Mode only** | The change is a file-mode update. |
| **Rename only** | The file moved and its content did not change. |
| **Not exposed by forge** | The forge sent no content and no reason for it. |

**Not exposed by forge** exists because of GitHub. Its pull request files
endpoint omits the patch for a binary, empty, rename-only or mode-only file
alike. tongs reads the path to tell those cases apart where it can, and shows
this badge where it cannot: a mode change looks the same as binary content
there, and a file whose path carries no text signal could be binary rather than
empty or unchanged. The badge names that limit instead of implying the file
failed to load.

Select a file or a hunk to move the content view. Large files are shown in
bounded row windows with **Previous rows** and **Next rows**. **Unified** shows
the rows in one sequence. **Split** aligns the old and new sides in two panes;
an empty cell means that side has no matching row. Changed lines and their line
numbers can be selected to highlight a line. Non-text, unavailable and
placeholder rows are read-only.

The desktop checks the revision and snapshot while it loads pages. If a
snapshot expires or the review changes during loading, it asks you to reload
the latest diff. If paging reaches a safety bound, it keeps a bounded partial
view and says the diff is incomplete. Invalid or inconsistent page data is
reported as an error, never combined with another revision.

## Write comments

There are two composers:

- The **in-diff composer**, opened from a line's `+` gutter button or with
  ++c++, comments on a line or a selected range.
- The **general composer** on **Overview** writes a review-level comment.

Both offer the same two writes:

- **Add comment now** posts immediately to the selected review.
- **Start a review**, or **Add to review** once one is pending, adds the entry
  to a durable draft instead.

### Suggest a change

Suggestions come from the in-diff composer.

1. Select one or more contiguous new-side lines, in **Unified** or **Split**.
   Drag over the line numbers, Shift-click a second line, or focus a line
   number and press ++shift+enter++ to extend the range within the same hunk.
2. Open the composer on the selection and choose **Insert suggestion**. It
   fills in a suggestion block built from the selected source.
3. Edit the block, or add an explanation above it, and check it with
   **Preview**.
4. Choose **Add comment now**, or **Start a review** or **Add to review**.

The block follows each forge's syntax. GitHub gets a `suggestion` fence over the
whole range. GitLab gets a `suggestion:-0+N` fence anchored on the first line,
where N is one less than the number of selected lines.

**Insert suggestion** is refused, with the reason, for an old-side selection,
non-contiguous lines, deletion rows and a partial diff. Refresh the diff to
complete a partial selection. It is also refused when the selection changed
underneath the press, for example because the diff moved to a later revision;
reselect the lines and try again.

## Collect a review in a draft

A durable draft is stored locally until you submit or discard it. When a review
opens with exactly one saved draft, the workspace adopts it. When there are
several, the **Your review** drawer offers **Choose a preserved draft**. While a
draft is active, the composers add general comments, selected lines and
suggestions to it, and **Your review** tracks it, submits it or discards it.

Open **Your review** from the review header on **Overview**, **Files changed**
or **Discussions**.
Its count shows the pending comments, and the draft stays bound to the review
revision it was created from.

### Submit and read the outcome

A durable submission shows its confirmed progress and ends as submitted, paused
or unknown. An unknown result asks you to check the receipt or choose how to
reconcile. Confirmed steps are never replayed automatically, and the workspace
never silently repeats an uncertain remote write.

- A partial or paused submission can resume its existing attempt.
- An unknown submission offers explicit choices: retry the remaining steps,
  return the draft to editing, or mark it submitted after you inspect the forge.

The **Quick verdict** section on **Overview** skips the draft. It submits the
text in the general composer with **Submit comment verdict**, **Approve
review** or **Request changes**, limited to the verdicts this forge and this
account can record. A quick verdict ends as accepted, rejected or unknown.

### When the revision changes

If the review gets a new revision, inline comments stay on the old draft and
cannot be submitted against the new revision. The workspace can create a
separate draft for the current revision that carries over the body, the verdict
and the general comments. Old inline comments and replies stay behind for you
to recreate deliberately. Check the draft's revision before you submit it.

### Discard a pending review

Discard from either place:

- **Discard review** in **Your review**.
- The composer's **More** overflow, labelled **More review actions** for
  assistive technology. It appears in the in-diff composer only while a review
  is pending.

Both ask first, with the same sentence: how many pending comments would go, and
whether the summary and the chosen verdict would go with them. Both then make
the same `drafts.discard` call the terminal app makes from ++shift+d++. Press
++escape++ to cancel the confirmation; the drawer and composer stay open, and a
further ++escape++ closes them.

A discard removes only the local draft. Nothing unsent is sent, and nothing
already published changes. Afterwards the diff drops its pending cards, the
**Your review** count returns to zero, and the composer offers **Start a
review** again. A discard is refused while a save or a submission attempt holds
the draft, and the button shows the reason.

### Resolve a version conflict

If another writer changes the stored draft before your save lands, the
workspace reports a version conflict. It shows both versions side by side: your
unsaved text, and the stored draft with its version number and last update
time. Nothing is chosen for you.

- **Keep my text and save over version N** saves your text on top of the stored
  version.
- **Take the stored version and keep mine to copy** loads the stored draft for
  editing and keeps your text on screen in a read-only box until you dismiss
  it. A further conflict adds another copy instead of replacing the first.

If the stored draft was already submitted, or is held by a submission attempt
elsewhere, keeping your text is refused with that reason. Both versions stay on
screen so you can copy your text out.

## Discussions

**Discussions** lists every published thread that has a diff position,
unresolved threads first. Each has **Show in diff**, which opens it where it was
written. The list does not take new comments; write in a composer instead. An
indicator names the active draft and its stored version. **Review workflow** in
the application bar jumps to this tab.

## Lifecycle actions

The review lifecycle actions, **Merge**, **Close**, **Reopen** and **Remove
approval**, sit in the review header next to **Your review**. The header is the
same on **Overview**, **Files changed** and **Discussions**, so you can act on
the review from whichever of those tabs you are reading; see
[Read review details](/desktop/workspace/#read-review-details).

- An action the forge does not permit for this review is disabled, and its
  tooltip says it is unsupported. While support is still loading, or if it
  could not be read, every action is disabled with that reason.
- Each action takes two presses: the first changes the button to
  **Confirm _action_**, and only the second sends it.
- When merging is permitted, the header also offers **Squash commits** and
  **Delete source branch after merge**. Changing either option withdraws a
  pending **Confirm Merge**, so what you confirm is always what is on screen.
- A refused action is reported in the header as an alert.
- If the result of an action is unknown, every action stays disabled until a
  retained receipt settles the result or you acknowledge the uncertainty.
  **Check retained receipt** looks for a receipt the app kept for that action;
  when none was kept, inspect the review on the forge and choose **I inspected
  the forge; acknowledge uncertainty**. The unknown result follows you across
  the tabs, so an action is never replayed by switching tabs.
- An unknown result from any other immediate write, such as an inline comment
  on **Files changed**, a reply, a resolve or a verdict, blocks further writes
  in the same way. The header reports it on **Overview**, **Files changed** and
  **Discussions** with **I inspected the forge; acknowledge uncertainty**, so
  you can clear it from whichever tab you are on. Only a lifecycle action
  offers **Check retained receipt**.

## Keyboard

The **Files changed** diff and the **Your review** drawer answer the desktop
review keys, such as ++c++ to comment, ++shift+c++ to open **Your review** and
++v++ to cycle the verdict. The keys act from anywhere in those two surfaces,
even with nothing focused. **Overview**, **Commits**, **Discussions** and
**Pipelines** do not register them. The full map, and how it differs from the
terminal app, is in the [keybinding reference](/reference/keybindings/#desktop-app-beta).
