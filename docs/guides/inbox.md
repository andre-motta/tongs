# Inbox

The inbox is the first screen you see when tongs launches. It shows open merge
requests and pull requests from every repo under your scan root, pulled from both
GitHub and GitLab in a single view.

## How repo discovery works

On startup, tongs walks your scan root directory (default `~/git`) up to
`scan_depth` levels deep. For each directory that contains a `.git` folder, it
reads the git remotes and determines:

- Whether the remote points to GitHub or GitLab (including self-hosted instances
  configured in `[hosts.*]`)
- The project namespace and name

Repos with unrecognized remotes (Bitbucket, local-only, etc.) are silently
skipped.

## Inbox tabs

The inbox has three tabs, switched with number keys:

| Key | Tab | Shows |
|-----|-----|-------|
| ++1++ | My Reviews | MRs/PRs where you are a requested reviewer |
| ++2++ | My MRs | MRs/PRs you authored |
| ++3++ | All Open | Every open MR/PR across all discovered repos |

Each tab fetches data lazily and in parallel across repos.

## The MR list

Each row in the inbox shows:

- **Forge indicator** -- `GH` for GitHub, `GL` for GitLab
- **Project name** -- namespace/repo
- **MR title** and number
- **CI status** -- pass, fail, running, or pending
- **Comment count**
- **Author** and **last updated** timestamp

Press ++s++ to cycle the sort order (updated, title, CI status, author).

Press ++enter++ on any row to open the MR detail view.

## Per-repo scoped inbox

You can filter the inbox to a single project:

1. Press ++r++ to open the **repo list**
2. Use ++slash++ to search repos by name
3. Press ++f++ to cycle the forge filter (All / GH / GL)
4. Press ++s++ to cycle the sort order (name / forge / host)
5. Press ++enter++ on a repo to show only MRs from that project

Press ++r++ again to return to the full inbox.

## Refreshing

Press ++ctrl+r++ to reload the current view. tongs re-fetches MR data from the
forge APIs, respecting the cache TTL settings in your configuration.
