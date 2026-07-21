# Diff viewer

The diff viewer is the core review surface. Open it by pressing ++2++ from any MR
detail view to switch to the Diff tab.

## Layout

The diff viewer uses a split-pane layout:

- **Left pane** -- file tree showing all changed files with status indicators
  (`M` modified, `A` added, `D` deleted, `R` renamed)
- **Right pane** -- the diff content for the selected file, with syntax
  highlighting, line numbers, and gutter markers

## File navigation

| Key | Action |
|-----|--------|
| ++n++ | Jump to the next file |
| ++shift+n++ | Jump to the previous file |

You can also click any file in the file tree to jump directly to it.

## Line navigation

| Key | Action |
|-----|--------|
| ++j++ | Move cursor down one line |
| ++k++ | Move cursor up one line |

The cursor highlights the active line and updates the gutter indicator.

## Visual line selection

Select multiple lines for multi-line comments:

| Key | Action |
|-----|--------|
| ++shift+j++ | Extend selection down |
| ++shift+k++ | Extend selection up |
| ++ctrl++ + click | Extend selection to clicked line |
| ++escape++ | Clear selection |

## Truncated diffs

When a forge API truncates a large diff (returning no patch content), the file
still appears in the file tree with its +/- stats. The diff pane shows a
"Diff not available" message with a prompt to press ++o++ to view the full file
in your browser.

## Syntax highlighting

tongs uses Pygments to apply syntax highlighting to 500+ languages. All lines in
a file are highlighted in a single bulk Pygments call for performance, rather
than per-line.

## Word-level diffs

Within modified lines, tongs highlights the specific words that changed using
bold and underline styling. This makes it easy to spot small changes in long
lines without reading the entire line.

## Context folding

Long unchanged sections between changes are collapsed into a marker showing how
many lines were hidden (for example, "... 42 unchanged lines ..."). This keeps
the diff focused on what actually changed.

## Markdown preview

For `.md` files, press ++m++ to toggle between the raw diff and a rendered
Markdown preview.

## Inline comments in the diff

Existing discussion threads appear as gutter markers next to the relevant lines
in the diff. Press ++d++ on a line with a marker to expand or collapse the
thread. Press ++open-bracket++ and ++close-bracket++ to jump between comments.

### Adding a comment

1. Navigate to the line (or select multiple lines) where you want to comment
2. Press ++c++ to open the comment editor at the bottom of the screen
3. Write your comment
4. Press ++ctrl+s++ to submit, or ++escape++ to cancel

### Suggesting changes

1. Select the lines you want to suggest a replacement for
2. Press ++f3++ to open your `$EDITOR` with the selected code pre-filled
3. Edit the code to show your suggested replacement
4. Save and close the editor
5. tongs posts the suggestion using the forge's native suggestion syntax
   (GitHub `` ```suggestion `` / GitLab `` ```suggestion:-0+N ``)

### External editor

Press ++f2++ inside the comment editor to switch to your preferred external
editor. The comment text is transferred to the editor and back when you save and
close.
