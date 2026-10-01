---
title: First run
description: "What tongs does the first time it starts: scanning your repositories and filling the inbox."
---

Start tongs from any directory:

```bash
tongs
```

The inbox opens at once. While it loads, tongs looks for your repositories and
asks each forge for open reviews.

<figure class="shot"><a href="/media/inbox.webp" aria-label="Open the full-size screenshot of the inbox"><div class="frame"><div class="bar" data-pagefind-ignore><span><b>tongs</b> &middot; inbox</span><span class="meta"><span class="full">full size &#8599;</span></span></div><picture><source media="(max-width: 560px)" srcset="/media/inbox-m.webp" width="560" height="400" /><img src="/media/inbox.webp" width="1440" height="378" alt="The tongs inbox listing open reviews from GitHub and GitLab repositories."></picture></div></a><figcaption class="cap">demo data, cropped on small screens</figcaption></figure>

## What tongs scans

tongs walks your **scan root**, `~/git` by default, up to five directory levels
deep. For each git repository it finds, it reads the remotes and works out the
forge:

- github.com and gitlab.com are recognized on their own.
- Hostnames listed under `[hosts.*]` in the config use the forge you set there.

Repositories with any other remote, including a self-hosted forge that is not
listed under `[hosts.*]`, or with no remote at all, are skipped. tongs does not
guess the forge from a hostname.

To scan a different directory for one run, pass `--scan-root` (or `-d`):

```bash
tongs --scan-root ~/projects
```

To change it for good, set `scan_root` in the config file:

```toml title="~/.config/tongs/config.toml"
[general]
scan_root = "~/projects"
scan_depth = 5
```

## The inbox tabs

The inbox has three tabs. Each one loads the first time you open it, fetching
from every repository in parallel.

| Key | Tab | Shows |
|-----|-----|-------|
| ++1++ | My Reviews | Reviews where you are a requested reviewer |
| ++2++ | My MRs | Reviews you opened |
| ++3++ | All Open | Every open review across your repositories |

Press ++s++ to cycle the sort order and ++r++ for the repository list, where
++enter++ scopes the inbox to one project.

## Open a review

Move to a row and press ++enter++. The review opens on its overview, with four
more tabs a number key away:

| Key | Tab |
|-----|-----|
| ++1++ | Overview |
| ++2++ | Diff |
| ++3++ | Commits |
| ++4++ | Discussion |
| ++5++ | Pipeline |

Press ++escape++ to go back to the inbox. A few keys work almost everywhere:

| Key | Action |
|-----|--------|
| ++question++ | Show or hide the keybindings panel |
| ++ctrl+p++ | Command palette |
| ++ctrl+r++ | Refresh the current view |
| ++o++ | Open the review in your browser |
| ++q++ | Back, or quit from the inbox |

The [Keybindings reference](/reference/keybindings/) lists every key.

## If something is missing

### No repositories found

- Check that the scan root holds git repositories with GitHub or GitLab
  remotes.
- Raise `scan_depth` if your repositories sit more than five levels below the
  scan root.
- For a self-hosted forge, add a `[hosts.*]` section. See
  [Self-hosted forges](/getting-started/#self-hosted-forges).

### A tab reports a skipped host

tongs shows a warning naming the host and the reason, usually a missing or
expired credential. See [If sign-in fails](/getting-started/#if-sign-in-fails).

### Slow startup

- Lower `scan_depth` if tongs walks too many directories.
- Lower `max_parallel` in `[concurrency]` if you hit forge rate limits. It
  defaults to 8 requests at a time.

## Next

- [Inbox](/guides/inbox/) covers filtering, sorting and the repository list.
- [Diffs and comments](/guides/diffs/) covers reading a change and commenting on
  it.
- [Review drafts](/guides/review-drafts/) covers collecting comments and
  submitting them as one review.
