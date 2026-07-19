# Discussions

The Discussion tab (++5++ from MR detail) provides a card-based view of all
conversations on a merge request. It complements the inline diff markers by
showing every thread in one scrollable list.

## Discussion cards

Each discussion card shows:

- **Author** and timestamp
- **Diff snippet** for inline discussions, showing the surrounding code context
- **Full Markdown-rendered thread** with all replies
- **Resolution status** for resolvable threads (GitLab)

## Navigation

| Key | Action |
|-----|--------|
| ++j++ / ++k++ | Move between discussion cards |
| ++close-bracket++ / ++open-bracket++ | Jump to next / previous unresolved discussion |
| ++enter++ | Jump to the diff location (inline discussions only) |

Pressing ++enter++ on an inline discussion switches to the Diff tab and scrolls
to the exact line where the comment was left.

## Filtering

Press ++f++ to cycle through filter modes:

| Filter | Shows |
|--------|-------|
| All | Every discussion thread |
| Unresolved | Only unresolved threads |
| Resolved | Only resolved threads |

## Replying

Press ++r++ on any discussion card to open the comment editor and post a reply to
that thread. The reply is submitted to the forge and appended to the card
immediately.

## Resolving threads

Press ++r+shift++ (uppercase R) on a discussion to toggle its resolution status.
This requires a double-press to confirm. Resolution is supported on GitLab; on
GitHub, threads do not have a resolve/unresolve concept.

## General MR comments

Press ++c++ from the Overview tab (++1++) to post a top-level comment on the MR
that is not attached to any specific line.

## Cross-tab workflow

A typical review workflow using discussions:

1. Open the **Discussion tab** to scan unresolved threads
2. Press ++enter++ on a thread to jump to its location in the diff
3. Review the surrounding code in context
4. Press ++r++ to reply, or ++r+shift++ to resolve
5. Press ++5++ to return to the Discussion tab and continue
