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

`public/media/*`, except the trailer files listed under the brand kit, are tongs' own screen captures with public-looking demo data
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

## Brand kit (copied unchanged from the tongs site kit)

Original work made for tongs, copied byte for byte from the final site kit
(`export/site-kit/`, its `CREDITS.md` has the full record). The marks are
hand-built vector geometry; the stills and loops are renders of a procedural
Blender scene authored for tongs (Blender is a GPL-3.0 tool only, so the
renders carry no license from it), with CC0 Poly Haven textures
(`metal_plate_02`, `rusty_metal_03`; no attribution required). No AI image
generation and no third-party logos: "github" and "gitlab" appear only as
lowercase engraved text inside the cube, a deliberate style. The wordmark
outlines come from JetBrains Mono under the SIL OFL 1.1; the site draws the
wordmark as live text and ships no wordmark SVG.

| In `public/` | Use |
|---|---|
| `favicon.svg` (switches with `prefers-color-scheme`), `favicon.ico` (16/32/48), `apple-touch-icon.png` (180) | head tags |
| `icon-192.png`, `icon-512.png`, `icon-maskable-512.png` | `site.webmanifest` (`any`, `any`, `maskable`) |
| `logo-mark-2c-dark.svg`, `logo-mark-2c-light.svg` | header mark, one per theme (20 px) |
| `og-image.png` (1200x630, wordmark and "Unified code review for the terminal.") | `og:image` and `twitter:image` on every page |
| `hero-loop.webm` (VP9), `hero-loop.mp4` (H.264), 1920x1080, 10 s, silent | homepage hero light, dark scheme, wider than 900 px, motion allowed; attached by `home.ts` |
| `hero-banner-plain-1920x1080.webp`, `.png` fallback | poster and still for the hero loop (loop frame 0); the only hero image phones and reduced motion load |
| `media/trailer.mp4` (1920x1080, 58 s, H.264 and AAC) | homepage trailer, `preload="none"`, click to play |
| `media/trailer-poster.webp` (1280x720 re-encode of the end card) | trailer poster |
| `media/trailer.en.vtt` (English captions, written for tongs from the trailer narration) | captions track on the homepage trailer |

Kit files not shipped on the site: `wordmark*.svg` (social and press),
`github-social-preview.png` (set in the GitHub repository settings) and
`hero-loop-poster.png` (the same image as `hero-banner-plain-1920x1080.png`).

## Trailer

The trailer's picture is tongs' own terminal and desktop apps on fabricated
demo data plus the renders above; its captions and motion graphics were made
for tongs. Its voice is synthetic (Chatterbox TTS, MIT, and Kokoro-82M,
Apache-2.0). Its sound effects come from the Mixkit Sound Effects Free License
and the Sonniss #GameAudioGDC Bundle License; neither requires attribution,
and the raw files are not redistributed.

| Asset | Source | License | Credit shown |
|---|---|---|---|
| Music: "With These Hands" by Scott Buckley | [scottbuckley.com.au](https://www.scottbuckley.com.au/library/with-these-hands/) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) | under the player and in the shared footer on every page. Changes: shortened to 58 s by cutting whole bars, level automation, loudness mastering, ducked under the voiceover |

The homepage also links to the same trailer on YouTube as a plain text link.
Nothing is embedded, so no page requests a YouTube or Google domain.

## Trademarks

GitHub and GitLab appear on the site as names only, never as logos. The shared
footer carries the notice: "GitHub is a trademark of GitHub, Inc. GITLAB is a
trademark of GitLab Inc. in the United States and other countries and regions.
tongs is an independent project, not affiliated with or endorsed by either."
GitLab's trademark guidelines do not permit its logo without written
permission, and using only GitHub's mark would be lopsided.
