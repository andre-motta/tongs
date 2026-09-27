# Third-party assets in the site

Everything the published site ships that tongs did not write, with its license.
Update this file when a font, theme, media file or runtime package is added.

## Fonts (self-hosted, Latin subset only)

| Asset | Package (pinned) | License | Shipped files |
|---|---|---|---|
| Inter, variable (weights 100 to 900; the site uses 400 to 700) | `@fontsource-variable/inter` 5.3.0 | SIL Open Font License 1.1, copyright the Inter project authors (Rasmus Andersson) | `inter-latin-wght-normal.woff2` |
| JetBrains Mono 400, 500, 700 | `@fontsource/jetbrains-mono` 5.3.0 | SIL Open Font License 1.1, copyright the JetBrains Mono project authors | `jetbrains-mono-latin-{400,500,700}-normal.woff2` |

`src/components/Fonts.astro` declares only these four files, so no other
subset or weight reaches `dist/`. The OFL allows bundling with software; the
fonts are not modified or renamed.

## Code theme

| Asset | Source | License | Notes |
|---|---|---|---|
| Monokai token colors | `@shikijs/themes` 4.4.3 (`monokai`), originally from VS Code's built-in theme | MIT | `ec.config.mjs` lifts five token colors for 7:1 contrast on `#272822` (still under design review) |

## Runtime code shipped to visitors

| Package | License | What ships |
|---|---|---|
| `@astrojs/starlight` 0.42.4 | MIT | docs page scripts (TOC, sidebar, search dialog, tabs) |
| `astro-expressive-code` (via Starlight) | MIT | copy button script and code block CSS |
| `pagefind` / `@pagefind/default-ui` (via Starlight) | MIT | search index and search UI |
| `astro` 7.3.5 | MIT | link prefetch client script (`_astro/page.*.js`) on the homepage and docs pages |

Build-only packages (`@astrojs/markdown-satteri` 0.4.2,
`starlight-links-validator` 0.26.0; both MIT) produce the output but ship no
code of their own. `sharp` (Apache-2.0, with LGPL-3.0 libvips binaries) is an
optional dependency of Astro and is installed, but the site processes no
images with it.

## Media

`public/media/*` are tongs' own screen captures with public-looking demo data
(`acme/*`, `platform/*`, `data/*`). They are part of this repository and MIT
licensed with it.

- The terminal captures (`inbox*`, `repos*`, `diff*`, `ci*`, `draft*`) are
  stock `textual-dark` captures of tongs on the public demo data. They are
  rasterized from the Textual SVG frames of the capture harness, and `repos*`
  was rendered by the harness with `--theme textual-dark`. Stills:
  `inbox.webp` 1440x378, `inbox-poster.webp` 1280x336, `inbox-m.webp`
  560x400, `repos.webp` 760x452, `repos-m.webp` 500x452, `diff-poster.webp`
  1280x640, `diff-m.webp` 640x560, `draft.webp` 1000x756, `draft-m.webp`
  660x440, `ci.webp` 1440x744, `ci-poster.webp` 1280x640, `ci-m.webp`
  740x511. The loops (`inbox-loop`, `diff-loop`, `ci-loop`, WebM and MP4) are
  encoded from the same frames at the poster sizes.
- `desktop-review.webp` (1920x1080) and `desktop-review-m.webp` (960x540) are a
  lossy WebP conversion of a guarded capture of the desktop renderer on the
  demo data, not edited. The page labels it beta.

## Brand kit (copied unchanged from the tongs brand kit)

Original work made for tongs (hand-built vector geometry, no third-party
artwork, no AI generation, per the kit's README). The wordmark outlines in the
kit come from JetBrains Mono under the SIL OFL 1.1; the files copied here
contain the mark only.

| In `public/` | Kit source | Use |
|---|---|---|
| `favicon.svg`, `favicon.ico`, `apple-touch-icon.png`, `icon-192.png`, `icon-512.png`, `icon-maskable-512.png` | same names | Head tags and `site.webmanifest` |
| `logo-mark-2c-dark.svg` | `mark-small-duo.svg` | header mark, dark theme (20 px) |
| `logo-mark-2c-light.svg` | `mark-small-light.svg` | header mark, light theme (20 px) |

The two header marks carry the site's planned logo filenames so a later kit
delivery under those names replaces them in place.

## Brand kit placeholders (not in this tree yet)

The pages still reserve these final filenames: `og-image.png` (until it exists
in `public/`, pages omit `og:image` and `twitter:image` and use the small
Twitter card), `hero-loop.webm`, `hero-loop.mp4`,
`hero-banner-plain-1920x1080.webp`, and `media/trailer.mp4` with
`media/trailer-poster.png` (the trailer slot renders only once
`public/media/trailer.mp4` exists). Record each one's license here when it
lands in `public/`.
