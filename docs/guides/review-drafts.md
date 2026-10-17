---
title: Review drafts
description: "Collect comments in a local review draft, recover it after a crash, and submit it to the forge as one review."
opens: "++ctrl+g++ from a review"
lead: "Your drafts survive. Review mode saves every comment to disk as you write it, and nothing reaches the forge until you submit."
---

Outside review mode, a comment posts to the forge as soon as you save it. In
review mode, general comments, inline comments, suggestions and replies go into
a local draft instead. When you are done, you pick a verdict and submit the
whole draft as one review.

## Start a review

Open a review from the inbox and press ++ctrl+g++. tongs creates a draft bound
to the revision you are looking at and says "Review mode started. Comments are
now saved locally."

A status bar stays on screen while the draft is active:

```text
REVIEW DRAFT | 2 comments | v3 | editable | Ctrl+G review
```

It shows the comment count, the draft version and its state. It adds
`old revision`, `version conflict` or `saving` when they apply.

## Add comments

Use the same keys as always. With a draft active, each one saves to the draft:

| Key | Where | Saves |
|-----|-------|-------|
| ++c++ | Overview or diff | A general comment, or an inline comment on the selected line |
| ++shift+j++ / ++shift+k++, then ++c++ | Diff | An inline comment on a range of lines |
| ++f3++ | Diff | A suggestion on new-side lines |
| ++r++ | Diff or Discussion tab | A reply to an existing thread |

Press ++ctrl+s++ in the comment editor to save. The notification reads "Draft
saved locally." Inline draft comments show in the diff under their line as
`DRAFT new: ...` or `DRAFT old: ...`, in both unified and split view.

Resolving a thread with ++shift+r++ is not part of the draft. It still acts on
the forge right away.

## Your drafts survive

Every save is a transaction in a local SQLite database, so a draft outlives a
crash, a closed terminal or a reboot. Open the same review again and tongs
loads the draft for you.

A review can hold more than one draft, for example one per revision. On the MR
detail screen, press ++ctrl+n++ to cycle through them.

A draft holds up to 200 comments. The summary and each comment can be up to
65,536 bytes, and tab and newline are the only control characters they can
contain. Both apps check these limits on every save and refuse a save over a
limit with an error. In the terminal, your text stays in the editor so you can
shorten it.

:::note
Drafts live in `drafts.db` in the tongs data directory
(`~/.local/share/tongs/` on Linux). The file is created with mode `0600`.
Clearing the cache does not touch it. The desktop app (beta) reads and writes
the same drafts. While a review is being submitted, a lock file in
`drafts.db.locks/` next to it stops a second app from submitting it too. tongs
deletes the lock file when the submission ends or the app closes, and deletes
any left behind by an app that crashed the next time it starts.
:::

## Submit the review

Press ++ctrl+g++ again to open the review draft screen.

<figure class="shot"><a href="/media/draft.webp" aria-label="Open the full-size screenshot of the review draft screen"><div class="frame"><div class="bar" data-pagefind-ignore><span><b>tongs</b> &middot; review draft</span><span class="meta"><span class="full">full size &#8599;</span></span></div><picture><source media="(max-width: 560px)" srcset="/media/draft-m.webp" width="660" height="440" /><img src="/media/draft.webp" width="1000" height="756" loading="lazy" decoding="async" alt="The review draft screen: version 3, editable and ready, two inline draft comments on pipelines/backfill.py lines 12 and 13, the second with a suggestion block, the verdict set to approve, and an empty summary box."></picture></div></a><figcaption class="cap">demo data, cropped on small screens</figcaption></figure>

The screen lists every draft comment, the verdict and a summary box. From here:

| Key | Action |
|-----|--------|
| ++v++ | Cycle the verdict |
| ++ctrl+s++ | Submit the review |
| ++e++ | Edit the selected comment |
| ++x++ | Remove the selected comment (press twice) |
| ++shift+d++ | Discard the whole draft (press twice) |
| ++escape++ | Close (press twice to drop unsaved summary or verdict changes) |

The verdicts are Comment and Approve on both forges. GitHub adds Request
changes, which needs a summary. Pressing ++shift+a++ while a draft is active
opens this screen instead of approving on its own.

On GitHub, a draft whose comments are all inline goes up as a single pull
request review, with its summary and verdict. Otherwise tongs sends each
comment in order, then the summary and the verdict. When the last step lands, tongs says "Review submitted." and hides
the draft from both apps at once.

The submitted draft stays in `drafts.db` for 24 hours, so a status check right
after the submission still finds it. After that, tongs deletes it and the
record of its submission from local storage. Drafts that are still being
edited or submitted are never deleted. To keep submitted drafts for longer or
shorter, set
[`submitted_retention`](/reference/configuration/#drafts).

## When a submission is interrupted

tongs records each step before it sends it, so an interrupted submission can
pick up where it stopped. It never repeats a write it cannot confirm.

- **Paused.** A step failed with a clear answer from the forge. Press ++r++ to
  resume the remaining steps.
- **Unknown.** tongs cannot tell whether a step reached the forge. Check the
  review on the forge, then choose one:

| Key | Action |
|-----|--------|
| ++1++ | Retry the remaining steps. This may post a duplicate. |
| ++2++ | Return the remaining comments to local editing |
| ++3++ | Mark the remaining comments as submitted |

Each choice needs a second press.

## When the review changes

A draft belongs to the revision you started it on. When new commits arrive,
the bar shows `old revision`. You can still read that draft, but you cannot
submit it or add inline comments or replies to it.

Open the draft screen and press ++shift+n++ to start a draft for the current
revision. General comments and replies to threads that still exist carry
over. Inline comments stay in the old draft, so you can read them and add
them again on the new diff.

If the same draft changes somewhere else, such as in a second terminal or the
desktop app, tongs refuses the save and keeps your text. The bar shows
`version conflict`, and the draft screen shows the recovered summary and
verdict so you can try again.

Every key on this page is also listed in the
[keybindings reference](/reference/keybindings/#review-draft).
