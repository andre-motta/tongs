# Pipelines

The Pipeline tab (++5++ from MR detail) shows CI/CD pipelines associated with the
current merge request. It provides a three-level drill-down from pipeline to job
to log.

## Three-level drill-down

### Level 1: Pipeline list

The top level shows all pipelines for the MR, with:

- Pipeline ID and status (running, passed, failed, canceled)
- Branch name and commit SHA
- Duration and timestamp

Press ++enter++ to drill into a pipeline's jobs.

### Level 2: Job list

Jobs are grouped by stage. Each job row shows:

- Job name and status
- Stage name
- Duration

Press ++enter++ on a job to view its full log output.

### Level 3: Job log

The log view renders ANSI color output natively, preserving the CI system's
original formatting. Line numbers appear in the left gutter.

Press ++escape++ to drill back out one level. While a log search is open, the
first ++escape++ closes the search and returns to where you were reading, and
the next one drills out.

## Log search

Press ++slash++ in the log view to search for text across the full job output.
This is useful for locating errors or specific build steps in long logs.

The log is searched as you type. A status line under the log shows the match
count, the line number and a preview of the current match, or tells you when a
term has no matches. Press ++n++ and ++n+shift++ to walk to the next and
previous match, wrapping around at either end. ++enter++ keeps the search and
returns to the log, and ++escape++ closes it and restores the scroll position
you started from.

## Actions

| Key | Action | Confirmation |
|-----|--------|-------------|
| ++c+shift++ | Cancel a running pipeline or job | Double-press |
| ++r+shift++ | Retry a failed pipeline or job | Double-press |

All destructive actions require pressing the key twice to confirm.

## Opening in browser or editor

| Key | Action |
|-----|--------|
| ++o++ | Open the pipeline or job page in your default browser |
| ++f2++ | Open the job log in your `$EDITOR` for deeper analysis |

Opening the log in an editor is useful for searching with your editor's native
find tools, copying sections, or comparing logs side by side.

## Lazy loading

Pipeline data is only fetched when you first open the Pipeline tab. Subsequent
visits use the cached data until you refresh with ++ctrl+r++.
