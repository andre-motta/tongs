// www.tongs.tools: homepage (src/pages/index.astro) plus Starlight docs.
// Markdown lives in ../docs through the src/content/docs symlink.
import { existsSync } from 'node:fs';
import { copyFile, readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';
import { satteri } from '@astrojs/markdown-satteri';
import starlightLinksValidator from 'starlight-links-validator';
import { keycapsPlugin, tableLabelsPlugin } from './src/plugins/markdown.mjs';
import { redirectHashes, redirects, sidebar, site } from './src/data/nav.mjs';

/** Keep the MkDocs-era /sitemap.xml URL working next to sitemap-index.xml. */
const legacySitemap = {
  name: 'tongs-legacy-sitemap',
  hooks: {
    'astro:build:done': async ({ dir }) => {
      const out = fileURLToPath(dir);
      await copyFile(`${out}sitemap-index.xml`, `${out}sitemap.xml`);
    },
  },
};

/**
 * Astro's redirect stubs use a meta refresh, which drops the URL fragment.
 * Put a script ahead of it that carries the fragment over (renamed headings
 * are mapped through redirectHashes); the meta refresh and link stay as the
 * no-JS fallback.
 */
const redirectFragments = {
  name: 'tongs-redirect-fragments',
  hooks: {
    'astro:build:done': async ({ dir }) => {
      const out = fileURLToPath(dir);
      for (const [from, to] of Object.entries(redirects)) {
        const file = `${out}${from.replace(/^\//, '')}/index.html`;
        const html = await readFile(file, 'utf8');
        const map = JSON.stringify(redirectHashes[from] ?? {});
        const script =
          `<script>(function(){var m=${map},h=location.hash.slice(1);` +
          `try{h=decodeURIComponent(h)}catch(e){}` +
          `location.replace(${JSON.stringify(to)}+(h?'#'+encodeURIComponent(m[h]||h):''))})()</script>`;
        const marker = '<meta http-equiv="refresh"';
        if (!html.includes(marker)) throw new Error(`redirect stub without meta refresh: ${file}`);
        await writeFile(file, html.replace(marker, script + marker));
      }
    },
  },
};

/**
 * og-image.png (1200x630) comes from the brand kit. If it is ever missing from
 * public/, pages omit og:image and twitter:image and use the small
 * twitter:card, so no page points at a missing file.
 */
const hasOg = existsSync(fileURLToPath(new URL('./public/og-image.png', import.meta.url)));
// Same rule for the brand-kit trailer: the homepage slot renders only while
// public/media/trailer.mp4 exists, so no empty player ever ships.
const hasTrailer = existsSync(fileURLToPath(new URL('./public/media/trailer.mp4', import.meta.url)));

export default defineConfig({
  site: 'https://www.tongs.tools',
  trailingSlash: 'ignore',
  redirects,
  vite: {
    resolve: { preserveSymlinks: true },
    define: {
      'import.meta.env.TONGS_HAS_OG': JSON.stringify(hasOg),
      'import.meta.env.TONGS_HAS_TRAILER': JSON.stringify(hasTrailer),
    },
  },
  markdown: {
    processor: satteri({
      mdastPlugins: [keycapsPlugin],
      hastPlugins: [tableLabelsPlugin],
    }),
  },
  integrations: [
    starlight({
      title: 'tongs',
      description:
        'Unified code review for the terminal. A keyboard-driven review inbox for GitHub pull requests and GitLab merge requests.',
      disable404Route: true,
      customCss: ['./src/styles/tokens.css', './src/styles/docs.css'],
      // No social icons: forges appear as text only, never as logos (the
      // header and footer carry a text GitHub link).
      sidebar,
      components: {
        Head: './src/components/starlight/Head.astro',
        Header: './src/components/starlight/Header.astro',
        SiteTitle: './src/components/starlight/SiteTitle.astro',
        ThemeSelect: './src/components/starlight/ThemeSelect.astro',
        ThemeProvider: './src/components/starlight/ThemeProvider.astro',
        PageTitle: './src/components/starlight/PageTitle.astro',
        Footer: './src/components/starlight/Footer.astro',
        PageFrame: './src/components/starlight/PageFrame.astro',
      },
      plugins: [
        starlightLinksValidator({
          errorOnRelativeLinks: true,
          errorOnInvalidHashes: true,
          errorOnLocalLinks: true,
        }),
      ],
    }),
    legacySitemap,
    redirectFragments,
  ],
});
