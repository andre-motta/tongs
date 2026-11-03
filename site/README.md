# The `www.tongs.tools` site

Astro 7 and Starlight 0.42. The homepage is `src/pages/index.astro`; the docs
are the Markdown files in the repository's `docs/`, reached through the
committed symlink `src/content/docs -> ../../../docs`. The `/agents/` section
is the [tongs-agents](https://github.com/andre-motta/tongs-agents)
documentation, fetched at build time (see [Agents section](#agents-section)).

```bash
npm ci --prefix site
npm run build --prefix site     # strict: broken links or anchors fail it; output in site/dist
npm run dev --prefix site       # local server with reload
```

`build` and `dev` first run `scripts/sync-agents-docs.mjs`, which downloads
from `codeload.github.com`. Without network access they fail, unless the
pinned archive is already cached in `node_modules/.cache/tongs-agents-docs/`.

Run Node under the repository's memory guard (see `.agents/testing/README.md`).

## Where things live

| Path | What |
|---|---|
| `src/data/nav.mjs` | The one IA: sidebar, header links, homepage routes, footer, redirects |
| `agents-docs.lock.json` | The tongs-agents commit and archive sha256 that `/agents/` is built from |
| `scripts/sync-agents-docs.mjs` | Fetches, verifies and extracts the pinned tongs-agents docs into `src/content/agents/` (git-ignored) |
| `src/data/home.mjs` | Homepage key grid and review draft steps |
| `src/plugins/keys.mjs` | `++key++` parsing and keycap rendering |
| `src/plugins/markdown.mjs` | Sätteri plugins: keycaps, table `data-label` for phones |
| `src/components/starlight/` | Starlight overrides: Head, Header, SiteTitle, ThemeSelect, ThemeProvider (blocked-storage-safe theme bootstrap), PageTitle, Footer, PageFrame |
| `src/styles/tokens.css` | Brand and semantic tokens, shared components |
| `src/styles/docs.css` | `--sl-*` token bridge and docs chrome |
| `src/styles/home.css` | Homepage and 404 only |
| `ec.config.mjs` | Expressive Code: one Monokai theme with AAA lifts |
| `THIRD_PARTY.md` | Licenses of fonts, theme and media |

## Writing docs

Front matter replaces the Markdown `# H1`:

```yaml
---
title: Pipelines and logs          # the h1 and the browser title
description: One sentence for search results and social cards.
opens: "++5++ from a review"      # optional: "open with [5] from a review" in the eyebrow
lead: Optional larger first paragraph under the title.
eyebrow: pipelines                 # optional: last segment of "$ docs / review / pipelines"
---
```

- Keys: `++ctrl+g++`, `++shift+r++`, `++enter++`, `++escape++`, `++slash++`,
  `++f2++`. Modifier first. A lone letter keeps its case (`++r++` shows `r`);
  in a chord it is capitalized (`Ctrl + G`).
- Asides: `:::note`, `:::tip`, `:::caution`, `:::danger`, with an optional
  title as `:::caution[Second press acts at once]`, closed by `:::`.
- Links: site URLs with a trailing slash, `/guides/pipelines/#search`. Relative
  `.md` links fail the build.
- Code titles: ` ```toml title="~/.config/tongs/config.toml" `.
- Tabs need an `.mdx` file and
  `import { Tabs, TabItem } from '@astrojs/starlight/components';`.
- `SDLC.md`, `site-plan.md` and `work/` are never published.

## Agents section

`/agents/` publishes the `docs/` folder of
[tongs-agents](https://github.com/andre-motta/tongs-agents) at the commit in
`agents-docs.lock.json`:

```json
{
  "repo": "andre-motta/tongs-agents",
  "commit": "<full 40-character commit SHA>",
  "sha256": "<sha256 of https://codeload.github.com/<repo>/tar.gz/<commit>>"
}
```

Before every build and dev server, `scripts/sync-agents-docs.mjs`:

- downloads the commit's source archive and refuses it unless its sha256
  matches the lock;
- refuses the whole archive if any member has an absolute path or a `..`
  segment, or if a link or special file sits under `docs/`;
- writes `docs/**` except `docs/work/**` and `docs/SDLC.md` to
  `src/content/agents/`, with `docs/README.md` as the `/agents/` index;
- adds a `slug` front matter for each page's `/agents/` URL, turns relative
  links between published pages into site links, and points relative links to
  anything else (source files, `docs/work/`) at GitHub at the pinned commit.

The fetched pages are never committed. The sidebar sections live in the
`Agents` group of `src/data/nav.mjs`; add a page there when tongs-agents adds
one. Its tests are `tests/site/agents-docs-sync.test.mjs`
(`node --test --test-concurrency=1 tests/site/*.test.mjs`, from the
repository root).

To move the pin, for example to a release tag, find the tag's commit (for an
annotated tag, the `^{}` line names the commit), hash its archive, put both
values in the lock file and run the strict build:

```bash
git ls-remote https://github.com/andre-motta/tongs-agents 'refs/tags/v0.10.0*'
commit=<the commit SHA from above>
curl -fsSL "https://codeload.github.com/andre-motta/tongs-agents/tar.gz/$commit" | sha256sum
```
