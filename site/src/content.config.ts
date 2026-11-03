import { defineCollection } from 'astro:content';
import { z } from 'astro/zod';
import { glob } from 'astro/loaders';
import { i18nLoader } from '@astrojs/starlight/loaders';
import { docsSchema, i18nSchema } from '@astrojs/starlight/schema';
import { slug as githubSlug } from 'github-slugger';

/**
 * Entry ids, so URLs, as Astro's default glob loader makes them (a `slug`
 * front matter wins, otherwise each path segment slugified and a trailing
 * `/index` dropped), except that the `docs/` prefix is removed: tongs pages
 * stay at the site root and the tongs-agents pages land under /agents/.
 */
function entryId({ entry, data }: { entry: string; data: Record<string, unknown> }): string {
  if (data.slug) return String(data.slug);
  const segments = entry.replace(/\.[^/.]+$/, '').split('/');
  if (segments[0] === 'docs') segments.shift();
  return segments
    .map((segment) => githubSlug(segment))
    .join('/')
    .replace(/\/index$/, '');
}

// Markdown stays in the repository's docs/ folder; src/content/docs is a
// committed symlink to it (vite.resolve.preserveSymlinks keeps the paths
// stable). Repository-only records are excluded here, so they are never
// built, indexed by search, or listed in the sitemap.
//
// src/content/agents holds the tongs-agents pages, written by
// scripts/sync-agents-docs.mjs from the commit pinned in agents-docs.lock.json
// before every build and dev server (git-ignored, never committed). The sync
// already leaves out that repository's docs/work/ and docs/SDLC.md; the
// exclusions below repeat it so a stale copy can never publish them.
export const collections = {
  docs: defineCollection({
    loader: glob({
      base: './src/content',
      pattern: [
        'docs/**/*.{md,mdx}',
        '!docs/SDLC.md',
        '!docs/site-plan.md',
        '!docs/work/**',
        '!docs/index.md',
        '!docs/assets/**',
        'agents/**/*.{md,mdx}',
        '!agents/SDLC.md',
        '!agents/work/**',
      ],
      generateId: entryId,
    }),
    schema: docsSchema({
      extend: z.object({
        // Page eyebrow, rendered by the PageTitle override as
        // `$ docs / <group> / <eyebrow>`. Both default from the sidebar:
        // group is the page's sidebar group, eyebrow the last URL segment.
        group: z.string().optional(),
        eyebrow: z.string().optional(),
        // Right-hand hint in the eyebrow, with keycap syntax:
        // `opens: "++5++ from a review"` renders "open with [5] from a review".
        opens: z.string().optional(),
        // Optional lead paragraph shown under the h1.
        lead: z.string().optional(),
      }),
    }),
  }),
  // UI string overrides (src/content/i18n/en.json), e.g. "Search docs".
  i18n: defineCollection({ loader: i18nLoader(), schema: i18nSchema() }),
};
