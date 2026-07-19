# tongs documentation site plan

Plan for the tongs documentation and landing page, hosted on GitLab Pages (or GitHub Pages).

## Technology

- **Static site generator:** MkDocs with the Material for MkDocs theme
- **Why Material:** standard for Python projects, built-in search, dark/light mode, code annotation support, tabbed content, admonitions, and `attr_list` for image callouts
- **Hosting:** GitLab Pages via `.gitlab-ci.yml` (or GitHub Pages via GitHub Actions)
- **Domain:** `andre-motta.github.io/tongs` (already configured in pyproject.toml)

### MkDocs configuration skeleton

```yaml
site_name: tongs
site_description: Terminal-native code review across GitHub and GitLab
site_url: https://andre-motta.github.io/tongs
repo_url: https://github.com/andre-motta/tongs
repo_name: andre-motta/tongs

theme:
  name: material
  palette:
    - scheme: slate
      primary: deep purple
      accent: amber
      toggle:
        icon: material/brightness-4
        name: Switch to light mode
    - scheme: default
      primary: deep purple
      accent: amber
      toggle:
        icon: material/brightness-7
        name: Switch to dark mode
  features:
    - navigation.instant
    - navigation.tracking
    - navigation.sections
    - navigation.expand
    - content.code.copy
    - content.tabs.link
    - search.highlight
    - search.suggest

plugins:
  - search
  - minify:
      minify_html: true

markdown_extensions:
  - admonition
  - pymdownx.details
  - pymdownx.superfences
  - pymdownx.tabbed:
      alternate_style: true
  - attr_list
  - md_in_html
  - pymdownx.keys         # renders keyboard shortcuts nicely
  - toc:
      permalink: true

nav:
  - Home: index.md
  - Getting started: getting-started.md
  - Guides:
    - Inbox: guides/inbox.md
    - Reviewing diffs: guides/reviewing-diffs.md
    - Commenting: guides/commenting.md
    - MR actions: guides/mr-actions.md
  - Reference:
    - Keybindings: reference/keybindings.md
    - Configuration: reference/configuration.md
  - Architecture: architecture.md
  - Contributing: contributing.md
```

## Page structure

```
docs/
  index.md                    # Landing page / hero
  getting-started.md          # First-run guide
  guides/
    inbox.md                  # Inbox workflows
    reviewing-diffs.md        # Diff viewer guide
    commenting.md             # Comments and suggestions
    mr-actions.md             # Approve, merge, close
  reference/
    keybindings.md            # Full keybinding reference
    configuration.md          # Every TOML key documented
  architecture.md             # Internal architecture
  contributing.md             # Development guide
  assets/
    hero-banner.png           # (existing)
    icon.png                  # (existing)
    screenshots/              # Annotated screenshots
    recordings/               # VHS/asciinema recordings
```

## Content outline per page

### index.md -- Landing page

Short and punchy. Mirrors the top of the README but optimized for a web visitor.

- **Hero section:** tagline ("One TUI. Every forge. Full review."), one-paragraph description, install command
- **30-second demo recording** (embedded asciinema or GIF)
- **Four value prop cards:**
  1. Multi-forge inbox -- GitHub PRs and GitLab MRs in one view
  2. Real diff review -- split-pane with syntax highlighting and word-level diffs
  3. Inline comments -- select lines, write comments, suggest changes
  4. Zero config -- picks up existing gh/glab auth tokens automatically
- **"Get started" CTA** linking to getting-started.md
- **Comparison table** (tongs vs gh-dash vs web UI)
- **Links:** PyPI, GitHub, License

### getting-started.md -- First-run guide

- System requirements (Python 3.12+)
- Install methods (pipx, uvx, pip, from source)
- Auth setup (`gh auth login`, `glab auth login`, `.netrc` fallback)
- First launch walkthrough with annotated screenshot
- Configuration basics (scan root, self-hosted forges)
- Troubleshooting common issues (no repos found, auth errors)

### guides/inbox.md -- Inbox workflows

- How repo discovery works (filesystem scan, remote detection)
- My Reviews / My MRs / All Open tabs
- Opening an MR detail view
- Per-repo scoped inbox (from repo list)
- Refreshing data
- Screenshot: inbox with mixed GitHub/GitLab repos, showing forge indicators

### guides/reviewing-diffs.md -- Diff viewer guide

- Opening the diff tab
- File tree navigation (click, n/Shift+N)
- Line-by-line navigation (j/k)
- Understanding the diff display (gutter line numbers, color coding)
- Word-level diff highlighting
- Context folding
- Markdown preview toggle
- Screenshot: split-pane diff with word-level highlighting visible

### guides/commenting.md -- Comments and suggestions

- Inline comments (c on a diff line)
- Multi-line selection (Shift+J/K, Ctrl+Click)
- Comment editor (bottom dock, Ctrl+S to submit, Esc to cancel)
- External editor integration (F2)
- Suggested changes workflow (F3 opens $EDITOR with template)
- General MR comments
- Screenshot: comment editor open on a diff line
- Recording: full comment workflow from selection to submission

### guides/mr-actions.md -- MR actions

