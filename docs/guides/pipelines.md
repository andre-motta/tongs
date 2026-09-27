---
title: Pipelines and logs
description: "Drill from a pipeline to its jobs to a full log, search the log, and retry or cancel runs from the terminal."
opens: "++5++ from a review"
lead: "The Pipeline tab shows the CI runs for the review you have open. Drill from a pipeline to its jobs to a full log, search it, and retry what failed without opening the forge."
---

Open a review from the inbox and press ++5++ to reach the Pipeline tab. You can
also press ++ctrl+p++ and run the **Pipeline** command. GitLab pipelines and
GitHub Actions workflow runs render the same way, so a failing job looks the
same on either forge.

<figure class="shot"><a href="/media/ci.webp" aria-label="Open the full-size screenshot of the Pipeline tab"><div class="frame"><div class="bar" data-pagefind-ignore><span><b>tongs</b> &middot; pipeline</span><span class="meta"><span class="full">full size &#8599;</span></span></div><picture><source media="(max-width: 560px)" srcset="/media/ci-m.webp" width="740" height="511" /><img src="/media/ci.webp" width="1440" height="744" alt="The Pipeline tab showing a failed pipeline's jobs grouped by stage, with the pytest:integration job marked FAILED and the prompt: Retry job pytest:integration? Press R again."></picture></div></a><figcaption class="cap">demo data, cropped on small screens</figcaption></figure>

## Drill down from pipeline to log

The tab has three levels. Move with ++j++ and ++k++, go one level deeper with
++enter++, and come back out with ++escape++.

### Pipeline list

Each pipeline shows a status icon, its ID, when it ran, the short commit SHA,
the branch and how long it took. A failed pipeline has a red border and a
running one a yellow border.

### Job list

Jobs are grouped by stage. Each row shows the job name and how long it ran. A
failed job also carries the word `FAILED`, so the status never depends on
color alone. Jobs allowed to fail are marked `allow failure`.

### Job log

The log keeps the CI system's own ANSI colors, with line numbers in the gutter.
For very long logs the view keeps the last 50,000 lines. ++f2++ opens the full
log.

:::note
Pipelines load the first time you open the tab and stay in memory while the
review is open. After a job retry or cancel, the job list you are on reloads
by itself. ++ctrl+r++ reloads the review and refreshes the level of the
Pipeline tab you are on: the pipeline list, a pipeline's jobs or a job log.
:::

## Search a log

Press ++slash++ in a log to search the whole output as you type. The search
ignores case. A status line shows the match count, the line number and a
preview of the current match, or says when a term has no matches.

++n++ and ++shift+n++ walk forward and back, wrapping at either end. ++enter++
keeps the search and returns to the log. ++escape++ closes it and puts you back
where you were reading. The next ++escape++ drills out a level.

## Retry and cancel

Actions that change the forge ask for a second press of the same key. The first
press shows a prompt such as `Retry job pytest:integration? Press R again.`

| Key | Action | Confirm |
|-----|--------|---------|
| ++shift+r++ | Retry a failed or canceled pipeline or job | Twice |
| ++shift+c++ | Cancel a running or pending pipeline or job | Twice |
| ++o++ | Open the pipeline or job page in your browser | None |
| ++f2++ | Open the job log in your editor | None |

Retrying a pipeline reruns the jobs that did not pass, not the whole pipeline.
GitHub cannot cancel a single job,
so on GitHub cancel the whole workflow run from the pipeline list.

:::caution
The second press acts on the forge right away. A retried job starts a new run
with your credentials, and tongs cannot undo a cancel.
:::

## Open a log in your editor

++f2++ hands the log to your editor, which helps with long logs or with
comparing two runs side by side. tongs reads `$VISUAL`, then `$EDITOR`, and
otherwise opens the first of `nvim`, `vim`, `vi`, `nano` or `less` it finds on
your `PATH`:

```bash title="~/.bashrc"
# Open job logs and long comments in VS Code
export VISUAL="code --wait"
```

tongs writes the log to a private temporary file, waits for the editor to
exit, then deletes the file. The [`[editor]`](/reference/configuration/#editor)
table in Configuration applies to the desktop app (beta) only.

Every key on this page is also listed in the
[keybindings reference](/reference/keybindings/#pipeline-tab).