- Approve / Unapprove
- Merge
- Close
- Double-press confirmation pattern
- Merge readiness indicator (what each blocker means)
- Commits tab
- Open in browser / copy URL
- Screenshot: MR detail overview showing merge readiness indicator

### reference/keybindings.md -- Full keybinding reference

Organized into tables by context with anchor links:

- Global keybindings
- Inbox keybindings
- Repo list keybindings
- MR detail keybindings
- Diff viewer keybindings
- Comment editor keybindings

Use the `pymdownx.keys` extension to render key combinations nicely (e.g., ++ctrl+s++).

### reference/configuration.md -- Configuration reference

Every TOML key documented with type, default value, and description:

- `[general]` section (scan_root, scan_depth)
- `[editor]` section (command, external_editor_enabled)
- `[ui]` section (theme, diff_style, show_draft_mrs, ascii_mode)
- `[cache]` section (mr_list_ttl, diff_ttl, max_size_mb)
- `[concurrency]` section (max_parallel, request_timeout)
- `[hosts.*]` section (hostname, forge_type for self-hosted instances)
- `[plugins.*]` section (plugin-specific config)

### architecture.md -- Internal architecture

Move the architecture section from the old README here, expanded:

- Module map with descriptions
- Forge abstraction (ForgeClient ABC, adding a new forge)
- Auth cascade design
- Scanner design (filesystem walk, remote parsing)
- Diff pipeline (parser -> models -> renderer -> position mapper)
- State management
- Plugin system (planned)

### contributing.md -- Development guide

Mirror CONTRIBUTING.md with additional detail:

- Development setup
- Running tests
- Code style (ruff)
- Adding a new forge backend
- Adding a new view/widget
- Test conventions
- PR process

## Screenshots and recordings plan

### Priority recordings (create first)

1. **Hero demo** (30 seconds) -- launch tongs, see inbox populate, open an MR, scroll the diff, leave an inline comment, approve. This is the single most impactful asset. Record with `vhs` (https://github.com/charmbracelet/vhs) for reproducibility.

2. **Comment workflow** (15 seconds) -- navigate to a diff line, select multiple lines, press F3, edit suggestion in $EDITOR, see it post. Demonstrates the most unique feature.

### Priority screenshots (annotated with numbered callouts)

1. **Inbox view** -- mixed GitHub and GitLab repos visible, My Reviews tab active, showing forge indicators (GH/GL), CI status, and comment counts

2. **Split-pane diff** -- file tree on left with status indicators (M/A/D/R), diff content on right with word-level highlighting, gutter line numbers visible

3. **Comment editor** -- bottom-docked editor open, showing the file:line header, text area, and keyboard hints

4. **Merge readiness** -- MR detail overview tab showing the readiness indicator with visible blockers (e.g., "blocked -- CI failing, has conflicts")

5. **Repo list** -- searchable repo list with forge filter, showing mixed GitHub/GitLab repos

### Screenshot tooling

- Use `textual screenshot` (built into the Textual devtools) for pixel-perfect terminal captures
- Annotate with numbered callouts using MkDocs Material's `attr_list` extension
- Maintain both light and dark variants for key screenshots

## Branding direction

### Current assets

- `docs/assets/hero-banner.png` -- existing hero banner
- `docs/assets/icon.png` -- existing icon

### Color palette

Match the MkDocs Material theme to the TUI's visual identity:

- **Primary:** deep purple (terminal aesthetic, differentiation from the blue/green of most dev tools)
- **Accent:** amber (high contrast against purple, good for CTAs and highlights)
- **Dark scheme by default** (slate) -- developers reviewing code prefer dark mode; light mode available as toggle

### Typography

Use Material theme defaults (Roboto). No custom fonts needed for a CLI tool's docs site.

### Tone of voice

- **Direct and technical.** Developers do not want marketing fluff in docs.
- **Task-oriented.** Each guide answers "how do I..." not "here is the theory of..."
- **Concise.** Short paragraphs, bullet lists, code examples. Every page should be scannable.
- **No emojis in docs content.** Terminal tools should feel professional and understated.

### Logo/icon direction

The existing icon works. If a refresh is considered:

- A pair of metalworking tongs gripping a merge request (the metalworking metaphor is strong and unique)
- Flat/geometric style that scales well as a favicon and terminal icon
- Single color version for use in terminal output (ASCII mode)

## Implementation phases

### Phase A -- Minimal viable docs site

1. Set up MkDocs Material with the nav structure above
2. Write `index.md` (landing page) and `getting-started.md`
3. Write `reference/keybindings.md` and `reference/configuration.md`
4. Add CI job to build and deploy to Pages
5. Record the hero demo with `vhs`

### Phase B -- Guides

1. Write all four guide pages (inbox, diffs, commenting, actions)
2. Capture and annotate the five priority screenshots
3. Record the comment workflow
4. Write `architecture.md`

### Phase C -- Polish

1. Add search configuration and SEO metadata
2. Add light/dark screenshot variants
3. Write the contributing page with expanded detail
4. Add a changelog page (auto-generated from git tags)
5. Add version selector if multiple releases warrant it
